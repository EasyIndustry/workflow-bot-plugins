"""
`archivos.comparar_conteo`: todo contra todo, o sólo lo que coincide con una
etiqueta o un patrón. Con el filesystem falso del núcleo.
Corre con `python -m pytest plugins/archivos`.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeFs  # noqa: E402

from plugins.archivos.plugin import build_plugin  # noqa: E402

# El caso que motivó el 'patron': la salida de un generador trae cinco archivos
# por documento y la carpeta exportada sólo el entregable. Las dos
# carpetas están completas —no falta nada—, pero contadas enteras dan 15 vs 3.
SALIDA_GENERADOR = "D:/generador/OUTPUT/AP962"
EXPORTADA = "//server-nuevo/CASOS/AP962 Cliente 1.0/Cliente 1.0 - exportado"
MODELOS = ("A00-B", "B00-B", "B01-A")
ARCHIVOS = {
    **{
        f"{SALIDA_GENERADOR}/AP962-{m}{sufijo}": ""
        for m in MODELOS
        for sufijo in (".pdf", "-firma.pdf", "-borrador.pdf", "-original.png", "-meta.txt")
    },
    **{f"{EXPORTADA}/AP962-{m}.pdf": "" for m in MODELOS},
}

ENTREGABLE = r"^[A-Z]{2}\d{3}-[AB]\d{2}-[A-Z]\.pdf$"


def _comparar(archivos=None, log=None, **params):
    reg = ToolRegistry(adapters={"fs": FakeFs(files=archivos or ARCHIVOS)})
    reg._add_plugin("archivos", "plugins.archivos:PLUGIN", build_plugin())
    anotar = (lambda m, level="info": log(m)) if log else (lambda *_a, **_k: None)

    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params(
            {"carpeta_a": SALIDA_GENERADOR, "carpeta_b": EXPORTADA, **params}, {}
        )
        return ToolContext(
            run_id="run-test", case_id="AP962", params=declarados, extras=extras,
            config={}, context={}, log=anotar, ports=ports or {},
        )

    return reg.execute("archivos.comparar_conteo", factory)


def test_sin_criterio_cuenta_carpeta_contra_carpeta():
    r = _comparar()
    assert r.status == "err"
    assert r.outputs == {"cantidad_a": 15, "cantidad_b": 3}


def test_la_etiqueta_no_puede_aislar_el_entregable():
    # Lo que distingue al entregable es un sufijo que NO tiene, y un substring
    # no sabe expresar eso: el prefijo está en los 15 y '.pdf' también en los
    # derivados. De acá sale la necesidad del 'patron'.
    assert _comparar(etiqueta="AP962").outputs == {"cantidad_a": 15, "cantidad_b": 3}
    assert _comparar(etiqueta=".pdf").outputs == {"cantidad_a": 9, "cantidad_b": 3}


def test_el_mismo_patron_de_los_dos_lados_hace_comparables_las_carpetas():
    r = _comparar(patron_a=ENTREGABLE, patron_b=ENTREGABLE)
    assert r.status == "ok"
    assert r.outputs == {"cantidad_a": 3, "cantidad_b": 3}


def test_filtrar_un_solo_lado_alcanza_cuando_el_otro_ya_viene_limpio():
    # La carpeta exportada ya tiene sólo entregables, así que el patrón se pone
    # donde sobra algo y el otro lado se cuenta entero. Sirve en las dos
    # posiciones: cuál carpeta es 'a' y cuál 'b' deja de importar.
    assert _comparar(patron_a=ENTREGABLE).outputs == {"cantidad_a": 3, "cantidad_b": 3}
    r = _comparar(carpeta_a=EXPORTADA, carpeta_b=SALIDA_GENERADOR, patron_b=ENTREGABLE)
    assert r.status == "ok"
    assert r.outputs == {"cantidad_a": 3, "cantidad_b": 3}


def test_filtrar_un_solo_lado_deja_que_el_otro_se_ensucie():
    # La contracara de lo anterior, y la razón de que el doc recomiende el mismo
    # patrón de los dos lados: un archivo suelto en la carpeta sin filtrar mueve
    # el conteo y el control se pone en rojo sin que falte ningún entregable.
    sucia = {**ARCHIVOS, f"{EXPORTADA}/Thumbs.db": ""}
    assert _comparar(archivos=sucia, patron_a=ENTREGABLE).status == "err"
    r = _comparar(archivos=sucia, patron_a=ENTREGABLE, patron_b=ENTREGABLE)
    assert r.status == "ok"
    assert r.outputs == {"cantidad_a": 3, "cantidad_b": 3}


def test_la_etiqueta_va_sobre_las_dos_y_se_combina_con_el_patron():
    # La etiqueta identifica el caso (igual de los dos lados); el patrón, la
    # forma del archivo (distinta de cada lado). Acá recorta al lado B.
    r = _comparar(etiqueta="-B0", patron_a=ENTREGABLE, patron_b=ENTREGABLE)
    assert r.status == "ok"
    assert r.outputs == {"cantidad_a": 2, "cantidad_b": 2}


def test_el_patron_cuenta_de_menos_en_los_dos_lados_y_sigue_siendo_ok():
    # Un patrón que no matchea nada da 0 y 0: coinciden, pero no comparó nada.
    # Es 'ok' a propósito —el flujo pregunta si son iguales, no si hay algo—,
    # y el log queda con el criterio para que se vea de dónde salió el cero.
    registro = []
    r = _comparar(patron_a=r"^nada$", patron_b=r"^nada$", log=registro.append)
    assert r.status == "ok"
    assert r.outputs == {"cantidad_a": 0, "cantidad_b": 0}
    assert any("/^nada$/" in m and ": 0" in m for m in registro)


def test_una_carpeta_que_no_existe_es_err_y_no_un_cero_silencioso():
    # Antes contaba 0, así que dos rutas mal escritas daban 0 vs 0 -> 'ok': el
    # control más tranquilizador era el que no miraba nada.
    r = _comparar(carpeta_b="D:/no/existe")
    assert r.status == "err"
    assert "no existe la carpeta" in r.message and "D:/no/existe" in r.message

    r = _comparar(carpeta_a="D:/tampoco", carpeta_b="D:/no/existe")
    assert r.status == "err"


def test_una_ruta_que_es_un_archivo_no_pasa_por_carpeta():
    r = _comparar(carpeta_b=f"{EXPORTADA}/AP962-A00-B.pdf")
    assert r.status == "err"
    assert "no existe la carpeta" in r.message


def test_con_regex_rota_es_err_y_dice_cual_de_los_dos():
    assert "'patron_a'" in _comparar(patron_a="[").message
    assert "'patron_b'" in _comparar(patron_b="[").message
    assert "expresión regular" in _comparar(patron_a="[").message


def test_el_log_dice_el_criterio_de_cada_lado_por_separado():
    # Con un lado filtrado y el otro no, un 3 vs 3 sale de dos preguntas
    # distintas: si el log mostrara sólo los números no se vería.
    registro = []
    _comparar(patron_a=ENTREGABLE, log=registro.append)
    assert len(registro) == 1
    assert f"/{ENTREGABLE}/): 3" in registro[0]
    assert "todos los archivos): 3" in registro[0]
