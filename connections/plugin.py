"""
Plugin `connections` — llamadas HTTP guardadas, genéricas para cualquier
instalación de `workflow-bot-core`.

Resuelve dos necesidades distintas, y por eso son dos `Resource` separados en
vez de uno solo:

- **sources**: de dónde sale la grilla del panel principal. Trae una lista y
  de ahí salen las filas, la clave de cada una (`case_id`) y las columnas.
  Nunca es un nodo de un flujo — lo lee la webapp para dibujar la tabla y para
  disparar `Instance.run(row=fila)` por fila o en tanda.
- **actions**: una llamada HTTP guardada que un nodo de un flujo dispara para
  traer un dato puntual y seguir el flujo con eso. Es la única de las dos que
  expone un Tool.

Este plugin nació como `webapp/connections` de `workflow-bot-app` — un
default de esa app, con acceso privilegiado a su propio store de Config
(sabía qué variables eran secretas y las tapaba en logs y respuestas). Acá es
un plugin de terceros como cualquier otro del catálogo, y por eso hay una
diferencia real, no cosmética:

**`{env.CLAVE}`** se resuelve contra `os.environ` — el entorno del proceso,
no un store de Config privado. Si el host ya resuelve esa referencia antes de
que el plugin vea el item (como hace `workflow-bot-core` con cualquier
`ctx.resource()` o `run_action(item=...)`, para cualquier plugin — ver
`Instance.resource_items`), acá no hay nada que hacer: ya llega resuelto. Esta
resolución propia es sólo la red de contención para lo que todavía llegue
literal — típicamente, "armar y probar" un source o una Action **antes** de
guardarla, que manda la config cruda como params y no pasa por esa colección.
En una instalación sin ese store (el núcleo a secas, por CLI), exportar la
variable como variable de entorno del sistema alcanza.

**No tapa secretos.** La versión de la webapp conocía qué variables eran
secretas (vía su propio store) y las ocultaba de la traza del run y de la
respuesta mostrada en "Probar". Este plugin no tiene — ni debe tener — esa
visibilidad: ningún plugin de terceros ve qué es secreto en el host. Si una
Action pone el token en la URL (query string) en vez de en un header, o la
API ajena lo repite en una respuesta de error, va a aparecer en claro en la
traza del run y en la pantalla de "Probar". Mismo riesgo que cualquier otro
plugin que llama a una URL con un dato sensible adentro.

Paginación de un source: por defecto se trae la fuente entera y se pagina en
memoria (el mismo mecanismo que ya usa un CSV cargado entero). Sólo si el
source declara `page_param` se le pide página por página a la API externa —
es la salida para cuando traer todo de una vez no da abasto. Las dos cosas
conviven en el mismo schema; se elige una según lo que ese source necesite,
nunca las dos a la vez.

Tipos de source: hoy sólo `http`. Un origen de archivos (CSV, etc.) es un
módulo aparte a propósito — no contamina este.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

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

def _si_el_nucleo_sabe(cls, **campos) -> dict:
    """
    Los kwargs que el contrato vendorizado realmente declara.

    `options_from` y `describe_extra_params` llegaron en el núcleo
    v0.3.1-beta.8 (core#27), y un host puede tener un núcleo anterior. Con
    uno viejo estos kwargs no existen y `Param(...)`/`FunctionTool(...)`
    reventarían **al importar**, o sea que no cargaría el plugin entero y
    Sources y Actions desaparecerían de la pantalla. Así, con un núcleo viejo
    lo único que falta es el buscador y los params descubiertos.
    """
    declarados = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in campos.items() if k in declarados}


# ── Resources ─────────────────────────────────────────────────────────────

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

SOURCES = Resource(
    name="sources",
    label="Sources",
    item_label="Source",
    key_field="name",
    doc=(
        "Fuente de datos para la grilla del panel principal: trae una lista, y "
        "de ahí salen las filas, la clave de caso y las columnas."
    ),
    fields=(
        Field(
            "kind", ParamType.ENUM, label="Tipo", default="http", choices=("http",),
            doc="Por ahora sólo 'http'. Un origen de archivos es un módulo aparte.",
        ),
        Field("url", ParamType.STR, label="URL", required=True),
        Field("method", ParamType.ENUM, label="Método", default="GET", choices=_HTTP_METHODS),
        Field("headers", ParamType.JSON, label="Headers", default={}),
        Field("payload", ParamType.JSON, label="Payload", default={}),
        Field(
            "results_path", ParamType.STR, label="Camino al array",
            doc="Ej. 'data.items'. Vacío si la raíz de la respuesta ya es la lista.",
        ),
        Field(
            "key_field", ParamType.STR, label="Campo clave", required=True,
            doc="Qué campo de cada fila identifica el caso (case_id) para el bot.",
        ),
        Field("default_flow", ParamType.STR, label="Flujo por defecto"),
        # Qué columnas no dibuja la grilla. Sólo la vista: la fila entera, con
        # todos sus campos, le sigue llegando al flujo. Se guardan las ocultas
        # y no las visibles para que un campo nuevo de la API aparezca solo en
        # vez de quedar escondido sin que nadie lo sepa.
        Field(
            "columnas_ocultas", ParamType.JSON, label="Columnas ocultas", default=[],
            doc="Columnas que la grilla no muestra. Siguen llegando al flujo: sólo cambia lo que se ve.",
        ),
        # Paginación externa — opcional. Vacío = se trae todo y se pagina en
        # memoria, igual que un CSV cargado entero.
        Field(
            "page_param", ParamType.STR, label="Parámetro de página",
            doc="Nombre del query param de página en la API. Vacío = sin paginación externa.",
        ),
        Field("page_size_param", ParamType.STR, label="Parámetro de tamaño de página"),
        Field("page_size", ParamType.INT, label="Filas por página", default=100),
        Field(
            "total_path", ParamType.STR, label="Camino al total",
            doc="Ej. 'data.total'. Para saber cuándo dejar de pedir páginas.",
        ),
    ),
)

ACTIONS = Resource(
    name="actions",
    label="Actions",
    item_label="Action",
    key_field="name",
    doc=(
        "Llamada HTTP guardada que un nodo de un flujo dispara para traer un dato "
        "puntual y seguir con eso — no alimenta la grilla."
    ),
    fields=(
        Field("url", ParamType.STR, label="URL", required=True),
        Field("method", ParamType.ENUM, label="Método", default="GET", choices=_HTTP_METHODS),
        Field("headers", ParamType.JSON, label="Headers", default={}),
        Field("payload", ParamType.JSON, label="Payload", default={}),
        Field(
            "results_path", ParamType.STR, label="Camino al resultado",
            doc="Opcional: qué parte de la respuesta guardar en {result}.",
        ),
    ),
)


# ── Helpers de HTTP, propios del plugin ────────────────────────────────────


def _dig(obj, path: str):
    """Recorre un camino con puntos ('data.items') dentro de dicts/listas."""
    if not path:
        return obj
    current = obj
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.lstrip("-").isdigit():
            idx = int(part)
            current = current[idx] if -len(current) <= idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _con_query(url: str, params: dict) -> str:
    """Agrega/pisa query params sin tocar el resto de la URL."""
    partes = urlparse(url)
    query = dict(parse_qsl(partes.query))
    query.update({k: str(v) for k, v in params.items()})
    return urlunparse(partes._replace(query=urlencode(query)))


def _cuerpo(payload) -> str | None:
    if not payload:
        return None
    return json.dumps(payload)


# El adaptador HTTP del núcleo no pone User-Agent, y urllib manda el suyo:
# `Python-urllib/3.x`. Cloudflare (y otros WAF) bloquean ese con un 403 antes
# de mirar el token, así que una fuente con el `{env.X}` bien resuelto seguía
# fallando y parecía un problema de credenciales. Uno propio, sólo si la
# conexión no declara el suyo en los headers.
# Sólo ASCII: http.client codifica las cabeceras en latin-1, y un carácter
# fuera de ese rango (ej. una raya larga "—") revienta con UnicodeEncodeError
# antes de salir a la red, para cualquier Action/Source que no declare su
# propio User-Agent.
USER_AGENT = "workflow-bot-core (Bot) - plugin connections"


def _headers_con_json(headers: dict, cuerpo: str | None) -> dict:
    headers = dict(headers or {})
    if cuerpo is not None and not any(h.lower() == "content-type" for h in headers):
        headers["Content-Type"] = "application/json"
    if not any(h.lower() == "user-agent" for h in headers):
        headers["User-Agent"] = USER_AGENT
    return headers


class _SinVariable(Exception):
    """Un `{env.X}` sin valor en el entorno: se dice antes de mandar nada."""


def _pedir(http, cfg: dict, *, timeout: float):
    """Un request HTTP a partir de un schema de source/action, con `{env.X}` resuelto acá."""
    cfg = {**cfg, **_resolver_env({k: cfg.get(k) for k in ("url", "headers", "payload")})}
    if faltan := _env_faltantes({k: cfg.get(k) for k in ("url", "headers", "payload")}):
        raise _SinVariable(_error_faltantes(faltan))
    cuerpo = _cuerpo(cfg.get("payload"))
    return http.request(
        cfg["url"],
        method=cfg.get("method") or "GET",
        headers=_headers_con_json(cfg.get("headers") or {}, cuerpo),
        body=cuerpo,
        timeout=timeout,
    )


def _coincide(fila, termino: str) -> bool:
    termino = termino.lower()
    if not isinstance(fila, dict):
        return termino in str(fila).lower()
    return any(termino in str(v).lower() for v in fila.values())


def _coincide_filtros(fila, filtros: dict) -> bool:
    if not isinstance(fila, dict):
        return False
    return all(str(fila.get(campo)) == str(valor) for campo, valor in filtros.items())


# Por encima de esto una columna no es para filtrar por igualdad — un id o un
# texto libre tiene un valor casi distinto por fila, y el desplegable
# terminaría siendo la lista entera de la fuente en vez de una ayuda.
_MAX_VALORES_FACETA = 50


def _facetas(filas: list) -> dict:
    """Valores únicos por columna, para poblar el desplegable de cada filtro."""
    valores: dict[str, set] = {}
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        for campo, valor in fila.items():
            valores.setdefault(campo, set()).add(str(valor))
    return {
        campo: sorted(vs) for campo, vs in valores.items()
        if 0 < len(vs) <= _MAX_VALORES_FACETA
    }


def _fetch_page(
    http, cfg: dict, *, offset: int, limit: int, timeout: float,
    search: str | None = None, filters: dict | None = None,
) -> dict:
    """
    Trae una página de filas de un source.

    Con `page_param` declarado, le pide sólo esa página a la API externa. Sin
    eso, trae la fuente entera de una vez y corta/pagina en memoria.

    `search` filtra por substring en cualquier valor de la fila; `filters` es
    por columna, igualdad exacta (el desplegable de cada columna). Sin
    paginación externa filtran la fuente entera antes de cortar la página —
    una búsqueda de verdad. Con paginación externa sólo pueden filtrar la
    página que ya se trajo: pedirle "todas las páginas" a una API ajena para
    poder buscar arruina la razón de tener `page_param`.

    `facets` (valores únicos por columna, para esos mismos desplegables) sale
    de las filas que ya pasaron `search` pero no `filters`: así elegir un valor
    en una columna no le achica las opciones a las demás.
    """
    externa = bool(cfg.get("page_param"))
    peticion = dict(cfg)

    if externa:
        tam = int(cfg.get("page_size") or limit or 100)
        pagina = offset // tam + 1
        params = {cfg["page_param"]: pagina}
        if cfg.get("page_size_param"):
            params[cfg["page_size_param"]] = tam
        peticion["url"] = _con_query(cfg["url"], params)

    try:
        respuesta = _pedir(http, peticion, timeout=timeout)
    except _SinVariable as exc:
        return {"error": str(exc), "rows": [], "total": 0, "facets": {}}
    if not respuesta.ok:
        return {"error": f"la fuente respondió {respuesta.status}", "rows": [], "total": 0, "facets": {}}

    cuerpo = respuesta.json(default=None)
    filas = _dig(cuerpo, cfg.get("results_path") or "")
    if filas is None:
        filas = cuerpo if isinstance(cuerpo, list) else None
    if not isinstance(filas, list):
        return {
            "error": "la respuesta no es una lista de filas (revisá 'camino al array')",
            "rows": [], "total": 0, "facets": {},
        }

    if search:
        filas = [f for f in filas if _coincide(f, search)]
    facetas = _facetas(filas)
    if filters:
        filas = [f for f in filas if _coincide_filtros(f, filters)]

    total = _dig(cuerpo, cfg.get("total_path") or "")
    if not externa:
        # Sin paginación externa: ya está todo acá. Filtra, cuenta y recién
        # ahí pagina en memoria — el filtro tiene que mirar la fuente entera,
        # no la página que ya se había cortado.
        total = len(filas)
        if limit:
            filas = filas[offset: offset + limit]
    elif not isinstance(total, int):
        total = offset + len(filas)

    return {"error": None, "rows": filas, "total": total, "facets": facetas}


_PATRON_VAR = re.compile(r"\{(\w+)\}")
_PATRON_ENV = re.compile(r"\{env\.(\w+)\}")


# ── Variables de entorno: {env.CLAVE} ───────────────────────────────────
#
# Resuelto contra `os.environ`: es el único mecanismo genérico que cualquier
# instalación de workflow-bot-core puede ofrecer sin que el plugin necesite
# acceso privilegiado al Config del host (ver docstring del módulo). Un host
# con su propio store de variables ya resuelve `{env.CLAVE}` antes de que el
# plugin vea el item de una colección (workflow-bot-core#9: cualquier
# `ctx.resource()` lo hace, para cualquier plugin) — esta resolución es la
# red de contención para lo que llegue literal igual, típicamente "probar"
# algo antes de guardarlo.


def _sustituir_en_url(url: str, patron: re.Pattern, valor_de) -> str:
    """
    Sustituye placeholders en una URL, codificando lo que cae en la query.

    Un valor como `is:unread from:x` pegado crudo en `?q={q}` arma una URL con
    espacios y dos puntos sin codificar, que el servidor ajeno lee a medias o
    rechaza. En la query se codifica entero (`quote(safe="")`); en el camino
    queda como estaba, porque ahí una `/` en el valor puede ser a propósito.
    `valor_de(match) -> str | None`: None deja el placeholder literal.
    """
    corte = url.find("?")
    camino, query = (url, "") if corte < 0 else (url[:corte], url[corte:])

    def _crudo(m: re.Match) -> str:
        val = valor_de(m)
        return m.group(0) if val is None else str(val)

    def _codificado(m: re.Match) -> str:
        val = valor_de(m)
        return m.group(0) if val is None else quote(str(val), safe="")

    return patron.sub(_crudo, camino) + patron.sub(_codificado, query)


def _resolver_env(valor):
    """`{env.CLAVE}` resuelto en url, headers y payload contra `os.environ`."""

    def _mapear(v):
        if isinstance(v, str):
            return _PATRON_ENV.sub(lambda m: os.environ.get(m.group(1), m.group(0)), v)
        if isinstance(v, dict):
            return {k: _mapear(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_mapear(x) for x in v]
        return v

    resuelto = _mapear(valor)
    if isinstance(valor, dict) and isinstance(valor.get("url"), str):
        resuelto["url"] = _sustituir_en_url(
            valor["url"], _PATRON_ENV, lambda m: os.environ.get(m.group(1))
        )
    return resuelto


def _env_faltantes(valor) -> list[str]:
    """Los `{env.X}` que siguen sin resolver: variables que no están en el entorno."""
    faltan: list[str] = []

    def _ver(v):
        if isinstance(v, str):
            for m in _PATRON_ENV.finditer(v):
                if m.group(1) not in faltan:
                    faltan.append(m.group(1))
        elif isinstance(v, dict):
            for x in v.values():
                _ver(x)
        elif isinstance(v, list):
            for x in v:
                _ver(x)

    _ver(valor)
    return faltan


def _error_faltantes(faltan: list[str]) -> str:
    nombres = ", ".join(faltan)
    plural = "n las variables" if len(faltan) > 1 else " la variable"
    return f"falta{plural} {nombres} en el entorno (variable de entorno del sistema, o del store de Config del host si tiene uno)"


def _resolver(valor, obtener):
    """
    Sustituye `{campo}` en url/headers/payload contra `obtener(nombre)`.

    Recursivo porque el payload guardado es JSON libre — la referencia puede
    estar en la URL, en un header o anidada en el cuerpo. Lo que no resuelve
    queda literal, igual que el resto del motor: mejor un `{id_externo}` visible
    en la traza que un `None` silencioso.

    `obtener` es cualquier callable `(nombre) -> valor | None`, no un
    `ToolContext`: en un run es `ctx.var`, contra el contexto real; probando
    una Action antes de guardarla es un dict a mano que arma quien prueba —
    ahí no hay run, ni caso, ni contexto de donde sacar nada.
    """

    def _uno(m: re.Match) -> str:
        val = obtener(m.group(1))
        return str(val) if val is not None else m.group(0)

    def _mapear(v):
        if isinstance(v, str):
            return _PATRON_VAR.sub(_uno, v)
        if isinstance(v, dict):
            return {k: _mapear(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_mapear(x) for x in v]
        return v

    resuelto = _mapear(valor)
    if isinstance(valor, dict) and isinstance(valor.get("url"), str):
        resuelto["url"] = _sustituir_en_url(valor["url"], _PATRON_VAR, lambda m: obtener(m.group(1)))
    return resuelto


# ── Tool: connections.llamar — el único nodo de flujo de este plugin ──────

LLAMAR = ToolManifest(
    id="connections.llamar",
    label="llamar conexión",
    category="CONNECTIONS",
    doc=(
        "Ejecuta una Action guardada en Connections. Sustituye {variables} en "
        "la URL, headers y payload contra cualquier otro param del propio "
        "nodo primero (para pisar un campo con un literal distinto por nodo, "
        "ej. un 'texto' de comentario) y si no contra el contexto del run; "
        "deja la respuesta en {response} y, si la Action declara 'camino al "
        "resultado', también en {result}."
    ),
    params=(Param("connection", required=True, doc="Nombre de la Action guardada.",
                  **_si_el_nucleo_sabe(Param, options_from="actions")),),
    extra_params=True,
    extra_params_doc="Cualquier otro param pisa al {variable} de mismo nombre en la Action, antes que el contexto del run.",
    outputs=(
        Output("response", ParamType.JSON),
        Output("status", ParamType.INT),
        Output("result", ParamType.JSON),
    ),
)


def _ejecutar_guardada(ctx: ToolContext, nombre: str):
    """
    Lo que comparten `llamar` y `llamar_y_fusionar`: buscar la Action, pegarle
    y devolver (guardada, respuesta, cuerpo_resp, resultado) — o un
    `ToolResult` de error si algo de eso falla, para que el caller lo
    propague tal cual.
    """
    guardada = ctx.resource("actions", nombre)
    if guardada is None:
        disponibles = ", ".join(ctx.resource_keys("actions")) or "ninguna"
        return ToolResult.err(f"no existe la conexión '{nombre}'. Disponibles: {disponibles}")

    # Un extra del propio nodo (`texto=...` en el .mmd) pisa a la variable de
    # mismo nombre del contexto del run: es lo que permite compartir una sola
    # Action guardada entre varios nodos que mandan un texto distinto cada uno.
    resuelta = _resolver(
        {"url": guardada["url"], "headers": guardada.get("headers") or {},
         "payload": guardada.get("payload") or {}},
        lambda nombre: ctx.extras.get(nombre, ctx.var(nombre)),
    )
    # `{env.X}` acá y no sólo adentro de `_pedir`: la línea del log tiene que
    # ser la URL que salió, no la plantilla.
    resuelta = _resolver_env(resuelta)
    try:
        respuesta = _pedir(
            ctx.port(port_names.HTTP), {**resuelta, "method": guardada.get("method")},
            timeout=float(ctx.config("connectionsTimeout") or 30.0),
        )
    except _SinVariable as exc:
        return ToolResult.err(f"la conexión '{nombre}': {exc}")
    # Esta línea va a la traza del run. A diferencia de un host con acceso
    # privilegiado a qué es secreto, este plugin no tapa nada acá: si el
    # token va en la URL en vez de en un header, va a quedar visible en la
    # traza (ver docstring del módulo).
    ctx.log(f"{guardada.get('method', 'GET')} {resuelta['url']} → {respuesta.status}")
    cuerpo_resp = respuesta.json(default=respuesta.text)
    resultado = _dig(cuerpo_resp, guardada.get("results_path") or "")
    return guardada, respuesta, cuerpo_resp, resultado


def _llamar(ctx: ToolContext) -> ToolResult:
    nombre = ctx.params["connection"]
    ejecutada = _ejecutar_guardada(ctx, nombre)
    if isinstance(ejecutada, ToolResult):
        return ejecutada
    _guardada, respuesta, cuerpo_resp, resultado = ejecutada

    if not respuesta.ok:
        return ToolResult.err(
            f"la conexión '{nombre}' respondió {respuesta.status}",
            status=respuesta.status, response=cuerpo_resp,
        )
    return ToolResult.ok(response=cuerpo_resp, status=respuesta.status, result=resultado)


# ── Tool: connections.llamar_y_fusionar ────────────────────────────────────
#
# Distinto de `llamar` a propósito, no un flag más: además de {response},
# {status} y {result}, vuelca cada campo de primer nivel de la respuesta
# directo al contexto del run — así un nodo más adelante puede leer {pais} o
# {tratamiento_intranet} sin un segundo tool que los saque de {response}. Es
# lo que un flujo necesita para "traer la fila de nuevo y seguir con sus
# datos al día". El precio es real —puede pisar cualquier variable del
# contexto con el mismo nombre que un campo de la respuesta— así que es un
# tool aparte, con un nombre que lo dice, no un default silencioso.

FUSIONAR = ToolManifest(
    id="connections.llamar_y_fusionar",
    label="llamar y fusionar al contexto",
    category="CONNECTIONS",
    doc=(
        "Como 'llamar conexión', pero además vuelca cada campo de primer nivel "
        "de la respuesta al contexto del run (pisa cualquier variable con el "
        "mismo nombre). Para cuando un nodo más adelante necesita leer un "
        "campo de la fila recién traída directo por su nombre."
    ),
    params=(Param("connection", required=True, doc="Nombre de la Action guardada.",
                  **_si_el_nucleo_sabe(Param, options_from="actions")),),
    extra_params=True,
    extra_params_doc="Cualquier otro param pisa al {variable} de mismo nombre en la Action, antes que el contexto del run.",
    outputs=(
        Output("response", ParamType.JSON),
        Output("status", ParamType.INT),
        Output("result", ParamType.JSON),
    ),
    extra_outputs=True,
    extra_outputs_doc="Cada campo de primer nivel de la respuesta, si es un objeto JSON.",
)


def _llamar_y_fusionar(ctx: ToolContext) -> ToolResult:
    nombre = ctx.params["connection"]
    ejecutada = _ejecutar_guardada(ctx, nombre)
    if isinstance(ejecutada, ToolResult):
        return ejecutada
    _guardada, respuesta, cuerpo_resp, resultado = ejecutada

    if not respuesta.ok:
        return ToolResult.err(
            f"la conexión '{nombre}' respondió {respuesta.status}",
            status=respuesta.status, response=cuerpo_resp,
        )
    extra = cuerpo_resp if isinstance(cuerpo_resp, dict) else {}
    return ToolResult.ok(response=cuerpo_resp, status=respuesta.status, result=resultado, **extra)


# ── Actions: probar, sin wizard ────────────────────────────────────────────

PREVIEW = Action(
    "preview",
    "Probar",
    doc="Trae filas sin guardar nada — arma y probá un source antes de guardarlo.",
    # Atada a la colección: un agente por MCP la pide con `item=<fuente>` y los
    # params salen del item guardado, en vez de reconstruirlos a mano.
    resource="sources",
    params=(
        Param("url", required=True),
        Param("method", default="GET"),
        Param("headers", ParamType.JSON, default={}),
        Param("payload", ParamType.JSON, default={}),
        Param("results_path"),
        Param("page_param"),
        Param("page_size_param"),
        Param("page_size", ParamType.INT, default=100),
        Param("total_path"),
        Param("limit", ParamType.INT, default=25),
        Param("offset", ParamType.INT, default=0),
        Param("search", doc="Filtra por substring en cualquier valor de la fila."),
        Param(
            "filters", ParamType.JSON, default={},
            doc="Filtra por igualdad exacta, columna por columna: {'campo': 'valor'}.",
        ),
    ),
)


def _preview(ctx: ToolContext) -> ToolResult:
    resultado = _fetch_page(
        ctx.port(port_names.HTTP), ctx.params,
        offset=ctx.params.get("offset") or 0,
        limit=ctx.params.get("limit") or 25,
        timeout=float(ctx.config("connectionsTimeout") or 30.0),
        search=ctx.params.get("search") or None,
        filters=ctx.params.get("filters") or None,
    )
    if resultado["error"]:
        return ToolResult.err(resultado["error"])
    return ToolResult.ok(rows=resultado["rows"], total=resultado["total"], facets=resultado["facets"])


TEST = Action(
    "test",
    "Probar conexión",
    doc="Ejecuta la conexión guardada tal cual y muestra la respuesta.",
    params=(Param("connection", required=True),),
    resource="actions",
)


def _test(ctx: ToolContext) -> ToolResult:
    nombre = ctx.params["connection"]
    guardada = ctx.resource("actions", nombre)
    if guardada is None:
        return ToolResult.err(f"no existe la conexión '{nombre}'")

    try:
        respuesta = _pedir(
            ctx.port(port_names.HTTP), guardada,
            timeout=float(ctx.config("connectionsTimeout") or 30.0),
        )
    except _SinVariable as exc:
        return ToolResult.err(str(exc))
    cuerpo_resp = respuesta.json(default=respuesta.text)
    if not respuesta.ok:
        return ToolResult.err(f"respondió {respuesta.status}", status=respuesta.status, response=cuerpo_resp)
    return ToolResult.ok(f"respondió {respuesta.status}", status=respuesta.status, response=cuerpo_resp)


PROBAR_LLAMADA = Action(
    "probar_llamada",
    "Probar",
    doc=(
        "Ejecuta la llamada tal cual está en el formulario, sin guardar nada. "
        "Si la URL, los headers o el payload traen {variables} —como las "
        "resuelve `connections.llamar` en un run real—, se sustituyen contra "
        "'Variables para probar'; lo que no se declara ahí queda literal."
    ),
    params=(
        Param("url", required=True),
        Param("method", default="GET"),
        Param("headers", ParamType.JSON, default={}),
        Param("payload", ParamType.JSON, default={}),
        Param("results_path"),
        Param(
            "vars", ParamType.JSON, default={},
            doc="Valores para reemplazar los {llaves} de la URL, headers o payload al probar. No se guardan.",
        ),
    ),
)


def _probar_llamada(ctx: ToolContext) -> ToolResult:
    variables = ctx.params.get("vars") or {}
    resuelta = _resolver(
        {"url": ctx.params["url"], "headers": ctx.params.get("headers") or {},
         "payload": ctx.params.get("payload") or {}},
        variables.get,
    )
    try:
        respuesta = _pedir(
            ctx.port(port_names.HTTP), {**resuelta, "method": ctx.params.get("method")},
            timeout=float(ctx.config("connectionsTimeout") or 30.0),
        )
    except _SinVariable as exc:
        return ToolResult.err(str(exc))
    cuerpo_resp = respuesta.json(default=respuesta.text)
    resultado = _dig(cuerpo_resp, ctx.params.get("results_path") or "")

    if not respuesta.ok:
        return ToolResult.err(f"respondió {respuesta.status}", status=respuesta.status, response=cuerpo_resp)
    return ToolResult.ok(
        f"respondió {respuesta.status}", status=respuesta.status, response=cuerpo_resp, result=resultado,
    )


# ── Qué variables pide la Action elegida ────────────────────────────────


def _variables_en(valor) -> list[str]:
    """Los nombres de `{var}` adentro de un valor, en orden y sin repetir."""
    fuera: list[str] = []

    def _ver(v):
        if isinstance(v, str):
            for m in _PATRON_VAR.finditer(v):
                if m.group(1) not in fuera:
                    fuera.append(m.group(1))
        elif isinstance(v, dict):
            for x in v.values():
                _ver(x)
        elif isinstance(v, list):
            for x in v:
                _ver(x)

    _ver(valor)
    return fuera


def _describir_extras(node_params: dict, leer_item) -> tuple[Param, ...]:
    """
    Los params extra que acepta un nodo, según la Action que tenga elegida.

    Es el `Tool.describe_extra_params` del contrato (core#27): los dos tools de
    acá aceptan params extra que pisan las `{variables}` de la Action (ver
    `extra_params_doc`), pero quien edita el flujo no tenía cómo saber
    *cuáles* — había que abrir Connections, leer la URL y el payload y
    escribir los nombres a mano en el `.mmd`. Ahora la tarjeta los ofrece,
    diciendo en qué campo aparece cada uno.

    Se recorren los mismos tres campos que recorre `_resolver` al ejecutar, así
    lo que se ofrece es exactamente lo que se va a sustituir: un campo con un
    literal —`"texto": ""`— no tiene variable y no aparece, porque tampoco se
    podría pisar desde el nodo.

    Corre mientras alguien edita, no en un run: `leer_item` lo liga el núcleo,
    es de sólo lectura y no ve los campos `secret`.
    """
    nombre = (node_params.get("connection") or "").strip()
    if not nombre:
        return ()
    guardada = leer_item("actions", nombre)
    if not guardada:
        return ()

    donde: dict[str, list[str]] = {}
    for campo in ("url", "headers", "payload"):
        for var in _variables_en(guardada.get(campo)):
            donde.setdefault(var, []).append(campo)

    return tuple(
        Param(
            var,
            doc=f"Pisa a {{{var}}} en {' y '.join(campos)} de «{nombre}». "
                "Vacío, sale del contexto del run.",
        )
        for var, campos in donde.items()
    )


# ── Manifest y armado ───────────────────────────────────────────────────

MANIFEST = PluginManifest(
    name="connections",
    label="Connections",
    version="0.1.1",
    doc=(
        "Sources (grilla + ejecución por fila) y Actions (llamada HTTP guardada "
        "para usar dentro de un flujo)."
    ),
    ports=(port_names.HTTP,),
    settings=(
        Setting("connectionsTimeout", ParamType.FLOAT, label="Timeout (s)", default=30.0),
    ),
    resources=(SOURCES, ACTIONS),
    actions=(PREVIEW, TEST, PROBAR_LLAMADA),
)


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[
            FunctionTool(manifest=LLAMAR, fn=_llamar,
                         **_si_el_nucleo_sabe(FunctionTool, describe_extra_params=_describir_extras)),
            FunctionTool(manifest=FUSIONAR, fn=_llamar_y_fusionar,
                         **_si_el_nucleo_sabe(FunctionTool, describe_extra_params=_describir_extras)),
        ],
        actions=[
            FunctionAction(action=PREVIEW, fn=_preview),
            FunctionAction(action=TEST, fn=_test),
            FunctionAction(action=PROBAR_LLAMADA, fn=_probar_llamada),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin", "SOURCES", "ACTIONS"]
