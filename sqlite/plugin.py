"""
Plugin `sqlite` — consultas de sólo lectura, guardadas y reutilizables en un
flujo, contra un archivo SQLite externo (no la base del propio Bot).

Primer plugin de "lectura SQL" del catálogo — el más chico de la serie que
arrancó en workflow-bot-core#39 (un port para leer bases SQL externas sin que
el plugin importe un driver). Postgres/MySQL quedaron resueltos aparte, con
un `SocketPort` de transporte crudo (TCP/TLS) sobre el que un plugin futuro
va a tener que implementar el protocolo de cable de cada motor a mano. SQLite
no necesita nada de eso: es un archivo local, ya se abre con `sqlite3` de
stdlib, y por eso workflow-bot-core#40 le dio su propio port, chico y
desacoplado — `SqliteFilePort`, declarado como `sqlite_file` en el manifest.

**Nunca se interpola el SQL.** Una `Query` guardada tiene su sentencia fija,
con `?` donde va cada parámetro posicional — el mismo mecanismo de
`sqlite3`/`DB-API`. Lo único que puede variar por corrida son los *valores*
de la lista `params`: un valor que sea exactamente `{variable}` se resuelve
contra el contexto del run (o un param extra del nodo), igual que
`connections.llamar`, pero sobre un placeholder posicional ya separado del
texto de la query, nunca sobre la sentencia en sí. Es lo que hace que esto no
pueda terminar en una inyección SQL: no hay forma de que un valor cambie qué
SQL se ejecuta, sólo con qué se lo ejecuta.

**Siempre de sólo lectura.** El port abre el archivo en modo `ro` de la URI
de `sqlite3` — un `INSERT`/`UPDATE` falla al nivel del motor, no por una
convención de este plugin que un SQL mal escrito pudiera esquivar.

**Límite honesto, heredado del port** (`SqliteFilePort`, igual que
`StoragePort` del núcleo): abstrae la conexión, no el dialecto. El SQL que se
guarda es SQL de SQLite — sintaxis, funciones y PRAGMAs incluidos — y es
responsabilidad de quien lo escribe.
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
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.ports import PortError


def _si_el_nucleo_sabe(cls, **campos) -> dict:
    """
    Los kwargs que el contrato vendorizado realmente declara.

    `options_from`, `describe_extra_params` y `dry_run` llegaron en distintas
    versiones del núcleo, y un host puede tener una anterior. Con una vieja
    estos kwargs no existen y `Param(...)`/`ToolManifest(...)` reventarían
    **al importar** — no cargaría el plugin entero. Así, con un núcleo viejo
    lo único que falta es el buscador, los params descubiertos o el dry run.
    """
    declarados = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in campos.items() if k in declarados}


# ── Resource ──────────────────────────────────────────────────────────────

QUERIES = Resource(
    name="queries",
    label="Queries",
    item_label="Query",
    key_field="nombre",
    doc=(
        "Una consulta SQL guardada contra un archivo SQLite externo, para "
        "correr desde un nodo de flujo. Siempre de sólo lectura."
    ),
    fields=(
        Field(
            "path", ParamType.STR, label="Archivo SQLite", required=True,
            doc="Ruta al archivo .sqlite/.db externo. Se abre siempre de sólo lectura: "
            "un INSERT/UPDATE falla al nivel del motor.",
        ),
        Field(
            "sql", ParamType.STR, label="SQL", required=True,
            doc="La sentencia, con '?' donde va cada parámetro posicional. Nunca se "
            "interpola texto acá adentro — eso es lo que evita una inyección.",
        ),
        Field(
            "params", ParamType.JSON, label="Parámetros", default=[],
            doc="Lista de valores para los '?' de la SQL, en orden. Un valor que sea "
            "exactamente '{variable}' se resuelve contra el nodo o el contexto del run "
            "al ejecutar, conservando su tipo (no todo pasa a texto).",
        ),
    ),
)


# ── {variable}: sólo sobre los valores de `params`, nunca sobre el SQL ────

_PATRON_VAR = re.compile(r"\{(\w+)\}")


def _resolver_params(valor, obtener):
    """
    Sustituye `{variable}` dentro de `params` (nunca dentro de `sql`).

    Un valor que **es entero** un placeholder (`"{id}"`, nada más) se
    reemplaza por lo que devuelva `obtener`, conservando su tipo — un id
    numérico sigue llegando como número al bind de `sqlite3`, no como texto.
    Un placeholder **adentro** de un string más largo (`"pref_{id}"`) sí se
    resuelve como texto, porque ahí sólo puede ir texto.

    Recursivo porque `params` es JSON libre: puede traer una lista anidada o
    un dict como valor de un parámetro.
    """
    if isinstance(valor, str):
        entero = _PATRON_VAR.fullmatch(valor)
        if entero:
            val = obtener(entero.group(1))
            return valor if val is None else val
        return _PATRON_VAR.sub(
            lambda m: valor if (v := obtener(m.group(1))) is None else str(v), valor,
        )
    if isinstance(valor, dict):
        return {k: _resolver_params(v, obtener) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_resolver_params(v, obtener) for v in valor]
    return valor


def _variables_en(valor) -> list[str]:
    """Los nombres de `{var}` adentro de `params`, en orden y sin repetir."""
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
    Los params extra que acepta el nodo, según la Query que tenga elegida —
    `Tool.describe_extra_params` del contrato. Sólo mira `params`, nunca
    `sql`: ahí es donde `connection.llamar` busca lo que va a sustituir.

    Corre mientras alguien edita, no en un run: `leer_item` lo liga el
    núcleo, es de sólo lectura.
    """
    nombre = (node_params.get("connection") or "").strip()
    if not nombre:
        return ()
    # `key_field="nombre"` explícito: el lector que liga el núcleo default a
    # "name", y la clave de `queries` es "nombre" (ver `QUERIES.key_field`).
    guardada = leer_item("queries", nombre, key_field="nombre")
    if not guardada:
        return ()

    variables = _variables_en(guardada.get("params"))
    return tuple(
        Param(var, doc=f"Pisa a {{{var}}} en los params de «{nombre}». Vacío, sale del contexto del run.")
        for var in variables
    )


# ── Tool: sqlite.consultar — el único nodo de flujo de este plugin ────────

CONSULTAR = ToolManifest(
    id="sqlite.consultar",
    label="consultar sqlite",
    category="SQLITE",
    doc=(
        "Ejecuta una Query guardada contra un archivo SQLite externo, de sólo "
        "lectura. Sustituye {variables} en sus `params` —nunca en el SQL— contra "
        "cualquier otro param del propio nodo primero, y si no contra el "
        "contexto del run. {filas} trae todas; {primera} la primera, para leerla "
        "con {NODO.primera.columna}; {hay} es 'si' o 'no', para un nodo de decisión."
    ),
    params=(Param("connection", required=True, doc="Nombre de la Query guardada.",
                  **_si_el_nucleo_sabe(Param, options_from="queries")),),
    extra_params=True,
    extra_params_doc="Cualquier otro param pisa al {variable} de mismo nombre en los params de la Query.",
    outputs=(
        Output("filas", ParamType.JSON, doc="Todas las filas, cada una como dict."),
        Output("primera", ParamType.JSON, doc="La primera fila, o vacía."),
        Output("cantidad", ParamType.INT),
        Output("hay", ParamType.STR, doc="'si' o 'no'."),
    ),
    **_si_el_nucleo_sabe(ToolManifest, dry_run="run"),
)


def _ejecutar_guardada(ctx: ToolContext, nombre: str):
    """Comparte buscar la Query y correrla. `ToolResult` de error si algo falla."""
    # `key_field="nombre"`: el default del núcleo es "name"; la clave de
    # `queries` es "nombre" (ver `QUERIES.key_field`).
    guardada = ctx.resource("queries", nombre, key_field="nombre")
    if guardada is None:
        disponibles = ", ".join(ctx.resource_keys("queries", key_field="nombre")) or "ninguna"
        return ToolResult.err(f"no existe la query '{nombre}'. Disponibles: {disponibles}")

    params = _resolver_params(
        guardada.get("params") or [],
        lambda n: ctx.extras.get(n, ctx.var(n)),
    )
    try:
        filas = ctx.port(port_names.SQLITE_FILE).query(guardada["path"], guardada["sql"], params)
    except PortError as exc:
        return ToolResult.err(f"la query '{nombre}': {exc}")
    return filas


def _consultar(ctx: ToolContext) -> ToolResult:
    vacios = {"filas": [], "primera": {}, "cantidad": 0, "hay": "no"}
    nombre = ctx.params["connection"]
    ejecutada = _ejecutar_guardada(ctx, nombre)
    if isinstance(ejecutada, ToolResult):
        return ToolResult.err(ejecutada.message, **vacios)
    filas = ejecutada
    ctx.log(f"{nombre}: {len(filas)} fila(s)")
    return ToolResult.ok(
        filas=filas, primera=filas[0] if filas else {}, cantidad=len(filas),
        hay="si" if filas else "no",
    )


# ── Actions: probar ─────────────────────────────────────────────────────

PROBAR = Action(
    "probar", "Probar",
    doc="Ejecuta la Query guardada tal cual —sin resolver {variables} de sus params— y muestra las filas.",
    resource="queries",
    params=(Param("nombre", required=True, options_from="queries"),),
)


def _probar(ctx: ToolContext) -> ToolResult:
    nombre = ctx.params["nombre"]
    guardada = ctx.resource("queries", nombre, key_field="nombre")
    if guardada is None:
        return ToolResult.err(f"no existe la query '{nombre}'")
    try:
        filas = ctx.port(port_names.SQLITE_FILE).query(guardada["path"], guardada["sql"], guardada.get("params") or [])
    except PortError as exc:
        return ToolResult.err(str(exc))
    return ToolResult.ok(f"{len(filas)} fila(s)", filas=filas, cantidad=len(filas))


PROBAR_CONSULTA = Action(
    "probar_consulta", "Probar",
    doc=(
        "Ejecuta la consulta tal cual está en el formulario, sin guardar nada. Si "
        "`params` trae {variables}, se sustituyen contra 'Variables para probar'; lo "
        "que no se declara ahí queda literal."
    ),
    params=(
        Param("path", required=True),
        Param("sql", required=True),
        Param("params", ParamType.JSON, default=[]),
        Param(
            "vars", ParamType.JSON, default={},
            doc="Valores para reemplazar los {llaves} de 'params' al probar. No se guardan.",
        ),
    ),
)


def _probar_consulta(ctx: ToolContext) -> ToolResult:
    variables = ctx.params.get("vars") or {}
    params = _resolver_params(ctx.params.get("params") or [], variables.get)
    try:
        filas = ctx.port(port_names.SQLITE_FILE).query(ctx.params["path"], ctx.params["sql"], params)
    except PortError as exc:
        return ToolResult.err(str(exc))
    return ToolResult.ok(f"{len(filas)} fila(s)", filas=filas, cantidad=len(filas))


# ── Manifest y armado ───────────────────────────────────────────────────

MANIFEST = PluginManifest(
    name="sqlite",
    label="SQLite",
    version="0.1.0",
    doc=(
        "Queries de sólo lectura, guardadas y reutilizables en un flujo, contra "
        "un archivo SQLite externo."
    ),
    ports=(port_names.SQLITE_FILE,),
    resources=(QUERIES,),
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

__all__ = ["MANIFEST", "PLUGIN", "build_plugin", "QUERIES"]
