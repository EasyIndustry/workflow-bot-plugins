"""
Plugin `procesos` — preguntarle al sistema operativo si un ejecutable está
corriendo, y correr un comando cualquiera, contra el port `process` que ya
existe.

Un flujo necesita saber si una app externa —cualquiera, no sólo esa— sigue
abierta antes de seguir. No hace falta un plugin con nombre de esa app ni un
port nuevo para esto: alcanza con listar procesos, algo que el sistema
operativo ya sabe hacer con un comando (`tasklist` en Windows, `pgrep` en
Linux/macOS) y que `process.run` ya puede ejecutar.

`ejecutar` (issue de catálogo: hacía falta para invocar un CLI cualquiera
—`claude -p "..."`, `git`, un script— desde un flujo) es lo mismo un
escalón más genérico: el port ya sabía correr cualquier comando, sólo
faltaba un tool que lo expusiera. `argumentos` es siempre una lista, nunca
una sola línea con espacios —el port nunca usa `shell=True`— así que no hay
forma de que un argumento con un `;` o un espacio adentro se interprete como
otra cosa.

Lo que este plugin NO resuelve, a propósito: adjuntarse a la ventana de esa
app para clickear o escribir en ella. Eso es el plugin `ventanas`, sobre el
port `window`.
"""

from __future__ import annotations

import os
import re
import sys

from backend.core import ports as port_names
from backend.core.contract import (
    FunctionTool,
    Output,
    Param,
    ParamType,
    Plugin,
    PluginManifest,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from backend.core.ports import PortError

MANIFEST = PluginManifest(
    name="procesos",
    label="Procesos",
    version="0.2.0",
    doc="Preguntarle al sistema operativo si un ejecutable está corriendo, y correr un comando.",
    ports=(port_names.PROCESS,),
)


def _comando_listado() -> tuple[str, ...]:
    """El comando que lista procesos, según el sistema operativo de la máquina."""
    if sys.platform.startswith("win"):
        return ("tasklist",)
    return ("ps", "-A", "-o", "comm=")


ESTA_CORRIENDO = ToolManifest(
    id="procesos.esta_corriendo",
    label="está corriendo",
    category="PROCESOS",
    doc=(
        "Lista los procesos de la máquina y busca uno cuyo nombre contenga "
        "'ejecutable' (sin distinguir mayúsculas). No distingue instancias: "
        "sólo dice si al menos una está corriendo."
    ),
    params=(Param("ejecutable", required=True, doc="Ej. 'notepad.exe' o 'notepad'."),),
    outputs=(Output("corriendo", ParamType.BOOL),),
)


def _esta_corriendo(ctx: ToolContext) -> ToolResult:
    ejecutable = ctx.params["ejecutable"].strip()
    if not ejecutable:
        return ToolResult.err("'ejecutable' vacío")

    resultado = ctx.port(port_names.PROCESS).run(_comando_listado(), timeout=15.0)
    if not resultado.ok and not resultado.stdout:
        return ToolResult.err(f"no se pudo listar procesos: {resultado.stderr or 'sin detalle'}")

    corriendo = ejecutable.lower() in resultado.stdout.lower()
    ctx.log(f"{ejecutable}: {'corriendo' if corriendo else 'no encontrado'}")
    if not corriendo:
        return ToolResult.err(f"'{ejecutable}' no está corriendo", corriendo=False)
    return ToolResult.ok(corriendo=True)


# ── procesos.ejecutar ─────────────────────────────────────────────────────

_PATRON_VAR = re.compile(r"\{(\w+)\}")


def _resolver_args(argumentos: list, obtener) -> list[str]:
    """
    Sustituye `{variable}` dentro de cada argumento, como texto — a
    diferencia de `sqlite`/`mariadb` no hay un tipo que conservar: todo
    argumento de un proceso es, al final, un string. `None` deja el
    placeholder literal (no se puede distinguir "no está" de "vale None").
    """

    def _uno(m: re.Match) -> str:
        val = obtener(m.group(1))
        return m.group(0) if val is None else str(val)

    return [_PATRON_VAR.sub(_uno, str(a)) for a in (argumentos or [])]


EJECUTAR = ToolManifest(
    id="procesos.ejecutar",
    label="ejecutar comando",
    category="PROCESOS",
    doc=(
        "Corre un comando del sistema y espera a que termine (nunca por un shell: "
        "'argumentos' es una lista, un elemento por argumento, nunca una sola línea con "
        "espacios). Un elemento de 'argumentos' que sea exactamente '{variable}' se "
        "sustituye contra cualquier otro param del propio nodo primero, y si no contra "
        "el contexto del run. Ej.: comando='claude', argumentos=['-p', '{prompt}']."
    ),
    params=(
        Param("comando", required=True, doc="El ejecutable. Ej. 'claude', 'git'."),
        Param("argumentos", ParamType.JSON, default=[], doc="Lista de argumentos, en orden."),
        Param("cwd", doc="Directorio de trabajo. Vacío: el que ya tiene el proceso del Bot."),
        Param("timeout", ParamType.FLOAT, default=60.0, doc="Segundos antes de cortarlo. Cortado no es error de infraestructura: vuelve como 'ok'=no."),
        Param(
            "variables_de_entorno", ParamType.JSON, default={},
            doc="Variables de entorno que se SUMAN a las del proceso del Bot (nunca las reemplazan: "
            "si reemplazaran, un comando que necesita PATH/HOME dejaría de arrancar).",
        ),
    ),
    extra_params=True,
    extra_params_doc="Cualquier otro param pisa al {variable} de mismo nombre dentro de 'argumentos'.",
    outputs=(
        Output("stdout", ParamType.STR),
        Output("stderr", ParamType.STR),
        Output("exit_code", ParamType.INT),
        Output("ok", ParamType.STR, doc="'si' o 'no': exit_code 0 y sin timeout."),
    ),
)


def _ejecutar(ctx: ToolContext) -> ToolResult:
    vacios = {"stdout": "", "stderr": "", "exit_code": -1, "ok": "no"}
    comando = (ctx.params.get("comando") or "").strip()
    if not comando:
        return ToolResult.err("'comando' vacío", **vacios)

    argumentos = _resolver_args(ctx.params.get("argumentos") or [], lambda n: ctx.extras.get(n, ctx.var(n)))
    cwd = (ctx.params.get("cwd") or "").strip() or None
    timeout = float(ctx.params.get("timeout") or 60.0)
    # Nunca `env=extra` crudo: subprocess.run reemplaza el entorno entero si
    # `env` no es None, y eso le haría perder PATH/HOME al comando. Se
    # arranca de una copia del propio entorno del Bot y sólo se le suma.
    extra_env = ctx.params.get("variables_de_entorno") or {}
    env = {**os.environ, **extra_env} if extra_env else None

    try:
        resultado = ctx.port(port_names.PROCESS).run([comando, *argumentos], cwd=cwd, timeout=timeout, env=env)
    except PortError as exc:
        return ToolResult.err(str(exc), **vacios)

    salida = {"stdout": resultado.stdout, "stderr": resultado.stderr, "exit_code": resultado.exit_code}
    if resultado.timed_out:
        ctx.log(f"{comando}: no terminó en {timeout}s")
        return ToolResult.err(f"'{comando}' no terminó en {timeout}s", **salida, ok="no")
    ctx.log(f"{comando}: exit {resultado.exit_code}")
    if resultado.exit_code != 0:
        return ToolResult.err(f"'{comando}' terminó con código {resultado.exit_code}", **salida, ok="no")
    return ToolResult.ok(**salida, ok="si")


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[
            FunctionTool(manifest=ESTA_CORRIENDO, fn=_esta_corriendo),
            FunctionTool(manifest=EJECUTAR, fn=_ejecutar),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
