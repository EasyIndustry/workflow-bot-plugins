"""
Tests del plugin `oauth`, sin red: el proveedor (Google) y la API local de
este Bot son un `FakeHttp` guionado; el reloj, un `FakeClock`.
Necesita el núcleo (`backend/`) del repo de la app: se busca en la variable
`WORKFLOW_BOT_APP` o en `../workflow-bot-app`, al lado de este catálogo.
Corre con `python -m pytest oauth` desde la raíz del catálogo.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from urllib.parse import parse_qs, urlparse

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

import pytest  # noqa: E402

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.ports import HttpResponse, PortError  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeClock, FakeHttp  # noqa: E402

import oauth.plugin as modulo  # noqa: E402
from oauth.plugin import build_plugin  # noqa: E402

TOKEN_URL = "https://oauth2.googleapis.com/token"
BOT = "http://127.0.0.1:8123/api/core"
CUENTA = {
    "nombre": "google", "proveedor": "google", "client_id": "cid.apps.googleusercontent.com",
    "client_secret": "s3creto", "scopes": "https://www.googleapis.com/auth/gmail.modify",
    "variable": "GOOGLE_TOKEN", "refresh_token": "1//refresco",
}


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.setenv("BOT_PORT", "8123")
    modulo._VIGENTES.clear()


def _json(datos, status=200):
    return HttpResponse(status=status, text=json.dumps(datos), headers={"content-type": "application/json"})


def _http(**extra) -> FakeHttp:
    return FakeHttp({
        TOKEN_URL: _json({"access_token": "ya29.nuevo", "expires_in": 3599, "token_type": "Bearer"}),
        f"{BOT}/env/GOOGLE_TOKEN": _json({"name": "GOOGLE_TOKEN"}),
        **extra,
    })


def _ctx(params, cuentas, clock):
    def factory(declaracion, ports=None):
        if hasattr(declaracion, "split_params"):
            d, e = declaracion.split_params(params, {})
        else:  # una Action
            d, e = {p.name: params.get(p.name, p.default) for p in declaracion.params}, {}
        return ToolContext(
            run_id="", case_id="", params=d, extras=e, config={}, context={},
            log=lambda m, level="info": None, ports=ports or {},
            resources=lambda coleccion: cuentas if coleccion == "cuentas" else [],
        )
    return factory


def _reg(http, clock):
    reg = ToolRegistry(adapters={"http": http, "clock": clock})
    reg._add_plugin("oauth", "oauth:PLUGIN", build_plugin())
    return reg


def _token(http, clock=None, cuentas=(CUENTA,), cuenta="google"):
    clock = clock or FakeClock()
    return _reg(http, clock).execute("oauth.token", _ctx({"cuenta": cuenta}, list(cuentas), clock))


def _accion(nombre, params, http, cuentas=(CUENTA,), clock=None):
    clock = clock or FakeClock()
    return _reg(http, clock).execute_action("oauth", nombre, _ctx(params, list(cuentas), clock))


def _llamadas(http, url):
    return [c for c in http.calls if c["url"] == url]


# ── oauth.token ───────────────────────────────────────────────────────────

def test_token_renueva_con_el_refresh_y_lo_deja_en_la_variable_secreta():
    http = _http()
    r = _token(http)
    assert r.status == "ok", r.message
    assert r.outputs["variable"] == "GOOGLE_TOKEN" and r.outputs["renovado"] is True
    assert 3500 < r.outputs["vence_en"] <= 3599
    assert "ya29" not in json.dumps(r.outputs)  # el token no sale por la traza

    [pedido] = _llamadas(http, TOKEN_URL)
    cuerpo = parse_qs(pedido["body"])
    assert pedido["method"] == "POST" and pedido["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert cuerpo == {"client_id": ["cid.apps.googleusercontent.com"], "client_secret": ["s3creto"],
                      "grant_type": ["refresh_token"], "refresh_token": ["1//refresco"]}
    [guardado] = _llamadas(http, f"{BOT}/env/GOOGLE_TOKEN")
    assert guardado["method"] == "PUT" and json.loads(guardado["body"]) == {"value": "ya29.nuevo", "secret": True}


def test_mientras_sirve_no_pide_otro_ni_reescribe_la_variable():
    http, clock = _http(), FakeClock()
    _token(http, clock)
    clock.ticks += 3000  # 50 minutos: todavía le quedan más de 2
    r = _token(http, clock)
    assert r.outputs["renovado"] is False
    assert len(_llamadas(http, TOKEN_URL)) == 1 and len(_llamadas(http, f"{BOT}/env/GOOGLE_TOKEN")) == 1

    clock.ticks += 500  # ya entró en el margen de vencimiento
    assert _token(http, clock).outputs["renovado"] is True
    assert len(_llamadas(http, TOKEN_URL)) == 2


def test_reautorizar_invalida_el_token_de_memoria():
    http, clock = _http(), FakeClock()
    _token(http, clock)
    otra = {**CUENTA, "refresh_token": "1//otro"}
    assert _token(http, clock, cuentas=(otra,)).outputs["renovado"] is True


def test_sin_autorizar_refresh_revocado_y_cuenta_que_no_existe():
    r = _token(_http(), cuentas=({**CUENTA, "refresh_token": ""},))
    assert r.status == "err" and "Autorizar" in r.message

    revocado = _http(**{TOKEN_URL: _json({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, 400)})
    r = _token(revocado)
    assert r.status == "err" and "revoked" in r.message and "400" in r.message
    assert _llamadas(revocado, f"{BOT}/env/GOOGLE_TOKEN") == []

    r = _token(_http(), cuenta="otra")
    assert r.status == "err" and "Cargadas: google" in r.message


def test_variable_con_nombre_que_una_conexion_no_podria_leer():
    r = _token(_http(), cuentas=({**CUENTA, "variable": "google.token"},))
    assert r.status == "err" and "letras, números y _" in r.message


def test_si_este_bot_no_deja_guardar_la_variable_es_err_y_no_queda_en_memoria():
    http = _http(**{f"{BOT}/env/GOOGLE_TOKEN": _json({"detail": "no"}, 403)})
    r = _token(http)
    assert r.status == "err" and "403" in r.message
    assert modulo._VIGENTES == {}


# ── Autorizar ─────────────────────────────────────────────────────────────

def test_autorizar_paso_1_abre_la_pantalla_de_google_con_refresh_offline():
    r = _accion("autorizar", {"nombre": "google"}, FakeHttp())
    assert r.status == "ok"
    url = urlparse(r.outputs["abrir_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert q == {"client_id": "cid.apps.googleusercontent.com", "redirect_uri": "http://127.0.0.1:8123/",
                 "response_type": "code", "scope": "https://www.googleapis.com/auth/gmail.modify",
                 "access_type": "offline", "prompt": "consent"}


def test_autorizar_paso_2_canjea_guarda_el_refresh_sin_mandar_el_secret_y_deja_el_token():
    http = _http(**{
        TOKEN_URL: _json({"access_token": "ya29.primero", "refresh_token": "1//nuevo", "expires_in": 3599}),
        f"{BOT}/resources/oauth/cuentas/google": _json({"item": {}}),
    })
    pegado = "http://127.0.0.1:8123/?state=&code=4%2F0Abc-def&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.modify"
    r = _accion("autorizar", {"nombre": "google", "codigo": pegado}, http, cuentas=({**CUENTA, "refresh_token": ""},))
    assert r.status == "ok", r.message
    assert r.outputs["indicador"]["estado"] == "ok"

    canje = parse_qs(_llamadas(http, TOKEN_URL)[0]["body"])
    assert canje["code"] == ["4/0Abc-def"] and canje["grant_type"] == ["authorization_code"]
    assert canje["redirect_uri"] == ["http://127.0.0.1:8123/"]  # el mismo del paso 1

    [put] = _llamadas(http, f"{BOT}/resources/oauth/cuentas/google")
    item = json.loads(put["body"])["item"]
    assert item["refresh_token"] == "1//nuevo" and "client_secret" not in item and item["client_id"] == CUENTA["client_id"]
    assert json.loads(_llamadas(http, f"{BOT}/env/GOOGLE_TOKEN")[0]["body"])["value"] == "ya29.primero"


def test_autorizar_con_error_del_proveedor_o_sin_refresh():
    r = _accion("autorizar", {"nombre": "google", "codigo": "http://127.0.0.1:8123/?error=access_denied"}, FakeHttp())
    assert r.status == "err" and "access_denied" in r.message and r.outputs["indicador"]["estado"] == "err"

    sin_refresh = _http(**{TOKEN_URL: _json({"access_token": "ya29.x", "expires_in": 3599})})
    r = _accion("autorizar", {"nombre": "google", "codigo": "4/0Abc"}, sin_refresh)
    assert r.status == "err" and "revocar" in r.message


def test_probar_fuerza_un_token_nuevo():
    http = _http()
    _token(http)
    r = _accion("probar", {"nombre": "google"}, http)
    assert r.status == "ok" and "GOOGLE_TOKEN" in r.message and r.outputs["indicador"]["estado"] == "ok"
    assert len(_llamadas(http, TOKEN_URL)) == 2


def test_proveedor_otro_necesita_sus_urls():
    r = _accion("autorizar", {"nombre": "google"}, FakeHttp(), cuentas=({**CUENTA, "proveedor": "otro"},))
    assert r.status == "err" and "auth_url" in r.message


# ── Crear conexiones de Gmail ─────────────────────────────────────────────

def test_crear_gmail_escribe_las_conexiones_con_la_variable_y_saltea_las_que_existen():
    base = f"{BOT}/resources/connections/actions/"
    respuestas = {base + "Gmail%20%C2%B7%20leer": _json({"item": {"name": "Gmail · leer"}})}

    class Http(FakeHttp):
        def request(self, url, *, method="GET", headers=None, body=None, timeout=30.0):
            self.calls.append({"url": url, "method": method, "headers": dict(headers or {}), "body": body, "timeout": timeout})
            if method == "GET":
                return respuestas.get(url) or _json({"detail": "no existe"}, 404)
            return _json({"item": json.loads(body)["item"]})

    http = Http()
    r = _accion("crear_gmail", {"nombre": "google"}, http)
    assert r.status == "ok", r.message
    assert r.outputs["salteadas"] == ["Gmail · leer"] and "Gmail · buscar" in r.outputs["creadas"]
    puts = {json.loads(c["body"])["item"]["name"]: json.loads(c["body"])["item"] for c in http.calls if c["method"] == "PUT"}
    assert "Gmail · leer" not in puts and len(puts) == 8
    buscar = puts["Gmail · buscar"]
    assert buscar["headers"]["Authorization"] == "Bearer {env.GOOGLE_TOKEN}"
    assert buscar["url"].endswith("/messages?q={q}&maxResults=20") and buscar["results_path"] == "messages"
    assert puts["Gmail · responder"]["payload"] == {"raw": "{raw}", "threadId": "{thread_id}"}

    r = _accion("crear_gmail", {"nombre": "google", "pisar": True}, Http())
    assert len(r.outputs["creadas"]) == 9


def test_crear_gmail_solo_para_una_cuenta_de_google():
    r = _accion("crear_gmail", {"nombre": "google"}, FakeHttp(), cuentas=({**CUENTA, "proveedor": "microsoft"},))
    assert r.status == "err" and "Google" in r.message
