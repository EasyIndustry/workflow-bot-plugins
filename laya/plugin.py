"""
Plugin `laya` — preguntarle a Laya (un modelo de decisión local, Apache 2.0)
cosas tipadas sobre un texto o una fila: sí/no, elegir entre opciones o
ubicar en una escala. No genera texto: devuelve la respuesta con su
probabilidad, en una sola pasada y en milisegundos.

Por qué habla por HTTP y no carga el modelo acá: Laya es torch más ~1,5 GB de
pesos, idealmente con GPU. Meter eso en el runtime del Bot como
`requirements.txt` haría pesada cada instalación y, peor, bajaría los pesos
por fuera de los ports. El paquete `laya` ya trae su servidor (`laya-serve`,
`POST /v1/systemone`), así que corre una vez en la máquina que tenga la GPU y
cualquier Bot de la red le pregunta por el port `http`. Ver `README.md`.

Cómo se usa en un flujo: el tool es `ok` si Laya contestó y `err` si no
respondió o la pregunta está mal armada. La respuesta va en un output
(`{respuesta}`, `{eleccion}`, `{nivel}`) para ramificar con un nodo de
decisión (`D{respuesta}` con aristas `|si|`, `|no|`, `|dudoso|`). Mezclar las
dos cosas en `ok`/`err` haría que "Laya dijo que no" y "Laya está apagado"
tomaran la misma arista.

`laya.preguntar` hace varias preguntas en un solo request y deja un JSON con
una entrada por pregunta, que el flujo lee con el camino anidado del núcleo
(`{NODO.respuestas.urgente.respuesta}`, núcleo v0.3.1-beta.13, core#35). Cada
entrada tiene exactamente los outputs del tool suelto del mismo tipo: pasar de
un nodo por pregunta a uno solo no cambia qué se lee.

`dudoso`: con `minimo` > 0, una respuesta menos segura que eso no se toma como
buena y el output dice `dudoso`. Laya a veces se equivoca con mucha
convicción, así que esto no reemplaza un control del flujo, pero sí ataja las
respuestas al azar para mandarlas a una persona.
"""

from __future__ import annotations

import ast
import json
import re

from backend.core import ports as port_names
from backend.core.contract import (
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    Setting,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.ports import PortError

URL = "url"
API_KEY = "api_key"
TIMEOUT = "timeout"
MODELO = "modelo"

# 8010 y no el 8000 que usa `laya-serve` por defecto: el 8000 es el del Bot, y
# los dos suelen correr en la misma máquina.
URL_POR_DEFECTO = "http://127.0.0.1:8010"
# Lo que hay que instalar aparte. Va en el doc del plugin, en la ayuda de la
# dirección y en el error de "no responde": son los lugares que la app muestra
# (el README del plugin no lo ve nadie que no abra el repo).
INSTALAR_SERVIDOR = (
    "Necesita laya-serve corriendo aparte, en esta PC o en otra de la red (mejor con GPU). "
    "Se instala una vez: 'pip install laya fastapi uvicorn' y se arranca con "
    "'LAYA_PORT=8010 LAYA_MODELS=multilingual laya-serve' (en Windows: set LAYA_PORT=8010, "
    "set LAYA_MODELS=multilingual, laya-serve). La primera vez baja ~1,5 GB de modelo."
)
DUDOSO = "dudoso"
# El tope de laya-serve (MAX_QUESTIONS): pasarlo es un 413 que se ve mejor acá.
MAX_PREGUNTAS = 64

MANIFEST = PluginManifest(
    name="laya",
    label="Laya",
    version="0.2.1",
    doc="Decisiones tipadas sobre un texto o una fila (sí/no, elegir, puntuar) con Laya, un modelo local. "
    + INSTALAR_SERVIDOR,
    ports=(port_names.HTTP,),
    settings=(
        Setting(
            URL, ParamType.STR, label="Dirección de laya-serve", default=URL_POR_DEFECTO,
            doc="Donde corre laya-serve: http://<PC>:<LAYA_PORT>. " + INSTALAR_SERVIDOR,
        ),
        Setting(
            API_KEY, ParamType.STR, label="Clave", secret=True,
            doc="La misma que LAYA_API_KEY del servidor. Vacía si el servidor no pide clave.",
        ),
        Setting(
            TIMEOUT, ParamType.INT, label="Segundos a esperar cada respuesta", default=60,
            doc="La primera pregunta puede tardar si el servidor arrancó con LAYA_PRELOAD=0 y "
            "todavía tiene que cargar el modelo.",
        ),
        Setting(
            MODELO, ParamType.STR, label="Modelo",
            choices=("", "multilingual", "english", "typed-decisions"),
            doc="Vacío: el servidor elige según el idioma del texto (castellano → multilingual).",
        ),
    ),
)


# ── Lo que llega del flujo ────────────────────────────────────────────────

_DOC_TEXTO = (
    "Lo que Laya tiene que mirar: un texto, o una {variable} con una fila u objeto. "
    "Acierta más con datos planos y ya dichos en palabras ('stock bajo, bajando') que con "
    "números sueltos o JSON anidado."
)
_DOC_PREGUNTA = "La pregunta, concreta y sobre UNA cosa. Ej.: '¿El cliente pide algo urgente?'"
_DOC_MINIMO = (
    "Seguridad mínima (0 a 1) para dar la respuesta por buena; por debajo sale 'dudoso'. "
    "0: nunca dudoso."
)


def _estructura(crudo) -> object:
    """
    El objeto, si lo que llegó es uno escrito como texto; si no, tal cual.

    Un param STR con una {variable} que tiene un dict llega como `str(dict)`
    (el repr de Python, con comillas simples), que no es JSON. Se recupera el
    objeto para que Laya lo reciba estructurado —el servidor lo serializa
    igual para todas las preguntas— en vez de un texto con pinta de código.
    """
    if not isinstance(crudo, str):
        return crudo
    texto = crudo.strip()
    if texto[:1] in ("{", "["):
        for leer in (json.loads, ast.literal_eval):
            try:
                valor = leer(texto)
            except (ValueError, SyntaxError):
                continue
            if isinstance(valor, (dict, list)):
                return valor
    return crudo


def _lista(crudo, nombre: str) -> list[str] | dict[str, str] | str:
    """
    Opciones o niveles: una lista JSON, un objeto {etiqueta: descripción}, o
    etiquetas separadas por coma — lo que cabe cómodo en un nodo de un .mmd.
    Un str de vuelta es el mensaje de error.
    """
    valor = crudo
    if isinstance(crudo, str):
        texto = crudo.strip()
        if texto[:1] in ("{", "["):
            valor = _estructura(texto)
            if isinstance(valor, str):
                return f"'{nombre}' empieza como JSON pero no es JSON válido"
        else:
            valor = [t.strip() for t in texto.split(",") if t.strip()]
    if isinstance(valor, dict):
        valor = {str(k).strip(): str(v).strip() for k, v in valor.items() if str(k).strip()}
    elif isinstance(valor, list):
        valor = [str(v).strip() for v in valor if str(v).strip()]
    else:
        return f"'{nombre}' tiene que ser una lista, un objeto o etiquetas separadas por coma"
    if len(valor) < 2:
        return f"'{nombre}' necesita al menos dos"
    return valor


def _numero(crudo, nombre: str, defecto: float) -> float | str:
    if crudo is None or crudo == "":
        return defecto
    try:
        valor = float(crudo)
    except (TypeError, ValueError):
        return f"'{nombre}' no es un número: {crudo!r}"
    if not 0 <= valor <= 1:
        return f"'{nombre}' va de 0 a 1, no {valor}"
    return valor


# ── Los tres tipos de pregunta: cómo se arman y cómo se leen ──────────────
#
# Un tipo es (armar, leer). `armar` recibe lo que dio el flujo para UNA
# pregunta y devuelve (la pregunta para laya-serve, cómo leer su respuesta) o
# un mensaje de error; `leer` recibe la respuesta cruda y devuelve los outputs
# o un mensaje de error. Los tools sueltos y `preguntar` pasan por acá, así que
# una misma pregunta deja lo mismo por los dos caminos.

VACIOS = {
    "si_no": {"respuesta": "", "probabilidad": 0.0},
    "elegir": {"eleccion": "", "confianza": 0.0, "probabilidades": {}},
    "puntuar": {"nivel": "", "indice": -1, "valor": 0.0, "confianza": 0.0},
}


def _armar_si_no(pregunta: str, datos: dict, minimo: float):
    umbral = _numero(datos.get("umbral"), "umbral", 0.5)
    if isinstance(umbral, str):
        return umbral

    def leer(contestada: dict) -> dict | str:
        try:
            p = float(contestada["noul"])
        except (KeyError, TypeError, ValueError):
            return "la respuesta de Laya no trae 'noul'"
        es_si = p >= umbral
        seguridad = p if es_si else 1 - p
        respuesta = ("si" if es_si else "no") if seguridad >= minimo else DUDOSO
        return {"respuesta": respuesta, "probabilidad": round(p, 4)}

    return {"type": "noul", "instructions": pregunta}, leer


def _armar_elegir(pregunta: str, datos: dict, minimo: float):
    opciones = _lista(datos.get("opciones"), "opciones")
    if isinstance(opciones, str):
        return opciones

    def leer(contestada: dict) -> dict | str:
        eleccion = contestada.get("choice")
        if eleccion is None:
            return "la respuesta de Laya no trae 'choice'"
        probabilidades = contestada.get("probabilities") or {}
        confianza = float(probabilidades.get(eleccion, contestada.get("answer_confidence", 0.0)))
        return {
            "eleccion": str(eleccion) if confianza >= minimo else DUDOSO,
            "confianza": round(confianza, 4),
            "probabilidades": probabilidades,
        }

    return {"type": "choice", "instructions": pregunta, "criteria": opciones}, leer


def _armar_puntuar(pregunta: str, datos: dict, minimo: float):
    niveles = _lista(datos.get("niveles"), "niveles")
    if isinstance(niveles, str):
        return niveles
    if isinstance(niveles, dict):
        # Una escala es ordenada: con descripciones el orden sería el del objeto,
        # que es fácil de romper sin darse cuenta. Lista y listo.
        return "'niveles' es una lista ordenada, no un objeto"

    def leer(contestada: dict) -> dict | str:
        try:
            por_indice = {int(k): float(v) for k, v in (contestada.get("probabilities") or {}).items()}
            valor = float(contestada["score"])
        except (KeyError, TypeError, ValueError):
            return "la respuesta de Laya no trae 'score' y 'probabilities'"
        if not por_indice:
            return "la respuesta de Laya no trae 'probabilities'"
        indice = max(por_indice, key=por_indice.get)
        confianza = por_indice[indice]
        if confianza < minimo or not 0 <= indice < len(niveles):
            nivel, indice = DUDOSO, -1
        else:
            nivel = niveles[indice]
        return {"nivel": nivel, "indice": indice, "valor": round(valor, 4), "confianza": round(confianza, 4)}

    return {"type": "score", "instructions": pregunta, "criteria": niveles}, leer


TIPOS = {"si_no": _armar_si_no, "elegir": _armar_elegir, "puntuar": _armar_puntuar}


def _armar(tipo: str, datos: dict, minimo_por_defecto: float):
    """(pregunta para laya-serve, leer) o un mensaje de error."""
    pregunta = str(datos.get("pregunta") or "").strip()
    if not pregunta:
        return "falta 'pregunta'"
    minimo = _numero(datos.get("minimo"), "minimo", minimo_por_defecto)
    if isinstance(minimo, str):
        return minimo
    return TIPOS[tipo](pregunta, datos, minimo)


# ── laya-serve ────────────────────────────────────────────────────────────

def _base(ctx: ToolContext) -> str:
    return str(ctx.config(URL) or URL_POR_DEFECTO).strip().rstrip("/")


def _pedir(ctx: ToolContext, metodo: str, camino: str, payload: dict | None = None):
    """Un request a laya-serve. Devuelve (respuesta, json) o levanta PortError."""
    headers = {}
    cuerpo = None
    if payload is not None:
        cuerpo = json.dumps(payload, ensure_ascii=False)
        headers["Content-Type"] = "application/json"
    clave = str(ctx.config(API_KEY) or "").strip()
    if clave:
        headers["Authorization"] = f"Bearer {clave}"
    respuesta = ctx.port(port_names.HTTP).request(
        f"{_base(ctx)}{camino}", method=metodo, headers=headers, body=cuerpo,
        timeout=float(ctx.config(TIMEOUT) or 60),
    )
    return respuesta, respuesta.json(default=None)


def _consultar(ctx: ToolContext, preguntas: dict[str, dict]) -> dict[str, dict] | str:
    """Manda las preguntas en un request y devuelve {id: respuesta cruda}, o el error."""
    payload = {"state": _estructura(ctx.params["texto"]), "questions": preguntas}
    modelo = str(ctx.config(MODELO) or "").strip()
    if modelo:
        payload["model"] = modelo
    try:
        respuesta, datos = _pedir(ctx, "POST", "/v1/systemone", payload)
    except PortError as exc:
        return f"laya-serve no responde en {_base(ctx)} ({exc}). {INSTALAR_SERVIDOR}"
    if respuesta.status == 401:
        return "laya-serve pide clave y la de Config no coincide (401)"
    if not respuesta.ok:
        detalle = datos.get("detail") if isinstance(datos, dict) else respuesta.text[:200]
        return f"laya-serve respondió {respuesta.status}: {detalle}"
    contestadas = datos.get("answers") if isinstance(datos, dict) else None
    if not isinstance(contestadas, dict) or any(not isinstance(contestadas.get(k), dict) for k in preguntas):
        return f"respuesta sin 'answers' desde {_base(ctx)}: ¿es laya-serve?"
    return contestadas


# ── Los tools de una pregunta ─────────────────────────────────────────────

def _una(tipo: str):
    """El tool suelto de un tipo: arma, consulta y lee UNA pregunta."""

    def fn(ctx: ToolContext) -> ToolResult:
        vacios = VACIOS[tipo]
        armada = _armar(tipo, ctx.params, 0.0)
        if isinstance(armada, str):
            return ToolResult.err(armada, **vacios)
        pregunta, leer = armada
        contestadas = _consultar(ctx, {"q": pregunta})
        if isinstance(contestadas, str):
            return ToolResult.err(contestadas, **vacios)
        leida = leer(contestadas["q"])
        if isinstance(leida, str):
            return ToolResult.err(leida, **vacios)
        ctx.log(f"Laya: {_resumen(leida)}")
        return ToolResult.ok(**leida)

    return fn


def _resumen(leida: dict) -> str:
    if "respuesta" in leida:
        return f"{leida['respuesta']} (P(sí)={leida['probabilidad']:.2f})"
    if "eleccion" in leida:
        return f"{leida['eleccion']} ({leida['confianza']:.2f})"
    return f"{leida['nivel']} (valor {leida['valor']:.2f}, {leida['confianza']:.2f})"


_TEXTO = Param("texto", required=True, doc=_DOC_TEXTO)
_PREGUNTA = Param("pregunta", required=True, doc=_DOC_PREGUNTA)
_MINIMO = Param("minimo", ParamType.FLOAT, default=0.0, doc=_DOC_MINIMO)

SI_NO = ToolManifest(
    id="laya.si_no",
    label="preguntar sí o no",
    category="LAYA",
    doc=(
        "Pregunta de sí o no sobre 'texto'. {respuesta} es 'si', 'no' o 'dudoso', para un nodo "
        "de decisión; {probabilidad} es la de que sea sí. Es el tipo de pregunta en que Laya "
        "más acierta: un problema complejo conviene partirlo en varias de éstas."
    ),
    params=(
        _TEXTO, _PREGUNTA,
        Param("umbral", ParamType.FLOAT, default=0.5, doc="Desde qué probabilidad es 'si'."),
        _MINIMO,
    ),
    outputs=(
        Output("respuesta", ParamType.STR, doc="'si', 'no' o 'dudoso'."),
        Output("probabilidad", ParamType.FLOAT, doc="Probabilidad de que sea sí, de 0 a 1."),
    ),
)

ELEGIR = ToolManifest(
    id="laya.elegir",
    label="elegir una opción",
    category="LAYA",
    doc=(
        "Elige una de 'opciones' según 'texto'. {eleccion} es la etiqueta elegida, o 'dudoso'. "
        "Con muchas opciones Laya se sesga a alguna: mejor pocas, con una descripción corta "
        "cada una, o varias preguntas de sí/no."
    ),
    params=(
        _TEXTO, _PREGUNTA,
        Param(
            "opciones", required=True,
            doc='Etiquetas separadas por coma (ventas, soporte, admin), o un objeto con una '
            'descripción por etiqueta: {"ventas": "compra o presupuesto", "soporte": "algo no anda"}.',
        ),
        _MINIMO,
    ),
    outputs=(
        Output("eleccion", ParamType.STR, doc="La etiqueta elegida, o 'dudoso'."),
        Output("confianza", ParamType.FLOAT, doc="Probabilidad de la elegida."),
        Output("probabilidades", ParamType.JSON, doc="{etiqueta: probabilidad} de todas."),
    ),
)

PUNTUAR = ToolManifest(
    id="laya.puntuar",
    label="ubicar en una escala",
    category="LAYA",
    doc=(
        "Ubica 'texto' en una escala ordenada de 'niveles', de menor a mayor. {nivel} es el más "
        "probable (o 'dudoso'), {indice} su posición desde 0, y {valor} el promedio ponderado: "
        "entre 0 y la cantidad de niveles menos uno, para comparar con un umbral."
    ),
    params=(
        _TEXTO, _PREGUNTA,
        Param("niveles", required=True, doc="De menor a mayor, separados por coma: nada, algo, mucho."),
        _MINIMO,
    ),
    outputs=(
        Output("nivel", ParamType.STR, doc="El nivel más probable, o 'dudoso'."),
        Output("indice", ParamType.INT, doc="Posición del nivel, desde 0. -1 si es dudoso."),
        Output("valor", ParamType.FLOAT, doc="Promedio ponderado de las posiciones."),
        Output("confianza", ParamType.FLOAT, doc="Probabilidad del nivel elegido."),
    ),
)


# ── laya.preguntar: varias en una pasada ──────────────────────────────────

# Un id es un tramo del camino `{NODO.respuestas.<id>.respuesta}`: con puntos
# se partiría en dos, y sólo dígitos el núcleo lo toma como índice de lista.
_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PREGUNTAR = ToolManifest(
    id="laya.preguntar",
    label="hacer varias preguntas",
    category="LAYA",
    doc=(
        "Varias preguntas sobre el mismo 'texto' en un solo request. {respuestas} tiene una "
        "entrada por pregunta con los mismos outputs que su tool suelto: se lee "
        "{NODO.respuestas.<id>.respuesta} (si_no), .eleccion (elegir) o .nivel (puntuar), "
        "también desde un nodo de decisión. Necesita núcleo v0.3.1-beta.13 para leer lo anidado."
    ),
    params=(
        _TEXTO,
        Param(
            "preguntas", ParamType.JSON, required=True,
            doc='Un objeto {id: pregunta}. Cada pregunta: {"tipo": "si_no" | "elegir" | "puntuar", '
            '"pregunta": "...", y "opciones" (elegir), "niveles" (puntuar), "umbral" (si_no) o '
            '"minimo" según el tipo}. El id va sin puntos ni espacios: urgente, area, enojo.',
        ),
        Param("minimo", ParamType.FLOAT, default=0.0, doc=_DOC_MINIMO + " Vale para las que no traen el suyo."),
    ),
    outputs=(
        Output("respuestas", ParamType.JSON, doc="{id: outputs del tool suelto de su tipo}."),
        Output("dudosas", ParamType.JSON, doc="Los ids que salieron 'dudoso', para mandar el caso a una persona."),
    ),
)


def _preguntar(ctx: ToolContext) -> ToolResult:
    vacios = {"respuestas": {}, "dudosas": []}
    minimo = _numero(ctx.params.get("minimo"), "minimo", 0.0)
    if isinstance(minimo, str):
        return ToolResult.err(minimo, **vacios)
    preguntas = _estructura(ctx.params["preguntas"])
    if not isinstance(preguntas, dict) or not preguntas:
        return ToolResult.err("'preguntas' tiene que ser un objeto {id: pregunta} con al menos una", **vacios)
    if len(preguntas) > MAX_PREGUNTAS:
        return ToolResult.err(f"son {len(preguntas)} preguntas; laya-serve acepta hasta {MAX_PREGUNTAS}", **vacios)

    armadas, lectores = {}, {}
    for id_, datos in preguntas.items():
        if not _ID.match(str(id_)):
            return ToolResult.err(f"id '{id_}': sin puntos, espacios ni sólo dígitos (va en {{NODO.respuestas.{id_}...}})", **vacios)
        if not isinstance(datos, dict):
            return ToolResult.err(f"'{id_}': cada pregunta es un objeto con 'tipo' y 'pregunta'", **vacios)
        tipo = str(datos.get("tipo") or "").strip()
        if tipo not in TIPOS:
            return ToolResult.err(f"'{id_}': 'tipo' es {', '.join(TIPOS)}, no '{tipo}'", **vacios)
        armada = _armar(tipo, datos, minimo)
        if isinstance(armada, str):
            return ToolResult.err(f"'{id_}': {armada}", **vacios)
        armadas[id_], lectores[id_] = armada

    contestadas = _consultar(ctx, armadas)
    if isinstance(contestadas, str):
        return ToolResult.err(contestadas, **vacios)
    respuestas = {}
    for id_, leer in lectores.items():
        leida = leer(contestadas[id_])
        if isinstance(leida, str):
            return ToolResult.err(f"'{id_}': {leida}", **vacios)
        respuestas[id_] = leida
        ctx.log(f"Laya · {id_}: {_resumen(leida)}")
    dudosas = [id_ for id_, r in respuestas.items() if DUDOSO in (r.get("respuesta"), r.get("eleccion"), r.get("nivel"))]
    return ToolResult.ok(respuestas=respuestas, dudosas=dudosas)


# ── laya.disponible ───────────────────────────────────────────────────────

DISPONIBLE = ToolManifest(
    id="laya.disponible",
    label="está disponible",
    category="LAYA",
    doc=(
        "Pregunta a laya-serve si está arriba. ok si responde; err si no, para desviar el "
        "flujo a una persona antes de empezar en vez de a mitad de camino."
    ),
    outputs=(Output("modelos", ParamType.JSON, doc="Los modelos que ya tiene cargados."),),
)


def _disponible(ctx: ToolContext) -> ToolResult:
    try:
        respuesta, datos = _pedir(ctx, "GET", "/health")
    except PortError as exc:
        return ToolResult.err(f"laya-serve no responde en {_base(ctx)} ({exc}). {INSTALAR_SERVIDOR}", modelos=[])
    if not respuesta.ok or not isinstance(datos, dict) or datos.get("status") != "ok":
        return ToolResult.err(f"{_base(ctx)} respondió {respuesta.status}: ¿es laya-serve?", modelos=[])
    modelos = datos.get("loaded") or []
    ctx.log(f"laya-serve arriba en {_base(ctx)}; cargados: {', '.join(map(str, modelos)) or 'ninguno todavía'}")
    return ToolResult.ok(modelos=modelos)


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[
            FunctionTool(manifest=SI_NO, fn=_una("si_no")),
            FunctionTool(manifest=ELEGIR, fn=_una("elegir")),
            FunctionTool(manifest=PUNTUAR, fn=_una("puntuar")),
            FunctionTool(manifest=PREGUNTAR, fn=_preguntar),
            FunctionTool(manifest=DISPONIBLE, fn=_disponible),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
