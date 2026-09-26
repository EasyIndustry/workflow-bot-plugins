"""
Plugin `ventanas` — automatizar una ventana nativa de Windows desde un
flujo: encontrarla, clickear un control, escribirle texto, leerlo.

Por qué es un plugin aparte y no un tool de cada app: no tiene nada
específico de una app en particular. El caso típico es una app de escritorio
que sólo se maneja con la GUI (tildar una opción según lo que diga el flujo
antes de procesar un caso, algo que un modo "carpeta vigilada" no puede
parametrizar por caso), y la forma -encontrar ventana, clickear por título,
tipear, leer- es la misma para cualquier app sin versión por línea de
comandos.

Los seis tools son una envoltura fina sobre `WindowPort`
(`backend/core/ports.py`), nada más: el plugin no sabe qué hay detrás del
adapter (en Windows, pywinauto vía UI Automation - `adapters/window_pywinauto.py`).
`ventana` viaja entre nodos como el JSON que devuelve `encontrar` ({handle,
titulo, proceso}), no como el objeto interno del port: es lo único que un
flujo puede guardar en una variable y pasar de un nodo a otro.

**Qué NO hace, a propósito, porque el port no lo tiene hoy:**

- **Click derecho.** `WindowPort.click` sólo hace el click primario
  (`invoke()` de UI Automation, con fallback a `click_input()` simulando el
  mouse). No hay forma de pedir el secundario. Agregar un
  tool `click_derecho` que no puede clickear con el botón derecho sería
  peor que no tenerlo -pasaría por soportado sin estarlo-, así que no está.
  Para tenerlo hace falta ampliar `WindowPort.click` con un parámetro de
  botón (o un método nuevo) en `workflow-bot-core`.
- **Leer si un checkbox está tildado.** `click` invocado sobre un
  `CheckBox:` lo tilda/destilda (toggle), pero `read_text` devuelve el
  nombre accesible del control, no su estado de "Toggle" de UI Automation
  -son cosas distintas en el patrón de accesibilidad de Windows-. Por eso
  `marcar_checkbox` de este plugin es sinónimo de `click`, no un "poner en
  True/False": sin poder leer el estado actual, no hay forma de saber si
  hace falta clickear o no para dejarlo como se pide. Un flujo que necesite
  un estado exacto tiene que asumir el estado inicial (ej. "siempre arranca
  destildado") o el port necesita un método de lectura de estado nuevo.
- **Abrir la app.** `ProcessPort.run` espera a que el proceso termine antes
  de devolver el control (`backend/core/ports.py`), así que no sirve para
  lanzar una GUI que tiene que quedar abierta mientras el flujo sigue.
  La ventana ya tiene que estar
  abierta -tarea del operador, o de un paso previo del flujo con otro
  mecanismo- antes de usar cualquier tool de acá.

`seleccionar_en_lista` es la única excepción a "envoltura fina": es
`click(dropdown)` + `click(opcion)` en un solo nodo, porque expandir un
combo y elegir un ítem son siempre dos clicks seguidos y no vale la pena
armar dos nodos por flujo para eso.
"""

from __future__ import annotations

from backend.core import ports as port_names
from backend.core.contract import (
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.ports import WindowInfo

MANIFEST = PluginManifest(
    name="ventanas",
    label="Ventanas",
    version="0.1.0",
    doc="Encontrar una ventana nativa de Windows, clickear, tipear y leer sus controles — genérico, para cualquier app de escritorio sin línea de comandos.",
    ports=(port_names.WINDOW,),
)

_PARAM_VENTANA = Param(
    "ventana", ParamType.JSON, required=True,
    doc="El JSON que devolvió 'encontrar ventana': {handle, titulo, proceso}. No armar este valor a mano — el handle es un token opaco del adapter.",
)
_PARAM_CONTROL_DOC = (
    "El control dentro de la ventana, por su título ('Guardar'), opcionalmente "
    "con el tipo delante separado por ':' ('Button:Guardar', 'CheckBox:Modo rápido', "
    "'Edit:Carpeta:'). El tipo hace falta cuando el mismo título nombra dos "
    "controles a la vez -típico en diálogos estándar de Windows, ej. el rótulo "
    "'Carpeta:' y el campo que rotula-, y sin él no hay forma de decir a cuál "
    "de los dos referirse."
)


def _ventana_de(ctx: ToolContext) -> WindowInfo | ToolResult:
    bruto = ctx.params["ventana"]
    if not isinstance(bruto, dict) or not bruto.get("handle"):
        return ToolResult.err(
            "'ventana' tiene que ser el JSON que devolvió 'encontrar ventana' "
            f"({{handle, titulo, proceso}}), no {bruto!r}"
        )
    return WindowInfo(handle=str(bruto["handle"]), title=bruto.get("titulo", ""), process=bruto.get("proceso", ""))


# ── encontrar ────────────────────────────────────────────────────────────

ENCONTRAR = ToolManifest(
    id="ventanas.encontrar",
    label="encontrar ventana",
    category="VENTANAS",
    doc=(
        "Busca una ventana ya abierta por (parte de) 'titulo' o por 'proceso' "
        "(ruta del ejecutable, más preciso si hay varias ventanas con el mismo "
        "texto en el título). No abre la app: tiene que estar corriendo de "
        "antes. Devuelve 'ventana', el JSON que el resto de los tools de este "
        "plugin esperan recibir tal cual."
    ),
    params=(
        Param("titulo", default="", doc="Parte del título de la ventana. Ignorado si se da 'proceso'."),
        Param("proceso", default="", doc="Ruta del ejecutable, ej. 'D:\\MiApp\\MiApp.exe'."),
        Param("timeout", ParamType.FLOAT, default=15.0, doc="Segundos a esperar a que la ventana aparezca."),
    ),
    outputs=(Output("ventana", ParamType.JSON, doc="{handle, titulo, proceso} — pasarlo tal cual a los demás tools."),),
)


def _encontrar(ctx: ToolContext) -> ToolResult:
    window = ctx.port(port_names.WINDOW)
    titulo, proceso = ctx.params["titulo"].strip(), ctx.params["proceso"].strip()
    if not titulo and not proceso:
        return ToolResult.err("hace falta 'titulo' o 'proceso' para buscar la ventana")

    info = window.find_window(title=titulo or None, process=proceso or None, timeout=ctx.params["timeout"])
    ctx.log(f"ventana encontrada: '{info.title}' (handle {info.handle})")
    return ToolResult.ok(ventana={"handle": info.handle, "titulo": info.title, "proceso": info.process})


# ── click ────────────────────────────────────────────────────────────────

CLICK = ToolManifest(
    id="ventanas.click",
    label="click",
    category="VENTANAS",
    doc=(
        "Clickea 'control' dentro de 'ventana' (la que devolvió 'encontrar "
        "ventana'). Sirve para un botón, para tildar/destildar un checkbox "
        "(cada click invierte el estado: no hay forma de leerlo antes, ver "
        "el docstring del módulo) y para abrir un combo/dropdown -después "
        "hace falta otro click sobre el ítem, o usar 'seleccionar en lista'. "
        "No hay click derecho: el port no lo soporta hoy."
    ),
    params=(_PARAM_VENTANA, Param("control", required=True, doc=_PARAM_CONTROL_DOC), Param("timeout", ParamType.FLOAT, default=15.0)),
)


def _click(ctx: ToolContext) -> ToolResult:
    ventana = _ventana_de(ctx)
    if isinstance(ventana, ToolResult):
        return ventana
    window = ctx.port(port_names.WINDOW)
    control = ctx.params["control"]
    window.click(ventana, control, timeout=ctx.params["timeout"])
    ctx.log(f"'{ventana.title}': click en '{control}'")
    return ToolResult.ok(f"click en '{control}'")


# ── marcar_checkbox (alias semántico de click) ────────────────────────────

MARCAR_CHECKBOX = ToolManifest(
    id="ventanas.marcar_checkbox",
    label="marcar/desmarcar checkbox",
    category="VENTANAS",
    doc=(
        "Alias de 'click' para dejar más claro en el flujo que 'control' es "
        "un checkbox. Es un TOGGLE, no un 'poner en True/False': el port no "
        "puede leer si ya está tildado (ver el docstring del módulo), así "
        "que un flujo que necesite un estado exacto tiene que partir de un "
        "estado inicial conocido."
    ),
    params=(_PARAM_VENTANA, Param("control", required=True, doc=_PARAM_CONTROL_DOC + " Normalmente con el tipo 'CheckBox:' delante."), Param("timeout", ParamType.FLOAT, default=15.0)),
)


def _marcar_checkbox(ctx: ToolContext) -> ToolResult:
    return _click(ctx)


# ── escribir_texto ──────────────────────────────────────────────────────

ESCRIBIR_TEXTO = ToolManifest(
    id="ventanas.escribir_texto",
    label="escribir texto",
    category="VENTANAS",
    doc="Escribe 'texto' en 'control' dentro de 'ventana' (reemplaza el contenido, no lo agrega al final).",
    params=(
        _PARAM_VENTANA, Param("control", required=True, doc=_PARAM_CONTROL_DOC),
        Param("texto", required=True), Param("timeout", ParamType.FLOAT, default=15.0),
    ),
)


def _escribir_texto(ctx: ToolContext) -> ToolResult:
    ventana = _ventana_de(ctx)
    if isinstance(ventana, ToolResult):
        return ventana
    window = ctx.port(port_names.WINDOW)
    control, texto = ctx.params["control"], ctx.params["texto"]
    window.type_text(ventana, control, texto, timeout=ctx.params["timeout"])
    ctx.log(f"'{ventana.title}': '{control}' <- {texto!r}")
    return ToolResult.ok(f"escrito en '{control}'")


# ── leer_texto ──────────────────────────────────────────────────────────

LEER_TEXTO = ToolManifest(
    id="ventanas.leer_texto",
    label="leer texto",
    category="VENTANAS",
    doc=(
        "El texto de 'control' dentro de 'ventana', o -si 'control' queda "
        "vacío- el título de la ventana entera (no su contenido: para leer "
        "lo que muestra un documento/editor hace falta pedir ESE control, "
        "ej. 'Edit:' o 'Document:')."
    ),
    params=(_PARAM_VENTANA, Param("control", default="", doc=_PARAM_CONTROL_DOC + " Vacío: el título de la ventana."), Param("timeout", ParamType.FLOAT, default=15.0)),
    outputs=(Output("texto", ParamType.STR),),
)


def _leer_texto(ctx: ToolContext) -> ToolResult:
    ventana = _ventana_de(ctx)
    if isinstance(ventana, ToolResult):
        return ventana
    window = ctx.port(port_names.WINDOW)
    control = ctx.params["control"].strip() or None
    texto = window.read_text(ventana, control, timeout=ctx.params["timeout"])
    return ToolResult.ok(texto=texto)


# ── seleccionar_en_lista ──────────────────────────────────────────────────

SELECCIONAR_EN_LISTA = ToolManifest(
    id="ventanas.seleccionar_en_lista",
    label="seleccionar en lista/dropdown",
    category="VENTANAS",
    doc=(
        "Atajo para 'elegir una opción de un combo': clickea 'dropdown' "
        "para abrirlo y después clickea 'opcion' (el ítem, por su título, "
        "ya expandido). Equivalente a dos 'click' seguidos -existe como un "
        "solo nodo porque siempre van juntos."
    ),
    params=(
        _PARAM_VENTANA,
        Param("dropdown", required=True, doc="El control del combo/dropdown, antes de abrirlo. " + _PARAM_CONTROL_DOC),
        Param("opcion", required=True, doc="El ítem a elegir, por su título, una vez que el combo está abierto."),
        Param("timeout", ParamType.FLOAT, default=15.0),
    ),
)


def _seleccionar_en_lista(ctx: ToolContext) -> ToolResult:
    ventana = _ventana_de(ctx)
    if isinstance(ventana, ToolResult):
        return ventana
    window = ctx.port(port_names.WINDOW)
    dropdown, opcion, timeout = ctx.params["dropdown"], ctx.params["opcion"], ctx.params["timeout"]
    window.click(ventana, dropdown, timeout=timeout)
    window.click(ventana, opcion, timeout=timeout)
    ctx.log(f"'{ventana.title}': '{dropdown}' -> '{opcion}'")
    return ToolResult.ok(f"'{opcion}' elegido en '{dropdown}'")


_TOOLS = (
    (ENCONTRAR, _encontrar),
    (CLICK, _click),
    (MARCAR_CHECKBOX, _marcar_checkbox),
    (ESCRIBIR_TEXTO, _escribir_texto),
    (LEER_TEXTO, _leer_texto),
    (SELECCIONAR_EN_LISTA, _seleccionar_en_lista),
)


def build_plugin() -> Plugin:
    return Plugin(manifest=MANIFEST, tools=[FunctionTool(manifest=m, fn=f) for m, f in _TOOLS])


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
