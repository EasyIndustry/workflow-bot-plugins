"""
Tests del plugin `sqlite`, sin tocar un archivo real: `FakeSqliteFile`
guionado (no interpreta el SQL, igual que el resto de los fakes de
almacenamiento del núcleo).

Necesita el núcleo (`backend/`) de una instalación de workflow-bot-core con
los ports `socket`/`sqlite_file` (workflow-bot-core#39/#40): se busca en la
variable `WORKFLOW_BOT_APP` o en `../workflow-bot-app`, al lado de este
catálogo (mismo mecanismo que usan `oauth`, `bots` y `connections`).

Corre con `python -m pytest sqlite` desde la raíz del catálogo.
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
from backend.core.ports import PortError  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeSqliteFile  # noqa: E402

from sqlite.plugin import QUERIES, _describir_extras, _resolver_params, build_plugin  # noqa: E402


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


def _registry(sqlite_file: FakeSqliteFile) -> ToolRegistry:
    reg = ToolRegistry(adapters={"sqlite_file": sqlite_file})
    reg._add_plugin("sqlite", "sqlite.plugin:PLUGIN", build_plugin())
    return reg


# ── El plugin carga limpio ──────────────────────────────────────────────


def test_el_plugin_carga_sin_errores():
    reg = _registry(FakeSqliteFile())
    assert not reg.errors
    plugin = next(p for p in reg.plugins if p.name == "sqlite")
    assert plugin.ports == ("sqlite_file",)
    assert {r.name for r in plugin.manifest.resources} == {"queries"}
    assert {a.name for a in plugin.manifest.actions} == {"probar", "probar_consulta"}
    assert set(reg.tool_ids) == {"sqlite.consultar"}


# ── _resolver_params: nunca toca `sql`, sólo los valores de `params` ────


def test_resolver_params_preserva_el_tipo_en_un_placeholder_exacto():
    """`"{id}"` solo, sin texto alrededor, se reemplaza por el valor crudo."""
    resultado = _resolver_params(["{id}", "literal"], {"id": 42}.get)
    assert resultado == [42, "literal"]


def test_resolver_params_substituye_como_texto_si_esta_embebido():
    resultado = _resolver_params(["pref_{id}_suf"], {"id": 42}.get)
    assert resultado == ["pref_42_suf"]


def test_resolver_params_placeholder_sin_resolver_queda_literal():
    resultado = _resolver_params(["{no_existe}"], {}.get)
    assert resultado == ["{no_existe}"]


def test_resolver_params_es_recursivo_sobre_listas_y_dicts():
    resultado = _resolver_params([{"a": "{x}"}, ["{y}"]], {"x": 1, "y": 2}.get)
    assert resultado == [{"a": 1}, [2]]


# ── sqlite.consultar — el tool de flujo ──────────────────────────────────


def test_consultar_resuelve_variables_del_contexto_del_run():
    guardada = {"path": "/datos/casos.sqlite", "sql": "select * from casos where id = ?", "params": ["{id_externo}"]}
    resources = lambda coleccion: {"queries": [{"nombre": "casos", **guardada}]}.get(coleccion, [])
    sqlite_file = FakeSqliteFile({"/datos/casos.sqlite": [{"id": 123, "estado": "listo"}]})

    resultado = _registry(sqlite_file).execute(
        "sqlite.consultar",
        _ctx_factory({"connection": "casos"}, context={"id_externo": 123}, resources=resources),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["filas"] == [{"id": 123, "estado": "listo"}]
    assert resultado.outputs["primera"] == {"id": 123, "estado": "listo"}
    assert resultado.outputs["cantidad"] == 1
    assert resultado.outputs["hay"] == "si"
    llamada = sqlite_file.calls[0]
    assert llamada["params"] == (123,)  # tipo conservado, no "123"


def test_consultar_sin_filas_dice_hay_no():
    guardada = {"path": "/datos/vacio.sqlite", "sql": "select 1", "params": []}
    resources = lambda coleccion: {"queries": [{"nombre": "vacio", **guardada}]}.get(coleccion, [])
    sqlite_file = FakeSqliteFile({"/datos/vacio.sqlite": []})

    resultado = _registry(sqlite_file).execute(
        "sqlite.consultar", _ctx_factory({"connection": "vacio"}, resources=resources),
    )

    assert resultado.status == "ok"
    assert resultado.outputs == {"filas": [], "primera": {}, "cantidad": 0, "hay": "no"}


def test_consultar_query_inexistente_lista_las_disponibles():
    resources = lambda coleccion: {"queries": [{"nombre": "otra"}]}.get(coleccion, [])

    resultado = _registry(FakeSqliteFile()).execute(
        "sqlite.consultar", _ctx_factory({"connection": "no-existe"}, resources=resources),
    )

    assert resultado.status == "err"
    assert "otra" in resultado.message
    # Los outputs vacíos siguen presentes, para que un nodo de decisión no se quede sin {hay}.
    assert resultado.outputs["hay"] == "no"


def test_consultar_un_extra_del_nodo_pisa_al_contexto():
    guardada = {"path": "/datos/x.sqlite", "sql": "select * from t where col = ?", "params": ["{col}"]}
    resources = lambda coleccion: {"queries": [{"nombre": "q", **guardada}]}.get(coleccion, [])
    sqlite_file = FakeSqliteFile({"/datos/x.sqlite": [{"col": "AR"}]})

    resultado = _registry(sqlite_file).execute(
        "sqlite.consultar",
        _ctx_factory({"connection": "q", "col": "AR"}, context={"col": "del contexto, no se usa"}, resources=resources),
    )

    assert resultado.status == "ok"
    assert sqlite_file.calls[0]["params"] == ("AR",)


def test_consultar_placeholder_sin_resolver_queda_literal():
    guardada = {"path": "/datos/x.sqlite", "sql": "select * from t where id = ?", "params": ["{id_externo}"]}
    resources = lambda coleccion: {"queries": [{"nombre": "q", **guardada}]}.get(coleccion, [])
    sqlite_file = FakeSqliteFile({"/datos/x.sqlite": []})

    resultado = _registry(sqlite_file).execute(
        "sqlite.consultar", _ctx_factory({"connection": "q"}, context={}, resources=resources),
    )

    assert resultado.status == "ok"
    assert sqlite_file.calls[0]["params"] == ("{id_externo}",)


def test_consultar_un_error_del_port_vuelve_como_err():
    guardada = {"path": "/no/esta/guionado.sqlite", "sql": "select 1", "params": []}
    resources = lambda coleccion: {"queries": [{"nombre": "q", **guardada}]}.get(coleccion, [])

    resultado = _registry(FakeSqliteFile()).execute(
        "sqlite.consultar", _ctx_factory({"connection": "q"}, resources=resources),
    )

    assert resultado.status == "err"
    assert "guionado.sqlite" in resultado.message


# ── Action "probar" — probar una Query ya guardada ───────────────────────


def test_probar_ejecuta_la_query_guardada_tal_cual():
    guardada = {"path": "/datos/ping.sqlite", "sql": "select 1", "params": []}
    resources = lambda coleccion: {"queries": [{"nombre": "ping", **guardada}]}.get(coleccion, [])
    sqlite_file = FakeSqliteFile({"/datos/ping.sqlite": [{"1": 1}]})

    resultado = _registry(sqlite_file).execute_action(
        "sqlite", "probar", _ctx_factory({"nombre": "ping"}, resources=resources),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["filas"] == [{"1": 1}]
    assert resultado.outputs["cantidad"] == 1


def test_probar_sobre_query_inexistente():
    resultado = _registry(FakeSqliteFile()).execute_action(
        "sqlite", "probar", _ctx_factory({"nombre": "no-existe"}, resources=lambda _c: []),
    )

    assert resultado.status == "err"


# ── Action "probar_consulta" — probar antes de guardar ───────────────────


def test_probar_consulta_resuelve_vars_del_formulario():
    sqlite_file = FakeSqliteFile({"/datos/casos.sqlite": [{"id": 7, "estado": "ok"}]})

    resultado = _registry(sqlite_file).execute_action(
        "sqlite", "probar_consulta",
        _ctx_factory({
            "path": "/datos/casos.sqlite",
            "sql": "select * from casos where id = ?",
            "params": ["{id_externo}"],
            "vars": {"id_externo": 7},
        }),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["filas"] == [{"id": 7, "estado": "ok"}]
    assert sqlite_file.calls[0]["params"] == (7,)


def test_probar_consulta_sin_variable_declarada_queda_literal():
    sqlite_file = FakeSqliteFile({"/datos/x.sqlite": []})

    resultado = _registry(sqlite_file).execute_action(
        "sqlite", "probar_consulta",
        _ctx_factory({"path": "/datos/x.sqlite", "sql": "select * from t where id = ?", "params": ["{id_externo}"]}),
    )

    assert resultado.status == "ok"
    assert sqlite_file.calls[0]["params"] == ("{id_externo}",)


def test_probar_consulta_no_necesita_nada_guardado():
    """Corre sólo con lo que trae el formulario — nada de `resources`."""
    sqlite_file = FakeSqliteFile({"/datos/ping.sqlite": [{"ok": 1}]})

    resultado = _registry(sqlite_file).execute_action(
        "sqlite", "probar_consulta",
        _ctx_factory({"path": "/datos/ping.sqlite", "sql": "select 1"}),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["cantidad"] == 1


# ── Los params extra que la tarjeta del flujo ofrece ────────────────────


def _leer(guardada):
    """El `leer_item` que liga el núcleo: sólo lectura."""
    def leer(coleccion, clave, key_field="nombre"):
        return guardada if (coleccion, clave) == ("queries", "casos") else None
    return leer


def test_params_extra_son_las_variables_de_los_params_de_la_query():
    guardada = {"path": "/x.sqlite", "sql": "select * from t where a=? and b=?", "params": ["{a}", "{b}"]}

    extras = _describir_extras({"connection": "casos"}, _leer(guardada))

    assert [p.name for p in extras] == ["a", "b"]
    assert all(not p.required for p in extras)
    assert "casos" in extras[0].doc


def test_un_campo_con_literal_no_se_ofrece():
    guardada = {"path": "/x.sqlite", "sql": "select * from t where a=? and b=?", "params": ["literal", "{b}"]}

    extras = _describir_extras({"connection": "casos"}, _leer(guardada))

    assert [p.name for p in extras] == ["b"]


def test_params_extra_sin_conexion_elegida_o_inexistente():
    guardada = {"path": "/x.sqlite", "sql": "select * from t where a=?", "params": ["{a}"]}
    assert _describir_extras({}, _leer(guardada)) == ()
    assert _describir_extras({"connection": "no existe"}, _leer(guardada)) == ()


def test_el_tool_sabe_describirse():
    """Es el optativo del contrato, colgado del FunctionTool."""
    [tool] = build_plugin().tools
    assert callable(getattr(tool, "describe_extra_params", None))
    assert tool.manifest.extra_params


def test_el_param_connection_declara_su_coleccion():
    [tool] = build_plugin().tools
    [param] = [p for p in tool.manifest.params if p.name == "connection"]
    assert param.options_from == QUERIES.name
    assert param.choices == ()
