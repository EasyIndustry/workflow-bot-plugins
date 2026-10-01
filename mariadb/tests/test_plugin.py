"""
Tests del plugin `mariadb` (el tool, las Actions, el manifest), sobre
`FakeSocket` guionado — el protocolo en sí tiene su propia suite en
`test_protocolo.py`. Mismo mecanismo de `WORKFLOW_BOT_APP` que el resto del
catálogo.

Corre con `python -m pytest mariadb` desde la raíz del catálogo.
"""

from __future__ import annotations

import os
import pathlib
import sys

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

from backend.core.contract import Action, ToolContext, ToolManifest  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeSocket  # noqa: E402

from mariadb.plugin import CONEXIONES, _describir_extras, _resolver_sql, build_plugin  # noqa: E402

from test_protocolo import HOST, PORT, _OK, _en_chunks, _resultset_una_fila, _saludo  # noqa: E402


def _ctx_factory(node_params, config=None, context=None, resources=None):
    def factory(declaracion, ports=None):
        if isinstance(declaracion, Action):
            declaracion = ToolManifest(
                id=f"action.{declaracion.name}", label=declaracion.label,
                category="ACCIÓN", params=declaracion.params,
            )
        declarados, extras = declaracion.split_params(node_params, config or {})
        return ToolContext(
            run_id="run-test", case_id="0044",
            params=declarados, extras=extras,
            config=config or {}, context=context or {},
            log=lambda *_a, **_k: None, resources=resources, ports=ports or {},
        )

    return factory


def _registry(sock: FakeSocket) -> ToolRegistry:
    reg = ToolRegistry(adapters={"socket": sock})
    reg._add_plugin("mariadb", "mariadb.plugin:PLUGIN", build_plugin())
    return reg


# ── El plugin carga limpio ──────────────────────────────────────────────


def test_el_plugin_carga_sin_errores():
    reg = _registry(FakeSocket())
    assert not reg.errors
    plugin = next(p for p in reg.plugins if p.name == "mariadb")
    assert plugin.ports == ("socket",)
    assert {r.name for r in plugin.manifest.resources} == {"conexiones"}
    assert {a.name for a in plugin.manifest.actions} == {"probar", "probar_consulta"}
    assert set(reg.tool_ids) == {"mariadb.consultar"}


# ── _resolver_sql: cita cada {variable}, nunca pega texto crudo ─────────


def test_resolver_sql_cita_el_valor_resuelto():
    resultado = _resolver_sql("select * from t where id = {id}", {"id": 42}.get)
    assert resultado == "select * from t where id = 42"


def test_resolver_sql_un_string_sale_citado_y_escapado():
    resultado = _resolver_sql("select * from t where nombre = {n}", {"n": "o'brien"}.get)
    assert resultado == r"select * from t where nombre = 'o\'brien'"


def test_resolver_sql_placeholder_sin_resolver_queda_literal():
    resultado = _resolver_sql("select * from t where id = {no_existe}", {}.get)
    assert resultado == "select * from t where id = {no_existe}"


# ── mariadb.consultar — el tool de flujo ─────────────────────────────────


def test_consultar_resuelve_y_cita_variables_del_contexto_del_run():
    guardada = {
        "nombre": "casos", "host": HOST, "port": PORT, "usuario": "u", "clave": "p", "base": "memoria",
        "sql": "select * from casos where id = {id_externo}",
    }
    resources = lambda coleccion: {"conexiones": [guardada]}.get(coleccion, [])
    sock = FakeSocket({(HOST, PORT): _en_chunks(_saludo(), _OK, *_resultset_una_fila("id", "123"))})

    resultado = _registry(sock).execute(
        "mariadb.consultar",
        _ctx_factory({"connection": "casos"}, context={"id_externo": 123}, resources=resources),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["filas"] == [{"id": "123"}]
    assert resultado.outputs["cantidad"] == 1
    assert resultado.outputs["hay"] == "si"
    # El SQL que viajó tiene el valor citado, no crudo (lo manda COM_QUERY, op 0x03).
    enviados = [l["data"] for l in sock.calls if l["op"] == "send"]
    com_query = enviados[-2][4:]
    assert com_query == b"\x03" + b"select * from casos where id = 123"


def test_consultar_query_inexistente_lista_las_disponibles():
    resources = lambda coleccion: {"conexiones": [{"nombre": "otra"}]}.get(coleccion, [])

    resultado = _registry(FakeSocket()).execute(
        "mariadb.consultar", _ctx_factory({"connection": "no-existe"}, resources=resources),
    )

    assert resultado.status == "err"
    assert "otra" in resultado.message
    assert resultado.outputs["hay"] == "no"


def test_consultar_sin_filas_dice_hay_no():
    guardada = {"nombre": "q", "host": HOST, "port": PORT, "usuario": "u", "clave": "p", "base": "memoria", "sql": "select 1"}
    resources = lambda coleccion: {"conexiones": [guardada]}.get(coleccion, [])
    sock = FakeSocket({(HOST, PORT): _en_chunks(_saludo(), _OK, _OK)})  # OK de auth + OK sin result set

    resultado = _registry(sock).execute("mariadb.consultar", _ctx_factory({"connection": "q"}, resources=resources))

    assert resultado.status == "ok"
    assert resultado.outputs == {"filas": [], "primera": {}, "cantidad": 0, "hay": "no"}


def test_consultar_un_error_de_conexion_vuelve_como_err():
    guardada = {"nombre": "q", "host": "no-existe.test", "port": 9999, "usuario": "u", "clave": "p", "base": "x", "sql": "select 1"}
    resources = lambda coleccion: {"conexiones": [guardada]}.get(coleccion, [])

    resultado = _registry(FakeSocket()).execute("mariadb.consultar", _ctx_factory({"connection": "q"}, resources=resources))

    assert resultado.status == "err"
    assert "no se pudo conectar" in resultado.message


# ── Action "probar" — la Conexión guardada tal cual ──────────────────────


def test_probar_ejecuta_la_conexion_guardada_tal_cual():
    guardada = {"nombre": "ping", "host": HOST, "port": PORT, "usuario": "u", "clave": "p", "base": "memoria", "sql": "select 1 as uno"}
    resources = lambda coleccion: {"conexiones": [guardada]}.get(coleccion, [])
    sock = FakeSocket({(HOST, PORT): _en_chunks(_saludo(), _OK, *_resultset_una_fila("uno", "1"))})

    resultado = _registry(sock).execute_action("mariadb", "probar", _ctx_factory({"nombre": "ping"}, resources=resources))

    assert resultado.status == "ok"
    assert resultado.outputs["filas"] == [{"uno": "1"}]


def test_probar_sobre_conexion_inexistente():
    resultado = _registry(FakeSocket()).execute_action(
        "mariadb", "probar", _ctx_factory({"nombre": "no-existe"}, resources=lambda _c: []),
    )
    assert resultado.status == "err"


# ── Action "probar_consulta" — sin guardar nada ──────────────────────────


def test_probar_consulta_resuelve_y_cita_vars_del_formulario():
    sock = FakeSocket({(HOST, PORT): _en_chunks(_saludo(), _OK, *_resultset_una_fila("id", "7"))})

    resultado = _registry(sock).execute_action(
        "mariadb", "probar_consulta",
        _ctx_factory({
            "host": HOST, "port": PORT, "usuario": "u", "clave": "p", "base": "memoria",
            "sql": "select id from casos where id = {id_externo}",
            "vars": {"id_externo": 7},
        }),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["filas"] == [{"id": "7"}]
    enviados = [l["data"] for l in sock.calls if l["op"] == "send"]
    assert enviados[-2][4:] == b"\x03select id from casos where id = 7"


def test_probar_consulta_sin_variable_declarada_queda_literal():
    sock = FakeSocket({(HOST, PORT): _en_chunks(_saludo(), _OK, _OK)})

    resultado = _registry(sock).execute_action(
        "mariadb", "probar_consulta",
        _ctx_factory({"host": HOST, "port": PORT, "usuario": "u", "clave": "p", "base": "x",
                      "sql": "select * from t where id = {id_externo}"}),
    )

    assert resultado.status == "ok"
    enviados = [l["data"] for l in sock.calls if l["op"] == "send"]
    assert enviados[-2][4:] == b"\x03select * from t where id = {id_externo}"


# ── Los params extra que la tarjeta del flujo ofrece ────────────────────


def _leer(guardada):
    def leer(coleccion, clave, key_field="nombre"):
        return guardada if (coleccion, clave) == ("conexiones", "casos") else None
    return leer


def test_params_extra_son_las_variables_del_sql():
    guardada = {"sql": "select * from t where a={a} and b={b}"}

    extras = _describir_extras({"connection": "casos"}, _leer(guardada))

    assert [p.name for p in extras] == ["a", "b"]
    assert "casos" in extras[0].doc


def test_params_extra_sin_conexion_elegida_o_inexistente():
    guardada = {"sql": "select * from t where a={a}"}
    assert _describir_extras({}, _leer(guardada)) == ()
    assert _describir_extras({"connection": "no existe"}, _leer(guardada)) == ()


def test_el_tool_sabe_describirse():
    [tool] = build_plugin().tools
    assert callable(getattr(tool, "describe_extra_params", None))
    assert tool.manifest.extra_params


def test_el_param_connection_declara_su_coleccion():
    [tool] = build_plugin().tools
    [param] = [p for p in tool.manifest.params if p.name == "connection"]
    assert param.options_from == CONEXIONES.name
    assert param.choices == ()


def test_el_campo_clave_esta_marcado_secreto():
    [campo] = [f for f in CONEXIONES.fields if f.name == "clave"]
    assert campo.secret is True
