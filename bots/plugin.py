"""
Plugin `bots` — hablar con **otros Bots** de la red desde un flujo.

Cada Bot expone una API HTTP (la misma que usa su navegador). Este plugin la
usa para que un flujo pueda preguntar qué está haciendo otro Bot, elegir uno
libre, mandarle un caso y esperar el resultado. Con eso se arma un Bot padre
que reparte trabajo entre hijos, o Bots que se coordinan mirando lo que hizo
el otro — todo escrito en Mermaid, sin código.

Quien opera nunca ve una URL en un flujo: los Bots se dan de alta una vez en
la colección **Bots conocidos** (nombre, dirección, nota) y los nodos los
nombran por su nombre. La misma colección tiene la acción **Probar**, que
dice si el otro responde y cuántos runs tiene en vuelo, y **Abrir en pestaña**,
que abre la pantalla del otro Bot en el navegador.

Qué hace cada paso, en el idioma de la API del otro Bot:

- `bots.estado`        → `GET /runs/en-vuelo` y `GET /runs?limit=1`
- `bots.elegir_libre`  → lo anterior sobre varios; el que menos tiene en vuelo
- `bots.correr`        → `POST /runs` (sin esperar): vuelve un **ticket**
- `bots.esperar`       → `GET /runs/ticket/<ticket>` hasta que termine

`bots.correr` no bloquea al padre mientras el hijo trabaja: devuelve el ticket
en el acto y `bots.esperar` es el que aguarda, sondeando cada tanto y dejando
en el log del padre en qué paso va el hijo. Así un padre puede mandar a tres
hijos y después esperar a los tres.

`bots.comparar` es de otra familia: no reparte trabajo, compara **contenido**
entre dos Bots —los flujos que tiene cada uno, o los items de una colección de
un plugin— y devuelve qué está sólo en uno, sólo en el otro, y qué difiere.
Existe porque mover un flujo o una plantilla de un Bot a otro hoy se hace a
mano, y lo primero que hace falta para hacerlo sin pisar nada es ver qué
cambia. Sólo lee: no copia nada.

Lo que `comparar` NO puede ver, y por qué importa: los campos que una colección
declaró `secret` vuelven tapados, así que dos items cuyos campos visibles son
todos iguales pueden diferir justo en el secreto. Por eso hay un estado
`indeterminado` además de `igual`: decir "igual" ahí sería afirmar algo que no
se puede ver. La migración completa —elegir qué pisar, y los secretos— es una
pantalla de la app, no un flujo: un plugin no puede pedirle nada a la base
(`storage` y `crypto` son ports del núcleo) ni dibujar una pantalla (sólo
declara colecciones y botones).

Tampoco asume que del otro lado haya un plugin instalado, sólo que hay un Bot:
si el destino no tiene la colección que se le pide, eso es un resultado —todo
lo de acá queda como "sólo en el origen"— y no una falla. Es el caso de un Bot
nuevo de la flota, que todavía no tiene nada.

Y hay dos **Actions** para hacerlo desde la pantalla, no desde un flujo:
`comparar` (la misma comparación, más `vista`: la tabla que la app dibuja, con
casillas y un botón para migrar lo elegido) y `migrar`, que le pide a **este**
Bot que empuje al otro. El plugin nunca toca un secreto: el único que puede
leer los de una instalación es la instalación misma, así que `migrar` es un
pedido a la propia app y no una implementación.

`vista` va en la Action y no en los outputs del tool a propósito: un tool es un
nodo de un flujo, y una vista adentro de sus outputs viajaría en el contexto de
cada corrida y se guardaría en la base con cada una, sin que ningún flujo la
lea. Las dos salen de la misma función, así que no pueden divergir.

Los campos secretos quedan **afuera de una migración** salvo que se los pida
explícito (`incluir_secretos`, default false), y el motivo es el caso
desatendido: un flujo corre cada vez, así que si alguien rotó ese secreto en el
destino, la corrida siguiente lo revierte al valor viejo, y la otra también.
Afuera no significa que el item no viaje: viaja con esos campos en `None` y el
destino conserva los suyos, así que se puede corregir la url de una conexión
sin pisarle el token al otro Bot. De `env` se omiten sólo las variables
marcadas secretas —una variable *es* su valor, no hay forma de mandarla sin
él—, y el informe nombra cada una con el motivo.

Qué se puede pisar no lo decide este plugin: lo decide la app, que es la única
que puede leer un secreto de su propia instalación. Por eso `migrar` no chequea
nada de esto antes de pedir — cortar acá bloquearía los dos casos de arriba.

Lo que este plugin NO resuelve, a propósito: la carrera entre dos Bots que
miran la misma lista y quieren el mismo caso. Sondear "qué hace el otro" no
alcanza para eso; hace falta un reclamo atómico en la fuente de datos (un
campo "asignado a" que se escribe una sola vez). Ver el README.
"""

from __future__ import annotations

import ast
import json
import os
import re
from urllib.parse import quote

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

TIMEOUT = "botsTimeout"
MI_DIRECCION = "botsMiDireccion"
PREFIJO_API = "/api/core"
_URL_VALIDA = re.compile(r"^https?://[^\s/]+(:\d+)?$")

# De un flujo, lo que significa algo al compararlo. `updated_at` queda afuera:
# difiere siempre —son dos bases distintas— y no dice nada del contenido.
_CAMPOS_FLUJO = ("content", "folder", "state", "description")
# De un item de colección, lo que NO es del plugin sino del núcleo.
_CAMPOS_DEL_NUCLEO = ("_updated_at", "_error")

BOTS = Resource(
    name="bots",
    label="Bots conocidos",
    item_label="Bot",
    key_field="nombre",
    doc=(
        "Los otros Bots de la red a los que un flujo puede pedirle algo. La "
        "dirección es la que muestra el ícono de la bandeja del otro Bot en "
        "'Copiar dirección para otras PCs' (ej. http://10.0.0.41:8000)."
    ),
    fields=(
        Field("nombre", ParamType.STR, label="Nombre", required=True, doc="Cómo lo nombran los flujos, ej. 'Estación 2'."),
        Field("url", ParamType.STR, label="Dirección", required=True, doc="http://ip:puerto, sin nada después."),
        Field("nota", ParamType.STR, label="Nota", doc="Qué hace ese Bot, dónde está."),
    ),
)

# Las dos Actions del feature de migración. Se declaran acá —y sus funciones
# viven más abajo, con el resto— porque el manifest las nombra al construirse.
COMPARAR_ACCION = Action(
    "comparar", "Comparar contenido",
    doc="Qué tiene distinto este Bot respecto del mío: sus flujos, o los items de una colección. "
        "Sólo lee. Después se puede elegir qué migrar.",
    resource="bots",
    params=(
        # `destino` con alias `nombre`: el item de la colección entrega sus
        # campos por su nombre real (`key_field` es "nombre"), y el alias deja
        # que el param se siga llamando igual que en el tool.
        Param(
            "destino", required=True, aliases=("nombre",), options_from="bots",
            doc="Contra qué Bot comparar. Viene puesto del renglón desde donde se apretó.",
        ),
        Param(
            "que", ParamType.ENUM, default="flujos", choices=("flujos", "registros"),
            doc="'flujos' = los diagramas. 'registros' = los items de una colección de un plugin. "
            "Las variables de entorno no se pueden comparar (un secreto no sale por la API): "
            "para ésas está la acción 'Migrar'.",
        ),
        Param(
            "plugin", default="", options_from="core:plugins",
            doc="Sólo con que=registros: de qué plugin es la colección. Va como figura en Plugins "
            "('Connections') o su nombre interno ('connections'), el que tengas a mano.",
        ),
        Param(
            "coleccion", default="", options_from="core:resources:{plugin}",
            doc="Sólo con que=registros: qué colección de ese plugin, por el título que muestra la "
            "pantalla ('Sources') o su nombre interno ('sources'). Si no existe, el error te lista "
            "las que hay.",
        ),
    ),
)

MIGRAR_ACCION = Action(
    # "Migrar lo elegido" nombraba la selección de la tabla, que acá no existe:
    # suelto en la pantalla del plugin se leía como si hiciera lo mismo que el
    # botón del renglón. El nombre tiene que decir para qué sirve ESTE camino.
    "migrar", "Migrar a mano (secretos y variables de entorno)",
    doc="El camino directo, sin comparar antes: se escriben las claves a mano. Es el único con la "
        "casilla 'incluir secretos' y el único que puede mover variables de entorno. Para todo lo "
        "demás conviene 'Comparar contenido', en el renglón de cada Bot: ahí se ve qué cambia antes "
        "de tocar nada. Escribe en el otro Bot y pisa lo que haya con ese nombre; no borra nada.",
    dangerous=True,
    params=(
        Param(
            "destino", required=True, options_from="bots",
            doc="El Bot que va a RECIBIR, por su nombre en 'Bots conocidos' (no su dirección). "
            "Ej.: Estación 2",
        ),
        Param(
            "que", ParamType.ENUM, default="flujos", choices=("flujos", "registros", "env"),
            doc="'flujos' = los diagramas. 'registros' = los items de una colección de un plugin "
            "(hay que llenar 'plugin' y 'coleccion'). 'env' = variables de entorno.",
        ),
        Param(
            "plugin", default="", options_from="core:plugins",
            doc="Sólo con que=registros: de qué plugin es la colección. Va como figura en Plugins "
            "('Connections') o su nombre interno ('connections'), el que tengas a mano.",
        ),
        Param(
            "coleccion", default="", options_from="core:resources:{plugin}",
            doc="Sólo con que=registros: qué colección de ese plugin, por el título que muestra la "
            "pantalla ('Sources') o su nombre interno ('sources'). Si no existe, el error te lista "
            "las que hay.",
        ),
        Param(
            "claves", ParamType.JSON, default=[], required=True,
            doc='Una LISTA de qué migrar, entre corchetes. Flujos: su nombre tal cual. Registros: el '
            'campo que los identifica (el que la colección muestra como clave). env: el nombre de la '
            'variable. Ej.: ["EXPORTAR V3", "PROCESAR watch"]',
        ),
        Param(
            "incluir_secretos", ParamType.BOOL, default=False,
            doc="Sin marcar, lo demás del item viaja igual y el otro Bot conserva su secreto. "
            "Marcado, se lo pisa a ciegas: no hay forma de saber si el de allá era distinto, porque "
            "un secreto no sale por la API de nadie. Para 'env' hace falta marcarlo: una variable es "
            "su valor, no hay otra parte que mandar.",
        ),
    ),
)

MANIFEST = PluginManifest(
    name="bots",
    label="Bots",
    version="0.4.0",
    doc="Hablar con otros Bots de la red desde un flujo: qué hacen, mandarles un caso, esperar el resultado.",
    ports=(port_names.HTTP, port_names.CLOCK),
    settings=(
        Setting(
            TIMEOUT, ParamType.INT, label="Segundos antes de dar por caído a un Bot", default=15,
            doc="Cuánto esperar cada respuesta HTTP del otro Bot.",
        ),
        Setting(
            MI_DIRECCION, ParamType.STR, label="Dirección de este Bot",
            doc="Normalmente va VACÍA: sola resuelve al Bot que está corriendo, en su puerto real. "
            "Sólo se llena para un caso raro, y entonces tiene que ser loopback "
            "(http://127.0.0.1:<puerto de ESTE Bot>). No poner la dirección de red que ofrece la "
            "bandeja en 'Copiar dirección para otras PCs' —ésa es para que otros Bots lleguen acá, y "
            "migrar sólo se puede pedir desde la propia máquina— ni el puerto de otra instalación de "
            "esta misma PC, que sería comparar y migrar el contenido de otro Bot.",
        ),
    ),
    resources=(BOTS,),
    actions=(
        Action(
            "probar", "Probar",
            doc="Le pregunta al Bot si responde y cuántos runs tiene en vuelo.",
            resource="bots",
            params=(Param("nombre", required=True, options_from="bots"),),
        ),
        Action(
            "abrir", "Abrir en pestaña",
            doc="Abre la pantalla de ese Bot en una pestaña nueva del navegador.",
            resource="bots",
            params=(Param("nombre", required=True, options_from="bots"),),
        ),
        COMPARAR_ACCION,
        MIGRAR_ACCION,
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _bot(ctx: ToolContext, nombre: str):
    """El item de la colección, o un ToolResult de error que quien llama devuelve tal cual."""
    nombre = (nombre or "").strip()
    bot = ctx.resource("bots", nombre, key_field="nombre") if nombre else None
    if bot is None:
        hay = ", ".join(ctx.resource_keys("bots", key_field="nombre")) or "ninguno"
        return ToolResult.err(f"no existe el Bot '{nombre}'. Conocidos: {hay}")
    url = (bot.get("url") or "").strip().rstrip("/")
    if not _URL_VALIDA.match(url):
        return ToolResult.err(f"la dirección de '{nombre}' no es http://ip:puerto: {url!r}")
    return {**bot, "url": url}


# ── Nombres de pantalla vs nombres internos ─────────────────────────────

def _listado(opciones: dict[str, str]) -> str:
    """Los internos, y entre paréntesis el label cuando no es el mismo."""
    partes = [
        f"{nombre} ({label})" if label and label.lower() != nombre.lower() else nombre
        for nombre, label in sorted(opciones.items())
    ]
    return ", ".join(partes) or "ninguno"


def _interno(escrito: str, opciones: dict[str, str], que: str, donde: str = ""):
    """
    El nombre interno de algo que se pudo haber escrito como lo muestra la
    pantalla. `opciones` es {nombre interno: label}.

    Desde la pantalla del plugin el label es lo ÚNICO que se ve: el nombre
    interno no figura en ninguna parte de la UI. Pedirlo era pedir un dato que
    no está a la vista, y escribir lo que sí se ve —'Connections', 'Sources'—
    fallaba con "1 de 1 no se migraron", sin decir cuál de los campos.

    Bajar todo a minúsculas no alcanza: la colección de ESTE plugin se llama
    `bots` y la pantalla la muestra como 'Bots conocidos'. Por eso se resuelve
    contra los dos nombres, y el interno gana — si alguien llamó a su plugin
    igual que el label de otro, lo que escribió literal es lo que quiso.
    """
    escrito = (escrito or "").strip()
    if escrito in opciones:
        return escrito
    bajo = escrito.lower()
    if exactos := [n for n in opciones if n.lower() == bajo]:
        return exactos[0]
    por_label = [n for n, label in opciones.items() if label.lower() == bajo]
    if len(por_label) == 1:
        return por_label[0]
    if len(por_label) > 1:
        # Elegir uno sería migrar contra la colección equivocada sin que se vea.
        return ToolResult.err(
            f"'{escrito}' no alcanza para saber de qué {que} se trata: así se llaman en pantalla "
            f"{len(por_label)} ({', '.join(sorted(por_label))}). Poné el nombre interno."
        )
    return ToolResult.err(f"no hay {que} '{escrito}'{donde}. Hay: {_listado(opciones)}")


def _catalogo(ctx: ToolContext, bot: dict):
    """
    Los plugins instalados en `bot` y sus colecciones, con los dos nombres:
    `{plugin: (label, {coleccion: label})}`. O un ToolResult de error.

    Sale de `/tools`, que ya trae el catálogo entero; no hay un endpoint de
    "resolvé este nombre". Es un request de más por acción, y se paga una sola
    vez y sólo con que=registros: es una acción de pantalla, no un lazo.
    """
    try:
        respuesta, datos = _pedir(ctx, bot["url"], "/tools")
    except PortError as exc:
        return ToolResult.err(f"no se pudo leer qué plugins tiene '{bot['nombre']}': {exc}")
    if not respuesta.ok or not isinstance(datos, dict):
        return ToolResult.err(
            f"'{bot['nombre']}' no contestó qué plugins tiene ({respuesta.status}); "
            "sin eso no se puede saber si 'plugin' y 'coleccion' existen"
        )
    catalogo = {}
    for p in datos.get("plugins") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        catalogo[str(p["name"])] = (
            str(p.get("label") or ""),
            {
                str(r["name"]): str(r.get("label") or "")
                for r in (p.get("resources") or [])
                if isinstance(r, dict) and r.get("name")
            },
        )
    return catalogo


def _registros(ctx: ToolContext, bot: dict, plugin: str, coleccion: str):
    """
    'plugin' y 'coleccion' tal como los nombra el código, o un ToolResult.

    Si el catálogo no se puede leer se sigue con lo que se escribió: `/tools`
    puede no estar en una app vieja, y traducir un nombre es una comodidad, no
    la validación —quien valida es el otro lado, que ya contesta "acá no hay
    una colección X"—. Dejar de migrar porque no se pudo traducir sería cambiar
    una molestia por una falla. Lo mismo con un catálogo vacío: sin plugins que
    mirar, "no existe" no es una conclusión, es no haber podido preguntar.
    """
    catalogo = _catalogo(ctx, bot)
    if isinstance(catalogo, ToolResult) or not catalogo:
        detalle = catalogo.message if isinstance(catalogo, ToolResult) else "no contestó ninguno"
        ctx.log(f"{detalle}; sigo con '{plugin}' y '{coleccion}' tal como se escribieron", "warning")
        return plugin.strip(), coleccion.strip()
    nombre_plugin = _interno(plugin, {n: label for n, (label, _) in catalogo.items()}, "plugin")
    if isinstance(nombre_plugin, ToolResult):
        return nombre_plugin
    nombre_coleccion = _interno(
        coleccion, catalogo[nombre_plugin][1], "colección", f" en '{nombre_plugin}'")
    if isinstance(nombre_coleccion, ToolResult):
        return nombre_coleccion
    if (plugin.strip(), coleccion.strip()) != (nombre_plugin, nombre_coleccion):
        # Que se vea en el log: lo que se escribió y lo que terminó usándose.
        ctx.log(f"'{plugin}' / '{coleccion}' es '{nombre_plugin}' / '{nombre_coleccion}'")
    return nombre_plugin, nombre_coleccion


def _como_objeto(valor) -> dict | None:
    """
    `row` como dict. Vacío es {}. Si viene como texto —un `row={variable}` que
    el núcleo interpoló: str(dict), con comillas simples de Python, o JSON—
    se parsea. None si no es un objeto.
    """
    if valor is None or valor == "":
        return {}
    if isinstance(valor, dict):
        return valor
    if isinstance(valor, str):
        for parser in (json.loads, ast.literal_eval):
            try:
                parseado = parser(valor)
            except (ValueError, SyntaxError):
                continue
            if isinstance(parseado, dict):
                return parseado
    return None


def _timeout(ctx: ToolContext) -> float:
    return float(ctx.config(TIMEOUT) or 15)


def _pedir(ctx: ToolContext, url: str, camino: str, *, method: str = "GET", payload: dict | None = None):
    """Un request a la API del otro Bot. Devuelve (respuesta, json) o levanta PortError."""
    cuerpo = json.dumps(payload) if payload is not None else None
    headers = {"Content-Type": "application/json"} if cuerpo else {}
    respuesta = ctx.port(port_names.HTTP).request(
        f"{url}{PREFIJO_API}{camino}", method=method, headers=headers, body=cuerpo, timeout=_timeout(ctx),
    )
    return respuesta, respuesta.json(default=None)


def _estado_de(ctx: ToolContext, bot: dict) -> dict | ToolResult:
    """Cuántos runs tiene en vuelo y cómo terminó el último. Err si no responde."""
    try:
        resp_vivos, vivos = _pedir(ctx, bot["url"], "/runs/en-vuelo")
        resp_ultimo, ultimos = _pedir(ctx, bot["url"], "/runs?limit=1&include_dry=false")
    except PortError as exc:
        return ToolResult.err(f"'{bot['nombre']}' no responde en {bot['url']}: {exc}")
    if not resp_vivos.ok or not isinstance(vivos, list):
        return ToolResult.err(f"'{bot['nombre']}' respondió {resp_vivos.status} a /runs/en-vuelo: ¿es un Bot?")
    ultimo = ultimos[0] if resp_ultimo.ok and isinstance(ultimos, list) and ultimos else {}
    return {
        "bot": bot["nombre"],
        "url": bot["url"],
        "corriendo": len(vivos),
        "libre": "si" if not vivos else "no",
        "en_vuelo": vivos,
        "ultimo_estado": ultimo.get("status") or "",
        "ultimo_flujo": ultimo.get("flow") or "",
        "ultimo_caso": ultimo.get("case_id") or "",
    }


def _es_loopback(url: str) -> bool:
    """
    Si esa dirección es la de la propia máquina.

    Importa porque la app sólo contesta `/migrar` desde loopback, y este
    setting lo escribe una persona que tiene a mano la dirección de red del
    Bot —la bandeja la ofrece para copiar—. Con la de red, `comparar` anda
    igual (lee `/workflows` y `/resources`, que no están acotados) y `migrar`
    da 403: distinguirlo acá es lo que evita que ese 403 se lea como un
    problema de emparejamiento.
    """
    host = url.split("//", 1)[-1].split(":")[0].split("/")[0].lower()
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def _mi_puerto() -> str:
    """
    El puerto en el que escucha el Bot que está corriendo este plugin.

    La app lo deja en el entorno al arrancar (`webapp/__main__.py`) y lo lee de
    ahí para armar su propia URL (`core_api.py:_url_app`), así que esto es el
    mecanismo de la casa y no una adivinanza. Leer `os.environ` no es saltarse
    un port: no hay I/O que abstraer ni nada que mockear — es en qué proceso
    estoy parado.

    Vacío si la variable no está: el plugin puede correr fuera de la app (en un
    test, o contra el repo), y ahí no hay un puerto propio que descubrir.
    """
    return (os.environ.get("BOT_PORT") or "").strip()


def _este_bot(ctx: ToolContext) -> dict | ToolResult:
    """
    Este Bot como si fuera uno de la colección, para poder comparar contra otro.

    El default sale del puerto real y no de un 8000 fijo, y la diferencia no es
    cosmética: con el 8000 fijo, un Bot que escucha en otro puerto le hablaba a
    **otra instalación** —la que estuviera en el 8000—, así que `comparar`
    informaba el contenido de otra máquina como si fuera el propio y `migrar`
    habría empujado el de la instalación equivocada, pisando los flujos del
    destino. Sin error visible: el informe parece correcto. Lo encontró la
    sesión de la app probando con dos Bots en 8101 y 8102.
    """
    puerto = _mi_puerto()
    url = str(ctx.config(MI_DIRECCION) or f"http://127.0.0.1:{puerto or '8000'}").strip().rstrip("/")
    if not _URL_VALIDA.match(url):
        return ToolResult.err(
            f"la configuración '{MI_DIRECCION}' no es http://ip:puerto: {url!r}"
        )
    return {"nombre": "este Bot", "url": url}


def _no_es_este_bot(url: str) -> str:
    """
    Por qué esa dirección no es la del Bot que está corriendo esto, o "".

    Sólo puede responder cuando la app dejó el puerto en el entorno; fuera de
    la app —un test, el repo— no hay con qué comparar y se calla, en vez de
    inventar una sospecha.
    """
    puerto = _mi_puerto()
    if not puerto or not _es_loopback(url):
        return ""
    suyo = url.rsplit(":", 1)[-1].rstrip("/")
    if suyo == puerto:
        return ""
    return (
        f"la configuración '{MI_DIRECCION}' apunta a {url}, pero este Bot escucha en el "
        f"puerto {puerto}: ese otro es una instalación distinta de la misma máquina"
    )


def _flujos_de(ctx: ToolContext, bot: dict) -> dict | ToolResult:
    """
    {nombre: {content, folder, state, description}} de un Bot.

    Son N+1 requests porque `GET /workflows` no trae el contenido
    (`Workflow.to_dict(with_content=False)`), y sin contenido no hay con qué
    comparar dos flujos que se llaman igual.
    """
    try:
        respuesta, lista = _pedir(ctx, bot["url"], "/workflows")
    except PortError as exc:
        return ToolResult.err(f"'{bot['nombre']}' no responde en {bot['url']}: {exc}")
    if not respuesta.ok or not isinstance(lista, list):
        return ToolResult.err(f"'{bot['nombre']}' respondió {respuesta.status} a /workflows: ¿es un Bot?")

    flujos = {}
    for entrada in lista:
        nombre = (entrada or {}).get("name") if isinstance(entrada, dict) else None
        if not nombre:
            continue
        try:
            resp_uno, uno = _pedir(ctx, bot["url"], f"/workflows/{quote(str(nombre), safe='')}")
        except PortError as exc:
            return ToolResult.err(f"'{bot['nombre']}' dejó de responder leyendo '{nombre}': {exc}")
        if not resp_uno.ok or not isinstance(uno, dict):
            ctx.log(f"{bot['nombre']}: no se pudo leer el flujo '{nombre}' ({resp_uno.status})", level="warning")
            continue
        flujos[str(nombre)] = {campo: uno.get(campo) or "" for campo in _CAMPOS_FLUJO}
    return flujos


def _items_de(
    ctx: ToolContext, bot: dict, plugin: str, coleccion: str, *, exigir: bool,
) -> tuple[dict, list, str] | ToolResult:
    """
    ({clave: item}, campos secretos, campo clave) de una colección de un Bot.

    Los campos `secret` salen de la definición que la propia respuesta trae, no
    de una lista escrita acá: cualquier plugin instalado en el otro Bot los
    declara en su manifest y el endpoint los publica.

    `exigir` distingue los dos lados, y no es simetría mal hecha:

    - En el **origen** va True: pedir una colección que este Bot no tiene es,
      casi siempre, un nombre mal escrito, y además no hay nada que comparar
      *desde*. Vale más un error que un informe vacío que parece una respuesta.
    - En el **destino** va False: que el otro Bot no tenga el plugin es un
      resultado, no una falla —"no tiene ninguno de estos, todos habría que
      copiarlos"—, y es justo el caso de un Bot nuevo de la flota, que no tiene
      nada instalado todavía. Un tool que se cae porque allá falta un plugin
      rompe por lo que no está, que es lo que no tiene que pasar.
    """
    camino = f"/resources/{quote(plugin, safe='')}/{quote(coleccion, safe='')}"
    try:
        respuesta, datos = _pedir(ctx, bot["url"], camino)
    except PortError as exc:
        return ToolResult.err(f"'{bot['nombre']}' no responde en {bot['url']}: {exc}")
    if not respuesta.ok or not isinstance(datos, dict):
        detalle = datos.get("detail") if isinstance(datos, dict) else respuesta.text[:200]
        if exigir:
            return ToolResult.err(
                f"'{bot['nombre']}' respondió {respuesta.status} a {camino}: {detalle or 'sin detalle'}. "
                f"¿Tiene instalado el plugin '{plugin}' con la colección '{coleccion}'?"
            )
        ctx.log(
            f"{bot['nombre']} no tiene la colección '{plugin}/{coleccion}' "
            f"({respuesta.status}): todo lo de acá queda como 'sólo en el origen'",
            level="warning",
        )
        return {}, [], ""

    definicion = datos.get("resource") or {}
    campos = definicion.get("fields") or []
    secretos = [c.get("name") for c in campos if isinstance(c, dict) and c.get("secret")]
    clave = definicion.get("key_field") or "name"
    items = {}
    for item in datos.get("items") or []:
        if not isinstance(item, dict):
            continue
        valor_clave = item.get(clave)
        if valor_clave in (None, ""):
            continue
        items[str(valor_clave)] = {
            k: v for k, v in item.items() if k not in _CAMPOS_DEL_NUCLEO and k != clave
        }
    return items, [s for s in secretos if s], str(clave)


def _diferencias(origen: dict, destino: dict, secretos: list, detalle: bool) -> tuple[list, dict]:
    """
    El diff entre dos mapas {clave: {campo: valor}}: (items, resumen).

    Es la única función que decide qué significa "distinto", a propósito: si el
    cálculo se muda a un endpoint de la app —para que la pantalla de migración y
    este tool no tengan dos implementaciones que divergen— se reemplaza esto y
    el resto del tool queda igual.

    `indeterminado` es el estado de un item cuyos campos visibles son todos
    iguales pero cuya colección declara campos `secret`: no salen por la API, así
    que podría diferir justo ahí. Decir `igual` sería afirmar algo que no se
    puede ver.
    """
    items = []
    for clave in sorted(set(origen) | set(destino)):
        aca, alla = origen.get(clave), destino.get(clave)
        if alla is None:
            items.append({"clave": clave, "estado": "solo_origen", "campos": []})
            continue
        if aca is None:
            items.append({"clave": clave, "estado": "solo_destino", "campos": []})
            continue

        distintos = sorted(
            campo for campo in set(aca) | set(alla)
            if campo not in secretos and aca.get(campo) != alla.get(campo)
        )
        if distintos:
            entrada = {"clave": clave, "estado": "distinto", "campos": distintos}
            if detalle:
                entrada["origen"] = {c: aca.get(c) for c in distintos}
                entrada["destino"] = {c: alla.get(c) for c in distintos}
            items.append(entrada)
        else:
            items.append({
                "clave": clave,
                "estado": "indeterminado" if secretos else "igual",
                "campos": [],
            })

    resumen = {estado: 0 for estado in ("igual", "distinto", "solo_origen", "solo_destino", "indeterminado")}
    for item in items:
        resumen[item["estado"]] += 1
    return items, resumen


# ── bots.estado ───────────────────────────────────────────────────────────

ESTADO = ToolManifest(
    id="bots.estado",
    label="estado de un Bot",
    category="BOTS",
    doc=(
        "Qué está haciendo otro Bot: cuántos runs tiene en vuelo, si está libre, "
        "y cómo terminó su último run. Err si no responde. Para ramificar: "
        "{libre} vale 'si' o 'no'."
    ),
    params=(Param("bot", required=True, options_from="bots", doc="Nombre en Bots conocidos."),),
    outputs=(
        Output("corriendo", ParamType.INT),
        Output("libre", ParamType.STR, doc="'si' o 'no'."),
        Output("en_vuelo", ParamType.JSON, doc="Los runs en vuelo del otro, con su paso si lo sabe."),
        Output("ultimo_estado", ParamType.STR, doc="ok / err del último run terminado."),
        Output("ultimo_flujo", ParamType.STR),
        Output("ultimo_caso", ParamType.STR),
    ),
)


def _estado(ctx: ToolContext) -> ToolResult:
    bot = _bot(ctx, ctx.params["bot"])
    if isinstance(bot, ToolResult):
        return bot
    estado = _estado_de(ctx, bot)
    if isinstance(estado, ToolResult):
        return estado
    ctx.log(f"{bot['nombre']}: {estado['corriendo']} en vuelo · último {estado['ultimo_estado'] or '—'}")
    return ToolResult.ok(**{k: v for k, v in estado.items() if k not in ("bot", "url")})


# ── bots.elegir_libre ─────────────────────────────────────────────────────

ELEGIR_LIBRE = ToolManifest(
    id="bots.elegir_libre",
    label="elegir el Bot más libre",
    category="BOTS",
    doc=(
        "De una lista de Bots conocidos, el que menos runs tiene en vuelo (en "
        "empate, el primero de la lista). Los que no responden se saltean; err "
        "si ninguno responde. Deja {bot} para usarlo en bots.correr."
    ),
    params=(Param("bots", required=True, options_from="bots", doc="Nombres separados por coma, ej. 'Estación 2, Estación 3'."),),
    outputs=(
        Output("bot", ParamType.STR),
        Output("corriendo", ParamType.INT, doc="Cuántos tenía en vuelo el elegido."),
        Output("caidos", ParamType.JSON, doc="Los que no respondieron."),
    ),
)


def _elegir_libre(ctx: ToolContext) -> ToolResult:
    nombres = [n.strip() for n in str(ctx.params["bots"]).split(",") if n.strip()]
    if not nombres:
        return ToolResult.err("'bots' vacío: hace falta al menos un nombre")
    mejor, caidos = None, []
    for nombre in nombres:
        bot = _bot(ctx, nombre)
        if isinstance(bot, ToolResult):
            return bot
        estado = _estado_de(ctx, bot)
        if isinstance(estado, ToolResult):
            ctx.log(estado.message, level="warning")
            caidos.append(nombre)
            continue
        if mejor is None or estado["corriendo"] < mejor["corriendo"]:
            mejor = estado
    if mejor is None:
        return ToolResult.err(f"ningún Bot respondió: {', '.join(caidos)}", caidos=caidos)
    ctx.log(f"elegido {mejor['bot']} con {mejor['corriendo']} en vuelo")
    return ToolResult.ok(bot=mejor["bot"], corriendo=mejor["corriendo"], caidos=caidos)


# ── bots.correr ───────────────────────────────────────────────────────────

CORRER = ToolManifest(
    id="bots.correr",
    label="correr un flujo en otro Bot",
    category="BOTS",
    doc=(
        "Le pide a otro Bot que corra un flujo suyo sobre un caso, sin esperar: "
        "vuelve en el acto con un {ticket} para bots.esperar. La fila que recibe "
        "el hijo es 'row' si se da, o {<campo_id>: case_id} si no — lo que un "
        "flujo que arranca por 'refrescar row' necesita."
    ),
    params=(
        Param("bot", required=True, options_from="bots", doc="Nombre en Bots conocidos."),
        Param("flujo", required=True, doc="Nombre del flujo **en el otro Bot**."),
        Param("case_id", required=True),
        Param("row", ParamType.JSON, default={}, doc="La fila para el hijo. Vacío: {<campo_id>: case_id}."),
        Param(
            "campo_id", default="case_id",
            doc="Con qué nombre va case_id en la fila cuando no se da 'row': el de la columna que "
            "identifica el caso en la fuente del otro Bot (ej. 'codigo').",
        ),
        Param("actor", default="", doc="Con qué actor corre en el otro Bot. Vacío: el suyo por defecto."),
    ),
    outputs=(
        Output("ticket", ParamType.STR),
        Output("bot", ParamType.STR),
    ),
)


def _correr(ctx: ToolContext) -> ToolResult:
    bot = _bot(ctx, ctx.params["bot"])
    if isinstance(bot, ToolResult):
        return bot
    case_id = str(ctx.params["case_id"]).strip()
    row = _como_objeto(ctx.params.get("row"))
    if row is None:
        return ToolResult.err("'row' tiene que ser un objeto JSON (o una {variable} que contenga uno)")
    cuerpo = {
        "flow": ctx.params["flujo"], "case_id": case_id,
        "row": row or {(str(ctx.params.get("campo_id") or "").strip() or "case_id"): case_id},
        "source": f"bot:{ctx.case_id}" if getattr(ctx, "case_id", None) else "bot",
    }
    if ctx.params.get("actor"):
        cuerpo["actor"] = ctx.params["actor"]
    try:
        respuesta, datos = _pedir(ctx, bot["url"], "/runs", method="POST", payload=cuerpo)
    except PortError as exc:
        return ToolResult.err(f"'{bot['nombre']}' no responde en {bot['url']}: {exc}")
    if not respuesta.ok or not isinstance(datos, dict) or not datos.get("ticket"):
        detalle = datos.get("detail") if isinstance(datos, dict) else respuesta.text[:200]
        if isinstance(detalle, dict):
            detalle = detalle.get("message") or json.dumps(detalle, ensure_ascii=False)
        return ToolResult.err(f"'{bot['nombre']}' no aceptó el flujo '{ctx.params['flujo']}' ({respuesta.status}): {detalle}")
    ctx.log(f"{bot['nombre']} ← {ctx.params['flujo']} / {case_id} · ticket {datos['ticket']}")
    return ToolResult.ok(ticket=datos["ticket"], bot=bot["nombre"])


# ── bots.esperar ──────────────────────────────────────────────────────────

ESPERAR = ToolManifest(
    id="bots.esperar",
    label="esperar a otro Bot",
    category="BOTS",
    doc=(
        "Espera a que termine un run pedido con bots.correr, sondeando cada "
        "'cada' segundos y anotando en el log en qué paso va el hijo. ok si el "
        "hijo terminó ok; err si terminó err, si el ticket no existe o si se "
        "agotó 'timeout' (el hijo sigue corriendo: no se lo cancela)."
    ),
    params=(
        Param("bot", required=True, options_from="bots"),
        Param("ticket", required=True, doc="El {ticket} que devolvió bots.correr."),
        Param("timeout", ParamType.INT, default=900, doc="Segundos máximos de espera."),
        Param("cada", ParamType.INT, default=5, doc="Cada cuántos segundos preguntar."),
    ),
    outputs=(
        Output("estado_hijo", ParamType.STR, doc="ok / err / en_vuelo (si venció el timeout)."),
        Output("run_id", ParamType.STR, doc="El run en el otro Bot, para mirar su traza allá."),
        Output("mensaje", ParamType.STR, doc="El mensaje con el que terminó el hijo."),
        Output("hijo", ParamType.JSON, doc="El resultado completo del run del hijo."),
    ),
    concurrency="concurrent",
)


def _esperar(ctx: ToolContext) -> ToolResult:
    bot = _bot(ctx, ctx.params["bot"])
    if isinstance(bot, ToolResult):
        return bot
    ticket = str(ctx.params["ticket"]).strip()
    limite = max(1, int(ctx.params.get("timeout") or 900))
    cada = max(1, int(ctx.params.get("cada") or 5))
    reloj = ctx.port(port_names.CLOCK)
    inicio = reloj.monotonic()
    ultimo_paso = None
    while True:
        try:
            respuesta, datos = _pedir(ctx, bot["url"], f"/runs/ticket/{ticket}")
        except PortError as exc:
            return ToolResult.err(f"'{bot['nombre']}' dejó de responder: {exc}", estado_hijo="", run_id="", mensaje="", hijo={})
        if not respuesta.ok or not isinstance(datos, dict):
            return ToolResult.err(f"'{bot['nombre']}' respondió {respuesta.status} al ticket {ticket}", estado_hijo="", run_id="", mensaje="", hijo={})
        estado = datos.get("estado")
        if estado == "terminado":
            run = datos.get("run") or {}
            salida = {
                "estado_hijo": run.get("status") or "", "run_id": run.get("run_id") or "",
                "mensaje": run.get("message") or "", "hijo": run,
            }
            if run.get("status") == "ok":
                ctx.log(f"{bot['nombre']} terminó ok · {run.get('run_id') or ''}")
                return ToolResult.ok(**salida)
            return ToolResult.err(f"{bot['nombre']} terminó {run.get('status')}: {run.get('message') or 'sin mensaje'}", **salida)
        if estado == "desconocido":
            return ToolResult.err(f"'{bot['nombre']}' no conoce el ticket {ticket} (¿se reinició?)", estado_hijo="", run_id="", mensaje="", hijo={})
        vivo = datos.get("vivo") or {}
        paso = f"{vivo.get('hechos') or 0}/{vivo.get('total') or '?'} · {vivo.get('paso') or ('en cola' if estado == 'en_cola' else 'en curso')}"
        if paso != ultimo_paso:
            ctx.log(f"{bot['nombre']}: {paso}")
            ultimo_paso = paso
        if reloj.monotonic() - inicio >= limite:
            return ToolResult.err(
                f"{bot['nombre']} sigue corriendo después de {limite} s ({paso}); el run sigue allá",
                estado_hijo="en_vuelo", run_id="", mensaje="", hijo=vivo,
            )
        reloj.sleep(cada)


# ── bots.comparar ─────────────────────────────────────────────────────────

COMPARAR = ToolManifest(
    id="bots.comparar",
    label="comparar contenido con otro Bot",
    category="BOTS",
    doc=(
        "Qué tiene distinto otro Bot: sus flujos, o los items de una colección "
        "de un plugin. Sólo lee, no copia nada. Cada clave queda en uno de "
        "cinco estados — igual, distinto (con qué campos), solo_origen, "
        "solo_destino, o indeterminado (los campos visibles coinciden pero la "
        "colección tiene campos secretos, que no salen por la API: podría "
        "diferir justo ahí). 'origen' vacío es este Bot. Con 'detalle', cada "
        "item distinto trae además los valores de los dos lados — puede ser "
        "mucho texto si son flujos."
    ),
    params=(
        Param("destino", required=True, options_from="bots", doc="Nombre en Bots conocidos: contra quién comparar."),
        Param("origen", default="", options_from="bots", doc="Vacío: este Bot (ver la configuración 'Dirección de este Bot')."),
        Param("que", ParamType.ENUM, default="flujos", choices=("flujos", "registros"), doc="'flujos' o 'registros' (los items de una colección)."),
        Param("plugin", default="", options_from="core:plugins", doc="Sólo con que=registros: de qué plugin es la colección, por su título en Plugins ('Connections') o su nombre interno ('connections')."),
        Param("coleccion", default="", options_from="core:resources:{plugin}", doc="Sólo con que=registros: qué colección, por el título de la pantalla ('Sources') o su nombre interno ('sources')."),
        Param("detalle", ParamType.BOOL, default=False, doc="Agregar a cada item distinto los valores de origen y destino."),
    ),
    outputs=(
        Output("diff", ParamType.JSON, doc="El informe completo: {origen, destino, que, coleccion, campos_comparados, campos_secretos, items, resumen}."),
        Output("hay_diferencias", ParamType.STR, doc="'si' o 'no', para ramificar."),
        Output("distintos", ParamType.JSON, doc="Claves que existen en los dos pero difieren."),
        Output("solo_origen", ParamType.JSON, doc="Claves que están sólo en el origen (las que habría que copiar)."),
        Output("solo_destino", ParamType.JSON, doc="Claves que están sólo en el destino."),
        Output("iguales", ParamType.JSON),
        Output("indeterminados", ParamType.JSON, doc="Claves que no se pueden comparar del todo por tener campos secretos."),
    ),
)


def _comparar(ctx: ToolContext) -> ToolResult:
    destino = _bot(ctx, ctx.params["destino"])
    if isinstance(destino, ToolResult):
        return destino
    # `.get` y no `[...]`: la Action `comparar` no declara `origen` ni
    # `detalle` —su origen es siempre este Bot y la tabla no muestra valores—,
    # y comparte esta función con el tool, que sí los declara.
    nombre_origen = str(ctx.params.get("origen") or "").strip()
    origen = _bot(ctx, nombre_origen) if nombre_origen else _este_bot(ctx)
    if isinstance(origen, ToolResult):
        return origen
    if origen["url"] == destino["url"]:
        return ToolResult.err(f"'{origen['nombre']}' y '{destino['nombre']}' son el mismo Bot ({origen['url']})")
    if not nombre_origen and (problema := _no_es_este_bot(origen["url"])):
        # Aviso y no corte: comparar sólo lee. Pero el informe va a hablar de
        # otra instalación, y sin esto no habría modo de notarlo — los nombres
        # de flujo de otra máquina se leen igual de plausibles que los propios.
        ctx.log(f"{problema}. El informe es de ESA instalación, no de este Bot", level="warning")

    que = ctx.params["que"]
    plugin = str(ctx.params["plugin"]).strip()
    coleccion = str(ctx.params["coleccion"]).strip()
    secretos: list = []
    destino_sin_coleccion = False

    if que == "flujos":
        datos_origen = _flujos_de(ctx, origen)
        if isinstance(datos_origen, ToolResult):
            return datos_origen
        datos_destino = _flujos_de(ctx, destino)
        if isinstance(datos_destino, ToolResult):
            return datos_destino
        comparados = list(_CAMPOS_FLUJO)
    else:
        if not plugin or not coleccion:
            return ToolResult.err("con que=registros hacen falta 'plugin' y 'coleccion'")
        # Contra el catálogo del ORIGEN: es el lado del que se lee, y el que
        # tiene que tener la colección sí o sí (al destino puede faltarle, y
        # eso `_items_de` lo admite a propósito con exigir=False).
        resuelto = _registros(ctx, origen, plugin, coleccion)
        if isinstance(resuelto, ToolResult):
            return resuelto
        plugin, coleccion = resuelto
        lectura_origen = _items_de(ctx, origen, plugin, coleccion, exigir=True)
        if isinstance(lectura_origen, ToolResult):
            return lectura_origen
        lectura_destino = _items_de(ctx, destino, plugin, coleccion, exigir=False)
        if isinstance(lectura_destino, ToolResult):
            return lectura_destino
        datos_origen, secretos, _ = lectura_origen
        datos_destino, secretos_destino, clave_destino = lectura_destino
        # Sin campo clave: el destino no tiene la colección (ver `_items_de`).
        destino_sin_coleccion = not clave_destino
        # La unión: si un lado declara un campo secreto que el otro no, igual no
        # se puede comparar. Pasa con dos versiones distintas del mismo plugin.
        secretos = sorted(set(secretos) | set(secretos_destino))
        comparados = sorted(
            {c for item in (*datos_origen.values(), *datos_destino.values()) for c in item} - set(secretos)
        )

    items, resumen = _diferencias(datos_origen, datos_destino, secretos, bool(ctx.params.get("detalle")))
    por_estado = {
        estado: [i["clave"] for i in items if i["estado"] == estado]
        for estado in ("igual", "distinto", "solo_origen", "solo_destino", "indeterminado")
    }

    diff = {
        "origen": {"bot": origen["nombre"], "url": origen["url"]},
        "destino": {"bot": destino["nombre"], "url": destino["url"]},
        "que": que,
        "coleccion": f"{plugin}/{coleccion}" if que == "registros" else "",
        # Siempre presente, como `campos_secretos`: quien lo renderiza no tiene
        # que distinguir "no pasó" de "no me lo dijeron". True = el destino no
        # tiene ese plugin/colección, así que el vacío de allá no significa
        # "colección vacía" sino "ni siquiera la tiene".
        "destino_sin_coleccion": destino_sin_coleccion,
        "campos_comparados": comparados,
        "campos_secretos": secretos,
        "items": items,
        "resumen": resumen,
    }
    hay = resumen["distinto"] + resumen["solo_origen"] + resumen["solo_destino"]
    ctx.log(
        f"{origen['nombre']} vs {destino['nombre']} ({que}): {resumen['igual']} iguales, "
        f"{resumen['distinto']} distintos, {resumen['solo_origen']} sólo acá, "
        f"{resumen['solo_destino']} sólo allá, {resumen['indeterminado']} indeterminados"
    )
    return ToolResult.ok(
        diff=diff,
        hay_diferencias="si" if hay else "no",
        distintos=por_estado["distinto"],
        solo_origen=por_estado["solo_origen"],
        solo_destino=por_estado["solo_destino"],
        iguales=por_estado["igual"],
        indeterminados=por_estado["indeterminado"],
    )


# ── Action: comparar, con la tabla que la app dibuja ─────────────────────
#
# La misma comparación que el tool, pero para una persona mirando la pantalla.
# `vista` va acá y no en los outputs del tool a propósito: un tool es un nodo
# de un flujo, y una vista adentro de sus outputs viajaría en el contexto de
# cada corrida y se guardaría en la base con cada una, sin que ningún flujo la
# lea. Las dos salen de `_comparar`, así que no pueden divergir.

# Lo que se puede empujar al destino. `igual` no —no hay nada que mover— y
# `solo_destino` tampoco: no existe acá, y migrar no borra del otro lado.
_ESTADOS_MIGRABLES = frozenset({"distinto", "solo_origen", "indeterminado"})

# Para la columna Estado. En la tabla van leídos y no con el nombre interno:
# `solo_destino` al lado de `igual` se ve como un dato de máquina metido entre
# palabras. Los valores crudos siguen estando en `diff.items` y en las listas
# planas del tool, que es lo que lee un flujo — acá es presentación.
_ETIQUETAS_ESTADO = {
    "igual": "igual",
    "distinto": "distinto",
    "solo_origen": "sólo en este Bot",
    "solo_destino": "sólo en el destino",
    "indeterminado": "indeterminado",
}

_NOTAS = {
    # Dice qué va a pasar, no sólo qué no se puede saber: con los secretos
    # afuera el item se migra igual y el otro Bot conserva el suyo, así que
    # "no se puede comparar" sin esa mitad se lee como "no se puede migrar".
    "indeterminado": (
        "tiene campos secretos: no se puede saber si difieren. Migrarlo no los toca — "
        "el otro Bot conserva los suyos"
    ),
    "solo_destino": "está sólo en el destino; migrar no lo borra de allá",
}


def _vista_de(diff: dict, destino_bot: str) -> dict:
    """
    El informe como tabla, con la convención que la app dibuja.

    Las columnas salen de lo comparado y no de una lista fija porque cambian
    por colección — es justo el motivo por el que esto va en el resultado y no
    en el manifest.

    Una colección con campos secretos se puede elegir igual: con
    `incluir_secretos` en false el item viaja sin ellos y el destino conserva
    los suyos, así que tildar una fila **sí** hace algo —corrige lo comparable
    y deja el secreto de la otra punta donde está—. El botón manda
    `incluir_secretos: false`; para pisarlos hay que ir a la acción `migrar`,
    que tiene la casilla.
    """
    es_flujo = diff["que"] == "flujos"

    filas = []
    for item in diff["items"]:
        fila = {
            "clave": item["clave"],
            "estado": _ETIQUETAS_ESTADO.get(item["estado"], item["estado"]),
            "campos": ", ".join(item["campos"]),
        }
        if item["estado"] not in _ESTADOS_MIGRABLES:
            fila["_elegible"] = False
        nota = _NOTAS.get(item["estado"], "")
        if nota:
            fila["_nota"] = nota
        filas.append(fila)

    plugin, _, coleccion = diff["coleccion"].partition("/")
    aviso = "Migrar escribe en el otro Bot y pisa lo que haya con ese nombre. No borra nada de allá que no esté acá."
    if diff["campos_secretos"]:
        aviso += (
            f" Los campos secretos ({', '.join(diff['campos_secretos'])}) no se migran: "
            "el otro Bot conserva los suyos. Para pisarlos, la acción 'Migrar' con "
            "'incluir secretos' marcado."
        )

    return {
        "tipo": "tabla",
        "clave": "clave",
        "columnas": [
            {"campo": "estado", "label": "Estado"},
            {"campo": "clave", "label": "Flujo" if es_flujo else "Nombre"},
            {"campo": "campos", "label": "Qué difiere"},
        ],
        "filas": filas,
        "seleccion": {
            "accion": "migrar",
            "param": "claves",
            "params": {
                "destino": destino_bot,
                "que": diff["que"],
                "plugin": plugin,
                "coleccion": coleccion,
                "incluir_secretos": False,
            },
            "etiqueta": f"Migrar lo elegido a {destino_bot}",
            "aviso": aviso,
        },
    }


def _comparar_accion(ctx: ToolContext) -> ToolResult:
    resultado = _comparar(ctx)
    if resultado.failed:
        return resultado
    diff = resultado.outputs["diff"]
    vista = _vista_de(diff, diff["destino"]["bot"])
    return ToolResult.ok(
        resultado.message, vista=vista, **resultado.outputs,
    )


# ── Action: migrar lo elegido ───────────────────────────────────────────

def _migrar(ctx: ToolContext) -> ToolResult:
    """
    Le pide a **este** Bot que empuje al otro. El plugin no toca un secreto.

    Quien lee un secreto en claro es la app, en su propio `POST /migrar`: el
    plugin no puede —`storage` y `crypto` no están en `PLUGIN_PORTS`— y es
    justamente la razón por la que esto es un pedido y no una implementación.

    Lo de los secretos se decide acá y no en el endpoint porque el endpoint no
    tiene con qué: su cuerpo es `{destino, que, plugin, coleccion, claves}` y
    manda el item completo. Así que la única palanca es **qué claves se
    mandan**, y como los campos `secret` se declaran por colección y no por
    item, la regla que queda es simple: una colección con secretos se migra
    entera o no se migra.
    """
    destino = _bot(ctx, ctx.params["destino"])
    if isinstance(destino, ToolResult):
        return destino
    yo = _este_bot(ctx)
    if isinstance(yo, ToolResult):
        return yo
    if problema := _no_es_este_bot(yo["url"]):
        # Acá sí corta, al revés que en `comparar`: esto escribe. Con la
        # dirección de otra instalación, migrar empuja el contenido de ESA al
        # destino y le pisa lo suyo, sin que nada se vea mal en el camino.
        return ToolResult.err(
            f"no se migró nada: {problema}. Migrar desde acá empujaría el contenido de esa otra "
            f"instalación a '{destino['nombre']}' y le pisaría lo suyo. Corregí la configuración "
            f"(va http://127.0.0.1:{_mi_puerto()}) o dejala vacía, que ya resuelve sola."
        )

    claves = ctx.params["claves"]
    if isinstance(claves, str):
        claves = [c.strip() for c in claves.split(",") if c.strip()]
    if not claves:
        return ToolResult.err("no se eligió nada para migrar")

    que = ctx.params["que"]
    plugin = str(ctx.params["plugin"]).strip()
    coleccion = str(ctx.params["coleccion"]).strip()
    incluir_secretos = bool(ctx.params["incluir_secretos"])

    if que == "registros":
        if not (plugin and coleccion):
            return ToolResult.err("con que=registros hacen falta 'plugin' y 'coleccion'")
        resuelto = _registros(ctx, yo, plugin, coleccion)
        if isinstance(resuelto, ToolResult):
            return resuelto
        plugin, coleccion = resuelto

    # Sin chequeo previo de secretos a propósito. La app resuelve el caso mejor
    # de lo que podría acá: con `incluir_secretos` en false, un item de
    # colección **viaja igual** con sus campos secretos en `None` y el destino
    # conserva los suyos —se puede corregir la url de una conexión sin pisarle
    # el token al otro Bot—, y de `env` omite sólo las variables marcadas
    # secretas, nombrando cada una en el informe. Cortar acá por "tiene
    # secretos" bloquearía las dos cosas, incluida una variable `env` no
    # secreta, que se migra sin problema.
    cuerpo = {
        "destino": destino["url"], "que": que,
        "plugin": plugin, "coleccion": coleccion, "claves": list(claves),
        "incluir_secretos": incluir_secretos,
    }
    try:
        respuesta, datos = _pedir(ctx, yo["url"], "/migrar", method="POST", payload=cuerpo)
    except PortError as exc:
        return ToolResult.err(f"este Bot no contestó en {yo['url']}: {exc}")
    if not isinstance(datos, dict) or not respuesta.ok:
        detalle = datos.get("detail") if isinstance(datos, dict) else respuesta.text[:300]
        if isinstance(detalle, dict):
            detalle = detalle.get("message") or json.dumps(detalle, ensure_ascii=False)
        if respuesta.status == 404:
            detalle = (
                "este Bot tiene una versión de la app que no sabe migrar; "
                "actualizalo desde Config → Actualizaciones"
            )
        elif respuesta.status == 403 and not _es_loopback(yo["url"]):
            # El 403 de la app habla de emparejar desde la propia máquina, que
            # acá manda a mirar el lugar equivocado: lo que está mal es este
            # setting, no el emparejamiento.
            detalle = (
                f"la configuración '{MI_DIRECCION}' apunta a {yo['url']}, que no es la propia "
                "máquina. Migrar sólo se puede pedir desde el mismo Bot, así que ahí va "
                "http://127.0.0.1:<puerto>; la dirección de red es para que OTROS Bots lleguen a éste"
            )
        return ToolResult.err(f"no se pudo migrar a '{destino['nombre']}': {detalle or respuesta.status}")

    migrados = datos.get("migrados") or 0
    fallados = datos.get("fallados") or 0
    resultados = datos.get("resultados") or []
    for r in resultados:
        if isinstance(r, dict) and not r.get("ok"):
            ctx.log(f"{r.get('clave')}: {r.get('error') or 'falló'}", level="warning")

    # Una tabla también acá: el resultado de esto es "qué entró y qué no", y de
    # los que no entraron importa el motivo. Sin `seleccion`, porque no hay un
    # paso siguiente — es el final del camino, no una elección.
    vista = {
        "tipo": "tabla",
        "clave": "clave",
        "columnas": [
            {"campo": "clave", "label": "Nombre"},
            {"campo": "resultado", "label": "Resultado"},
        ],
        "filas": [
            {
                "clave": r.get("clave", ""),
                "resultado": "migrado" if r.get("ok") else (r.get("error") or "no se pudo migrar"),
            }
            for r in resultados if isinstance(r, dict)
        ],
    }
    salida = dict(
        migrados=migrados, fallados=fallados, resultados=resultados,
        destino=destino["nombre"], vista=vista,
    )
    if fallados:
        return ToolResult.err(f"{fallados} de {migrados + fallados} no se migraron a '{destino['nombre']}'", **salida)
    return ToolResult.ok(f"{migrados} migrado(s) a '{destino['nombre']}'", **salida)


# ── Action: probar un Bot conocido ────────────────────────────────────────


def _probar(ctx: ToolContext) -> ToolResult:
    """
    Además del resumen, deja `indicador`: lo que la app guarda por fila para
    dibujar el check verde (o la cruz) sin volver a apretar Probar. Va también
    en el err, con estado "err", para que un check viejo se apague en cuanto
    el otro Bot deja de responder — si sólo viniera en el ok, la fila seguiría
    diciendo "conectado" de la última vez que anduvo.
    """
    bot = _bot(ctx, ctx.params["nombre"])
    if isinstance(bot, ToolResult):
        return _con_indicador(bot)
    estado = _estado_de(ctx, bot)
    if isinstance(estado, ToolResult):
        return _con_indicador(estado)
    mensaje = f"responde · {estado['corriendo']} en vuelo · último run {estado['ultimo_estado'] or 'ninguno'}"
    return ToolResult.ok(
        mensaje,
        indicador={"estado": "ok", "texto": mensaje},
        **{k: v for k, v in estado.items() if k != "url"},
    )


def _con_indicador(fallo: ToolResult) -> ToolResult:
    fallo.outputs["indicador"] = {"estado": "err", "texto": fallo.message}
    return fallo


# ── Action: abrir un Bot conocido en el navegador ─────────────────────────


def _abrir(ctx: ToolContext) -> ToolResult:
    """
    No abre nada: el plugin corre en el servidor y la pestaña es del
    navegador. Devuelve `abrir_url` y la app hace el `window.open`.

    No sondea al otro Bot antes: para ver si responde está Probar, y abrir la
    pestaña de uno caído también sirve —el navegador dice qué pasa—. Sí valida
    la dirección, para no abrir una pestaña con `ftp://` o con una ruta.

    `?bot=<nombre>` lleva el nombre con que ESTE Bot lo conoce: la app del
    otro titula su pestaña con su propia identidad (`/api/core/identidad`) y,
    si todavía no se puso nombre, con este alias.
    """
    bot = _bot(ctx, ctx.params["nombre"])
    if isinstance(bot, ToolResult):
        return bot
    destino = f"{bot['url']}/?bot={quote(bot['nombre'], safe='')}"
    return ToolResult.ok(f"Abrir {bot['nombre']} en {bot['url']}", abrir_url=destino, bot=bot["nombre"])


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[
            FunctionTool(manifest=ESTADO, fn=_estado),
            FunctionTool(manifest=ELEGIR_LIBRE, fn=_elegir_libre),
            FunctionTool(manifest=CORRER, fn=_correr),
            FunctionTool(manifest=ESPERAR, fn=_esperar),
            FunctionTool(manifest=COMPARAR, fn=_comparar),
        ],
        actions=[
            FunctionAction(action=MANIFEST.action("probar"), fn=_probar),
            FunctionAction(action=MANIFEST.action("abrir"), fn=_abrir),
            FunctionAction(action=COMPARAR_ACCION, fn=_comparar_accion),
            FunctionAction(action=MIGRAR_ACCION, fn=_migrar),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
