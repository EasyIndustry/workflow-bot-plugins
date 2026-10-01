

# `PLUGIN` a nivel de paquete: es lo que busca `--plugin connections=connections`
# (la CLI y el MCP importan `connections:PLUGIN`).
from .plugin import PLUGIN  # noqa: E402,F401

__all__ = ["PLUGIN"]
