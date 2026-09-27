"""
Plugin `mensajeria` — mandar y recibir mensajes por cualquier servicio de
mensajería (Telegram, WhatsApp, Slack, un sistema propio…) a partir de
**plantillas** que arma quien usa el Bot.

Una plantilla es un servicio y una acción: "Telegram mensaje", "Telegram
archivo", "Telegram recibir". Tiene su URL base, sus headers (el token va como
`{env.TOKEN}`, la webapp lo resuelve), **los campos que se le agregan** (chat,
mensaje, menú…, cada uno con su tipo) y cómo se arma el envío o se lee lo
recibido. El plugin no sabe nada de ningún servicio: la forma de cada uno vive
en la plantilla, así que uno nuevo se suma cargando datos, no escribiendo
código. En el editor de un flujo, al elegir la plantilla, sus campos aparecen
como params del nodo (`describe_extra_params`, core#27).

Lo que esto hace y una conexión de `connections` no:

- **Tipos**: un campo `lista` ("Sí; No; Más tarde") se convierte en el arreglo
  que pide el servicio —con un molde por ítem y agrupado en filas, que es un
  menú de botones—; un `numero` viaja como número; un `archivo` sube el archivo.
- **Campos opcionales**: uno vacío se saca del cuerpo, en vez de mandar un
  `"reply_markup": {"inline_keyboard": ""}` que el servicio rechaza.
- **multipart/form-data**, para mandar archivos.
- **Recibir sin repetir**: guarda hasta dónde leyó (el `cursor` de la
  plantilla) y la próxima lectura sigue desde ahí.

Lo que NO hace, a propósito o porque el núcleo no lo permite hoy:

- **Bajar archivos recibidos**: el port `http` devuelve el cuerpo como texto
  (`HttpResponse.text`), así que un binario no llega entero.
- **Webhooks**: el servicio tendría que alcanzar al Bot por una dirección
  pública con HTTPS. Se lee consultando (`getUpdates` en Telegram), que anda
  detrás de cualquier red.
"""

from __future__ import annotations

import ast
import json
import mimetypes
import os
import re
import uuid
from urllib.parse import quote, urlencode

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
TIMEOUT = "timeout"
TIPOS = ("texto", "numero", "lista", "json", "archivo")
_NOMBRE_CAMPO = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

PLANTILLAS = Resource(
    name="plantillas",
    label="Plantillas de mensajería",
    item_label="Plantilla",
    key_field="nombre",
    doc=(
        "Una plantilla por servicio y acción (ej. 'Telegram mensaje'). Los campos son los que el nodo "
        "del flujo va a pedir; en 'enviar' o 'recibir' se usan como {campo}. 'Crear ejemplo de "
        "Telegram' deja tres armadas para copiar."
    ),
    fields=(
        Field("nombre", ParamType.STR, label="Nombre", required=True, doc="Cómo la elige el flujo, ej. 'Telegram mensaje'."),
        Field("servicio", ParamType.STR, label="Servicio", doc="Sólo informativo: Telegram, WhatsApp, Slack…"),
        Field(
            "url_base", ParamType.STR, label="URL base", required=True,
            doc="Ej. https://api.telegram.org/bot{env.TELEGRAM_TOKEN} — el token en Config → Variables, como secreto.",
        ),
        Field("headers", ParamType.JSON, label="Headers", default={}, doc='Ej. {"Authorization": "Bearer {env.TOKEN}"}.'),
        Field(
            "campos", ParamType.JSON, label="Campos", default=[],
            doc='Lista de campos: [{"nombre": "chat", "tipo": "texto", "obligatorio": true, "doc": "…"}, '
            '{"nombre": "menu", "tipo": "lista", "separador": ";", "item": {"text": "{valor}", '
            '"callback_data": "{valor}"}, "por_fila": 2}]. Tipos: texto, numero, lista, json, archivo. '
            'Opcional en cada uno: "defecto".',
        ),
        Field(
            "enviar", ParamType.JSON, label="Enviar", default={},
            doc='{"metodo": "POST", "ruta": "/sendMessage", "formato": "json" | "form" | "multipart", '
            '"cuerpo": {"chat_id": "{chat}", "text": "{mensaje}"}, "respuesta_id": "result.message_id"}. '
            'Un "{campo}" solo en un valor conserva el tipo; si el campo viene vacío, esa clave se saca.',
        ),
        Field(
            "recibir", ParamType.JSON, label="Recibir", default={},
            doc='{"ruta": "/getUpdates?offset={cursor}", "lista": "result", "id": "update_id", '
            '"salida": {"chat": "message.chat.id", "texto": "message.text|message.caption"}}. '
            'Con "|" se prueba el siguiente camino si el primero no está; un índice -1 es el último.',
        ),
        Field("cursor", ParamType.STR, label="Cursor", doc="Hasta dónde se leyó. Lo lleva el plugin; vaciarlo relee desde el principio."),
    ),
)

CREAR_TELEGRAM = Action(
    "crear_telegram", "Crear ejemplo de Telegram",
    doc="Crea plantillas armadas para Telegram (mensaje con menú de botones, archivo, recibir y "
    "confirmar botón), que leen el token de la variable indicada. Las que ya existen no se tocan.",
    params=(
        Param("variable", default="TELEGRAM_TOKEN", doc="La variable de Config con el token que da @BotFather."),
        Param("pisar", ParamType.BOOL, default=False, doc="Reemplazar las que ya existen con el mismo nombre."),
    ),
)

MANIFEST = PluginManifest(
    name="mensajeria",
    label="Mensajería",
    version="0.1.1",
    doc=(
        "Mandar y recibir mensajes por cualquier servicio (Telegram, WhatsApp, Slack…) con plantillas que "
        "arma cada uno: sus campos, cómo se envía y cómo se lee lo recibido."
    ),
    ports=(port_names.HTTP, port_names.FS),
    settings=(
        Setting(TIMEOUT, ParamType.INT, label="Segundos a esperar cada respuesta", default=30),
        Setting(
            MI_DIRECCION, ParamType.STR, label="Dirección de este Bot",
            doc="Normalmente VACÍA: sola resuelve a http://127.0.0.1:<puerto de este Bot>. Es por donde "
            "el plugin guarda el cursor de lectura y crea las plantillas de ejemplo.",
        ),
    ),
    resources=(PLANTILLAS,),
    actions=(CREAR_TELEGRAM,),
)


# ── Caminos adentro de un JSON ────────────────────────────────────────────

def _dig(obj, camino: str):
    """'result.message_id', 'message.photo.-1.file_id'. None si no está."""
    actual = obj
    for tramo in (camino or "").split("."):
        if tramo == "":
            continue
        if isinstance(actual, dict):
            actual = actual.get(tramo)
        elif isinstance(actual, list) and re.fullmatch(r"-?\d+", tramo):
            i = int(tramo)
            actual = actual[i] if -len(actual) <= i < len(actual) else None
        else:
            return None
        if actual is None:
            return None
    return actual


def _primero(obj, caminos: str):
    """El primer camino que existe de 'a.b|c.d'."""
    for camino in str(caminos).split("|"):
        valor = _dig(obj, camino.strip())
        if valor is not None:
            return valor
    return None


def _objeto(valor):
    """Un JSON, o el repr de Python con que llega un objeto por un param de texto."""
    if isinstance(valor, str) and valor.strip()[:1] in ("{", "["):
        for leer in (json.loads, ast.literal_eval):
            try:
                return leer(valor.strip())
            except (ValueError, SyntaxError):
                continue
    return valor


# ── La plantilla y sus campos ─────────────────────────────────────────────

def _plantilla(ctx: ToolContext, nombre: str) -> dict | ToolResult:
    nombre = (nombre or "").strip()
    plantilla = ctx.resource("plantillas", nombre, key_field="nombre") if nombre else None
    if plantilla is None:
        hay = ", ".join(ctx.resource_keys("plantillas", key_field="nombre")) or "ninguna"
        return ToolResult.err(f"no existe la plantilla '{nombre}'. Cargadas: {hay}")
    return plantilla


def _campos(plantilla: dict) -> list[dict] | str:
    campos = _objeto(plantilla.get("campos") or [])
    if not isinstance(campos, list):
        return "'campos' tiene que ser una lista"
    vistos = set()
    for c in campos:
        if not isinstance(c, dict) or not _NOMBRE_CAMPO.match(str(c.get("nombre", ""))):
            return f"campo mal armado: {c!r} (cada uno necesita 'nombre' con letras, números y _)"
        if (c.get("tipo") or "texto") not in TIPOS:
            return f"campo '{c['nombre']}': 'tipo' es {', '.join(TIPOS)}, no '{c.get('tipo')}'"
        if c["nombre"] in vistos:
            return f"el campo '{c['nombre']}' está dos veces"
        vistos.add(c["nombre"])
    return campos


class _Archivo:
    """Un campo 'archivo' ya leído, para el multipart."""

    def __init__(self, nombre: str, datos: bytes, tipo: str):
        self.nombre, self.datos, self.tipo = nombre, datos, tipo


def _valor(campo: dict, crudo, fs) -> tuple[object, str]:
    """(valor tipado, "") o (None, el error)."""
    tipo = campo.get("tipo") or "texto"
    nombre = campo["nombre"]
    if tipo == "texto":
        return str(crudo), ""
    if tipo == "numero":
        try:
            numero = float(str(crudo).strip())
        except ValueError:
            return None, f"'{nombre}' tiene que ser un número: {crudo!r}"
        return (int(numero) if numero.is_integer() else numero), ""
    if tipo == "json":
        leido = _objeto(crudo) if isinstance(crudo, str) else crudo
        if isinstance(leido, str):
            return None, f"'{nombre}' tiene que ser JSON"
        return leido, ""
    if tipo == "archivo":
        ruta = str(crudo).strip()
        try:
            datos = fs.read_bytes(ruta)
        except PortError as exc:
            return None, f"'{nombre}': no se pudo leer '{ruta}': {exc}"
        base = fs.basename(ruta)
        return _Archivo(base, datos, mimetypes.guess_type(base)[0] or "application/octet-stream"), ""
    # lista
    leido = _objeto(crudo) if isinstance(crudo, str) else crudo
    if isinstance(leido, list):
        valores = [str(v).strip() for v in leido if str(v).strip()]
    else:
        separador = campo.get("separador") or ";"
        valores = [v.strip() for v in str(crudo).split(separador) if v.strip()]
    molde = campo.get("item")
    items = [_moldear(molde, v, i) for i, v in enumerate(valores)] if molde is not None else valores
    por_fila = int(campo.get("por_fila") or 0)
    if por_fila > 0:
        return [items[i:i + por_fila] for i in range(0, len(items), por_fila)], ""
    return items, ""


def _moldear(molde, valor: str, indice: int):
    """Un ítem de una lista con su molde: {valor} y {indice} adentro de cualquier string."""
    if isinstance(molde, str):
        return molde.replace("{valor}", valor).replace("{indice}", str(indice))
    if isinstance(molde, dict):
        return {k: _moldear(v, valor, indice) for k, v in molde.items()}
    if isinstance(molde, list):
        return [_moldear(v, valor, indice) for v in molde]
    return molde


def _valores(ctx: ToolContext, plantilla: dict) -> dict | str:
    """{campo: valor tipado} de lo que trae el nodo; los vacíos sin defecto no están."""
    campos = _campos(plantilla)
    if isinstance(campos, str):
        return campos
    fs = ctx.port(port_names.FS) if any((c.get("tipo") == "archivo") for c in campos) else None
    valores, faltan = {}, []
    for campo in campos:
        crudo = ctx.extras.get(campo["nombre"])
        if crudo is None or (isinstance(crudo, str) and not crudo.strip()):
            crudo = campo.get("defecto")
        if crudo is None or (isinstance(crudo, str) and not crudo.strip()) or crudo == []:
            if campo.get("obligatorio"):
                faltan.append(campo["nombre"])
            continue
        valor, error = _valor(campo, crudo, fs)
        if error:
            return error
        if (campo.get("tipo") == "lista") and not valor:
            if campo.get("obligatorio"):
                faltan.append(campo["nombre"])
            continue
        valores[campo["nombre"]] = valor
    if faltan:
        return f"faltan campos obligatorios de '{plantilla['nombre']}': {', '.join(faltan)}"
    return valores


# ── Armar el pedido ───────────────────────────────────────────────────────

_VACIO = object()


def _render(obj, valores: dict, conocidos: set):
    """
    Reemplaza {campo} con los valores. Un "{campo}" que ocupa el string entero
    conserva el tipo (un número, una lista, un archivo); adentro de un texto se
    pega como texto. Una clave cuyo valor queda vacío se saca, y un objeto que
    se queda sin nada por eso también: así un campo opcional sin completar no
    manda basura al servicio.
    """
    if isinstance(obj, str):
        entero = re.fullmatch(r"\{(\w+)\}", obj)
        if entero:
            return valores.get(entero.group(1), _VACIO)

        def uno(m):
            v = valores.get(m.group(1))
            if v is None:
                # Un campo de la plantilla sin completar queda vacío; cualquier
                # otra cosa entre llaves es texto del servicio y se deja.
                return "" if m.group(1) in conocidos else m.group(0)
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return _PLACEHOLDER.sub(uno, obj)
    if isinstance(obj, dict):
        fuera = {}
        for k, v in obj.items():
            r = _render(v, valores, conocidos)
            if r is _VACIO or (isinstance(v, dict) and v and r == {}):
                continue
            fuera[k] = r
        return fuera
    if isinstance(obj, list):
        return [r for r in (_render(v, valores, conocidos) for v in obj) if r is not _VACIO]
    return obj


def _url(plantilla: dict, ruta: str, valores: dict) -> str:
    def uno(m):
        v = valores.get(m.group(1))
        return quote(str(v), safe="") if v is not None and not isinstance(v, (dict, list, _Archivo)) else ""
    return str(plantilla.get("url_base") or "").rstrip("/") + _PLACEHOLDER.sub(uno, ruta or "")


def _multipart(cuerpo: dict) -> tuple[bytes, str]:
    frontera = "----bot" + uuid.uuid4().hex
    partes = []
    for clave, valor in cuerpo.items():
        if isinstance(valor, _Archivo):
            cabecera = (f'Content-Disposition: form-data; name="{clave}"; filename="{valor.nombre}"\r\n'
                        f"Content-Type: {valor.tipo}\r\n\r\n")
            partes.append(f"--{frontera}\r\n{cabecera}".encode() + valor.datos + b"\r\n")
        else:
            texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
            partes.append(f'--{frontera}\r\nContent-Disposition: form-data; name="{clave}"\r\n\r\n{texto}\r\n'.encode())
    return b"".join(partes) + f"--{frontera}--\r\n".encode(), f"multipart/form-data; boundary={frontera}"


_ENV_SIN_RESOLVER = re.compile(r"\{env\.(\w+)\}")


class _FaltaVariable(Exception):
    pass


def _pedir(ctx: ToolContext, plantilla: dict, metodo: str, url: str, formato: str = "", cuerpo=None):
    headers = {str(k): str(v) for k, v in (_objeto(plantilla.get("headers")) or {}).items()}
    # El núcleo reemplaza {env.X} al entregar la plantilla; si quedó uno es que
    # la variable no existe. Mandarlo así termina en un 401/404 del servicio que
    # no dice nada (Telegram contesta 404 a un token inválido).
    faltan = sorted({m.group(1) for texto in (url, *headers.values()) for m in _ENV_SIN_RESOLVER.finditer(texto)})
    if faltan:
        raise _FaltaVariable(f"falta la variable {', '.join(faltan)} en Config → Variables")
    body = None
    if cuerpo is not None and metodo not in ("GET", "HEAD"):
        if formato == "multipart":
            body, headers["Content-Type"] = _multipart(cuerpo)
        elif formato == "form":
            planos = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in cuerpo.items()}
            body, headers["Content-Type"] = urlencode(planos), "application/x-www-form-urlencoded"
        else:
            body, headers["Content-Type"] = json.dumps(cuerpo, ensure_ascii=False), "application/json"
    respuesta = ctx.port(port_names.HTTP).request(
        url, method=metodo, headers=headers, body=body, timeout=float(ctx.config(TIMEOUT) or 30),
    )
    return respuesta, respuesta.json(default=None)


def _detalle(respuesta, datos) -> str:
    if isinstance(datos, dict):
        for clave in ("description", "error_description", "message", "error", "detail"):
            if datos.get(clave):
                v = datos[clave]
                return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return respuesta.text[:200]


# ── mensajeria.enviar ─────────────────────────────────────────────────────

def _describir_extras(node_params: dict, leer_item) -> tuple[Param, ...]:
    """Los campos de la plantilla elegida, como params del nodo (core#27)."""
    nombre = (node_params.get("plantilla") or "").strip()
    plantilla = leer_item("plantillas", nombre) if nombre else None
    campos = _campos(plantilla) if plantilla else []
    if isinstance(campos, str):
        return ()
    tipos = {"texto": ParamType.STR, "numero": ParamType.FLOAT, "lista": ParamType.STR,
             "json": ParamType.JSON, "archivo": ParamType.PATH}
    return tuple(
        Param(c["nombre"], tipos[c.get("tipo") or "texto"], required=bool(c.get("obligatorio")),
              default=c.get("defecto"), doc=str(c.get("doc") or ""))
        for c in campos
    )


ENVIAR = ToolManifest(
    id="mensajeria.enviar",
    label="enviar mensaje",
    category="MENSAJERÍA",
    doc=(
        "Manda un mensaje con una plantilla de Plug ins → Mensajería. Al elegir la plantilla aparecen "
        "sus campos (chat, mensaje, menú…). {id} es el id del mensaje enviado, si la plantilla dice "
        "dónde encontrarlo."
    ),
    params=(Param("plantilla", required=True, options_from="plantillas", doc="Nombre de la plantilla."),),
    extra_params=True,
    extra_params_doc="Los campos de la plantilla elegida.",
    outputs=(
        Output("id", ParamType.STR, doc="El id del mensaje enviado (según 'respuesta_id' de la plantilla)."),
        Output("status", ParamType.INT),
        Output("respuesta", ParamType.JSON),
    ),
)


def _enviar(ctx: ToolContext) -> ToolResult:
    vacios = {"id": "", "status": 0, "respuesta": {}}
    plantilla = _plantilla(ctx, ctx.params["plantilla"])
    if isinstance(plantilla, ToolResult):
        return ToolResult.err(plantilla.message, **vacios)
    envio = _objeto(plantilla.get("enviar") or {})
    if not isinstance(envio, dict) or not envio:
        return ToolResult.err(f"la plantilla '{plantilla['nombre']}' no tiene 'enviar' (¿es una de recibir?)", **vacios)
    valores = _valores(ctx, plantilla)
    if isinstance(valores, str):
        return ToolResult.err(valores, **vacios)
    conocidos = {c["nombre"] for c in _campos(plantilla)}
    metodo = str(envio.get("metodo") or "POST").upper()
    formato = str(envio.get("formato") or "json")
    cuerpo = _render(envio.get("cuerpo") or {}, valores, conocidos)
    if any(isinstance(v, _Archivo) for v in (cuerpo.values() if isinstance(cuerpo, dict) else [])) and formato != "multipart":
        return ToolResult.err("la plantilla manda un archivo: 'formato' tiene que ser 'multipart'", **vacios)
    url = _url(plantilla, envio.get("ruta") or "", valores)
    try:
        respuesta, datos = _pedir(ctx, plantilla, metodo, url, formato, cuerpo)
    except _FaltaVariable as exc:
        return ToolResult.err(f"{plantilla['nombre']}: {exc}", **vacios)
    except PortError as exc:
        return ToolResult.err(f"{plantilla['nombre']}: el servicio no responde: {exc}", **vacios)
    if not respuesta.ok or (isinstance(datos, dict) and datos.get("ok") is False):
        return ToolResult.err(f"{plantilla['nombre']}: el servicio respondió {respuesta.status}: {_detalle(respuesta, datos)}",
                              **{**vacios, "status": respuesta.status, "respuesta": datos if datos is not None else {}})
    id_ = _primero(datos, envio["respuesta_id"]) if envio.get("respuesta_id") and datos is not None else None
    ctx.log(f"{plantilla['nombre']}: enviado" + (f" (id {id_})" if id_ is not None else ""))
    return ToolResult.ok(id="" if id_ is None else str(id_), status=respuesta.status,
                         respuesta=datos if datos is not None else {"texto": respuesta.text[:2000]})


# ── mensajeria.recibir ────────────────────────────────────────────────────

RECIBIR = ToolManifest(
    id="mensajeria.recibir",
    label="recibir mensajes",
    category="MENSAJERÍA",
    doc=(
        "Lee los mensajes nuevos con una plantilla que tenga 'recibir', desde donde quedó la última "
        "vez. {mensajes} trae cada uno con las columnas de 'salida'; {primero} el primero, para leerlo "
        "con {NODO.primero.texto}; {hay} es 'si' o 'no', para un nodo de decisión. Lo leído no se "
        "vuelve a leer: si el flujo falla después, ese mensaje no vuelve."
    ),
    params=(
        Param("plantilla", required=True, options_from="plantillas", doc="Nombre de la plantilla."),
        Param("limite", ParamType.INT, default=10, doc="Cuántos mensajes como máximo. Los demás quedan para la próxima."),
    ),
    outputs=(
        Output("mensajes", ParamType.JSON, doc="Lista, cada uno con las columnas de 'salida' y 'crudo'."),
        Output("primero", ParamType.JSON, doc="El primer mensaje, o vacío."),
        Output("cantidad", ParamType.INT),
        Output("hay", ParamType.STR, doc="'si' o 'no'."),
    ),
)


def _este_bot(ctx: ToolContext) -> str:
    puerto = (os.environ.get("BOT_PORT") or "8000").strip()
    return str(ctx.config(MI_DIRECCION) or f"http://127.0.0.1:{puerto}").strip().rstrip("/")


def _al_bot(ctx: ToolContext, metodo: str, camino: str, payload: dict | None = None):
    respuesta = ctx.port(port_names.HTTP).request(
        f"{_este_bot(ctx)}/api/core{camino}", method=metodo,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        body=json.dumps(payload, ensure_ascii=False) if payload is not None else None, timeout=20.0,
    )
    return respuesta, respuesta.json(default=None)


def _guardar_plantilla(ctx: ToolContext, plantilla: dict, **cambios) -> str | None:
    """
    Reescribe la plantilla con `cambios`, por la API local del Bot (un plugin no
    tiene otra forma de escribir su colección). La URL y los headers se mandan
    como se guardaron —con `{env.X}`—, no resueltos: `ctx.resource` los entrega
    ya resueltos, y reescribirlos así dejaría el token en claro en la plantilla.
    """
    try:
        respuesta, guardada = _al_bot(ctx, "GET", f"/resources/mensajeria/plantillas/{quote(plantilla['nombre'], safe='')}")
    except PortError as exc:
        return f"no se pudo leer la plantilla en {_este_bot(ctx)}: {exc}"
    if not respuesta.ok or not isinstance(guardada, dict):
        return f"este Bot respondió {respuesta.status} al leer la plantilla"
    item = guardada.get("item", guardada)
    item = {k: v for k, v in item.items() if not str(k).startswith("_")}
    item.update(cambios)
    try:
        respuesta, _ = _al_bot(ctx, "PUT", f"/resources/mensajeria/plantillas/{quote(plantilla['nombre'], safe='')}", {"item": item})
    except PortError as exc:
        return f"no se pudo guardar la plantilla en {_este_bot(ctx)}: {exc}"
    if not respuesta.ok:
        return f"este Bot respondió {respuesta.status} al guardar la plantilla"
    return None


def _recibir(ctx: ToolContext) -> ToolResult:
    vacios = {"mensajes": [], "primero": {}, "cantidad": 0, "hay": "no"}
    plantilla = _plantilla(ctx, ctx.params["plantilla"])
    if isinstance(plantilla, ToolResult):
        return ToolResult.err(plantilla.message, **vacios)
    spec = _objeto(plantilla.get("recibir") or {})
    if not isinstance(spec, dict) or not spec.get("ruta"):
        return ToolResult.err(f"la plantilla '{plantilla['nombre']}' no tiene 'recibir'", **vacios)
    cursor = str(plantilla.get("cursor") or "").strip()
    url = _url(plantilla, spec["ruta"], {"cursor": cursor} if cursor else {})
    try:
        respuesta, datos = _pedir(ctx, plantilla, "GET", url)
    except _FaltaVariable as exc:
        return ToolResult.err(f"{plantilla['nombre']}: {exc}", **vacios)
    except PortError as exc:
        return ToolResult.err(f"{plantilla['nombre']}: el servicio no responde: {exc}", **vacios)
    if not respuesta.ok or (isinstance(datos, dict) and datos.get("ok") is False):
        return ToolResult.err(f"{plantilla['nombre']}: el servicio respondió {respuesta.status}: {_detalle(respuesta, datos)}", **vacios)
    lista = _dig(datos, spec.get("lista") or "") if spec.get("lista") else datos
    if not isinstance(lista, list):
        return ToolResult.err(f"{plantilla['nombre']}: la respuesta no trae una lista en '{spec.get('lista')}'", **vacios)

    limite = max(1, int(ctx.params.get("limite") or 10))
    tomados = lista[:limite]
    salida = _objeto(spec.get("salida") or {})
    mensajes = [{**{col: _primero(m, camino) for col, camino in (salida or {}).items()}, "crudo": m} for m in tomados]

    # El cursor se guarda ANTES de devolver: si se guardara después y el flujo
    # fallara en el medio, la próxima lectura repetiría lo mismo para siempre.
    # Avanza sólo hasta lo que se devolvió; lo que quedó afuera por 'limite' viene la próxima.
    if tomados and spec.get("id"):
        ultimo = _dig(tomados[-1], spec["id"])
        try:
            nuevo = str(int(ultimo) + 1)
        except (TypeError, ValueError):
            return ToolResult.err(f"{plantilla['nombre']}: el '{spec['id']}' del último mensaje no es un número: {ultimo!r}", **vacios)
        if error := _guardar_plantilla(ctx, plantilla, cursor=nuevo):
            return ToolResult.err(f"{plantilla['nombre']}: no se pudo guardar hasta dónde se leyó ({error}); no se devuelve nada para no repetir", **vacios)

    ctx.log(f"{plantilla['nombre']}: {len(mensajes)} mensaje(s)" + (f", quedan {len(lista) - len(tomados)}" if len(lista) > len(tomados) else ""))
    return ToolResult.ok(mensajes=mensajes, primero=mensajes[0] if mensajes else {}, cantidad=len(mensajes),
                         hay="si" if mensajes else "no")


# ── Ejemplo de Telegram ───────────────────────────────────────────────────

def _telegram(variable: str) -> list[dict]:
    base = f"https://api.telegram.org/bot{{env.{variable}}}"
    chat = {"nombre": "chat", "tipo": "texto", "obligatorio": True, "doc": "El chat_id (un número; el de cada mensaje recibido viene en {primero.chat})."}
    return [
        {
            "nombre": "Telegram mensaje", "servicio": "Telegram", "url_base": base, "headers": {},
            "campos": [
                chat,
                {"nombre": "mensaje", "tipo": "texto", "obligatorio": True, "doc": "El texto."},
                {"nombre": "menu", "tipo": "lista", "separador": ";", "por_fila": 2,
                 "item": {"text": "{valor}", "callback_data": "{valor}"},
                 "doc": "Botones debajo del mensaje, separados por ';'. Lo que se aprieta llega en {primero.boton} al recibir."},
                {"nombre": "formato", "tipo": "texto", "doc": "Vacío, HTML o MarkdownV2."},
            ],
            "enviar": {"metodo": "POST", "ruta": "/sendMessage", "formato": "json",
                       "cuerpo": {"chat_id": "{chat}", "text": "{mensaje}", "parse_mode": "{formato}",
                                  "reply_markup": {"inline_keyboard": "{menu}"}},
                       "respuesta_id": "result.message_id"},
            "recibir": {},
        },
        {
            "nombre": "Telegram archivo", "servicio": "Telegram", "url_base": base, "headers": {},
            "campos": [
                chat,
                {"nombre": "archivo", "tipo": "archivo", "obligatorio": True, "doc": "La ruta del archivo a mandar."},
                {"nombre": "texto", "tipo": "texto", "doc": "Opcional: el texto debajo del archivo."},
            ],
            "enviar": {"metodo": "POST", "ruta": "/sendDocument", "formato": "multipart",
                       "cuerpo": {"chat_id": "{chat}", "document": "{archivo}", "caption": "{texto}"},
                       "respuesta_id": "result.message_id"},
            "recibir": {},
        },
        {
            "nombre": "Telegram recibir", "servicio": "Telegram", "url_base": base, "headers": {},
            "campos": [], "enviar": {},
            "recibir": {
                "ruta": "/getUpdates?offset={cursor}&timeout=0", "lista": "result", "id": "update_id",
                "salida": {
                    "chat": "message.chat.id|callback_query.message.chat.id",
                    "texto": "message.text|message.caption",
                    "boton": "callback_query.data",
                    "boton_id": "callback_query.id",
                    "de": "message.from.username|message.from.first_name|callback_query.from.username|callback_query.from.first_name",
                    "mensaje_id": "message.message_id|callback_query.message.message_id",
                    "archivo_id": "message.document.file_id|message.photo.-1.file_id",
                },
            },
        },
        {
            "nombre": "Telegram confirmar boton", "servicio": "Telegram", "url_base": base, "headers": {},
            "campos": [
                {"nombre": "boton_id", "tipo": "texto", "obligatorio": True, "doc": "El {primero.boton_id} de lo recibido."},
                {"nombre": "aviso", "tipo": "texto", "doc": "Opcional: un cartelito que ve quien apretó."},
            ],
            "enviar": {"metodo": "POST", "ruta": "/answerCallbackQuery", "formato": "json",
                       "cuerpo": {"callback_query_id": "{boton_id}", "text": "{aviso}"}},
            "recibir": {},
        },
    ]


def _crear_telegram(ctx: ToolContext) -> ToolResult:
    variable = (ctx.params.get("variable") or "TELEGRAM_TOKEN").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        return ToolResult.err(f"'variable' tiene que ser letras, números y _: {variable!r}")
    pisar = bool(ctx.params.get("pisar"))
    creadas, salteadas = [], []
    for plantilla in _telegram(variable):
        camino = f"/resources/mensajeria/plantillas/{quote(plantilla['nombre'], safe='')}"
        try:
            if not pisar:
                existe, _ = _al_bot(ctx, "GET", camino)
                if existe.ok:
                    salteadas.append(plantilla["nombre"])
                    continue
            respuesta, datos = _al_bot(ctx, "PUT", camino, {"item": plantilla})
        except PortError as exc:
            return ToolResult.err(f"no se pudo escribir en {_este_bot(ctx)}: {exc}", creadas=creadas, salteadas=salteadas)
        if not respuesta.ok:
            detalle = datos.get("detail") if isinstance(datos, dict) else respuesta.text[:200]
            return ToolResult.err(f"'{plantilla['nombre']}': este Bot respondió {respuesta.status}: {detalle}",
                                  creadas=creadas, salteadas=salteadas)
        creadas.append(plantilla["nombre"])
    mensaje = f"{len(creadas)} plantilla(s) creada(s)" + (f"; ya existían: {', '.join(salteadas)}" if salteadas else "")
    return ToolResult.ok(mensaje + f". Falta el token de @BotFather en Config → Variables como {variable} (secreto).",
                         creadas=creadas, salteadas=salteadas)


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[
            FunctionTool(manifest=ENVIAR, fn=_enviar, describe_extra_params=_describir_extras),
            FunctionTool(manifest=RECIBIR, fn=_recibir),
        ],
        actions=[FunctionAction(action=CREAR_TELEGRAM, fn=_crear_telegram)],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
