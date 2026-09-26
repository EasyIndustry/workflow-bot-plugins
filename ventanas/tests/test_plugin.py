"""
Tests del plugin `ventanas`.

Sin ninguna ventana de verdad: el port `window` es el `FakeWindow` del núcleo,
que guiona qué ventanas existen y qué controles se pueden leer, y registra
cada llamada. Lo que se prueba es lo único que este plugin decide -qué
llamadas al port hace cada tool, en qué orden, y cómo viaja la 'ventana'
entre nodos-, no el comportamiento de UI Automation.

Corre con `python -m pytest plugins/ventanas`.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeWindow  # noqa: E402

from plugins.ventanas.plugin import build_plugin  # noqa: E402

TITULO = "MiApp"
EXE = r"D:\MiApp\MiApp.exe"
# Lo que 'encontrar' devuelve y los demás tools reciben tal cual.
VENTANA = {"handle": "1", "titulo": TITULO, "proceso": ""}


def _ctx_factory(node_params):
    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params(node_params, {})
        return ToolContext(
            run_id="run-test", case_id="AP962", params=declarados, extras=extras,
            config={}, context={}, log=lambda *_a, **_k: None, ports=ports or {},
        )

    return factory


def _registry(window=None) -> ToolRegistry:
    reg = ToolRegistry(adapters={"window": window or FakeWindow({TITULO: {}})})
    reg._add_plugin("ventanas", "plugins.ventanas:PLUGIN", build_plugin())
    return reg


def _correr(tool, window=None, **params):
    window = window or FakeWindow({TITULO: {}})
    resultado = _registry(window).execute(f"ventanas.{tool}", _ctx_factory(params))
    return resultado, window


def test_el_paquete_expone_plugin_como_lo_busca_el_nucleo():
    import plugins.ventanas as paquete

    assert paquete.PLUGIN.manifest.name == "ventanas"


def test_el_plugin_carga_con_el_port_window_y_sus_seis_tools():
    reg = _registry()
    assert not reg.errors
    plugin = next(p for p in reg.plugins if p.name == "ventanas")
    assert set(plugin.ports) == {"window"}
    assert set(reg.tool_ids) == {
        "ventanas.encontrar", "ventanas.click", "ventanas.marcar_checkbox",
        "ventanas.escribir_texto", "ventanas.leer_texto", "ventanas.seleccionar_en_lista",
    }


# ── encontrar ────────────────────────────────────────────────────────────


def test_encontrar_por_titulo_devuelve_la_ventana_para_los_demas_nodos():
    resultado, window = _correr("encontrar", titulo=TITULO)

    assert resultado.status == "ok"
    assert resultado.outputs["ventana"] == VENTANA
    assert window.calls == [{"op": "find_window", "title": TITULO, "process": None}]


def test_encontrar_por_proceso_no_manda_titulo():
    resultado, window = _correr("encontrar", window=FakeWindow({EXE: {}}), proceso=EXE)

    assert resultado.status == "ok"
    assert resultado.outputs["ventana"]["proceso"] == EXE
    assert window.calls == [{"op": "find_window", "title": None, "process": EXE}]


def test_encontrar_sin_titulo_ni_proceso_es_err_antes_de_tocar_el_port():
    resultado, window = _correr("encontrar")

    assert resultado.status == "err"
    assert window.calls == []


def test_encontrar_una_ventana_que_no_esta_abierta_es_err():
    resultado, _ = _correr("encontrar", titulo="Una App Que No Existe")

    assert resultado.status == "err"


# ── click / marcar_checkbox ──────────────────────────────────────────────


def test_click_usa_el_handle_de_la_ventana_encontrada():
    window = FakeWindow({TITULO: {}})
    _correr("encontrar", window=window, titulo=TITULO)

    resultado = _registry(window).execute(
        "ventanas.click", _ctx_factory({"ventana": VENTANA, "control": "Button:Generate path"}),
    )

    assert resultado.status == "ok"
    assert window.calls[-1] == {"op": "click", "handle": "1", "control": "Button:Generate path", "button": "left"}


def test_marcar_checkbox_es_un_click_sobre_el_checkbox():
    window = FakeWindow({TITULO: {}})
    _correr("encontrar", window=window, titulo=TITULO)

    resultado = _registry(window).execute(
        "ventanas.marcar_checkbox", _ctx_factory({"ventana": VENTANA, "control": "CheckBox:Modo rápido"}),
    )

    assert resultado.status == "ok"
    assert window.calls[-1] == {"op": "click", "handle": "1", "control": "CheckBox:Modo rápido", "button": "left"}


def test_una_ventana_que_no_viene_de_encontrar_es_err_con_un_mensaje_claro():
    resultado, window = _correr("click", ventana={"titulo": TITULO}, control="Generate")

    assert resultado.status == "err"
    assert "encontrar ventana" in resultado.message
    assert window.calls == []  # no se intentó clickear nada


def test_un_handle_de_otra_corrida_es_err_del_port():
    # El handle es un token del adapter: usarlo sin haber buscado la ventana
    # en esta corrida (ej. guardado de un run anterior) falla en el port.
    resultado, _ = _correr("click", ventana=VENTANA, control="Generate")

    assert resultado.status == "err"



class CheckboxDeVerdad(FakeWindow):
    """Un click sobre un control con estado lo invierte, como en la app. El
    `FakeWindow` del núcleo guiona el estado pero el click no lo toca."""

    def __init__(self, estado, *, trabado=False):
        super().__init__({TITULO: {}}, states={TITULO: {CHECK: estado}})
        self.trabado = trabado

    def click(self, window, control, *, button="left", timeout=None):
        super().click(window, control, button=button, timeout=timeout)
        estados = self.states[TITULO]
        if not self.trabado and estados.get(control) in ("on", "off", "indeterminate"):
            estados[control] = "off" if estados[control] == "on" else "on"


CHECK = "CheckBox:Modo rápido"


def _marcar(window, **params):
    _correr("encontrar", window=window, titulo=TITULO)
    return _registry(window).execute(
        "ventanas.marcar_checkbox", _ctx_factory({"ventana": VENTANA, "control": CHECK, **params}),
    )


def _clicks(window):
    return [c for c in window.calls if c["op"] == "click"]


def test_marcar_tildado_clickea_solo_si_hace_falta():
    window = CheckboxDeVerdad("off")
    r = _marcar(window, estado="tildado")
    assert r.status == "ok" and r.outputs == {"estado": "on", "cambio": True}
    assert len(_clicks(window)) == 1

    # Ya tildado: lo deja como está, que es justo lo que un toggle no sabe hacer.
    window = CheckboxDeVerdad("on")
    r = _marcar(window, estado="tildado")
    assert r.status == "ok" and r.outputs == {"estado": "on", "cambio": False}
    assert _clicks(window) == []


def test_marcar_destildado_y_desde_indeterminado():
    window = CheckboxDeVerdad("on")
    assert _marcar(window, estado="destildado").outputs == {"estado": "off", "cambio": True}

    window = CheckboxDeVerdad("indeterminate")
    r = _marcar(window, estado="destildado")
    assert r.status == "ok" and r.outputs["estado"] == "off"
    assert len(_clicks(window)) == 2  # indeterminado → on → off


def test_marcar_confirma_y_es_err_si_el_click_no_lo_cambio():
    # Una app que ignora el click (deshabilitado, otro control con el mismo
    # nombre): decir ok sería mentir sobre cómo quedó.
    window = CheckboxDeVerdad("off", trabado=True)
    r = _marcar(window, estado="tildado")
    assert r.status == "err" and "quedó 'off'" in r.message
    assert r.outputs == {"estado": "off", "cambio": True}


def test_marcar_algo_que_no_es_un_checkbox_es_err_sin_clickear():
    window = CheckboxDeVerdad(None)
    r = _marcar(window, estado="tildado")
    assert r.status == "err" and "no tiene estado" in r.message
    assert _clicks(window) == []


def test_alternar_sigue_siendo_un_click_sin_leer_y_anda_sin_read_state():
    class SinReadState(FakeWindow):
        read_state = None  # un núcleo anterior a v0.3.1-beta.6

    window = SinReadState({TITULO: {}})
    r = _marcar(window)
    assert r.status == "ok" and r.outputs == {"estado": "", "cambio": True}
    assert [c["op"] for c in window.calls] == ["find_window", "click"]

    r = _marcar(SinReadState({TITULO: {}}), estado="tildado")
    assert r.status == "err" and "v0.3.1-beta.6" in r.message

# ── escribir_texto / leer_texto ──────────────────────────────────────────


def test_escribir_texto_manda_el_texto_al_control():
    window = FakeWindow({TITULO: {"Edit:Carpeta:": ""}})
    _correr("encontrar", window=window, titulo=TITULO)

    resultado = _registry(window).execute(
        "ventanas.escribir_texto",
        _ctx_factory({"ventana": VENTANA, "control": "Edit:Carpeta:", "texto": r"D:\casos\AP962"}),
    )

    assert resultado.status == "ok"
    assert window.windows[TITULO]["Edit:Carpeta:"] == r"D:\casos\AP962"


def test_leer_texto_devuelve_lo_que_muestra_el_control():
    window = FakeWindow({TITULO: {"Text:Result": "Finished"}})
    _correr("encontrar", window=window, titulo=TITULO)

    resultado = _registry(window).execute(
        "ventanas.leer_texto", _ctx_factory({"ventana": VENTANA, "control": "Text:Result"}),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["texto"] == "Finished"


def test_leer_texto_sin_control_pide_la_ventana_entera():
    window = FakeWindow({TITULO: {None: TITULO}})
    _correr("encontrar", window=window, titulo=TITULO)

    resultado = _registry(window).execute(
        "ventanas.leer_texto", _ctx_factory({"ventana": VENTANA}),
    )

    assert resultado.status == "ok"
    assert window.calls[-1] == {"op": "read_text", "handle": "1", "control": None}


# ── seleccionar_en_lista ─────────────────────────────────────────────────


def test_seleccionar_en_lista_abre_el_dropdown_y_despues_clickea_la_opcion():
    window = FakeWindow({TITULO: {}})
    _correr("encontrar", window=window, titulo=TITULO)

    resultado = _registry(window).execute(
        "ventanas.seleccionar_en_lista",
        _ctx_factory({"ventana": VENTANA, "dropdown": "ComboBox:Path direction", "opcion": "CCW"}),
    )

    assert resultado.status == "ok"
    assert window.calls[-2:] == [
        {"op": "click", "handle": "1", "control": "ComboBox:Path direction", "button": "left"},
        {"op": "click", "handle": "1", "control": "CCW", "button": "left"},
    ]
