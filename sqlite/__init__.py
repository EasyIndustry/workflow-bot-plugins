

# `PLUGIN` a nivel de paquete: es lo que busca `--plugin sqlite=sqlite`
# (la CLI y el MCP importan `sqlite:PLUGIN`).
from .plugin import PLUGIN  # noqa: E402,F401

__all__ = ["PLUGIN"]
