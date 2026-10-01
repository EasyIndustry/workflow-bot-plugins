"""
Tests del plugin `procesos`, sobre `FakeProcess` guionado (nunca corre un
comando de verdad). Mismo mecanismo de `WORKFLOW_BOT_APP` que el resto del
catálogo.

Corre con `python -m pytest procesos` desde la raíz del catálogo.
"""

from __future__ import annotations

import os
import pathlib
import sys

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.ports import ProcessResult  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeProcess  # noqa: E402

from procesos.plugin import build_plugin  # noqa: E402


def _ctx_factory(node_params, context=None):
    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params(node_params, {})
        return ToolContext(
            run_id="r", case_id="1", params=declarados, extras=extras,
            config={}, context=context or {},
            log=lambda *_a, **_k: None, ports=ports or {},
        )

    return factory


def _registry(process: FakeProcess) -> ToolRegistry:
    reg = ToolRegistry(adapters={"process": process})
    reg._add_plugin("procesos", "procesos.plugin:PLUGIN", build_plugin())
    return reg


def test_el_plugin_carga_sin_errores():
    reg = _registry(FakeProcess())
    assert not reg.errors
    assert set(reg.tool_ids) == {"procesos.esta_corriendo", "procesos.ejecutar"}


# ── procesos.esta_corriendo ───────────────────────────────────────────────


def test_esta_corriendo_lo_encuentra_en_el_listado():
    import sys as _sys

    process = FakeProcess()
    comando = ("tasklist",) if _sys.platform.startswith("win") else ("ps", "-A", "-o", "comm=")
    process.stub(comando[0], stdout="notepad.exe\nbash\n")

    resultado = _registry(process).execute("procesos.esta_corriendo", _ctx_factory({"ejecutable": "notepad"}))

    assert resultado.status == "ok"
    assert resultado.outputs["corriendo"] is True


def test_esta_corriendo_no_lo_encuentra():
    import sys as _sys

    process = FakeProcess()
    comando = ("tasklist",) if _sys.platform.startswith("win") else ("ps", "-A", "-o", "comm=")
    process.stub(comando[0], stdout="bash\n")

    resultado = _registry(process).execute("procesos.esta_corriendo", _ctx_factory({"ejecutable": "notepad"}))

    assert resultado.status == "err"
    assert resultado.outputs["corriendo"] is False


# ── procesos.ejecutar ──────────────────────────────────────────────────────


def test_ejecutar_corre_el_comando_y_trae_la_salida():
    process = FakeProcess().stub("claude", stdout="hola!\n")

    resultado = _registry(process).execute(
        "procesos.ejecutar", _ctx_factory({"comando": "claude", "argumentos": ["-p", "decime hola"]}),
    )

    assert resultado.status == "ok"
    assert resultado.outputs["stdout"] == "hola!\n"
    assert resultado.outputs["exit_code"] == 0
    assert resultado.outputs["ok"] == "si"
    assert process.calls[0]["command"] == ["claude", "-p", "decime hola"]


def test_ejecutar_resuelve_variables_del_contexto_del_run():
    process = FakeProcess().stub("claude", stdout="ok")

    resultado = _registry(process).execute(
        "procesos.ejecutar",
        _ctx_factory({"comando": "claude", "argumentos": ["-p", "{prompt}"]}, context={"prompt": "resumime esto"}),
    )

    assert resultado.status == "ok"
    assert process.calls[0]["command"] == ["claude", "-p", "resumime esto"]


def test_ejecutar_un_extra_del_nodo_pisa_al_contexto():
    process = FakeProcess().stub("claude", stdout="ok")

    resultado = _registry(process).execute(
        "procesos.ejecutar",
        _ctx_factory(
            {"comando": "claude", "argumentos": ["-p", "{prompt}"], "prompt": "del nodo"},
            context={"prompt": "del contexto, no se usa"},
        ),
    )

    assert resultado.status == "ok"
    assert process.calls[0]["command"] == ["claude", "-p", "del nodo"]


def test_ejecutar_placeholder_sin_resolver_queda_literal():
    process = FakeProcess().stub("claude", stdout="ok")

    resultado = _registry(process).execute(
        "procesos.ejecutar", _ctx_factory({"comando": "claude", "argumentos": ["-p", "{no_existe}"]}),
    )

    assert resultado.status == "ok"
    assert process.calls[0]["command"] == ["claude", "-p", "{no_existe}"]


def test_ejecutar_exit_code_distinto_de_cero_es_err_pero_trae_la_salida():
    process = FakeProcess().stub("git", exit_code=128, stderr="fatal: no es un repositorio\n")

    resultado = _registry(process).execute("procesos.ejecutar", _ctx_factory({"comando": "git", "argumentos": ["status"]}))

    assert resultado.status == "err"
    assert resultado.outputs["exit_code"] == 128
    assert resultado.outputs["stderr"] == "fatal: no es un repositorio\n"
    assert resultado.outputs["ok"] == "no"


def test_ejecutar_timeout_es_err_y_no_porterror():
    process = FakeProcess(results={"sleep": ProcessResult(exit_code=-1, timed_out=True)})

    resultado = _registry(process).execute(
        "procesos.ejecutar", _ctx_factory({"comando": "sleep", "argumentos": ["999"], "timeout": 1}),
    )

    assert resultado.status == "err"
    assert "no terminó" in resultado.message
    assert resultado.outputs["ok"] == "no"


def test_ejecutar_un_comando_no_permitido_es_err_no_crash():
    from backend.core.ports import PortError

    class _ConAllowlist(FakeProcess):
        def run(self, command, *, cwd=None, timeout=None, env=None):
            raise PortError("'rm' no está en la lista de comandos permitidos. Permitidos: claude, git")

    resultado = _registry(_ConAllowlist()).execute(
        "procesos.ejecutar", _ctx_factory({"comando": "rm", "argumentos": ["-rf", "/"]}),
    )

    assert resultado.status == "err"
    assert "permitidos" in resultado.message


def test_ejecutar_sin_comando_es_err():
    resultado = _registry(FakeProcess()).execute("procesos.ejecutar", _ctx_factory({"comando": ""}))
    assert resultado.status == "err"


def test_ejecutar_variables_de_entorno_se_suman_no_reemplazan():
    """Si `env` reemplazara el entorno entero, un comando que necesita PATH no arrancaría."""
    import os as _os

    process = FakeProcess()
    original_run = FakeProcess.run
    recibido = {}

    def _run_que_mira_env(self, command, *, cwd=None, timeout=None, env=None):
        recibido["env"] = env
        return original_run(self, command, cwd=cwd, timeout=timeout, env=env)

    process.run = _run_que_mira_env.__get__(process)
    process.stub("claude", stdout="ok")

    resultado = _registry(process).execute(
        "procesos.ejecutar",
        _ctx_factory({"comando": "claude", "argumentos": [], "variables_de_entorno": {"ANTHROPIC_API_KEY": "x"}}),
    )

    assert resultado.status == "ok"
    assert recibido["env"]["ANTHROPIC_API_KEY"] == "x"
    # El resto del entorno del propio proceso del Bot sigue ahí.
    clave_cualquiera = next(iter(_os.environ), None)
    if clave_cualquiera:
        assert recibido["env"].get(clave_cualquiera) == _os.environ[clave_cualquiera]
