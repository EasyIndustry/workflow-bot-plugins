"""
Plugin `archivos` — mover, copiar, eliminar y renombrar contra el port `fs`.

Vive fuera de `webapp/` y de `backend/` a propósito: es la prueba de que un
plugin de verdad externo (el que instalaría un tercero por entry point, no el
atajo `local_plugins` que usa `connections`) funciona sin tocar nada del
núcleo — se carga por ruta con `plugins: {"archivos": "plugins/archivos"}` en
cualquiera de las tools de este MCP (list_tools, check_flow, dry_run_flow,
run_action, run_flow).

`copiar` y `eliminar` no reciben un tipo: miran con `fs.is_dir(...)` si el
origen es una carpeta o un archivo y eligen `*_file`/`*_tree` solas — pedirle
al usuario del flujo que sepa de antemano qué es cada ruta sería repetir lo
que el propio filesystem ya sabe.
"""

from __future__ import annotations

import re

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
from backend.core.ports import PortError

MANIFEST = PluginManifest(
    name="archivos",
    label="Archivos",
    version="0.1.0",
    doc="Mover, copiar, eliminar y renombrar archivos o carpetas, contra el port fs.",
    ports=(port_names.FS,),
)

# ── mover ───────────────────────────────────────────────────────────────

MOVER = ToolManifest(
    id="archivos.mover",
    label="mover",
    category="ARCHIVOS",
    doc="Mueve un archivo o una carpeta entera a otra ruta.",
    params=(
        Param("origen", ParamType.PATH, required=True),
        Param("destino", ParamType.PATH, required=True, doc="Ruta final, no la carpeta contenedora."),
    ),
    outputs=(Output("ruta", ParamType.PATH),),
)


def _mover(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    origen = ctx.params["origen"]
    if not fs.exists(origen):
        return ToolResult.err(f"no existe: {origen}")
    final = fs.move(origen, ctx.params["destino"])
    ctx.log(f"{origen} -> {final}")
    return ToolResult.ok(ruta=final)


# ── copiar ──────────────────────────────────────────────────────────────

COPIAR = ToolManifest(
    id="archivos.copiar",
    label="copiar",
    category="ARCHIVOS",
    doc="Copia un archivo o una carpeta entera. El origen queda intacto.",
    params=(
        Param("origen", ParamType.PATH, required=True),
        Param("destino", ParamType.PATH, required=True, doc="Ruta final, no la carpeta contenedora."),
    ),
    outputs=(Output("ruta", ParamType.PATH),),
)


def _copiar(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    origen = ctx.params["origen"]
    if not fs.exists(origen):
        return ToolResult.err(f"no existe: {origen}")
    destino = ctx.params["destino"]
    final = fs.copy_tree(origen, destino) if fs.is_dir(origen) else fs.copy_file(origen, destino)
    ctx.log(f"{origen} -> {final}")
    return ToolResult.ok(ruta=final)


# ── eliminar ────────────────────────────────────────────────────────────

ELIMINAR = ToolManifest(
    id="archivos.eliminar",
    label="eliminar",
    category="ARCHIVOS",
    doc="Borra un archivo o una carpeta entera, con todo lo que tenga adentro.",
    # A diferencia de mover/copiar/renombrar, esto no se deshace: lo que se
    # borra no queda en ningún lado. Un actor de tipo `agent` no puede
    # correrlo sin que quien opera la instalación se lo habilite.
    dangerous=True,
    params=(Param("ruta", ParamType.PATH, required=True),),
)


def _eliminar(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    ruta = ctx.params["ruta"]
    if not fs.exists(ruta):
        return ToolResult.err(f"no existe: {ruta}")
    if fs.is_dir(ruta):
        fs.remove_tree(ruta)
    else:
        fs.remove_file(ruta)
    ctx.log(f"eliminado {ruta}")
    return ToolResult.ok(f"eliminado {ruta}")


# ── renombrar ───────────────────────────────────────────────────────────

RENOMBRAR = ToolManifest(
    id="archivos.renombrar",
    label="renombrar",
    category="ARCHIVOS",
    doc="Cambia el nombre de un archivo o carpeta, sin sacarlo de su carpeta.",
    params=(
        Param("ruta", ParamType.PATH, required=True),
        Param("nombre_nuevo", required=True, doc="Sólo el nombre, no una ruta."),
    ),
    outputs=(Output("ruta", ParamType.PATH),),
)


def _renombrar(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    ruta = ctx.params["ruta"]
    if not fs.exists(ruta):
        return ToolResult.err(f"no existe: {ruta}")
    final = fs.rename(ruta, ctx.params["nombre_nuevo"])
    ctx.log(f"{ruta} -> {final}")
    return ToolResult.ok(ruta=final)


# ── buscar ──────────────────────────────────────────────────────────────

# `buscar` y `comparar_conteo` filtran con el mismo criterio, así que el texto
# que lo explica y el código que lo aplica viven una sola vez: dos copias se
# separan en cuanto una de las dos cambie, y lo que un flujo escribe en
# 'patron' tiene que contar igual en las dos.
_DOC_ETIQUETA = "Substring del nombre, sin distinguir mayúsculas."
_DOC_PATRON = (
    "Expresión regular sobre el nombre del archivo (re.search, sin distinguir mayúsculas). "
    r"Ej. ^[A-Z]{2}\d{3}-[AB]\d{2}-[A-Z]\.pdf$"
)


def _filtro(etiqueta: str, patron: str):
    """
    El par `(coincide, criterio)` que salen de una etiqueta y un patrón: el
    primero dice si un nombre entra, el segundo lo describe para el log. Con
    los dos hay que cumplir los dos; sin ninguno entra todo.

    Levanta `re.error` si el patrón no compila — cada tool decide qué mensaje
    dar, porque el parámetro no se llama igual en todas.
    """
    etiqueta = etiqueta or ""
    patron = patron or ""
    regex = re.compile(patron, re.IGNORECASE) if patron else None

    def coincide(nombre: str) -> bool:
        if etiqueta and etiqueta.lower() not in nombre.lower():
            return False
        return regex is None or regex.search(nombre) is not None

    criterio = " y ".join(c for c in (
        f"'{etiqueta}'" if etiqueta else "",
        f"/{patron}/" if patron else "",
    ) if c)
    return coincide, criterio or "todos los archivos"


BUSCAR = ToolManifest(
    id="archivos.buscar",
    label="buscar",
    category="ARCHIVOS",
    doc=(
        "Busca, recursivo, los archivos de una carpeta. Por 'etiqueta' (substring "
        "del nombre) o por 'patron' (expresión regular sobre el nombre, re.search); "
        "al menos uno de los dos. Con 'subcarpeta' baja primero a la subcarpeta que "
        "corresponda y busca sólo ahí adentro, en vez de recorrer todo el árbol."
    ),
    params=(
        Param("carpeta", ParamType.PATH, required=True),
        Param(
            "subcarpeta",
            doc="Ej.: {case_id}. Baja primero a la subcarpeta DIRECTA de 'carpeta' cuyo nombre contenga esto "
            "—sin distinguir mayúsculas— y busca sólo ahí adentro. Con más de una, gana la "
            "modificada más recientemente, y el log dice cuál y cuáles dejó afuera. Sin esto se "
            "recorre el árbol entero de 'carpeta': contra una carpeta de casos terminados eso es "
            "visitar cada archivo de cada caso para llegar a uno.",
        ),
        Param("etiqueta", doc=_DOC_ETIQUETA),
        Param("patron", doc=_DOC_PATRON),
    ),
    outputs=(
        Output("rutas", ParamType.JSON, doc="Lista de rutas encontradas."),
        Output("cantidad", ParamType.INT),
        Output("primera", ParamType.PATH, doc="La primera ruta encontrada, para usarla directo como {primera}."),
        Output(
            "carpeta", ParamType.PATH,
            doc="La carpeta que contiene la PRIMERA ruta encontrada, para los tools que piden una "
            "carpeta y no archivos sueltos (ej. un 'exportar' que procesa una carpeta entera). Ojo: la búsqueda es "
            "recursiva, así que si los resultados están repartidos ésta es la de uno solo — "
            "mirá 'carpetas' para saber si hay más de una.",
        ),
        Output(
            "carpetas", ParamType.JSON,
            doc="Las carpetas distintas donde cayeron los resultados, ordenadas. Con una sola entrada, "
            "'carpeta' las representa a todas; con más de una, el resultado está repartido y elegir "
            "una sola deja las otras afuera.",
        ),
    ),
)


_SIN_NADA = {"rutas": [], "cantidad": 0, "primera": "", "carpeta": "", "carpetas": []}


def _hijos(ctx: ToolContext, fs, carpeta: str) -> list:
    """
    Las entradas que cuelgan directo de `carpeta`, sin bajar más.

    `walk(max_depth=1)` es lo que hace falta y lo que menos cuesta: emite las
    carpetas SIN statearlas (core#33, núcleo v0.3.1-beta.12). Un núcleo anterior
    no conoce el argumento, y ahí `list_dir` da lo mismo pero pagando un stat por
    entrada: sobre un share con decenas de miles de casos en el primer nivel, eso es
    más de un minuto contra un par de segundos, y casi todo son stats que se tiran.

    Se degrada, no se rompe: `archivos` tiene que poder instalarse en un Bot que
    todavía no actualizó el núcleo. El TypeError se atrapa en la llamada y no
    alrededor del consumo, para no confundirlo con uno de adentro del walk.
    """
    try:
        entradas = fs.walk(carpeta, max_depth=1)
    except TypeError:
        ctx.log(
            "este núcleo no tiene walk(max_depth=...) (core#33, v0.3.1-beta.12): "
            "se listan los hijos con stat, que sobre una carpeta grande es mucho más lento",
            "warning",
        )
        return list(fs.list_dir(carpeta))
    return list(entradas)


def _bajar_a(ctx: ToolContext, fs, carpeta: str, etiqueta: str):
    """
    La subcarpeta DIRECTA de `carpeta` que contiene `etiqueta` en el nombre; con
    más de una, la modificada más recientemente. O un ToolResult de error.

    Directa y no recursiva a propósito: la suposición real es "la carpeta del
    caso cuelga de acá", que es fácil de verificar, y no "los candidatos salen
    seguidos si ordeno", que depende del nivel en que estén y de que la etiqueta
    sea prefijo.

    Hacen falta los nombres de acá abajo y el mtime de las dos o tres que
    matcheen; ver `_hijos` para lo que cuesta cada forma de conseguirlos.
    """
    baja = etiqueta.lower()
    candidatas = [e for e in _hijos(ctx, fs, carpeta) if e.is_dir and baja in e.name.lower()]
    if not candidatas:
        return ToolResult.err(
            f"ninguna subcarpeta de {carpeta} tiene '{etiqueta}' en el nombre", **_SIN_NADA)

    # `walk` no statea las carpetas —de eso se trata—, así que el mtime se pide
    # aparte, y sólo el de las candidatas. Una que no se pueda statear va al
    # fondo en vez de tumbar la elección: que sobre una es motivo de warning,
    # no de que el flujo se caiga.
    fechas = {}
    for e in candidatas:
        try:
            fechas[e.path] = fs.stat(e.path).modified_at
        except PortError:
            fechas[e.path] = 0.0
    # El nombre desempata, para que dos con la misma fecha no dependan del
    # orden en que las devolvió el filesystem.
    candidatas.sort(key=lambda e: (-fechas[e.path], e.name))
    elegida = candidatas[0]
    if len(candidatas) > 1:
        # Warning y no error: con un id repetido —un caso rehecho, un apellido
        # mal tipeado— hay una respuesta razonable, pero que se elija sola sin
        # que quede escrito sería decidir por el operador en silencio.
        otras = ", ".join(e.name for e in candidatas[1:])
        ctx.log(
            f"{len(candidatas)} subcarpetas con '{etiqueta}': va '{elegida.name}' "
            f"por ser la más nueva; deja afuera {otras}",
            "warning",
        )
    return elegida.path


def _buscar(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    carpeta = ctx.params["carpeta"]
    if not fs.exists(carpeta) or not fs.is_dir(carpeta):
        return ToolResult.err(f"no existe la carpeta: {carpeta}")

    if not ctx.params.get("etiqueta") and not ctx.params.get("patron"):
        return ToolResult.err("hace falta 'etiqueta' o 'patron'")

    if subcarpeta := (ctx.params.get("subcarpeta") or "").strip():
        elegida = _bajar_a(ctx, fs, carpeta, subcarpeta)
        if isinstance(elegida, ToolResult):
            return elegida
        carpeta = elegida

    try:
        coincide, criterio = _filtro(ctx.params.get("etiqueta"), ctx.params.get("patron"))
    except re.error as exc:
        return ToolResult.err(f"'patron' no es una expresión regular válida: {exc}")

    rutas = [e.path for e in fs.walk(carpeta) if not e.is_dir and coincide(e.name)]
    ctx.log(f"{len(rutas)} archivo(s) con {criterio} en {carpeta}")
    if not rutas:
        return ToolResult.err(f"ningún archivo con {criterio} en {carpeta}", **_SIN_NADA)

    # La carpeta de cada resultado, no la que se buscó: `walk` es recursivo, así
    # que los archivos pueden estar en una subcarpeta —o en varias—. Un tool que
    # pide una carpeta (un 'exportar' de carpeta entera) necesita la que los
    # contiene, y encadenar la que se buscó le daría la de más arriba.
    carpetas = sorted({fs.parent(r) for r in rutas})
    if len(carpetas) > 1:
        ctx.log(
            f"están repartidos en {len(carpetas)} carpetas; 'carpeta' es la de la primera "
            f"({fs.parent(rutas[0])}) y deja las otras afuera",
            "warning",
        )
    return ToolResult.ok(
        rutas=rutas, cantidad=len(rutas), primera=rutas[0],
        carpeta=fs.parent(rutas[0]), carpetas=carpetas,
    )


# ── comparar_conteo ───────────────────────────────────────────────────────

def _doc_patron(lado: str) -> str:
    # `_DOC_PATRON` trae el ejemplo con llaves ({2}, {3}), así que se concatena:
    # un .format() sobre ese texto las leería como campos y reventaría.
    return (
        _DOC_PATRON + f" Filtra sólo 'carpeta_{lado}'; vacío la cuenta entera. El patrón va "
        "por lado porque las dos carpetas pueden guardar el mismo entregable rodeado de "
        "cosas distintas —la salida de un generador trae además los intermedios (-borrador, "
        "-firma, -meta) que la carpeta exportada no tiene—, así que el filtro se pone donde "
        "hace falta y no hay que acomodar las carpetas para que la filtrable caiga en un "
        "lado fijo. Ojo: filtrando uno solo, cualquier archivo suelto del otro mueve el "
        "conteo; el mismo patrón de los dos lados es el control más firme."
    )


COMPARAR_CONTEO = ToolManifest(
    id="archivos.comparar_conteo",
    label="comparar conteo",
    category="ARCHIVOS",
    doc=(
        "Cuenta, recursivo, los archivos de dos carpetas y compara: 'ok' si coinciden, "
        "'err' si no. Cada carpeta lleva su propio 'patron'; la que lo deja vacío se "
        "cuenta entera. Sin ningún filtro es carpeta contra carpeta."
    ),
    params=(
        Param("carpeta_a", ParamType.PATH, required=True),
        Param("patron_a", doc=_doc_patron("a")),
        Param("carpeta_b", ParamType.PATH, required=True),
        Param("patron_b", doc=_doc_patron("b")),
        Param(
            "etiqueta",
            doc=_DOC_ETIQUETA + " Va sobre las dos carpetas, no por lado: identifica el "
            "caso, que es el mismo de los dos lados. Lo que cambia de un lado al otro es "
            "la forma del archivo, y de eso se ocupan 'patron_a' y 'patron_b'.",
        ),
    ),
    outputs=(Output("cantidad_a", ParamType.INT), Output("cantidad_b", ParamType.INT)),
)


def _comparar_conteo(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    carpeta_a, carpeta_b = ctx.params["carpeta_a"], ctx.params["carpeta_b"]
    # Una carpeta que no está contaba 0, así que dos rutas mal escritas daban
    # 0 vs 0 → 'ok': el control más tranquilizador era el que no miraba nada. Con
    # un patrón es peor, porque ahí el 0 también es el resultado legítimo de uno
    # que no matchea y deja de distinguirse de la ruta equivocada. Que falle.
    for carpeta in (carpeta_a, carpeta_b):
        if not fs.exists(carpeta) or not fs.is_dir(carpeta):
            return ToolResult.err(f"no existe la carpeta: {carpeta}")

    etiqueta = ctx.params.get("etiqueta")
    filtros = {}
    for lado in ("a", "b"):
        try:
            filtros[lado] = _filtro(etiqueta, ctx.params.get(f"patron_{lado}"))
        except re.error as exc:
            return ToolResult.err(f"'patron_{lado}' no es una expresión regular válida: {exc}")
    (coincide_a, criterio_a), (coincide_b, criterio_b) = filtros["a"], filtros["b"]

    def contar(carpeta: str, coincide) -> int:
        return sum(1 for e in fs.walk(carpeta) if not e.is_dir and coincide(e.name))

    cantidad_a, cantidad_b = contar(carpeta_a, coincide_a), contar(carpeta_b, coincide_b)
    # Los dos criterios en el log, no sólo los números: con un lado filtrado y el
    # otro no, un 3 vs 3 sale de dos preguntas distintas y conviene que se vea.
    ctx.log(
        f"{carpeta_a} ({criterio_a}): {cantidad_a} · "
        f"{carpeta_b} ({criterio_b}): {cantidad_b}"
    )
    if cantidad_a != cantidad_b:
        return ToolResult.err(
            f"no coinciden: {cantidad_a} vs {cantidad_b}", cantidad_a=cantidad_a, cantidad_b=cantidad_b,
        )
    return ToolResult.ok(cantidad_a=cantidad_a, cantidad_b=cantidad_b)


_TOOLS = (
    (MOVER, _mover),
    (COPIAR, _copiar),
    (ELIMINAR, _eliminar),
    (RENOMBRAR, _renombrar),
    (BUSCAR, _buscar),
    (COMPARAR_CONTEO, _comparar_conteo),
)


def build_plugin() -> Plugin:
    return Plugin(manifest=MANIFEST, tools=[FunctionTool(manifest=m, fn=f) for m, f in _TOOLS])


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
