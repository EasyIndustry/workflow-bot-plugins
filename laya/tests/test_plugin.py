"""
Tests del plugin `laya`, sin modelo ni red: laya-serve es un `FakeHttp`
guionado con respuestas que tienen la forma real de `POST /v1/systemone`
(sacadas de un `Router.predict` de laya 0.3.20).
Necesita el núcleo (`backend/`) del repo de la app: se busca en la variable
`WORKFLOW_BOT_APP` o en `../workflow-bot-app`, al lado de este catálogo.
Corre con `python -m pytest laya` desde la raíz del catálogo.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.ports import HttpResponse, PortError  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeHttp  # noqa: E402

from laya.plugin import build_plugin  # noqa: E402

BASE = "http://127.0.0.1:8010"
PREGUNTAR = f"{BASE}/v1/systemone"
TEXTO = "La máquina dejó de andar y necesito que la arreglen hoy mismo"


def _json(datos, status=200):
    return HttpResponse(status=status, text=json.dumps(datos), headers={"content-type": "application/json"})


def _contesta(respuesta: dict) -> FakeHttp:
    return FakeHttp({PREGUNTAR: _json({"model": "multilingual", "answers": {"q": respuesta}, "usage": {}})})


def _correr(tool, params, http, config=None):
    reg = ToolRegistry(adapters={"http": http})
    reg._add_plugin("laya", "laya:PLUGIN", build_plugin())
    registro = []

    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params(params, {})
        return ToolContext(
            run_id="run-test", case_id="C1", params=declarados, extras=extras,
            config=dict(config or {}), context={},
            log=lambda m, level="info": registro.append(m), ports=ports or {},
        )

    return reg.execute(tool, factory), registro


def _cuerpo(http):
    return json.loads(http.calls[-1]["body"])


# ── si_no ─────────────────────────────────────────────────────────────────

NOUL = {"type": "noul", "noul": 0.9675, "confidence": 0.9675, "answer_confidence": 0.9675}


def test_si_no_manda_una_pregunta_noul_y_deja_la_respuesta_para_un_rombo():
    http = _contesta(NOUL)
    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "¿Es urgente?"}, http)
    assert r.status == "ok"
    assert r.outputs == {"respuesta": "si", "probabilidad": 0.9675}
    cuerpo = _cuerpo(http)
    assert cuerpo["state"] == TEXTO
    assert cuerpo["questions"] == {"q": {"type": "noul", "instructions": "¿Es urgente?"}}
    assert "model" not in cuerpo  # vacío: elige el servidor


def test_si_no_debajo_del_umbral_es_no_y_eso_sigue_siendo_ok():
    # "Laya dijo que no" no es una falla: la arista err queda para cuando no respondió.
    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?"}, _contesta({**NOUL, "noul": 0.2}))
    assert r.status == "ok" and r.outputs["respuesta"] == "no"

    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?", "umbral": 0.8}, _contesta({**NOUL, "noul": 0.7}))
    assert r.outputs["respuesta"] == "no"


def test_si_no_con_minimo_una_respuesta_al_azar_es_dudoso():
    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?", "minimo": 0.8}, _contesta({**NOUL, "noul": 0.55}))
    assert r.status == "ok" and r.outputs["respuesta"] == "dudoso"
    # La seguridad de un 'no' es 1 - P(sí): 0.1 de sí es un no muy seguro.
    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?", "minimo": 0.8}, _contesta({**NOUL, "noul": 0.1}))
    assert r.outputs["respuesta"] == "no"


def test_una_fila_llega_a_laya_como_objeto_aunque_el_param_sea_texto():
    # Un STR con {fila} recibe str(dict): comillas simples, no JSON.
    fila = {"cliente": "ACME", "estado": "atrasado"}
    http = _contesta(NOUL)
    _correr("laya.si_no", {"texto": str(fila), "pregunta": "?"}, http)
    assert _cuerpo(http)["state"] == fila

    http = _contesta(NOUL)
    _correr("laya.si_no", {"texto": json.dumps(fila), "pregunta": "?"}, http)
    assert _cuerpo(http)["state"] == fila

    # Un texto que sólo empieza con llave queda como texto.
    http = _contesta(NOUL)
    _correr("laya.si_no", {"texto": "{no es nada", "pregunta": "?"}, http)
    assert _cuerpo(http)["state"] == "{no es nada"


# ── elegir ────────────────────────────────────────────────────────────────

CHOICE = {
    "type": "choice", "choice": "soporte",
    "probabilities": {"ventas": 0.0059, "soporte": 0.9871, "admin": 0.007},
    "confidence": 0.929, "answer_confidence": 0.9871,
}


def test_elegir_con_etiquetas_separadas_por_coma():
    http = _contesta(CHOICE)
    r, _ = _correr("laya.elegir", {"texto": TEXTO, "pregunta": "¿Qué área?", "opciones": "ventas, soporte, admin"}, http)
    assert r.status == "ok"
    assert r.outputs["eleccion"] == "soporte" and r.outputs["confianza"] == 0.9871
    assert r.outputs["probabilidades"]["ventas"] == 0.0059
    assert _cuerpo(http)["questions"]["q"]["criteria"] == ["ventas", "soporte", "admin"]


def test_elegir_con_descripciones_las_manda_como_criterios():
    http = _contesta(CHOICE)
    opciones = {"ventas": "compra o presupuesto", "soporte": "algo no anda", "admin": "facturas"}
    _correr("laya.elegir", {"texto": TEXTO, "pregunta": "?", "opciones": json.dumps(opciones)}, http)
    assert _cuerpo(http)["questions"]["q"]["criteria"] == opciones


def test_elegir_con_minimo_y_opciones_mal_dadas():
    r, _ = _correr("laya.elegir", {"texto": TEXTO, "pregunta": "?", "opciones": "a, b", "minimo": 0.99}, _contesta(CHOICE))
    assert r.status == "ok" and r.outputs["eleccion"] == "dudoso"

    http = FakeHttp()
    r, _ = _correr("laya.elegir", {"texto": TEXTO, "pregunta": "?", "opciones": "una sola"}, http)
    assert r.status == "err" and "al menos dos" in r.message and http.calls == []
    r, _ = _correr("laya.elegir", {"texto": TEXTO, "pregunta": "?", "opciones": "[roto"}, http)
    assert r.status == "err" and "JSON válido" in r.message


# ── puntuar ───────────────────────────────────────────────────────────────

SCORE = {
    "type": "score", "score": 0.9562, "legend": {"0": "nada", "1": "algo", "2": "mucho"},
    "probabilities": {"0": 0.1705, "1": 0.7027, "2": 0.1267},
    "confidence": 0.2615, "answer_confidence": 0.7027,
}


def test_puntuar_da_el_nivel_mas_probable_y_el_valor_ponderado():
    http = _contesta(SCORE)
    r, _ = _correr("laya.puntuar", {"texto": TEXTO, "pregunta": "¿Qué tan enojado?", "niveles": "nada, algo, mucho"}, http)
    assert r.status == "ok"
    assert r.outputs == {"nivel": "algo", "indice": 1, "valor": 0.9562, "confianza": 0.7027}
    assert _cuerpo(http)["questions"]["q"] == {
        "type": "score", "instructions": "¿Qué tan enojado?", "criteria": ["nada", "algo", "mucho"],
    }


def test_puntuar_dudoso_y_niveles_como_objeto():
    r, _ = _correr("laya.puntuar", {"texto": TEXTO, "pregunta": "?", "niveles": "nada, algo, mucho", "minimo": 0.9}, _contesta(SCORE))
    assert r.outputs["nivel"] == "dudoso" and r.outputs["indice"] == -1
    assert r.outputs["valor"] == 0.9562  # el ponderado sigue sirviendo para un umbral propio

    r, _ = _correr("laya.puntuar", {"texto": TEXTO, "pregunta": "?", "niveles": '{"a": "x", "b": "y"}'}, FakeHttp())
    assert r.status == "err" and "ordenada" in r.message


# ── el servidor ───────────────────────────────────────────────────────────

def test_config_manda_clave_modelo_y_otra_direccion():
    otra = "http://10.0.0.7:8010/"
    http = FakeHttp({"http://10.0.0.7:8010/v1/systemone": _json({"answers": {"q": NOUL}})})
    config = {"url": otra, "api_key": "s3creto", "modelo": "multilingual", "timeout": 5}
    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?"}, http, config=config)
    assert r.status == "ok"
    llamada = http.calls[0]
    assert llamada["headers"]["Authorization"] == "Bearer s3creto"
    assert llamada["timeout"] == 5.0
    assert json.loads(llamada["body"])["model"] == "multilingual"


def test_servidor_caido_401_y_422_son_err_con_salidas_vacias():
    http = FakeHttp({PREGUNTAR: PortError("connection refused")})
    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?"}, http)
    assert r.status == "err" and "no responde" in r.message
    assert r.outputs == {"respuesta": "", "probabilidad": 0.0}

    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?"}, FakeHttp({PREGUNTAR: _json({"detail": "x"}, 401)}))
    assert r.status == "err" and "clave" in r.message

    detalle = "question 'q': a choice question takes 'criteria' as a dict"
    r, _ = _correr("laya.elegir", {"texto": TEXTO, "pregunta": "?", "opciones": "a, b"},
                   FakeHttp({PREGUNTAR: _json({"detail": detalle}, 422)}))
    assert r.status == "err" and "422" in r.message and detalle in r.message


def test_una_respuesta_que_no_es_de_laya_no_pasa_por_buena():
    r, _ = _correr("laya.si_no", {"texto": TEXTO, "pregunta": "?"}, FakeHttp({PREGUNTAR: _json({"hola": 1})}))
    assert r.status == "err" and "laya-serve" in r.message


def test_disponible():
    http = FakeHttp({f"{BASE}/health": _json({"status": "ok", "loaded": ["multilingual"], "device": "auto"})})
    r, registro = _correr("laya.disponible", {}, http)
    assert r.status == "ok" and r.outputs["modelos"] == ["multilingual"]
    assert http.calls[0]["method"] == "GET"

    r, _ = _correr("laya.disponible", {}, FakeHttp({f"{BASE}/health": PortError("timeout")}))
    assert r.status == "err" and r.outputs == {"modelos": []}


# ── preguntar: varias en una pasada ───────────────────────────────────────

VARIAS = {
    "urgente": {"tipo": "si_no", "pregunta": "¿Es urgente?"},
    "area": {"tipo": "elegir", "pregunta": "¿Qué área?", "opciones": "ventas, soporte, admin"},
    "enojo": {"tipo": "puntuar", "pregunta": "¿Qué tan enojado?", "niveles": ["nada", "algo", "mucho"]},
}


def _contestan(answers: dict) -> FakeHttp:
    return FakeHttp({PREGUNTAR: _json({"model": "multilingual", "answers": answers, "usage": {}})})


def test_preguntar_manda_todas_juntas_y_deja_lo_mismo_que_los_tools_sueltos():
    http = _contestan({"urgente": NOUL, "area": CHOICE, "enojo": SCORE})
    r, registro = _correr("laya.preguntar", {"texto": TEXTO, "preguntas": json.dumps(VARIAS)}, http)
    assert r.status == "ok"
    assert len(http.calls) == 1
    enviadas = _cuerpo(http)["questions"]
    assert enviadas["urgente"] == {"type": "noul", "instructions": "¿Es urgente?"}
    assert enviadas["area"]["criteria"] == ["ventas", "soporte", "admin"]
    assert enviadas["enojo"]["type"] == "score"

    respuestas = r.outputs["respuestas"]
    # Cada entrada es exactamente lo que deja su tool suelto.
    for id_, tool, respuesta in (("urgente", "laya.si_no", NOUL), ("area", "laya.elegir", CHOICE), ("enojo", "laya.puntuar", SCORE)):
        datos = {k: v for k, v in VARIAS[id_].items() if k != "tipo"}
        suelto, _ = _correr(tool, {"texto": TEXTO, **datos}, _contesta(respuesta))
        assert respuestas[id_] == suelto.outputs, id_
    assert respuestas["urgente"]["respuesta"] == "si" and respuestas["area"]["eleccion"] == "soporte"
    assert r.outputs["dudosas"] == []
    assert any("urgente" in m for m in registro)


def test_preguntar_minimo_general_y_propio_y_la_lista_de_dudosas():
    preguntas = {
        "urgente": {"tipo": "si_no", "pregunta": "?"},                 # 0.9675: pasa el 0.9
        "area": {"tipo": "elegir", "pregunta": "?", "opciones": "a, b", "minimo": 0.999},
        "enojo": {"tipo": "puntuar", "pregunta": "?", "niveles": "nada, algo, mucho"},  # 0.70 < 0.9
    }
    r, _ = _correr("laya.preguntar", {"texto": TEXTO, "preguntas": preguntas, "minimo": 0.9},
                   _contestan({"urgente": NOUL, "area": CHOICE, "enojo": SCORE}))
    assert r.status == "ok"
    assert r.outputs["respuestas"]["urgente"]["respuesta"] == "si"
    assert r.outputs["dudosas"] == ["area", "enojo"]


def test_preguntar_valida_antes_de_mandar_nada():
    http = FakeHttp()
    casos = {
        "no es un objeto": '["a"]',
        "sin preguntas": "{}",
        "con punto": {"a.b": {"tipo": "si_no", "pregunta": "?"}},
        "solo digitos": {"0": {"tipo": "si_no", "pregunta": "?"}},
        "tipo raro": {"x": {"tipo": "texto", "pregunta": "?"}},
        "sin pregunta": {"x": {"tipo": "si_no"}},
        "opciones rotas": {"x": {"tipo": "elegir", "pregunta": "?", "opciones": "una"}},
        "umbral fuera": {"x": {"tipo": "si_no", "pregunta": "?", "umbral": 3}},
    }
    for nombre, preguntas in casos.items():
        r, _ = _correr("laya.preguntar", {"texto": TEXTO, "preguntas": preguntas}, http)
        assert r.status == "err", nombre
        assert r.outputs == {"respuestas": {}, "dudosas": []}, nombre
    assert http.calls == []

    muchas = {f"p{i}": {"tipo": "si_no", "pregunta": "?"} for i in range(65)}
    r, _ = _correr("laya.preguntar", {"texto": TEXTO, "preguntas": muchas}, http)
    assert r.status == "err" and "64" in r.message


def test_preguntar_si_falta_una_respuesta_no_inventa_las_otras():
    r, _ = _correr("laya.preguntar", {"texto": TEXTO, "preguntas": VARIAS}, _contestan({"urgente": NOUL}))
    assert r.status == "err" and r.outputs["respuestas"] == {}
