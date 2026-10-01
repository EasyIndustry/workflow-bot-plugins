"""
Plugin `mariadb` — consultas de sólo lectura, guardadas y reutilizables en
un flujo, contra un servidor MySQL o MariaDB externo.

Segundo plugin de la serie "connections, pero para SQL" (ver `sqlite`, el
primero). A diferencia de SQLite, este motor habla por red con su propio
protocolo de cable — así que en vez de un port que "hablara SQL" (lo que se
evaluó primero en workflow-bot-core#39 y no cerraba: cada motor tiene su
propio driver nativo), esto corre sobre `SocketPort` (TCP crudo) y el
protocolo de MySQL/MariaDB está implementado a mano en `protocolo.py` —
nunca se importa `pymysql` ni `mysqlclient`. Ver el docstring de ese módulo
para el detalle del protocolo y sus límites.

MariaDB es un fork de MySQL que mantiene compatibilidad con el protocolo
cliente/servidor de MySQL — no es uno propio — por eso un solo plugin cubre
los dos motores sin ramas distintas.

**La diferencia real con `sqlite`, y por qué existe `escapar_valor`:** SQLite
tiene un canal de parámetros real (`?` posicional, bind en el motor) y por
eso `sqlite.consultar` nunca toca el texto del SQL. El protocolo *simple* de
MySQL/MariaDB (`COM_QUERY`, sin prepared statements — la elección deliberada
para este MVP) no tiene ese canal: todo es texto. Por eso un `{variable}`
resuelto acá se **escapa y cita** antes de pegarlo en el SQL guardado —más
débil que un bind real (depende de que el escapado cubra los casos), pero
evita que un valor con una comilla adentro cambie qué SQL se ejecuta.
"""

from __future__ import annotations

import dataclasses
import re

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

from .protocolo import ProtocoloError, cerrar, conectar, consultar, escapar_valor


def _si_el_nucleo_sabe(cls, **campos) -> dict:
    """Los kwargs que el contrato vendorizado realmente declara (ver `sqlite/plugin.py`)."""
    declarados = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in campos.items() if k in declarados}


# ── Resource ──────────────────────────────────────────────────────────────

CONEXIONES = Resource(
    name="conexiones",
    label="Conexiones",
    item_label="Conexión",
    key_field="nombre",
    doc=(
        "Una consulta SQL guardada contra un servidor MySQL/MariaDB externo, "
        "para correr desde un nodo de flujo. Siempre de sólo lectura."
    ),
    fields=(
        Field("host", ParamType.STR, label="Host", required=True),
        Field("port", ParamType.INT, label="Puerto", default=3306),
        Field("usuario", ParamType.STR, label="Usuario", required=True),
        Field(
            "clave", ParamType.STR, label="Contraseña", secret=True,
            doc="Nunca sale en claro de un listado — mismo criterio que el resto del catálogo.",
        ),
        Field("base", ParamType.STR, label="Base de datos", required=True),
        Field(
            "sql", ParamType.STR, label="SQL", required=True,
            doc="La sentencia. Un '{variable}' se resuelve y se cita al ejecutar — ver README: "
            "el protocolo simple no tiene un canal de parámetros aparte del texto.",
        ),
    ),
)


# ── {variable}: resuelto Y escapado, porque el SQL es todo lo que hay ─────

_PATRON_VAR = re.compile(r"\{(\w+)\}")


def _resolver_sql(sql: str, obtener) -> str:
    """
    Sustituye `{variable}` en el SQL, citando cada valor con `escapar_valor`.

    `obtener(nombre) -> valor | None`; `None` deja el placeholder literal —
    mismo criterio que `connections._resolver`: no hay forma de distinguir
    "no está en el contexto" de "vale None", y la primera es la que importa
    (que el placeholder se vea en la traza en vez de desaparecer en silencio).
    """

    def _uno(m: re.Match) -> str:
        val = obtener(m.group(1))
        return m.group(0) if val is None else escapar_valor(val)

    return _PATRON_VAR.sub(_uno, sql)


def _variables_en(sql: str) -> list[str]:
    fuera: list[str] = []
    for m in _PATRON_VAR.finditer(sql or ""):
        if m.group(1) not in fuera:
            fuera.append(m.group(1))
    return fuera


def _describir_extras(node_params: dict, leer_item) -> tuple[Param, ...]:
    """Los params extra que acepta el nodo, según la Conexión elegida (ver `sqlite/plugin.py`)."""
    nombre = (node_params.get("connection") or "").strip()
    if not nombre:
        return ()
    guardada = leer_item("conexiones", nombre, key_field="nombre")
    if not guardada:
        return ()
    return tuple(
        Param(var, doc=f"Pisa a {{{var}}} en el SQL de «{nombre}», citado. Vacío, sale del contexto del run.")
        for var in _variables_en(guardada.get("sql") or "")
    )


# ── Conectar y correr: lo que comparten el tool y las dos Actions ────────


def _con_conexion(ctx: ToolContext, guardada: dict, sql: str):
    """Abre, corre `sql`, cierra. `ToolResult` de error si algo falla."""
    try:
        conexion = conectar(
            ctx.port(port_names.SOCKET),
            host=guardada["host"], port=int(guardada.get("port") or 3306),
            usuario=guardada["usuario"], clave=guardada.get("clave") or "",
            base=guardada["base"], timeout=float(ctx.config("mariadbTimeout") or 10.0),
        )
    except (PortError, ProtocoloError) as exc:
        return ToolResult.err(f"no se pudo conectar a {guardada['host']}:{guardada.get('port') or 3306}: {exc}")
    try:
        return consultar(conexion, sql)
    except ProtocoloError as exc:
        return ToolResult.err(str(exc))
    finally:
        cerrar(ctx.port(port_names.SOCKET), conexion)


# ── Tool: mariadb.consultar — el único nodo de flujo de este plugin ──────

CONSULTAR = ToolManifest(
    id="mariadb.consultar",
    label="consultar mariadb",
    category="MARIADB",
    doc=(
        "Ejecuta una Conexión guardada contra MySQL/MariaDB, de sólo lectura. "
        "Sustituye {variables} en su SQL —citadas, nunca crudas— contra cualquier "
        "otro param del propio nodo primero, y si no contra el contexto del run. "
        "{filas} trae todas; {primera} la primera, para leerla con "
        "{NODO.primera.columna}; {hay} es 'si' o 'no', para un nodo de decisión."
    ),
    params=(Param("connection", required=True, doc="Nombre de la Conexión guardada.",
                  **_si_el_nucleo_sabe(Param, options_from="conexiones")),),
    extra_params=True,
    extra_params_doc="Cualquier otro param pisa al {variable} de mismo nombre en el SQL de la Conexión, citado.",
    outputs=(
        Output("filas", ParamType.JSON, doc="Todas las filas, cada una como dict. Valores siempre como texto."),
        Output("primera", ParamType.JSON, doc="La primera fila, o vacía."),
        Output("cantidad", ParamType.INT),
        Output("hay", ParamType.STR, doc="'si' o 'no'."),
    ),
    **_si_el_nucleo_sabe(ToolManifest, dry_run="run"),
)


def _consultar(ctx: ToolContext) -> ToolResult:
    vacios = {"filas": [], "primera": {}, "cantidad": 0, "hay": "no"}
    nombre = ctx.params["connection"]
    guardada = ctx.resource("conexiones", nombre, key_field="nombre")
    if guardada is None:
        disponibles = ", ".join(ctx.resource_keys("conexiones", key_field="nombre")) or "ninguna"
        return ToolResult.err(f"no existe la conexión '{nombre}'. Disponibles: {disponibles}", **vacios)

    sql = _resolver_sql(guardada["sql"], lambda n: ctx.extras.get(n, ctx.var(n)))
    filas = _con_conexion(ctx, guardada, sql)
    if isinstance(filas, ToolResult):
        return ToolResult.err(f"la conexión '{nombre}': {filas.message}", **vacios)
    ctx.log(f"{nombre}: {len(filas)} fila(s)")
    return ToolResult.ok(filas=filas, primera=filas[0] if filas else {}, cantidad=len(filas),
                         hay="si" if filas else "no")


# ── Actions: probar ─────────────────────────────────────────────────────

PROBAR = Action(
    "probar", "Probar",
    doc="Ejecuta la Conexión guardada tal cual —sin resolver {variables} de su SQL— y muestra las filas.",
    resource="conexiones",
    params=(Param("nombre", required=True, options_from="conexiones"),),
)


def _probar(ctx: ToolContext) -> ToolResult:
    nombre = ctx.params["nombre"]
    guardada = ctx.resource("conexiones", nombre, key_field="nombre")
    if guardada is None:
        return ToolResult.err(f"no existe la conexión '{nombre}'")
    filas = _con_conexion(ctx, guardada, guardada["sql"])
    if isinstance(filas, ToolResult):
        return filas
    return ToolResult.ok(f"{len(filas)} fila(s)", filas=filas, cantidad=len(filas))


PROBAR_CONSULTA = Action(
    "probar_consulta", "Probar",
    doc=(
        "Ejecuta la consulta tal cual está en el formulario, sin guardar nada. Si el SQL "
        "trae {variables}, se sustituyen (citadas) contra 'Variables para probar'; lo que "
        "no se declara ahí queda literal."
    ),
    params=(
        Param("host", required=True),
        Param("port", ParamType.INT, default=3306),
        Param("usuario", required=True),
        Param("clave", default=""),
        Param("base", required=True),
        Param("sql", required=True),
        Param(
            "vars", ParamType.JSON, default={},
            doc="Valores para reemplazar los {llaves} del SQL al probar (se citan solos). No se guardan.",
        ),
    ),
)


def _probar_consulta(ctx: ToolContext) -> ToolResult:
    variables = ctx.params.get("vars") or {}
    sql = _resolver_sql(ctx.params["sql"], variables.get)
    guardada = {k: ctx.params[k] for k in ("host", "port", "usuario", "clave", "base")}
    filas = _con_conexion(ctx, guardada, sql)
    if isinstance(filas, ToolResult):
        return filas
    return ToolResult.ok(f"{len(filas)} fila(s)", filas=filas, cantidad=len(filas))


# ── Manifest y armado ───────────────────────────────────────────────────

MANIFEST = PluginManifest(
    name="mariadb",
    label="MariaDB/MySQL",
    version="0.1.0",
    doc=(
        "Consultas de sólo lectura, guardadas y reutilizables en un flujo, contra "
        "un servidor MySQL o MariaDB externo. Protocolo simple, implementado a mano "
        "sobre el port `socket` — ver protocolo.py."
    ),
    ports=(port_names.SOCKET,),
    settings=(
        Setting("mariadbTimeout", ParamType.FLOAT, label="Timeout (s)", default=10.0),
    ),
    resources=(CONEXIONES,),
    actions=(PROBAR, PROBAR_CONSULTA),
)


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[
            FunctionTool(
                manifest=CONSULTAR, fn=_consultar,
                **_si_el_nucleo_sabe(FunctionTool, describe_extra_params=_describir_extras),
            ),
        ],
        actions=[
            FunctionAction(action=PROBAR, fn=_probar),
            FunctionAction(action=PROBAR_CONSULTA, fn=_probar_consulta),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin", "CONEXIONES"]
