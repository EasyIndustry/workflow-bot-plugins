"""
Re-exporta `PLUGIN` a nivel de paquete: es lo que busca el auto-descubrimiento
de `plugins_dir` (`backend/core/instance.py:_plugins_de_la_carpeta`) para una
carpeta con `__init__.py` — importa `archivos:PLUGIN`, no `archivos.plugin:PLUGIN`.
"""

from .plugin import MANIFEST, PLUGIN, build_plugin

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
