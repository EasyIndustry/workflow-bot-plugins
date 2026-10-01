

# `PLUGIN` a nivel de paquete: es lo que busca `--plugin mariadb=mariadb`
# (la CLI y el MCP importan `mariadb:PLUGIN`).
from .plugin import PLUGIN  # noqa: E402,F401

__all__ = ["PLUGIN"]
