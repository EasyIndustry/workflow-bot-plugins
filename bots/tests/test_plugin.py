"""
Tests del plugin `bots`, sin red ni tiempo real: el otro Bot es un `FakeHttp`
guionado con las respuestas de su API, y el reloj es un `FakeClock`.

Necesita el núcleo (`backend/`) del repo de la app: se busca en la variable
`WORKFLOW_BOT_APP` o en `../workflow-bot-app`, al lado de este catálogo.
Corre con `python -m pytest bots` desde la raíz del catálogo.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from urllib.parse import quote

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.ports import HttpResponse, PortError  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeClock, FakeHttp  # noqa: E402

from bots.plugin import build_plugin  # noqa: E402

BOTS = [
    {"nombre": "Estación 2", "url": "http://10.0.0.41:8000", "nota": ""},
    {"nombre": "Estación 3", "url": "http://10.0.0.42:8000/", "nota": ""},
    {"nombre": "Roto", "url": "ftp://nada", "nota": ""},
]
API2 = "http://10.0.0.41:8000/api/core"
API3 = "http://10.0.0.42:8000/api/core"


def _cuerpo(http, camino="/migrar"):
    """El body del request a `camino`. Por índice no sirve: con que=registros
    `migrar` y `comparar` piden antes `/tools` para traducir los nombres."""
    return json.loads(next(c for c in http.calls if c["url"].endswith(camino))["body"])


def _json(datos, status=200):
    return HttpResponse(status=status, text=json.dumps(datos), headers={"content-type": "application/json"})


def _correr(tool, params, http, clock=None, case_id="PADRE-1", config=None):
    reg = ToolRegistry(adapters={"http": http, "clock": clock or FakeClock()})
    plugin = build_plugin()
    reg._add_plugin("bots", "bots:PLUGIN", plugin)
    registro = []

    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params(params, {})
        return ToolContext(
            run_id="run-test", case_id=case_id, params=declarados, extras=extras,
            config=dict(config or {}), context={},
            log=lambda m, level="info": registro.append(m),
            ports=ports or {}, resources=lambda coleccion: BOTS if coleccion == "bots" else [],
        )

    return reg.execute(tool, factory), registro


def test_estado_dice_cuantos_corren_si_esta_libre_y_como_termino_el_ultimo():
    http = FakeHttp({
        f"{API2}/runs/en-vuelo": _json([{"case_id": "X", "hechos": 3, "total": 10, "paso": "Exportar"}]),
        f"{API2}/runs?limit=1": _json([{"status": "ok", "flow": "v3", "case_id": "AB123"}]),
    })
    r, _ = _correr("bots.estado", {"bot": "Estación 2"}, http)
    assert r.status == "ok"
    assert (r.outputs["corriendo"], r.outputs["libre"]) == (1, "no")
    assert (r.outputs["ultimo_estado"], r.outputs["ultimo_flujo"], r.outputs["ultimo_caso"]) == ("ok", "v3", "AB123")


def test_un_bot_desconocido_o_con_direccion_rara_es_err_antes_de_ir_a_la_red():
    http = FakeHttp()
    r, _ = _correr("bots.estado", {"bot": "Ninguno"}, http)
    assert r.status == "err" and "Conocidos: Estación 2, Estación 3, Roto" in r.message
    r, _ = _correr("bots.estado", {"bot": "Roto"}, http)
    assert r.status == "err" and "http://ip:puerto" in r.message
    assert http.calls == []


def test_si_no_responde_es_err_con_la_direccion():
    http = FakeHttp({f"{API2}/runs/en-vuelo": PortError("connection refused")})
    r, _ = _correr("bots.estado", {"bot": "Estación 2"}, http)
    assert r.status == "err" and "10.0.0.41" in r.message


def test_elegir_libre_toma_el_que_menos_tiene_y_saltea_al_caido():
    http = FakeHttp({
        f"{API2}/runs/en-vuelo": _json([{"case_id": "A"}, {"case_id": "B"}]),
        f"{API2}/runs?limit=1": _json([]),
        f"{API3}/runs/en-vuelo": _json([]),
        f"{API3}/runs?limit=1": _json([]),
    })
    r, _ = _correr("bots.elegir_libre", {"bots": "Estación 2, Estación 3"}, http)
    assert r.status == "ok" and r.outputs["bot"] == "Estación 3" and r.outputs["corriendo"] == 0

    http = FakeHttp({
        f"{API2}/runs/en-vuelo": PortError("timeout"),
        f"{API3}/runs/en-vuelo": _json([{"case_id": "A"}]),
        f"{API3}/runs?limit=1": _json([]),
    })
    r, _ = _correr("bots.elegir_libre", {"bots": "Estación 2, Estación 3"}, http)
    assert r.status == "ok" and r.outputs["bot"] == "Estación 3" and r.outputs["caidos"] == ["Estación 2"]

    http = FakeHttp({f"{API2}/runs/en-vuelo": PortError("x"), f"{API3}/runs/en-vuelo": PortError("y")})
    r, _ = _correr("bots.elegir_libre", {"bots": "Estación 2, Estación 3"}, http)
    assert r.status == "err" and "ningún Bot respondió" in r.message


def test_correr_pide_sin_esperar_y_devuelve_el_ticket():
    http = FakeHttp({f"{API2}/runs": _json({"ticket": "abc123def456", "estado": "en_cola"})})
    r, _ = _correr("bots.correr", {"bot": "Estación 2", "flujo": "EXPORTAR V3", "case_id": "AB123"}, http)
    assert r.status == "ok" and r.outputs["ticket"] == "abc123def456"
    [llamada] = http.calls
    assert llamada["method"] == "POST" and llamada["url"] == f"{API2}/runs"
    cuerpo = json.loads(llamada["body"])
    # Sin row, el hijo recibe lo que un flujo que empieza por "refrescar row" necesita.
    assert cuerpo["row"] == {"case_id": "AB123"} and cuerpo["flow"] == "EXPORTAR V3"
    assert cuerpo["source"] == "bot:PADRE-1"


def test_correr_con_flujo_inexistente_en_el_hijo_es_err_con_su_mensaje():
    http = FakeHttp({f"{API2}/runs": _json({"detail": 'No existe el flujo "nada"'}, status=404)})
    r, _ = _correr("bots.correr", {"bot": "Estación 2", "flujo": "nada", "case_id": "1"}, http)
    assert r.status == "err" and "404" in r.message and "No existe el flujo" in r.message


def test_esperar_sondea_anota_el_paso_del_hijo_y_termina_con_su_resultado():
    class Hijo:
        """Un FakeHttp que cambia de respuesta en cada consulta al ticket."""

        def __init__(self):
            self.respuestas = iter([
                {"estado": "en_cola", "vivo": {"hechos": 0, "total": 10, "paso": "", "en_cola": True}},
                {"estado": "en_vuelo", "vivo": {"hechos": 3, "total": 10, "paso": "Exportar"}},
                {"estado": "en_vuelo", "vivo": {"hechos": 3, "total": 10, "paso": "Exportar"}},
                {"estado": "terminado", "run": {"status": "ok", "run_id": "run-77", "message": ""}},
            ])
            self.calls = []

        def request(self, url, **kw):
            self.calls.append(url)
            return _json(next(self.respuestas))

    reloj = FakeClock()
    r, log = _correr("bots.esperar", {"bot": "Estación 2", "ticket": "t1", "cada": 5}, Hijo(), reloj)
    assert r.status == "ok"
    assert (r.outputs["estado_hijo"], r.outputs["run_id"]) == ("ok", "run-77")
    assert reloj.slept == [5, 5, 5]
    # El paso se anota cuando cambia, no en cada sondeo.
    assert [l for l in log if "Estación 2:" in l] == ["Estación 2: 0/10 · en cola", "Estación 2: 3/10 · Exportar"]


def test_esperar_err_si_el_hijo_termina_err_o_si_vence_el_timeout():
    http = FakeHttp({f"{API2}/runs/ticket/t2": _json({"estado": "terminado", "run": {"status": "err", "run_id": "run-8", "message": "Faltan archivos"}})})
    r, _ = _correr("bots.esperar", {"bot": "Estación 2", "ticket": "t2"}, http)
    assert r.status == "err" and "Faltan archivos" in r.message and r.outputs["estado_hijo"] == "err"

    http = FakeHttp({f"{API2}/runs/ticket/t3": _json({"estado": "en_vuelo", "vivo": {"hechos": 1, "total": 4, "paso": "x"}})})
    r, _ = _correr("bots.esperar", {"bot": "Estación 2", "ticket": "t3", "timeout": 12, "cada": 5}, http, FakeClock())
    assert r.status == "err" and "sigue corriendo" in r.message and r.outputs["estado_hijo"] == "en_vuelo"

    http = FakeHttp({f"{API2}/runs/ticket/t4": _json({"estado": "desconocido"})})
    r, _ = _correr("bots.esperar", {"bot": "Estación 2", "ticket": "t4"}, http)
    assert r.status == "err" and "no conoce el ticket" in r.message


def test_la_accion_probar_resume_el_estado():
    http = FakeHttp({f"{API2}/runs/en-vuelo": _json([]), f"{API2}/runs?limit=1": _json([{"status": "ok"}])})
    reg = ToolRegistry(adapters={"http": http, "clock": FakeClock()})
    reg._add_plugin("bots", "bots:PLUGIN", build_plugin())

    def factory(accion, ports=None):
        return ToolContext(
            run_id="", case_id="", params={"nombre": "Estación 2"}, extras={}, config={}, context={},
            log=lambda *a, **k: None, ports=ports or {}, resources=lambda c: BOTS,
        )

    r = reg.execute_action("bots", "probar", factory)
    assert r.status == "ok" and r.message == "responde · 0 en vuelo · último run ok"


def test_correr_sin_row_usa_campo_id_como_nombre_de_la_columna():
    # Cada fuente llama distinto a la columna que identifica el caso; el hijo
    # la necesita con SU nombre para que "refrescar row" la encuentre.
    http = FakeHttp({f"{API2}/runs": _json({"ticket": "t1", "estado": "en_cola"})})
    r, _ = _correr("bots.correr", {"bot": "Estación 2", "flujo": "f", "case_id": "AB123", "campo_id": "codigo"}, http)
    assert r.status == "ok"
    assert json.loads(http.calls[0]["body"])["row"] == {"codigo": "AB123"}

def test_correr_recibe_row_como_objeto_de_verdad():
    """
    `row={variable}` llega como dict, no como texto.

    Este test afirmaba lo contrario —que llegaba como `str(dict)` de Python, con
    comillas simples, y que el plugin lo parseaba igual— porque así era cuando se
    escribió. El núcleo lo arregló en la raíz (core#21): `resolve()` pasa la
    lista o el dict tal cual, y `_coerce(JSON)` rechaza con un mensaje claro
    cualquier texto que no sea JSON válido. O sea que el caso que este test
    cubría ya no existe: hoy ese string ni llega al plugin.

    `_como_objeto` se queda igual: `compatible_core` admite desde 0.3.0b4 y el
    arreglo entró en 0.3.1-beta.2, así que un Bot con un núcleo anterior todavía
    le pasa el texto.
    """
    http = FakeHttp({f"{API2}/runs": _json({"ticket": "t9", "estado": "en_cola"})})
    fila = {"codigo": "AB123", "carpeta": r"C:\casos\AB123\datos"}
    r, _ = _correr("bots.correr", {"bot": "Estación 2", "flujo": "f", "case_id": "AB123", "row": fila}, http)
    assert r.status == "ok"
    assert json.loads(http.calls[0]["body"])["row"] == fila

    # Un texto que no es JSON lo corta el núcleo, antes de llegar al tool.
    r, _ = _correr("bots.correr", {"bot": "Estación 2", "flujo": "f", "case_id": "1", "row": "no es un objeto"}, FakeHttp())
    assert r.status == "err" and "no es JSON válido" in r.message


# ── comparar: el diff entre dos Bots ─────────────────────────────────────
#
# La forma de la salida es un contrato compartido con la pantalla de migración
# de la app (workflow-bot-app), así que estos tests la fijan a propósito: si
# cambia, la pantalla y este tool dejan de decir lo mismo.

YO = "http://127.0.0.1:8000/api/core"
CONFIG_YO = {"botsMiDireccion": "http://127.0.0.1:8000"}


def _flujo(nombre, content, folder="", state="enabled", description=""):
    return {"name": nombre, "content": content, "folder": folder, "state": state,
            "description": description, "updated_at": 1_700_000_000.0}


def _http_flujos(aca: dict, alla: dict) -> FakeHttp:
    """
    Dos Bots con sus flujos: {nombre: contenido} de cada lado.

    Las URL de un flujo van con el nombre escapado porque así las pide el tool:
    un nombre con espacios ("PROCESAR watch") sin escapar no es una URL válida.
    Scriptearlas con el espacio literal no fallaba de frente, que es lo peor:
    `FakeHttp` cae al match por prefijo, devolvía la LISTA de flujos para el GET
    de uno solo, y el flujo se salteaba en silencio.
    """
    respuestas = {
        f"{YO}/workflows": _json([_flujo(n, c) for n, c in aca.items()]),
        f"{API2}/workflows": _json([_flujo(n, c) for n, c in alla.items()]),
    }
    for nombre, contenido in aca.items():
        respuestas[f"{YO}/workflows/{quote(nombre, safe='')}"] = _json(_flujo(nombre, contenido))
    for nombre, contenido in alla.items():
        respuestas[f"{API2}/workflows/{quote(nombre, safe='')}"] = _json(_flujo(nombre, contenido))
    return FakeHttp(respuestas)


def _comparar(http, **params):
    return _correr("bots.comparar", {"destino": "Estación 2", **params}, http, config=CONFIG_YO)


def test_comparar_flujos_clasifica_los_cuatro_estados():
    http = _http_flujos(
        aca={"igual": "A", "cambiado": "aca", "solo mio": "M"},
        alla={"igual": "A", "cambiado": "alla", "solo suyo": "S"},
    )
    r, _ = _comparar(http)

    assert r.status == "ok"
    assert r.outputs["iguales"] == ["igual"]
    assert r.outputs["distintos"] == ["cambiado"]
    assert r.outputs["solo_origen"] == ["solo mio"]
    assert r.outputs["solo_destino"] == ["solo suyo"]
    assert r.outputs["hay_diferencias"] == "si"
    assert r.outputs["diff"]["resumen"] == {
        "igual": 1, "distinto": 1, "solo_origen": 1, "solo_destino": 1, "indeterminado": 0,
    }


def test_comparar_flujos_dice_que_campo_difiere_y_no_mira_updated_at():
    # Mismo contenido, distinta carpeta: difiere 'folder' y nada más. Y aunque
    # los dos lados traen updated_at, no cuenta: difiere siempre y no dice nada.
    http = FakeHttp({
        f"{YO}/workflows": _json([_flujo("F", "igual", folder="CAM")]),
        f"{YO}/workflows/F": _json(_flujo("F", "igual", folder="CAM")),
        f"{API2}/workflows": _json([_flujo("F", "igual", folder="FORM")]),
        f"{API2}/workflows/F": _json(_flujo("F", "igual", folder="FORM")),
    })
    r, _ = _comparar(http)

    assert r.status == "ok"
    item = r.outputs["diff"]["items"][0]
    assert (item["clave"], item["estado"], item["campos"]) == ("F", "distinto", ["folder"])
    assert "updated_at" not in r.outputs["diff"]["campos_comparados"]


def test_comparar_sin_diferencias_deja_hay_diferencias_en_no():
    http = _http_flujos(aca={"F": "A"}, alla={"F": "A"})
    r, _ = _comparar(http)

    assert r.outputs["hay_diferencias"] == "no"
    assert r.outputs["diff"]["items"] == [{"clave": "F", "estado": "igual", "campos": []}]


def test_comparar_con_detalle_trae_los_valores_de_los_dos_lados():
    http = _http_flujos(aca={"F": "version de aca"}, alla={"F": "version de alla"})
    r, _ = _comparar(http, detalle=True)

    item = r.outputs["diff"]["items"][0]
    assert item["origen"] == {"content": "version de aca"}
    assert item["destino"] == {"content": "version de alla"}


def test_comparar_sin_detalle_no_manda_el_contenido_de_los_flujos():
    # El content de un flujo son miles de caracteres: por N flujos, el output
    # del nodo se vuelve inmanejable. Va sólo si se pide.
    http = _http_flujos(aca={"F": "un contenido largo"}, alla={"F": "otro"})
    r, _ = _comparar(http)

    assert "origen" not in r.outputs["diff"]["items"][0]
    assert "un contenido largo" not in json.dumps(r.outputs["diff"], ensure_ascii=False)


def _http_items(aca: list, alla: list, campos=None) -> FakeHttp:
    definicion = {
        "key_field": "nombre",
        "fields": campos or [{"name": "nombre", "secret": False}, {"name": "patron", "secret": False}],
    }
    camino = "/resources/plantillero/plantillas"
    return FakeHttp({
        f"{YO}{camino}": _json({"resource": definicion, "items": aca}),
        f"{API2}{camino}": _json({"resource": definicion, "items": alla}),
    })


def test_comparar_registros_usa_el_key_field_de_la_definicion_y_saca_lo_del_nucleo():
    http = _http_items(
        aca=[{"nombre": "plantilla_a", "patron": "^A", "_updated_at": 1.0},
             {"nombre": "solo mia", "patron": "^X", "_updated_at": 2.0}],
        alla=[{"nombre": "plantilla_a", "patron": "^B", "_updated_at": 999.0}],
    )
    r, _ = _comparar(http, que="registros", plugin="plantillero", coleccion="plantillas")

    assert r.status == "ok"
    assert r.outputs["distintos"] == ["plantilla_a"]
    assert r.outputs["solo_origen"] == ["solo mia"]
    # _updated_at es del núcleo, no del plugin: difiere y no cuenta.
    assert r.outputs["diff"]["items"][0]["campos"] == ["patron"]
    assert r.outputs["diff"]["coleccion"] == "plantillero/plantillas"


def test_comparar_registros_con_un_campo_secreto_es_indeterminado_no_igual():
    # Los campos visibles coinciden, pero la colección tiene un secreto que la
    # API no devuelve: podría diferir justo ahí. Decir "igual" sería mentir.
    campos = [{"name": "nombre", "secret": False}, {"name": "token", "secret": True}]
    http = _http_items(
        aca=[{"nombre": "api", "token": None}],
        alla=[{"nombre": "api", "token": None}],
        campos=campos,
    )
    r, _ = _comparar(http, que="registros", plugin="plantillero", coleccion="plantillas")

    assert r.outputs["indeterminados"] == ["api"]
    assert r.outputs["iguales"] == []
    assert r.outputs["diff"]["campos_secretos"] == ["token"]
    assert "token" not in r.outputs["diff"]["campos_comparados"]


def test_comparar_registros_sin_plugin_ni_coleccion_es_err():
    r, _ = _comparar(FakeHttp(), que="registros")
    assert r.status == "err" and "'plugin' y 'coleccion'" in r.message


def test_comparar_contra_un_bot_sin_ese_plugin_no_falla_es_todo_solo_origen():
    # Un Bot nuevo de la flota no tiene nada instalado. Que le falte el plugin
    # es la respuesta —"no tiene ninguno de estos"—, no una falla: un tool que
    # se cae por lo que allá no está rompe justo por lo que no está.
    camino = "/resources/plantillero/plantillas"
    definicion = {"key_field": "nombre", "fields": [{"name": "nombre", "secret": False}]}
    http = FakeHttp({
        f"{YO}{camino}": _json({"resource": definicion, "items": [{"nombre": "plantilla_a"}, {"nombre": "plantilla_b"}]}),
        f"{API2}{camino}": _json({"detail": "no hay una coleccion plantillas"}, status=404),
    })
    r, registro = _comparar(http, que="registros", plugin="plantillero", coleccion="plantillas")

    assert r.status == "ok"
    assert r.outputs["solo_origen"] == ["plantilla_a", "plantilla_b"]
    assert r.outputs["hay_diferencias"] == "si"
    # Y se distingue de "la tiene pero está vacía", que significa otra cosa.
    assert r.outputs["diff"]["destino_sin_coleccion"] is True
    assert any("no tiene la colección" in m for m in registro)


def test_comparar_una_coleccion_vacia_en_destino_no_es_lo_mismo_que_no_tenerla():
    camino = "/resources/plantillero/plantillas"
    definicion = {"key_field": "nombre", "fields": [{"name": "nombre", "secret": False}]}
    http = FakeHttp({
        f"{YO}{camino}": _json({"resource": definicion, "items": [{"nombre": "plantilla_a"}]}),
        f"{API2}{camino}": _json({"resource": definicion, "items": []}),
    })
    r, _ = _comparar(http, que="registros", plugin="plantillero", coleccion="plantillas")

    assert r.status == "ok"
    assert r.outputs["solo_origen"] == ["plantilla_a"]
    assert r.outputs["diff"]["destino_sin_coleccion"] is False


def test_comparar_una_coleccion_que_este_bot_no_tiene_si_es_err():
    # Del lado del origen sí corta: casi siempre es un nombre mal escrito, y no
    # hay nada que comparar *desde*. Un informe vacío parecería una respuesta.
    camino = "/resources/plantillero/platillas"
    http = FakeHttp({f"{YO}{camino}": _json({"detail": "no existe"}, status=404)})
    r, _ = _comparar(http, que="registros", plugin="plantillero", coleccion="platillas")

    assert r.status == "err"
    assert "plantillero" in r.message and "platillas" in r.message


def test_comparar_el_mismo_bot_de_los_dos_lados_es_err_antes_de_ir_a_la_red():
    http = FakeHttp()
    r, _ = _correr(
        "bots.comparar", {"destino": "Estación 2", "origen": "Estación 2"}, http, config=CONFIG_YO,
    )
    assert r.status == "err" and "el mismo Bot" in r.message
    assert http.calls == []


def test_comparar_entre_dos_otros_bots_no_pasa_por_este():
    # origen puede ser otro Bot: un Bot de control que compara dos de la flota.
    http = FakeHttp({
        f"{API3}/workflows": _json([_flujo("F", "tres")]),
        f"{API3}/workflows/F": _json(_flujo("F", "tres")),
        f"{API2}/workflows": _json([_flujo("F", "dos")]),
        f"{API2}/workflows/F": _json(_flujo("F", "dos")),
    })
    r, _ = _comparar(http, origen="Estación 3")

    assert r.status == "ok"
    assert r.outputs["distintos"] == ["F"]
    assert r.outputs["diff"]["origen"]["bot"] == "Estación 3"
    assert not any(c["url"].startswith("http://127.0.0.1") for c in http.calls)


def test_comparar_un_bot_que_no_responde_es_err():
    r, _ = _comparar(FakeHttp())
    assert r.status == "err"


# ── Actions: la tabla que dibuja la app, y migrar ────────────────────────
#
# `vista` va en la Action y no en los outputs del tool: un tool es un nodo de
# un flujo, y la vista viajaría en el contexto de cada corrida sin que ningún
# flujo la lea. Las dos salen de la misma función, así que no pueden divergir.


def _accion(nombre, params, http, config=None):
    """
    Corre una Action llenando los params como lo hace el núcleo.

    Usa `Param.read_from`, que además resuelve los alias — es lo que permite
    que la Action `comparar` reciba el `nombre` del item de la colección y lo
    lea como su param `destino`.
    """
    reg = ToolRegistry(adapters={"http": http, "clock": FakeClock()})
    reg._add_plugin("bots", "bots:PLUGIN", build_plugin())
    registro = []

    def factory(accion, ports=None):
        completos = {}
        for p in accion.params:
            valor = p.read_from(params)
            completos[p.name] = p.default if valor is None else valor
        return ToolContext(
            run_id="", case_id="", params=completos, extras={},
            # `is None` y no `or`: un `{}` explícito es "sin configurar nada",
            # que es justo el caso que se quiere probar.
            config=dict(CONFIG_YO if config is None else config), context={},
            log=lambda m, level="info": registro.append(m),
            ports=ports or {}, resources=lambda c: BOTS if c == "bots" else [],
        )

    return reg.execute_action("bots", nombre, factory), registro


def test_la_accion_comparar_recibe_el_destino_del_item_de_la_coleccion():
    # El botón vive en la fila de "Bots conocidos": el item entrega sus campos
    # por su nombre real ("nombre") y el alias lo lee como 'destino'.
    http = _http_flujos(aca={"F": "A"}, alla={"F": "A"})
    r, _ = _accion("comparar", {"nombre": "Estación 2"}, http)

    assert r.status == "ok"
    assert r.outputs["diff"]["destino"]["bot"] == "Estación 2"


def test_la_accion_comparar_devuelve_la_vista_con_columnas_y_seleccion():
    http = _http_flujos(
        aca={"igual": "A", "cambiado": "aca", "solo mio": "M"},
        alla={"igual": "A", "cambiado": "alla", "solo suyo": "S"},
    )
    r, _ = _accion("comparar", {"destino": "Estación 2"}, http)

    assert r.status == "ok"
    vista = r.outputs["vista"]
    assert vista["tipo"] == "tabla" and vista["clave"] == "clave"
    assert [c["campo"] for c in vista["columnas"]] == ["estado", "clave", "campos"]

    # La columna Estado va leída: `solo_destino` al lado de `igual` se ve como
    # un dato de máquina metido entre palabras. Los valores crudos siguen en
    # `diff.items` y en las listas planas, que es lo que lee un flujo.
    assert {f["estado"] for f in vista["filas"]} == {
        "igual", "distinto", "sólo en este Bot", "sólo en el destino",
    }

    por_clave = {f["clave"]: f for f in vista["filas"]}
    # Elegible sólo lo que se puede empujar: no un igual, no algo que está
    # sólo del otro lado (no existe acá, y migrar no borra de allá).
    assert por_clave["cambiado"].get("_elegible", True) is True
    assert por_clave["solo mio"].get("_elegible", True) is True
    assert por_clave["igual"]["_elegible"] is False
    assert por_clave["solo suyo"]["_elegible"] is False
    assert "sólo en el destino" in por_clave["solo suyo"]["_nota"]

    # Y el botón lleva el contexto, que es lo que la llamada nueva no sabría.
    seleccion = vista["seleccion"]
    assert seleccion["accion"] == "migrar" and seleccion["param"] == "claves"
    assert seleccion["params"] == {
        "destino": "Estación 2", "que": "flujos",
        "plugin": "", "coleccion": "", "incluir_secretos": False,
    }
    assert "pisa" in seleccion["aviso"]


def test_la_vista_de_una_coleccion_con_secretos_deja_elegir_igual():
    # Tildar una fila sí hace algo: el item viaja sin los campos secretos y el
    # destino conserva los suyos, así que se corrige lo comparable sin pisarle
    # el token al otro Bot. El aviso tiene que decir eso, no "no se puede".
    campos = [{"name": "nombre", "secret": False}, {"name": "token", "secret": True}]
    http = _http_items(
        aca=[{"nombre": "api", "token": None}, {"nombre": "otra", "token": None}],
        alla=[{"nombre": "api", "token": None}],
        campos=campos,
    )
    r, _ = _accion(
        "comparar",
        {"destino": "Estación 2", "que": "registros", "plugin": "plantillero", "coleccion": "plantillas"},
        http,
    )

    assert r.status == "ok"
    vista = r.outputs["vista"]
    assert vista["seleccion"]["params"]["incluir_secretos"] is False
    aviso = vista["seleccion"]["aviso"]
    assert "conserva los suyos" in aviso and "token" in aviso

    por_clave = {f["clave"]: f for f in vista["filas"]}
    assert por_clave["otra"].get("_elegible", True) is True  # solo_origen: se puede empujar
    assert por_clave["api"].get("_elegible", True) is True   # indeterminado: también
    # Y la nota dice qué va a pasar, no sólo qué no se sabe.
    assert "no los toca" in por_clave["api"]["_nota"]


def test_migrar_le_pide_a_su_propio_bot_y_manda_la_url_del_destino():
    http = FakeHttp({f"{YO}/migrar": _json({"migrados": 2, "fallados": 0, "resultados": [
        {"clave": "F1", "ok": True}, {"clave": "F2", "ok": True}]})})
    r, _ = _accion("migrar", {"destino": "Estación 2", "claves": ["F1", "F2"]}, http)

    assert r.status == "ok" and r.outputs["migrados"] == 2
    # El endpoint espera la URL del destino, no el nombre del Bot.
    cuerpo = json.loads(http.calls[0]["body"])
    assert cuerpo == {
        "destino": "http://10.0.0.41:8000", "que": "flujos",
        "plugin": "", "coleccion": "", "claves": ["F1", "F2"], "incluir_secretos": False,
    }
    # Y el pedido va a este Bot, que es el único que puede leer un secreto suyo.
    assert http.calls[0]["url"].startswith("http://127.0.0.1:8000")


def test_migrar_informa_los_que_fallaron_sin_perder_los_que_anduvieron():
    http = FakeHttp({f"{YO}/migrar": _json({"migrados": 1, "fallados": 1, "resultados": [
        {"clave": "F1", "ok": True}, {"clave": "F2", "ok": False, "error": "nombre inválido"}]})})
    r, registro = _accion("migrar", {"destino": "Estación 2", "claves": ["F1", "F2"]}, http)

    assert r.status == "err"
    assert (r.outputs["migrados"], r.outputs["fallados"]) == (1, 1)
    assert any("nombre inválido" in m for m in registro)
    # Y el resultado también sale como tabla, con el motivo de cada uno que no
    # entró: sin esto, la pantalla muestra el JSON crudo de los outputs.
    filas = {f["clave"]: f["resultado"] for f in r.outputs["vista"]["filas"]}
    assert filas == {"F1": "migrado", "F2": "nombre inválido"}
    assert "seleccion" not in r.outputs["vista"]  # es el final del camino


def test_migrar_no_corta_por_secretos_y_deja_que_la_app_decida():
    # Un corte acá bloquearía dos casos que la app resuelve mejor: un item de
    # colección viaja sin sus campos secretos y el destino conserva los suyos,
    # y de `env` se omiten sólo las variables marcadas secretas — una no
    # secreta se migra igual.
    http = FakeHttp({f"{YO}/migrar": _json({"migrados": 1, "fallados": 1, "resultados": [
        {"clave": "PUBLICA", "ok": True},
        {"clave": "TOKEN", "ok": False, "error": '"TOKEN" es secreta y no se pidió incluir secretos: no se migró'},
    ]})})
    r, registro = _accion(
        "migrar", {"destino": "Estación 2", "que": "env", "claves": ["PUBLICA", "TOKEN"]}, http)

    assert r.status == "err"  # una falló, y el informe dice cuál y por qué
    assert (r.outputs["migrados"], r.outputs["fallados"]) == (1, 1)
    assert any("no se migró" in m for m in registro)
    assert json.loads(http.calls[0]["body"])["incluir_secretos"] is False


def test_migrar_pasa_incluir_secretos_cuando_se_lo_piden():
    http = FakeHttp({f"{YO}/migrar": _json(
        {"migrados": 1, "fallados": 0, "resultados": [{"clave": "api", "ok": True}]})})
    r, _ = _accion("migrar", {
        "destino": "Estación 2", "que": "registros", "plugin": "plantillero",
        "coleccion": "plantillas", "claves": ["api"], "incluir_secretos": True}, http)

    assert r.status == "ok" and r.outputs["migrados"] == 1
    assert _cuerpo(http)["incluir_secretos"] is True


def test_migrar_contra_una_app_sin_el_endpoint_dice_que_actualizar():
    http = FakeHttp({f"{YO}/migrar": _json({"detail": "Not Found"}, status=404)})
    r, _ = _accion("migrar", {"destino": "Estación 2", "claves": ["F1"]}, http)

    assert r.status == "err"
    assert "Actualizaciones" in r.message


def test_migrar_con_la_direccion_de_red_en_vez_de_loopback_explica_el_403():
    # La bandeja del Bot ofrece copiar su dirección de red, así que es fácil
    # ponerla en el setting. Con ella `comparar` anda igual y `migrar` da 403,
    # y el mensaje de la app manda a mirar el emparejamiento: el problema real
    # es el setting.
    red = "http://10.0.0.40:8000"
    http = FakeHttp({f"{red}/api/core/migrar": _json({"detail": "Emparejar se hace desde el propio Bot"}, status=403)})
    r, _ = _accion(
        "migrar", {"destino": "Estación 2", "claves": ["F1"]}, http,
        config={"botsMiDireccion": red},
    )

    assert r.status == "err"
    assert "botsMiDireccion" in r.message and "127.0.0.1" in r.message


def test_sin_configurar_nada_el_origen_es_el_bot_que_corre_no_el_8000(monkeypatch):
    # El default era `http://127.0.0.1:8000` fijo, así que un Bot en otro
    # puerto le hablaba a OTRA instalación: comparar informaba el contenido de
    # otra máquina como propio, y migrar habría empujado el de la equivocada.
    # Lo encontró la sesión de la app con dos Bots en 8101 y 8102.
    monkeypatch.setenv("BOT_PORT", "8101")
    api = "http://127.0.0.1:8101/api/core"
    http = FakeHttp({
        f"{api}/workflows": _json([_flujo("mío", "A")]),
        f"{api}/workflows/{quote('mío', safe='')}": _json(_flujo("mío", "A")),
        f"{API2}/workflows": _json([]),
    })
    r, _ = _accion("comparar", {"destino": "Estación 2"}, http, config={})

    assert r.status == "ok"
    assert r.outputs["diff"]["origen"]["url"] == "http://127.0.0.1:8101"
    assert r.outputs["solo_origen"] == ["mío"]


def test_comparar_avisa_si_la_direccion_es_de_otra_instalacion(monkeypatch):
    # Sólo avisa: comparar lee. Pero el informe habla de otra máquina, y los
    # nombres de flujo ajenos se leen igual de plausibles que los propios.
    monkeypatch.setenv("BOT_PORT", "8101")
    http = _http_flujos(aca={"F": "A"}, alla={"F": "A"})
    r, registro = _accion("comparar", {"destino": "Estación 2"}, http, config=CONFIG_YO)

    assert r.status == "ok"
    assert any("instalación distinta" in m and "8101" in m for m in registro)


def test_migrar_corta_si_la_direccion_es_de_otra_instalacion(monkeypatch):
    # Acá no alcanza avisar: migrar empujaría el contenido de esa otra
    # instalación al destino y le pisaría lo suyo.
    monkeypatch.setenv("BOT_PORT", "8101")
    http = FakeHttp()
    r, _ = _accion("migrar", {"destino": "Estación 2", "claves": ["F1"]}, http, config=CONFIG_YO)

    assert r.status == "err"
    assert "no se migró nada" in r.message and "8101" in r.message
    assert http.calls == []


def test_sin_bot_port_no_se_inventa_una_sospecha(monkeypatch):
    # Fuera de la app —un test, el repo— no hay puerto propio con qué comparar.
    monkeypatch.delenv("BOT_PORT", raising=False)
    http = FakeHttp({f"{YO}/migrar": _json({"migrados": 1, "fallados": 0, "resultados": [{"clave": "F1", "ok": True}]})})
    r, _ = _accion("migrar", {"destino": "Estación 2", "claves": ["F1"]}, http, config=CONFIG_YO)

    assert r.status == "ok"


def test_migrar_sin_claves_es_err_antes_de_ir_a_la_red():
    http = FakeHttp()
    r, _ = _accion("migrar", {"destino": "Estación 2", "claves": []}, http)

    assert r.status == "err" and http.calls == []


def test_migrar_es_peligrosa_y_comparar_cuelga_de_la_coleccion():
    # Lo declarado, que es lo que la app usa para confirmar antes de escribir
    # en otra máquina y para saber dónde dibujar cada botón.
    acciones = {a.action.name: a.action for a in build_plugin().actions}
    assert acciones["migrar"].dangerous is True
    assert acciones["migrar"].resource == ""
    assert acciones["comparar"].resource == "bots"
    destino = next(p for p in acciones["comparar"].params if p.name == "destino")
    assert destino.aliases == ("nombre",)


# ── Nombres de pantalla vs nombres internos ──────────────────────────────
#
# Desde Plug ins → Acciones, el label es lo ÚNICO que se ve: el nombre interno
# no figura en ninguna pantalla. Escribir lo que se ve —'Connections',
# 'Sources'— fallaba con "1 de 1 no se migraron", sin decir cuál campo estaba
# mal, y el motivo quedaba sólo en el log del run.

CATALOGO = {"plugins": [
    {"name": "connections", "label": "Connections", "resources": [
        {"name": "sources", "label": "Sources"},
        {"name": "actions", "label": "Actions"}]},
    {"name": "bots", "label": "Bots", "resources": [
        {"name": "bots", "label": "Bots conocidos"}]},
    {"name": "plantillero", "label": "Plantillero", "resources": [
        {"name": "plantillas", "label": "Plantillas"}]},
]}


def _http_migrar(catalogo=CATALOGO, status_tools=200):
    respuestas = {f"{YO}/migrar": _json(
        {"migrados": 1, "fallados": 0, "resultados": [{"clave": "Casos TEST", "ok": True}]})}
    if catalogo is not None:
        respuestas[f"{YO}/tools"] = _json(catalogo, status=status_tools)
    return FakeHttp(respuestas)


def _migrar_registros(http, plugin, coleccion, claves=("Casos TEST",)):
    return _accion("migrar", {
        "destino": "Estación 2", "que": "registros", "plugin": plugin,
        "coleccion": coleccion, "claves": list(claves)}, http)


def test_migrar_acepta_los_nombres_que_muestra_la_pantalla():
    http = _http_migrar()
    r, registro = _migrar_registros(http, "Connections", "Sources")

    assert r.status == "ok"
    cuerpo = _cuerpo(http)
    assert (cuerpo["plugin"], cuerpo["coleccion"]) == ("connections", "sources")
    # Y se ve en el log qué se escribió y qué se terminó usando.
    assert any("'Connections' / 'Sources' es 'connections' / 'sources'" in m for m in registro)


def test_un_label_que_no_es_el_nombre_en_minuscula_tambien_resuelve():
    # El caso que un .lower() no arregla, y es de este mismo plugin: la
    # colección se llama `bots` y la pantalla la muestra 'Bots conocidos'.
    http = _http_migrar()
    r, _ = _migrar_registros(http, "Bots", "Bots conocidos")

    assert r.status == "ok"
    cuerpo = _cuerpo(http)
    assert (cuerpo["plugin"], cuerpo["coleccion"]) == ("bots", "bots")


def test_el_nombre_interno_sigue_andando_igual():
    http = _http_migrar()
    r, registro = _migrar_registros(http, "connections", "sources")

    assert r.status == "ok"
    cuerpo = _cuerpo(http)
    assert (cuerpo["plugin"], cuerpo["coleccion"]) == ("connections", "sources")
    # Nada que traducir: no ensucia el log.
    assert not any(" es 'connections'" in m for m in registro)


def test_una_coleccion_que_no_existe_lista_las_que_hay():
    # El error que faltaba: antes decía "1 de 1 no se migraron" y el motivo
    # quedaba en el log, sin nombrar los valores válidos.
    r, _ = _migrar_registros(_http_migrar(), "Connections", "Plantillas")

    assert r.status == "err"
    assert "no hay colección 'Plantillas' en 'connections'" in r.message
    assert "Hay: actions, sources" in r.message


def test_un_plugin_que_no_existe_lista_los_que_hay():
    r, _ = _migrar_registros(_http_migrar(), "Conections", "Sources")

    assert r.status == "err"
    assert "no hay plugin 'Conections'" in r.message
    # Sin paréntesis cuando el label es el mismo nombre con otra caja: repetir
    # "connections (Connections)" es ruido, y lo que hay que escribir es el
    # interno, que es justo lo que se muestra.
    assert "Hay: bots, connections, plantillero" in r.message


def test_dos_plugins_con_el_mismo_titulo_no_se_eligen_a_dedo():
    # Elegir uno sería migrar contra la colección equivocada sin que se note.
    catalogo = {"plugins": [
        {"name": "conexiones", "label": "Connections", "resources": [{"name": "s", "label": "S"}]},
        {"name": "connections_v2", "label": "Connections", "resources": [{"name": "s", "label": "S"}]},
    ]}
    r, _ = _migrar_registros(_http_migrar(catalogo), "Connections", "S")

    assert r.status == "err"
    assert "no alcanza para saber" in r.message
    assert "conexiones" in r.message and "connections_v2" in r.message


def test_el_nombre_interno_le_gana_al_titulo_de_otro():
    # Si alguien llamó a su plugin igual que el label de otro, lo escrito
    # literal es lo que quiso: no hay por qué adivinarle.
    catalogo = {"plugins": [
        {"name": "sources", "label": "Orígenes", "resources": [{"name": "x", "label": "X"}]},
        {"name": "connections", "label": "Sources", "resources": [{"name": "y", "label": "Y"}]},
    ]}
    http = _http_migrar(catalogo)
    r, _ = _migrar_registros(http, "sources", "X")

    assert r.status == "ok"
    assert _cuerpo(http)["plugin"] == "sources"


def test_sin_catalogo_sigue_con_lo_escrito_en_vez_de_cortar():
    # `/tools` puede no estar en una app vieja. Traducir es una comodidad, no
    # la validación: quien valida es el otro lado. Cortar acá cambiaría una
    # molestia por una falla.
    http = _http_migrar(catalogo=None)  # /tools sin guionar -> PortError
    r, registro = _migrar_registros(http, "plantillero", "plantillas")

    assert r.status == "ok"
    cuerpo = _cuerpo(http)
    assert (cuerpo["plugin"], cuerpo["coleccion"]) == ("plantillero", "plantillas")
    assert any("tal como se escribieron" in m for m in registro)


def test_un_catalogo_vacio_no_es_prueba_de_que_no_exista():
    http = _http_migrar({"plugins": []})
    r, registro = _migrar_registros(http, "Connections", "Sources")

    assert r.status == "ok"
    assert any("tal como se escribieron" in m for m in registro)


def test_con_que_flujos_no_pide_el_catalogo():
    # El request de más se paga sólo cuando hay nombres que traducir.
    http = _http_migrar()
    _accion("migrar", {"destino": "Estación 2", "claves": ["F1"]}, http)

    assert not any(c["url"].endswith("/tools") for c in http.calls)


def test_el_listado_muestra_el_titulo_cuando_no_es_el_mismo_nombre():
    # Acá sí hace falta el paréntesis: quien escribió 'Bots conocidos' tiene
    # que poder ver que el interno es `bots`, que no se deduce del título.
    r, _ = _migrar_registros(_http_migrar(), "Bots", "Conocidos")

    assert r.status == "err"
    assert "Hay: bots (Bots conocidos)" in r.message


def test_comparar_tambien_acepta_los_nombres_de_pantalla():
    # Mismos dos campos que `migrar`, misma traducción. Contra el catálogo del
    # ORIGEN, que es el lado del que se leen los items.
    http = _http_items(aca=[{"nombre": "plantilla_a", "patron": "^A"}], alla=[{"nombre": "plantilla_a", "patron": "^A"}])
    http.stub(f"{YO}/tools", text=json.dumps(CATALOGO))
    r, _ = _comparar(http, que="registros", plugin="Plantillero", coleccion="Plantillas")

    assert r.status == "ok"
    assert r.outputs["diff"]["coleccion"] == "plantillero/plantillas"
    # Y pidió el catálogo a este Bot, no al destino.
    tools = [c["url"] for c in http.calls if c["url"].endswith("/tools")]
    assert tools == [f"{YO}/tools"]


def test_los_params_que_nombran_otro_plugin_dicen_de_donde_salen_sus_opciones():
    # core#32: `plugin` y `coleccion` nombran una colección de OTRO plugin, así
    # que no hay un Resource propio al que apuntar; el núcleo expone para eso
    # el namespace `core:` (desde v0.3.1-beta.11).
    #
    # Las cadenas exactas se fijan acá porque `registry` valida la sintaxis y
    # el placeholder, pero NO el namespace: su regex es
    # ^core:[\w-]+(?::\{(\w+)\})?$, así que un `core:pluggins` pasaría el
    # chequeo de carga y se vería recién como un buscador vacío.
    plugin = build_plugin()
    declaraciones = [(a.name, a.params) for a in plugin.manifest.actions]
    declaraciones += [(t.manifest.id, t.manifest.params) for t in plugin.tools]

    revisadas = []
    for dueño, params in declaraciones:
        por_nombre = {p.name: p for p in params}
        if "plugin" not in por_nombre or "coleccion" not in por_nombre:
            continue
        revisadas.append(dueño)
        assert por_nombre["plugin"].options_from == "core:plugins", dueño
        assert por_nombre["coleccion"].options_from == "core:resources:{plugin}", dueño
        # La regla que el núcleo valida al cargar, replicada acá para que valga
        # también contra un core viejo: el placeholder nombra un param real de
        # ESTA declaración —no del plugin—, por `name` o por alias.
        validos = {p.name for p in params} | {a for p in params for a in p.aliases}
        assert "plugin" in validos, dueño

    # migrar (Action), comparar (Action) y comparar (tool): las tres.
    assert sorted(revisadas) == ["bots.comparar", "comparar", "migrar"]


def test_probar_deja_el_indicador_para_el_check_de_la_fila():
    http = FakeHttp({f"{API2}/runs/en-vuelo": _json([]), f"{API2}/runs?limit=1": _json([])})
    r, _ = _accion("probar", {"nombre": "Estación 2"}, http)
    assert r.status == "ok" and r.outputs["indicador"]["estado"] == "ok"


def test_probar_un_bot_caido_apaga_el_indicador():
    http = FakeHttp({f"{API2}/runs/en-vuelo": PortError("connection refused")})
    r, _ = _accion("probar", {"nombre": "Estación 2"}, http)
    assert r.status == "err"
    assert r.outputs["indicador"] == {"estado": "err", "texto": r.message}

    r, _ = _accion("probar", {"nombre": "Ninguno"}, FakeHttp())
    assert r.status == "err" and r.outputs["indicador"]["estado"] == "err"


def test_abrir_devuelve_la_direccion_con_el_nombre_y_no_va_a_la_red():
    http = FakeHttp()
    r, _ = _accion("abrir", {"nombre": "Estación 3"}, http)
    assert r.status == "ok"
    assert r.outputs["abrir_url"] == f"http://10.0.0.42:8000/?bot={quote('Estación 3', safe='')}"
    assert not http.calls


def test_abrir_no_abre_una_direccion_rara():
    r, _ = _accion("abrir", {"nombre": "Roto"}, FakeHttp())
    assert r.status == "err" and "abrir_url" not in r.outputs
