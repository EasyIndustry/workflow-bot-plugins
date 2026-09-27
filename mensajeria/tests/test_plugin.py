"""
Tests del plugin `mensajeria`, sin red: el servicio (Telegram, con la forma
real de su API) y la API local del Bot son un `FakeHttp` guionado. Las
plantillas son las mismas que crea 'Crear ejemplo de Telegram', con el token
ya resuelto, como las entrega el núcleo al plugin.
Necesita el núcleo (`backend/`) del repo de la app: se busca en la variable
`WORKFLOW_BOT_APP` o en `../workflow-bot-app`, al lado de este catálogo.
Corre con `python -m pytest mensajeria` desde la raíz del catálogo.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

import pytest  # noqa: E402

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.ports import HttpResponse, PortError  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeFs, FakeHttp, _norm  # noqa: E402

import mensajeria.plugin as modulo  # noqa: E402
from mensajeria.plugin import build_plugin  # noqa: E402

TG = "https://api.telegram.org/bot123:ABC"
BOT = "http://127.0.0.1:8123/api/core"


@pytest.fixture(autouse=True)
def _puerto(monkeypatch):
    monkeypatch.setenv("BOT_PORT", "8123")


def _plantillas(cursor=""):
    """Las del ejemplo, con {env.TELEGRAM_TOKEN} ya resuelto (así las ve el plugin)."""
    fuera = []
    for p in modulo._telegram("TELEGRAM_TOKEN"):
        fuera.append({**p, "url_base": p["url_base"].replace("{env.TELEGRAM_TOKEN}", "123:ABC"),
                      **({"cursor": cursor} if p["nombre"] == "Telegram recibir" else {})})
    return fuera


def _json(datos, status=200):
    return HttpResponse(status=status, text=json.dumps(datos), headers={"content-type": "application/json"})


class FsBytes(FakeFs):
    def __init__(self, archivos=None):
        super().__init__()
        self.bytes = {_norm(k): v for k, v in (archivos or {}).items()}

    def read_bytes(self, path):
        if _norm(path) not in self.bytes:
            raise PortError(f"no existe {path}")
        return self.bytes[_norm(path)]


def _correr(tool, params, http, plantillas=None, fs=None):
    reg = ToolRegistry(adapters={"http": http, "fs": fs or FsBytes()})
    reg._add_plugin("mensajeria", "mensajeria:PLUGIN", build_plugin())
    items = plantillas if plantillas is not None else _plantillas()

    def factory(declaracion, ports=None):
        if hasattr(declaracion, "split_params"):
            d, e = declaracion.split_params(params, {})
        else:
            d, e = {p.name: params.get(p.name, p.default) for p in declaracion.params}, {}
        return ToolContext(run_id="r", case_id="c", params=d, extras=e, config={}, context={},
                           log=lambda m, level="info": None, ports=ports or {},
                           resources=lambda coleccion: items if coleccion == "plantillas" else [])

    if hasattr(reg, "execute_action") and tool == "crear_telegram":
        return reg.execute_action("mensajeria", tool, factory)
    return reg.execute(tool, factory)


def _enviado(http):
    return json.loads(http.calls[-1]["body"])


OK_MENSAJE = _json({"ok": True, "result": {"message_id": 4711, "chat": {"id": 555}}})


# ── enviar ────────────────────────────────────────────────────────────────

def test_mensaje_con_menu_arma_los_botones_en_filas_y_saca_lo_vacio():
    http = FakeHttp({f"{TG}/sendMessage": OK_MENSAJE})
    r = _correr("mensajeria.enviar", {"plantilla": "Telegram mensaje", "chat": "555",
                                       "mensaje": "¿Aprobás el caso AB123?", "menu": "Sí; No; Más tarde"}, http)
    assert r.status == "ok", r.message
    assert r.outputs["id"] == "4711" and r.outputs["status"] == 200
    llamada = http.calls[-1]
    assert llamada["method"] == "POST" and llamada["url"] == f"{TG}/sendMessage"
    assert llamada["headers"]["Content-Type"] == "application/json"
    assert _enviado(http) == {
        "chat_id": "555", "text": "¿Aprobás el caso AB123?",
        "reply_markup": {"inline_keyboard": [
            [{"text": "Sí", "callback_data": "Sí"}, {"text": "No", "callback_data": "No"}],
            [{"text": "Más tarde", "callback_data": "Más tarde"}],
        ]},
    }  # sin parse_mode: el campo 'formato' vino vacío


def test_sin_menu_no_manda_un_reply_markup_vacio():
    http = FakeHttp({f"{TG}/sendMessage": OK_MENSAJE})
    _correr("mensajeria.enviar", {"plantilla": "Telegram mensaje", "chat": "555", "mensaje": "hola", "formato": "HTML"}, http)
    assert _enviado(http) == {"chat_id": "555", "text": "hola", "parse_mode": "HTML"}


def test_falta_un_obligatorio_y_no_se_manda_nada():
    http = FakeHttp()
    r = _correr("mensajeria.enviar", {"plantilla": "Telegram mensaje", "mensaje": "hola"}, http)
    assert r.status == "err" and "chat" in r.message and http.calls == []


def test_archivo_va_como_multipart_con_los_bytes_intactos():
    pdf = b"%PDF-1.4\n\x00\xff binario"
    http = FakeHttp({f"{TG}/sendDocument": OK_MENSAJE})
    fs = FsBytes({"C:/casos/AB123/informe.pdf": pdf})
    r = _correr("mensajeria.enviar", {"plantilla": "Telegram archivo", "chat": "555",
                                       "archivo": "C:/casos/AB123/informe.pdf", "texto": "Va el informe"}, http, fs=fs)
    assert r.status == "ok", r.message
    llamada = http.calls[-1]
    tipo = llamada["headers"]["Content-Type"]
    assert tipo.startswith("multipart/form-data; boundary=")
    frontera = tipo.split("boundary=")[1].encode()
    cuerpo = llamada["body"]
    assert isinstance(cuerpo, bytes) and cuerpo.endswith(b"--" + frontera + b"--\r\n")
    assert b'name="document"; filename="informe.pdf"' in cuerpo and b"Content-Type: application/pdf" in cuerpo
    assert pdf in cuerpo
    assert b'name="chat_id"\r\n\r\n555\r\n' in cuerpo and "Va el informe".encode() in cuerpo


def test_el_error_del_servicio_se_ve_como_lo_explica():
    http = FakeHttp({f"{TG}/sendMessage": _json({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, 400)})
    r = _correr("mensajeria.enviar", {"plantilla": "Telegram mensaje", "chat": "1", "mensaje": "x"}, http)
    assert r.status == "err" and "chat not found" in r.message and r.outputs["status"] == 400


def test_tipos_numero_json_lista_json_y_formato_form():
    plantilla = {
        "nombre": "Servicio X", "url_base": "https://x.test/api", "headers": {"Authorization": "Bearer t"},
        "campos": [
            {"nombre": "destino", "tipo": "numero", "obligatorio": True},
            {"nombre": "extra", "tipo": "json"},
            {"nombre": "etiquetas", "tipo": "lista"},
            {"nombre": "prioridad", "tipo": "texto", "defecto": "normal"},
        ],
        "enviar": {"metodo": "POST", "ruta": "/v1/{destino}/enviar", "formato": "json",
                   "cuerpo": {"a": "{destino}", "datos": "{extra}", "tags": "{etiquetas}", "texto": "prioridad {prioridad}"}},
    }
    http = FakeHttp({"https://x.test/api/v1/42/enviar": _json({"id": "m1"})})
    r = _correr("mensajeria.enviar", {"plantilla": "Servicio X", "destino": "42", "extra": '{"k": [1, 2]}',
                                       "etiquetas": '["uno", "dos"]'}, http, plantillas=[plantilla])
    assert r.status == "ok", r.message
    assert _enviado(http) == {"a": 42, "datos": {"k": [1, 2]}, "tags": ["uno", "dos"], "texto": "prioridad normal"}
    assert http.calls[-1]["headers"]["Authorization"] == "Bearer t"

    form = {**plantilla, "enviar": {**plantilla["enviar"], "formato": "form"}}
    http = FakeHttp({"https://x.test/api/v1/42/enviar": _json({"id": "m1"})})
    _correr("mensajeria.enviar", {"plantilla": "Servicio X", "destino": "42"}, http, plantillas=[form])
    assert http.calls[-1]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert http.calls[-1]["body"] == "a=42&texto=prioridad+normal"

    r = _correr("mensajeria.enviar", {"plantilla": "Servicio X", "destino": "no"}, FakeHttp(), plantillas=[plantilla])
    assert r.status == "err" and "número" in r.message


def test_el_editor_ofrece_los_campos_de_la_plantilla_elegida():
    def leer(coleccion, clave, key_field="name"):
        """Como el lector del núcleo (Instance._lector_de_items): busca por `key_field`, "name" si no se dice."""
        return next((p for p in _plantillas() if str(p.get(key_field, "")) == clave), None) if coleccion == "plantillas" else None
    params = modulo._describir_extras({"plantilla": "Telegram mensaje"}, leer)
    assert [(p.name, p.required) for p in params] == [("chat", True), ("mensaje", True), ("menu", False), ("formato", False)]
    archivo = modulo._describir_extras({"plantilla": "Telegram archivo"}, leer)
    assert next(p for p in archivo if p.name == "archivo").type.value == "path"
    assert modulo._describir_extras({}, leer) == ()


# ── recibir ───────────────────────────────────────────────────────────────

UPDATES = {"ok": True, "result": [
    {"update_id": 900, "message": {"message_id": 1, "from": {"username": "ana"}, "chat": {"id": 555}, "text": "hola"}},
    {"update_id": 901, "callback_query": {"id": "cb7", "from": {"first_name": "Beto"}, "data": "Sí",
                                          "message": {"message_id": 4711, "chat": {"id": 556}}}},
    {"update_id": 902, "message": {"message_id": 3, "from": {"username": "ana"}, "chat": {"id": 555},
                                   "caption": "foto", "photo": [{"file_id": "chica"}, {"file_id": "grande"}]}},
]}


def _http_recibir(updates, cursor_en_url=""):
    # Como la devuelve la app: con {env.X} sin resolver, y SIN la clave 'nombre',
    # que en un GET de un item suelto va en la URL y no en el cuerpo.
    guardada = {k: v for k, v in modulo._telegram("TELEGRAM_TOKEN")[2].items() if k != "nombre"}
    guardada["_updated_at"] = 1790000000.0
    return FakeHttp({
        f"{TG}/getUpdates?offset={cursor_en_url}&timeout=0": _json(updates),
        f"{BOT}/resources/mensajeria/plantillas/Telegram%20recibir": _json(guardada),
    })


def test_recibir_mapea_mensajes_y_botones_y_guarda_el_cursor_sin_resolver_el_token():
    http = _http_recibir(UPDATES, "900")
    r = _correr("mensajeria.recibir", {"plantilla": "Telegram recibir"}, http, plantillas=_plantillas(cursor="900"))
    assert r.status == "ok", r.message
    m = r.outputs["mensajes"]
    assert r.outputs["cantidad"] == 3 and r.outputs["hay"] == "si"
    assert {k: m[0][k] for k in ("chat", "texto", "de", "boton")} == {"chat": 555, "texto": "hola", "de": "ana", "boton": None}
    assert {k: m[1][k] for k in ("chat", "boton", "boton_id", "de", "mensaje_id")} == {
        "chat": 556, "boton": "Sí", "boton_id": "cb7", "de": "Beto", "mensaje_id": 4711}
    assert m[2]["texto"] == "foto" and m[2]["archivo_id"] == "grande"  # -1: la foto más grande
    assert r.outputs["primero"] == m[0]
    # Las columnas del primero, sueltas, para {chat} / {boton} sin pasar por {primero}.
    assert (r.outputs["chat"], r.outputs["texto"], r.outputs["de"]) == (555, "hola", "ana")
    assert r.outputs["boton"] == ""  # el primero no es un botón: vacío, no ausente

    [put] = [c for c in http.calls if c["method"] == "PUT"]
    item = json.loads(put["body"])["item"]
    assert item["cursor"] == "903"
    assert item["nombre"] == "Telegram recibir"  # el PUT la exige aunque el GET no la traiga
    assert "_updated_at" not in item
    assert "{env.TELEGRAM_TOKEN}" in item["url_base"] and "123:ABC" not in put["body"]


def test_recibir_con_limite_deja_el_resto_para_la_proxima():
    http = _http_recibir(UPDATES)
    r = _correr("mensajeria.recibir", {"plantilla": "Telegram recibir", "limite": 1}, http)
    assert r.outputs["cantidad"] == 1
    assert json.loads([c for c in http.calls if c["method"] == "PUT"][0]["body"])["item"]["cursor"] == "901"


def test_sin_nada_nuevo_es_ok_con_hay_no_y_no_toca_el_cursor():
    http = _http_recibir({"ok": True, "result": []})
    r = _correr("mensajeria.recibir", {"plantilla": "Telegram recibir"}, http)
    assert r.status == "ok" and r.outputs["hay"] == "no" and r.outputs["primero"] == {}
    assert r.outputs["chat"] == "" and r.outputs["boton"] == ""
    assert not any(c["method"] == "PUT" for c in http.calls)


def test_si_no_se_puede_guardar_el_cursor_no_devuelve_nada():
    http = _http_recibir(UPDATES)
    http.responses[f"{BOT}/resources/mensajeria/plantillas/Telegram%20recibir"] = _json({"detail": "no"}, 500)
    r = _correr("mensajeria.recibir", {"plantilla": "Telegram recibir"}, http)
    assert r.status == "err" and "repetir" in r.message and r.outputs["mensajes"] == []


def test_enviar_con_una_plantilla_de_recibir_y_al_reves():
    r = _correr("mensajeria.enviar", {"plantilla": "Telegram recibir"}, FakeHttp())
    assert r.status == "err" and "'enviar'" in r.message
    r = _correr("mensajeria.recibir", {"plantilla": "Telegram mensaje"}, FakeHttp())
    assert r.status == "err" and "'recibir'" in r.message
    r = _correr("mensajeria.recibir", {"plantilla": "No existe"}, FakeHttp())
    assert r.status == "err" and "Cargadas:" in r.message


# ── el ejemplo de Telegram ────────────────────────────────────────────────

def test_crear_telegram_escribe_las_plantillas_y_saltea_las_que_existen():
    class Http(FakeHttp):
        def request(self, url, *, method="GET", headers=None, body=None, timeout=30.0):
            self.calls.append({"url": url, "method": method, "body": body})
            if method == "GET":
                return _json({"item": {}}) if url.endswith("Telegram%20archivo") else _json({"detail": "no"}, 404)
            return _json({"item": json.loads(body)["item"]})

    http = Http()
    r = _correr("crear_telegram", {"variable": "MI_TOKEN"}, http)
    assert r.status == "ok", r.message
    assert r.outputs["salteadas"] == ["Telegram archivo"] and len(r.outputs["creadas"]) == 3
    puesta = next(json.loads(c["body"])["item"] for c in http.calls if c["method"] == "PUT")
    assert puesta["url_base"] == "https://api.telegram.org/bot{env.MI_TOKEN}"


def test_las_plantillas_de_ejemplo_son_validas():
    try:
        from backend.core.resources import _KEY_RE
    except ImportError:
        _KEY_RE = re.compile(r"^[\w\- ]+$")
    for p in modulo._telegram("TELEGRAM_TOKEN"):
        assert _KEY_RE.match(p["nombre"]), p["nombre"]
        assert isinstance(modulo._campos(p), list), p["nombre"]


def test_una_variable_que_no_existe_se_dice_en_vez_de_llamar_al_servicio():
    """Sin la variable el núcleo deja {env.X} literal, y Telegram contesta 404."""
    sin_token = [{**p, "url_base": p["url_base"]} for p in modulo._telegram("TELEGRAM_TOKEN")]
    http = FakeHttp()
    for tool, params in (("mensajeria.enviar", {"plantilla": "Telegram mensaje", "chat": "1", "mensaje": "x"}),
                         ("mensajeria.recibir", {"plantilla": "Telegram recibir"})):
        r = _correr(tool, params, http, plantillas=sin_token)
        assert r.status == "err" and "falta la variable TELEGRAM_TOKEN" in r.message, r.message
    assert http.calls == []
