"""
Plugin `ventanas` — automatizar una ventana nativa de escritorio (Windows o
Linux) desde un flujo: encontrarla, clickear un control, escribirle texto, leerlo.

Por qué es un plugin aparte y no un tool de cada app: no tiene nada
específico de una app en particular. El caso típico es una app de escritorio
que sólo se maneja con la GUI (tildar una opción según lo que diga el flujo
antes de procesar un caso, algo que un modo "carpeta vigilada" no puede
parametrizar por caso), y la forma -encontrar ventana, clickear por título,
tipear, leer- es la misma para cualquier app sin versión por línea de
comandos.

Los seis tools son una envoltura fina sobre `WindowPort`
(`backend/core/ports.py`), nada más: el plugin no sabe qué hay detrás del
adapter, que el núcleo elige según el sistema operativo: en Windows, pywinauto vía
UI Automation (`adapters/window_pywinauto.py`); en Linux, el árbol de accesibilidad
AT-SPI (`adapters/window_atspi.py`), que necesita el paquete del sistema
`python3-pyatspi` visible desde el Python del Bot y una app que exponga su árbol
(GTK y Qt sí; Wine en general no). En Linux escribir reemplaza el contenido del
campo y el click dispara la acción por defecto del control, sin mover el mouse.
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
- **Abrir la app.** `ProcessPort.run` espera a que el proceso termine antes
  de devolver el control (`backend/core/ports.py`), así que no sirve para
  lanzar una GUI que tiene que quedar abierta mientras el flujo sigue.
  La ventana ya tiene que estar
  abierta -tarea del operador, o de un paso previo del flujo con otro
  mecanismo- antes de usar cualquier tool de acá.

`marcar_checkbox` con `estado=tildado|destildado` deja el checkbox como se
pide: lee el estado con `WindowPort.read_state` (núcleo v0.3.1-beta.6,
core#25), clickea sólo si hace falta y vuelve a leer para confirmar. El estado
no está en `read_text` —ése devuelve la etiqueta, tildado o no—, por eso hace
falta el método aparte. Sin `estado` sigue siendo un click que invierte, como
siempre, y así anda también en un núcleo sin `read_state`.

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
    version="0.2.0",
    doc="Encontrar una ventana de escritorio (Windows o Linux), clickear, tipear y leer sus controles — "
    "genérico, para cualquier app sin línea de comandos. En Linux necesita python3-pyatspi y una app "
    "que exponga su árbol de accesibilidad (GTK/Qt).",
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
        "ventana'). Sirve para un botón, para un checkbox (cada click invierte "
        "el estado; para dejarlo tildado o destildado seguro, 'marcar checkbox' "
        "con 'estado') y para abrir un combo/dropdown -después "
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


# ── marcar_checkbox ───────────────────────────────────────────────────────

_ALTERNAR, _TILDADO, _DESTILDADO = "alternar", "tildado", "destildado"
_QUIERO = {_TILDADO: "on", _DESTILDADO: "off"}

MARCAR_CHECKBOX = ToolManifest(
    id="ventanas.marcar_checkbox",
    label="marcar/desmarcar checkbox",
    category="VENTANAS",
    doc=(
        "Deja un checkbox como se pide. Con estado=tildado o destildado lee cómo está, "
        "clickea sólo si hace falta y confirma que quedó así (err si no). Con 'alternar' "
        "(el de siempre) es un click que invierte, sin leer nada."
    ),
    params=(
        _PARAM_VENTANA,
        Param("control", required=True, doc=_PARAM_CONTROL_DOC + " Normalmente con el tipo 'CheckBox:' delante."),
        Param(
            "estado", ParamType.ENUM, default=_ALTERNAR, choices=(_ALTERNAR, _TILDADO, _DESTILDADO),
            doc="'tildado' o 'destildado' para dejarlo así, lo esté o no; 'alternar' invierte. "
            "Tildado/destildado necesita núcleo v0.3.1-beta.6.",
        ),
        Param("timeout", ParamType.FLOAT, default=15.0),
    ),
    outputs=(
        Output("estado", ParamType.STR, doc="Cómo quedó: 'on' u 'off'. Vacío con 'alternar', que no lo lee."),
        Output("cambio", ParamType.BOOL, doc="Si hizo falta clickear."),
    ),
)


def _marcar_checkbox(ctx: ToolContext) -> ToolResult:
    estado = ctx.params.get("estado") or _ALTERNAR
    if estado == _ALTERNAR:
        resultado = _click(ctx)
        resultado.outputs.update(estado="", cambio=not resultado.failed)
        return resultado

    ventana = _ventana_de(ctx)
    if isinstance(ventana, ToolResult):
        return ventana
    window = ctx.port(port_names.WINDOW)
    control, timeout, quiero = ctx.params["control"], ctx.params["timeout"], _QUIERO[estado]
    leer = getattr(window, "read_state", None)
    if not callable(leer):
        return ToolResult.err(
            f"este núcleo no puede leer el estado de un checkbox (read_state llegó en v0.3.1-beta.6); "
            f"actualizarlo, o usar estado=alternar partiendo de un estado conocido",
            estado="", cambio=False,
        )

    actual = leer(ventana, control, timeout=timeout)
    if actual is None:
        return ToolResult.err(f"'{control}' no tiene estado de tildado: ¿es un checkbox?", estado="", cambio=False)
    # Un indeterminado ("a medias") puede pedir dos clicks según la app: se
    # clickea hasta llegar, con un tope para no quedar dando vueltas.
    clicks = 0
    while actual != quiero and clicks < 3:
        window.click(ventana, control, timeout=timeout)
        clicks += 1
        actual = leer(ventana, control, timeout=timeout)
    if actual != quiero:
        return ToolResult.err(
            f"'{control}' quedó '{actual}' después de {clicks} click(s); se pidió {estado}",
            estado=actual or "", cambio=clicks > 0,
        )
    ctx.log(f"'{ventana.title}': '{control}' {estado}" + (f" ({clicks} click)" if clicks else " (ya estaba)"))
    return ToolResult.ok(estado=actual, cambio=clicks > 0)


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
