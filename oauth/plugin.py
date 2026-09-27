"""
Plugin `oauth` — conseguir y renovar tokens OAuth 2.0 para que las
conexiones de `connections` llamen a APIs como las de Google (Gmail, Drive,
Sheets, Calendar) o Microsoft.

Por qué hace falta un plugin: el token de acceso vence (en Google, a la hora),
y un header fijo en una conexión deja de andar enseguida. El plugin guarda el
refresh token de cada cuenta como secreto, pide un token nuevo cuando hace
falta y lo deja en una **variable de Config** (secreta). Las conexiones lo
leen con `Authorization: Bearer {env.GOOGLE_TOKEN}`: la webapp resuelve
`{env.X}` en cada request, en Actions y en sources (workflow-bot-app#9), así
que el valor renovado se usa sin tocar nada más.

Qué es de acá y qué no: las llamadas a cada API son Actions de `connections`
(datos, no código). El plugin sólo sabe de OAuth; la única cosa de un
proveedor que trae son las URLs de sus pantallas y, para Google, un paquete de
conexiones de Gmail que se crean con un botón.

Cómo escribe en la instalación: guardar el refresh token en la cuenta, dejar
el token en la variable y crear conexiones son escrituras en el propio Bot, y
van por su API local (`PUT /api/core/...` en 127.0.0.1), el mismo camino que
usa `bots.migrar`. Ningún plugin tiene otra forma de escribir una colección o
una variable.

El token de acceso NO sale como output de un tool: quedaría en claro en la
traza del run. Sale sólo por la variable secreta.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qs, quote, urlencode, urlparse

from backend.core import ports as port_names
from backend.core.contract import (
    Action,
    Field,
    FunctionAction,
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    Resource,
    Setting,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.ports import PortError

MI_DIRECCION = "direccion_bot"
TIMEOUT = 20.0
# Se renueva un poco antes de que venza: un token que vence en medio de una
# llamada larga falla con un 401 que parece un error de la API.
MARGEN = 120

PROVEEDORES = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # Sin estos dos Google no devuelve refresh token, o lo devuelve sólo la
        # primera vez que la cuenta acepta.
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "extra": {"prompt": "consent"},
    },
}

CUENTAS = Resource(
    name="cuentas",
    label="Cuentas OAuth",
    item_label="Cuenta",
    key_field="nombre",
    doc=(
        "Una cuenta por cada cliente OAuth. Después de guardarla: 'Autorizar' (dos pasos) para "
        "conseguir el refresh token, y 'Probar'. Ver el README del plugin para crear el cliente "
        "en Google Cloud Console."
    ),
    fields=(
        Field("nombre", ParamType.STR, label="Nombre", required=True, doc="Cómo la nombran los flujos, ej. 'google'."),
        Field(
            "proveedor", ParamType.ENUM, label="Proveedor", default="google",
            choices=("google", "microsoft", "otro"),
            doc="Google y Microsoft traen sus URLs; 'otro' usa las de abajo.",
        ),
        Field("client_id", ParamType.STR, label="Client ID", required=True),
        Field("client_secret", ParamType.STR, label="Client secret", secret=True),
        Field(
            "scopes", ParamType.STR, label="Permisos (scopes)", required=True,
            default="https://www.googleapis.com/auth/gmail.modify",
            doc="Separados por espacio. Gmail: .../auth/gmail.modify (leer, etiquetar, archivar y enviar). "
            "Drive: .../auth/drive. Microsoft: agregar offline_access.",
        ),
        Field(
            "variable", ParamType.STR, label="Variable del token", required=True, default="GOOGLE_TOKEN",
            doc="Dónde queda el token vigente, en Config → Variables. Las conexiones lo usan con "
            "Authorization: Bearer {env.<esta variable>}.",
        ),
        Field(
            "refresh_token", ParamType.STR, label="Refresh token", secret=True,
            doc="Lo completa 'Autorizar'. No hace falta tocarlo.",
        ),
        Field(
            "codigo", ParamType.STR, label="Código de autorización (paso 2)",
            doc="Después del paso 1 de 'Autorizar': pegar acá la dirección ENTERA a la que volvió el "
            "navegador (http://127.0.0.1:…/?code=…), guardar y apretar 'Autorizar' otra vez. Se vacía solo.",
        ),
        Field("auth_url", ParamType.STR, label="URL de autorización", doc="Sólo con proveedor 'otro'."),
        Field("token_url", ParamType.STR, label="URL de tokens", doc="Sólo con proveedor 'otro'."),
    ),
)

AUTORIZAR = Action(
    "autorizar", "Autorizar",
    doc=(
        "Paso 1, con 'Código' vacío: abre la pantalla del proveedor para aceptar. Al aceptar, el "
        "navegador vuelve a este Bot con ?code=… en la dirección. Paso 2: editar la cuenta, pegar esa "
        "dirección entera en 'Código de autorización', guardar y volver a apretar Autorizar."
    ),
    resource="cuentas",
    params=(
        Param("nombre", required=True, options_from="cuentas"),
        # Llega desde el campo `codigo` de la cuenta: el botón de una fila corre
        # la Action con lo guardado y no pide params aparte.
        Param("codigo", doc="Vacío para empezar. Después, la dirección entera a la que volvió el navegador (o sólo el code)."),
    ),
)
PROBAR = Action(
    "probar", "Probar",
    doc="Pide un token nuevo con el refresh token guardado y lo deja en la variable.",
    resource="cuentas",
    params=(Param("nombre", required=True, options_from="cuentas"),),
)
CREAR_GMAIL = Action(
    "crear_gmail", "Crear conexiones de Gmail",
    doc="Crea en Conexiones las llamadas de Gmail (buscar, leer, archivar, marcar leído, etiquetar, "
    "enviar, responder, adjuntos) usando la variable de esta cuenta. Las que ya existen no se tocan.",
    resource="cuentas",
    params=(
        Param("nombre", required=True, options_from="cuentas"),
        Param("pisar", ParamType.BOOL, default=False, doc="Reemplazar las que ya existen con el mismo nombre."),
    ),
)

MANIFEST = PluginManifest(
    name="oauth",
    label="OAuth",
    version="0.1.2",
    doc=(
        "Tokens OAuth 2.0 para APIs como Google o Microsoft: autoriza una vez, renueva solo y deja el "
        "token en una variable de Config que las conexiones usan con Bearer {env.VARIABLE}."
    ),
    ports=(port_names.HTTP, port_names.CLOCK),
    settings=(
        Setting(
            MI_DIRECCION, ParamType.STR, label="Dirección de este Bot",
            doc="Normalmente VACÍA: sola resuelve a http://127.0.0.1:<puerto de este Bot>. Es a donde "
            "vuelve el navegador al autorizar, y por donde el plugin guarda el token y las conexiones.",
        ),
    ),
    resources=(CUENTAS,),
    actions=(AUTORIZAR, PROBAR, CREAR_GMAIL),
)


# ── La cuenta y el proveedor ──────────────────────────────────────────────

def _cuenta(ctx: ToolContext, nombre: str) -> dict | ToolResult:
    nombre = (nombre or "").strip()
    cuenta = ctx.resource("cuentas", nombre, key_field="nombre") if nombre else None
    if cuenta is None:
        hay = ", ".join(ctx.resource_keys("cuentas", key_field="nombre")) or "ninguna"
        return ToolResult.err(f"no existe la cuenta '{nombre}'. Cargadas: {hay}")
    return cuenta


def _urls(cuenta: dict) -> tuple[str, str, dict] | ToolResult:
    proveedor = cuenta.get("proveedor") or "google"
    if proveedor in PROVEEDORES:
        p = PROVEEDORES[proveedor]
        return p["auth_url"], p["token_url"], p["extra"]
    auth, token = (cuenta.get("auth_url") or "").strip(), (cuenta.get("token_url") or "").strip()
    if not auth or not token:
        return ToolResult.err(f"la cuenta '{cuenta['nombre']}' es de proveedor 'otro': faltan auth_url y token_url")
    return auth, token, {}


def _variable(cuenta: dict) -> str | ToolResult:
    variable = (cuenta.get("variable") or "").strip()
    # El mismo alfabeto que `{env.X}` puede leer: una variable con un punto o
    # un espacio se guardaría y ninguna conexión podría nombrarla.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        return ToolResult.err(f"'variable' de '{cuenta['nombre']}' tiene que ser letras, números y _: {variable!r}")
    return variable


# ── Este Bot, por su API local ────────────────────────────────────────────

def _este_bot(ctx: ToolContext) -> str:
    """
    La dirección de este Bot. El puerto real sale de BOT_PORT, que la app deja
    en el entorno al arrancar (mismo mecanismo que `bots`): con un 8000 fijo,
    un Bot en otro puerto le escribiría a otra instalación.
    """
    puerto = (os.environ.get("BOT_PORT") or "8000").strip()
    return str(ctx.config(MI_DIRECCION) or f"http://127.0.0.1:{puerto}").strip().rstrip("/")


def _redirect(ctx: ToolContext) -> str:
    return _este_bot(ctx) + "/"


def _al_bot(ctx: ToolContext, metodo: str, camino: str, payload: dict | None = None):
    respuesta = ctx.port(port_names.HTTP).request(
        f"{_este_bot(ctx)}/api/core{camino}", method=metodo,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        body=json.dumps(payload, ensure_ascii=False) if payload is not None else None, timeout=TIMEOUT,
    )
    return respuesta, respuesta.json(default=None)


def _guardar_variable(ctx: ToolContext, variable: str, valor: str) -> str | None:
    """Deja el token en Config → Variables, como secreto. Devuelve el error, o None."""
    try:
        respuesta, _ = _al_bot(ctx, "PUT", f"/env/{quote(variable, safe='')}", {"value": valor, "secret": True})
    except PortError as exc:
        return f"no se pudo guardar la variable {variable} en {_este_bot(ctx)}: {exc}"
    if not respuesta.ok:
        return f"este Bot respondió {respuesta.status} al guardar la variable {variable}"
    return None


# ── Tokens ────────────────────────────────────────────────────────────────

# {cuenta: (token, vence, refresh_token con que se pidió)}. En memoria del
# proceso: después de reiniciar se pide uno nuevo, que es lo que hay que hacer
# igual. Guardar con qué refresh token se pidió hace que re-autorizar invalide
# el token viejo en vez de seguir sirviéndolo.
_VIGENTES: dict[str, tuple[str, float, str]] = {}


def _pedir_token(ctx: ToolContext, cuenta: dict, datos: dict) -> dict | str:
    """POST al endpoint de tokens. El JSON de la respuesta, o el mensaje de error."""
    urls = _urls(cuenta)
    if isinstance(urls, ToolResult):
        return urls.message
    _, token_url, _ = urls
    cuerpo = {"client_id": cuenta.get("client_id") or "", **datos}
    if cuenta.get("client_secret"):
        cuerpo["client_secret"] = cuenta["client_secret"]
    try:
        respuesta = ctx.port(port_names.HTTP).request(
            token_url, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode(cuerpo), timeout=TIMEOUT,
        )
    except PortError as exc:
        return f"no responde {token_url}: {exc}"
    leido = respuesta.json(default=None)
    if not respuesta.ok or not isinstance(leido, dict) or not leido.get("access_token"):
        # El proveedor explica bien qué pasó (invalid_grant: el refresh token se
        # revocó o venció; invalid_client: client_id/secret mal copiados).
        detalle = (leido or {}).get("error_description") or (leido or {}).get("error") if isinstance(leido, dict) else ""
        return f"el proveedor respondió {respuesta.status}: {detalle or respuesta.text[:200]}"
    return leido


def _vigente(ctx: ToolContext, cuenta: dict, *, forzar: bool = False) -> dict | ToolResult:
    """
    Un token vigente, dejado en la variable. {variable, vence, renovado}.

    Si el de memoria todavía sirve no se pide otro ni se reescribe la variable:
    un flujo que corre cada minuto no tiene por qué pegarle al proveedor ni a
    la base en cada run.
    """
    variable = _variable(cuenta)
    if isinstance(variable, ToolResult):
        return variable
    refresh = cuenta.get("refresh_token") or ""
    if not refresh:
        return ToolResult.err(f"la cuenta '{cuenta['nombre']}' no está autorizada: apretar 'Autorizar' en Plug ins → OAuth")
    ahora = ctx.port(port_names.CLOCK).now()
    guardado = _VIGENTES.get(cuenta["nombre"])
    if not forzar and guardado and guardado[2] == refresh and guardado[1] - MARGEN > ahora:
        return {"variable": variable, "vence": guardado[1], "renovado": False}

    leido = _pedir_token(ctx, cuenta, {"grant_type": "refresh_token", "refresh_token": refresh})
    if isinstance(leido, str):
        return ToolResult.err(f"'{cuenta['nombre']}': {leido}")
    vence = ahora + float(leido.get("expires_in") or 3600)
    if error := _guardar_variable(ctx, variable, leido["access_token"]):
        return ToolResult.err(error)
    _VIGENTES[cuenta["nombre"]] = (leido["access_token"], vence, refresh)
    return {"variable": variable, "vence": vence, "renovado": True}


# ── oauth.token ───────────────────────────────────────────────────────────

TOKEN = ToolManifest(
    id="oauth.token",
    label="renovar token",
    category="OAUTH",
    doc=(
        "Deja un token vigente de 'cuenta' en su variable de Config, para que las conexiones que "
        "siguen lo usen con Bearer {env.VARIABLE}. Si el que hay todavía sirve, no pide otro. "
        "Conviene como primer nodo de un flujo que llama a esas APIs."
    ),
    params=(Param("cuenta", required=True, options_from="cuentas", doc="El nombre de la cuenta en Plug ins → OAuth."),),
    outputs=(
        Output("variable", ParamType.STR, doc="Dónde quedó el token."),
        Output("vence_en", ParamType.INT, doc="Segundos que le quedan."),
        Output("renovado", ParamType.BOOL, doc="Si hubo que pedir uno nuevo."),
    ),
)


def _token(ctx: ToolContext) -> ToolResult:
    vacios = {"variable": "", "vence_en": 0, "renovado": False}
    cuenta = _cuenta(ctx, ctx.params["cuenta"])
    if isinstance(cuenta, ToolResult):
        return ToolResult.err(cuenta.message, **vacios)
    vigente = _vigente(ctx, cuenta)
    if isinstance(vigente, ToolResult):
        return ToolResult.err(vigente.message, **vacios)
    vence_en = int(vigente["vence"] - ctx.port(port_names.CLOCK).now())
    ctx.log(f"{cuenta['nombre']}: token en {vigente['variable']}, vence en {vence_en // 60} min"
            + (" (renovado)" if vigente["renovado"] else ""))
    return ToolResult.ok(variable=vigente["variable"], vence_en=vence_en, renovado=vigente["renovado"])


# ── Actions ───────────────────────────────────────────────────────────────

def _con_indicador(resultado: ToolResult) -> ToolResult:
    """El check por fila que la app guarda (mismo contrato que `bots.probar`), en el ok y en el err."""
    estado = "err" if resultado.failed else "ok"
    resultado.outputs["indicador"] = {"estado": estado, "texto": resultado.message}
    return resultado


def _codigo_de(pegado: str) -> str | ToolResult:
    """El code, de la dirección entera a la que volvió el navegador o pegado solo."""
    pegado = pegado.strip()
    if "://" in pegado or pegado.startswith("?") or "code=" in pegado:
        query = parse_qs(urlparse(pegado if "://" in pegado else "x://x/" + pegado.lstrip("/")).query)
        if query.get("error"):
            return ToolResult.err(f"el proveedor no autorizó: {query['error'][0]}")
        if not query.get("code"):
            return ToolResult.err("la dirección pegada no trae ?code=…: copiar la de la pestaña a la que volvió el navegador")
        return query["code"][0]
    return pegado


def _guardar_cuenta(ctx: ToolContext, cuenta: dict, **cambios) -> str | None:
    """
    Reescribe la cuenta con `cambios`. Los secretos que no cambian no se mandan:
    el PUT conserva los que llegan en None, así el client_secret no viaja de
    vuelta sin necesidad. Devuelve el error, o None.
    """
    secretos = {f.name for f in CUENTAS.fields if f.secret}
    item = {k: v for k, v in cuenta.items() if not k.startswith("_") and k not in secretos}
    item.update(cambios)
    try:
        respuesta, _ = _al_bot(ctx, "PUT", f"/resources/oauth/cuentas/{quote(cuenta['nombre'], safe='')}", {"item": item})
    except PortError as exc:
        return f"no se pudo guardar la cuenta en {_este_bot(ctx)}: {exc}"
    if not respuesta.ok:
        return f"este Bot respondió {respuesta.status} al guardar la cuenta"
    return None


def _autorizar(ctx: ToolContext) -> ToolResult:
    cuenta = _cuenta(ctx, ctx.params["nombre"])
    if isinstance(cuenta, ToolResult):
        return _con_indicador(cuenta)
    urls = _urls(cuenta)
    if isinstance(urls, ToolResult):
        return _con_indicador(urls)
    auth_url, _, extra = urls

    pegado = (ctx.params.get("codigo") or "").strip()
    if not pegado:
        consulta = {
            "client_id": cuenta.get("client_id") or "", "redirect_uri": _redirect(ctx),
            "response_type": "code", "scope": (cuenta.get("scopes") or "").strip(), **extra,
        }
        return ToolResult.ok(
            "Se abrió la pantalla para aceptar. Al terminar, el navegador vuelve a este Bot con ?code=… "
            "en la dirección: copiarla entera, editar la cuenta, pegarla en 'Código de autorización', "
            "guardar y apretar Autorizar otra vez.",
            abrir_url=f"{auth_url}?{urlencode(consulta)}",
        )

    # Un código se usa una sola vez y vence en minutos: pase lo que pase se
    # vacía el campo, así el próximo 'Autorizar' vuelve a empezar desde el paso
    # 1 en vez de reintentar para siempre un código muerto.
    codigo = _codigo_de(pegado)
    if isinstance(codigo, ToolResult):
        _guardar_cuenta(ctx, cuenta, codigo="")
        return _con_indicador(ToolResult.err(codigo.message + ". Apretar Autorizar para empezar de nuevo."))
    leido = _pedir_token(ctx, cuenta, {"grant_type": "authorization_code", "code": codigo, "redirect_uri": _redirect(ctx)})
    if isinstance(leido, str):
        _guardar_cuenta(ctx, cuenta, codigo="")
        return _con_indicador(ToolResult.err(
            f"no se pudo canjear el código: {leido}. Apretar Autorizar para empezar de nuevo."
        ))
    refresh = leido.get("refresh_token")
    if not refresh:
        _guardar_cuenta(ctx, cuenta, codigo="")
        return _con_indicador(ToolResult.err(
            "el proveedor no devolvió refresh token: revocar el acceso de la app en la cuenta y autorizar de nuevo"
        ))
    if error := _guardar_cuenta(ctx, cuenta, refresh_token=refresh, codigo=""):
        return _con_indicador(ToolResult.err(error))

    variable = _variable(cuenta)
    if isinstance(variable, ToolResult):
        return _con_indicador(variable)
    vence = ctx.port(port_names.CLOCK).now() + float(leido.get("expires_in") or 3600)
    if error := _guardar_variable(ctx, variable, leido["access_token"]):
        return _con_indicador(ToolResult.err(error))
    _VIGENTES[cuenta["nombre"]] = (leido["access_token"], vence, refresh)
    return _con_indicador(ToolResult.ok(f"autorizada; el token queda en {variable}"))


def _probar(ctx: ToolContext) -> ToolResult:
    cuenta = _cuenta(ctx, ctx.params["nombre"])
    if isinstance(cuenta, ToolResult):
        return _con_indicador(cuenta)
    vigente = _vigente(ctx, cuenta, forzar=True)
    if isinstance(vigente, ToolResult):
        return _con_indicador(vigente)
    minutos = int(vigente["vence"] - ctx.port(port_names.CLOCK).now()) // 60
    return _con_indicador(ToolResult.ok(f"token nuevo en {vigente['variable']}, vence en {minutos} min"))


# ── Conexiones de Gmail ───────────────────────────────────────────────────

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"


def _conexiones_gmail(variable: str) -> list[dict]:
    """Las Actions de `connections` para Gmail. {var} lo completa el nodo que la llama."""
    headers = {"Authorization": f"Bearer {{env.{variable}}}", "Accept": "application/json"}

    def conexion(nombre, url, method="GET", payload=None, results_path=""):
        return {"name": nombre, "url": url, "method": method, "headers": headers,
                "payload": payload or {}, "results_path": results_path}

    return [
        conexion("Gmail - buscar", f"{GMAIL}/messages?q={{q}}&maxResults=20", results_path="messages"),
        conexion("Gmail - leer", f"{GMAIL}/messages/{{id}}?format=raw"),
        conexion("Gmail - archivar", f"{GMAIL}/messages/{{id}}/modify", "POST", {"removeLabelIds": ["INBOX"]}),
        conexion("Gmail - marcar leído", f"{GMAIL}/messages/{{id}}/modify", "POST", {"removeLabelIds": ["UNREAD"]}),
        conexion("Gmail - etiquetar", f"{GMAIL}/messages/{{id}}/modify", "POST", {"addLabelIds": ["{etiqueta_id}"]}),
        conexion("Gmail - etiquetas", f"{GMAIL}/labels", results_path="labels"),
        conexion("Gmail - enviar", f"{GMAIL}/messages/send", "POST", {"raw": "{raw}"}),
        conexion("Gmail - responder", f"{GMAIL}/messages/send", "POST", {"raw": "{raw}", "threadId": "{thread_id}"}),
        conexion("Gmail - adjunto", f"{GMAIL}/messages/{{id}}/attachments/{{adjunto_id}}"),
    ]


def _crear_gmail(ctx: ToolContext) -> ToolResult:
    cuenta = _cuenta(ctx, ctx.params["nombre"])
    if isinstance(cuenta, ToolResult):
        return cuenta
    if (cuenta.get("proveedor") or "google") != "google":
        return ToolResult.err(f"'{cuenta['nombre']}' no es una cuenta de Google")
    variable = _variable(cuenta)
    if isinstance(variable, ToolResult):
        return variable
    pisar = bool(ctx.params.get("pisar"))
    creadas, salteadas = [], []
    for conexion in _conexiones_gmail(variable):
        camino = f"/resources/connections/actions/{quote(conexion['name'], safe='')}"
        try:
            if not pisar:
                existe, _ = _al_bot(ctx, "GET", camino)
                if existe.ok:
                    salteadas.append(conexion["name"])
                    continue
            respuesta, datos = _al_bot(ctx, "PUT", camino, {"item": conexion})
        except PortError as exc:
            return ToolResult.err(f"no se pudo escribir en {_este_bot(ctx)}: {exc}", creadas=creadas, salteadas=salteadas)
        if not respuesta.ok:
            detalle = datos.get("detail") if isinstance(datos, dict) else respuesta.text[:200]
            return ToolResult.err(f"'{conexion['name']}': este Bot respondió {respuesta.status}: {detalle}",
                                  creadas=creadas, salteadas=salteadas)
        creadas.append(conexion["name"])
    mensaje = f"{len(creadas)} conexión(es) creada(s)" + (f"; ya existían: {', '.join(salteadas)}" if salteadas else "")
    return ToolResult.ok(mensaje, creadas=creadas, salteadas=salteadas)


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[FunctionTool(manifest=TOKEN, fn=_token)],
        actions=[
            FunctionAction(action=AUTORIZAR, fn=_autorizar),
            FunctionAction(action=PROBAR, fn=_probar),
            FunctionAction(action=CREAR_GMAIL, fn=_crear_gmail),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
