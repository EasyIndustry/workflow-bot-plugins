"""
Plugin `procesos` — preguntarle al sistema operativo si un ejecutable está
corriendo, contra el port `process` que ya existe.

Un flujo necesita saber si una app externa —cualquiera, no sólo esa— sigue
abierta antes de seguir. No hace falta un plugin con nombre de esa app ni un
port nuevo para esto: alcanza con listar procesos, algo que el sistema
operativo ya sabe hacer con un comando (`tasklist` en Windows, `pgrep` en
Linux/macOS) y que `process.run` ya puede ejecutar.

Lo que este plugin NO resuelve, a propósito: adjuntarse a la ventana de esa
app para clickear o escribir en ella. Eso es el plugin `ventanas`, sobre el
port `window`.
"""

from __future__ import annotations

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

MANIFEST = PluginManifest(
    name="procesos",
    label="Procesos",
    version="0.1.0",
    doc="Preguntarle al sistema operativo si un ejecutable está corriendo.",
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


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[FunctionTool(manifest=ESTA_CORRIENDO, fn=_esta_corriendo)],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
