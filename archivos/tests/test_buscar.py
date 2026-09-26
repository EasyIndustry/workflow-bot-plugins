"""
`archivos.buscar`: por etiqueta, por expresión regular, o las dos. Con el
filesystem falso del núcleo. Corre con `python -m pytest plugins/archivos`.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeFs, _norm  # noqa: E402

from plugins.archivos.plugin import build_plugin  # noqa: E402

CASO = "C:/casos/PRUE001/datos"
ARCHIVOS = {
    f"{CASO}/PRUE001-A01-A.pdf": "",
    f"{CASO}/PRUE001-B01-A.pdf": "",
    f"{CASO}/PRUE001-A01-A-borrador.pdf": "",
    f"{CASO}/notas.txt": "",
    f"{CASO}/sub/otro-A02-B.PDF": "",
}


def _buscar(log=None, **params):
    reg = ToolRegistry(adapters={"fs": FakeFs(files=ARCHIVOS)})
    reg._add_plugin("archivos", "plugins.archivos:PLUGIN", build_plugin())
    anotar = (lambda m, level="info": log(m)) if log else (lambda *_a, **_k: None)

    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params({"carpeta": CASO, **params}, {})
        return ToolContext(
            run_id="run-test", case_id="PRUE001", params=declarados, extras=extras,
            config={}, context={}, log=anotar, ports=ports or {},
        )

    return reg.execute("archivos.buscar", factory)


def test_por_etiqueta_es_substring_sin_mayusculas_y_recursivo():
    r = _buscar(etiqueta=".pdf")
    assert r.status == "ok"
    assert r.outputs["cantidad"] == 4
    assert r.outputs["primera"].endswith("PRUE001-A01-A-borrador.pdf")  # orden del walk: alfabético


def test_por_patron_distingue_el_modelo_de_sus_derivados():
    # Sólo los entregables: <nombre>-A01-A.pdf, no el -borrador.pdf que los acompaña.
    r = _buscar(patron=r"^[A-Z0-9]+-[AB]\d{2}-[A-Z]\.pdf$")
    assert r.status == "ok"
    assert sorted(pathlib.Path(p).name for p in r.outputs["rutas"]) == [
        "PRUE001-A01-A.pdf", "PRUE001-B01-A.pdf", "otro-A02-B.PDF",
    ]


def test_el_patron_con_cuantificadores_escritos_a_mano():
    # Un id de dos letras y tres dígitos, un lado (A/B), dos dígitos y un tipo.
    # En el .mmd va sin llaves ({2} se leería como variable) y con la extensión adentro.
    patron = r"^[A-Z][A-Z]\d\d\d-[AB]\d\d-[A-Z]\.pdf$"
    archivos = {
        f"{CASO}/AB123-A01-A.pdf": "", f"{CASO}/AB123-B12-B.PDF": "",
        f"{CASO}/AB123-A01-A-borrador.pdf": "", f"{CASO}/PRUE001-A01-A.pdf": "",
        f"{CASO}/AB1234-A01-A.pdf": "", f"{CASO}/AB123-A1-A.pdf": "",
    }
    reg = ToolRegistry(adapters={"fs": FakeFs(files=archivos)})
    reg._add_plugin("archivos", "plugins.archivos:PLUGIN", build_plugin())

    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params({"carpeta": CASO, "patron": patron}, {})
        return ToolContext(
            run_id="run-test", case_id="AB123", params=declarados, extras=extras,
            config={}, context={}, log=lambda *_a, **_k: None, ports=ports or {},
        )

    r = reg.execute("archivos.buscar", factory)
    assert r.status == "ok"
    assert sorted(pathlib.Path(p).name for p in r.outputs["rutas"]) == ["AB123-A01-A.pdf", "AB123-B12-B.PDF"]


def test_etiqueta_y_patron_se_combinan():
    r = _buscar(etiqueta="PRUE001", patron=r"-A\d{2}-")
    assert r.status == "ok"
    assert r.outputs["cantidad"] == 2


def test_devuelve_la_carpeta_que_contiene_lo_encontrado():
    # `buscar` da archivos, y hay tools que piden una carpeta (un 'exportar'
    # de carpeta entera). Sin esto no se pueden encadenar: encadenar la carpeta que se
    # buscó tampoco sirve, porque el walk es recursivo y lo encontrado puede
    # estar más abajo.
    r = _buscar(patron=r"^PRUE001-[AB]\d{2}-[A-Z]\.pdf$")
    assert r.status == "ok"
    assert r.outputs["carpeta"] == CASO
    assert r.outputs["carpetas"] == [CASO]


def test_con_resultados_repartidos_avisa_que_carpeta_deja_afuera():
    # Una sola carpeta no representa al resultado, y elegirla en silencio haría
    # que un export procesara una parte como si fueran todos.
    registro = []
    r = _buscar(etiqueta=".pdf", log=registro.append)
    assert r.status == "ok"
    assert r.outputs["carpetas"] == [CASO, f"{CASO}/sub"]
    assert r.outputs["carpeta"] == CASO  # la de 'primera'
    assert any("repartidos en 2 carpetas" in m for m in registro)


def test_sin_coincidencias_es_err_con_salidas_vacias():
    r = _buscar(etiqueta="no-existe")
    assert r.status == "err"
    assert r.outputs == {"rutas": [], "cantidad": 0, "primera": "", "carpeta": "", "carpetas": []}


def test_sin_criterio_o_con_regex_rota_es_err_claro():
    assert "etiqueta" in _buscar().message
    assert "expresión regular" in _buscar(patron="[").message


# ── subcarpeta: bajar al caso antes de buscar ────────────────────────────
#
# Contra una carpeta de casos terminados grande (decenas de miles de casos,
# millones de archivos) recorrer el árbol entero para llegar a uno tarda
# minutos, y el nodo no escribe una
# sola línea hasta terminar el walk: el síntoma es "no encuentra nada y no deja
# log", que se lee como que cortó temprano cuando en realidad seguía caminando.

import dataclasses  # noqa: E402


class FsConFechas(FakeFs):
    """`FakeFs` con mtime por ruta: el del núcleo no lo modela, y el desempate
    por fecha entre dos carpetas del mismo caso es justo lo que hay que probar."""

    def __init__(self, *a, fechas=None, **kw):
        super().__init__(*a, **kw)
        self.fechas = dict(fechas or {})

    def stat(self, path):
        info = super().stat(path)
        if info.path in self.fechas:
            return dataclasses.replace(info, modified_at=self.fechas[info.path])
        return info

    def walk(self, path, max_depth=None):
        """`walk` con `max_depth` (core#33). El FakeFs del núcleo vendorizado
        todavía no lo tiene, así que sin esto sólo se probaría la degradación."""
        hondo = len(_norm(path).split("/"))
        for info in super().walk(path):
            if max_depth is None or len(info.path.split("/")) - hondo <= max_depth:
                yield info


RAIZ = "//server/CASOS TERMINADOS"
# El caso real: el mismo id dos veces, la segunda por un nombre mal tipeado.
VIEJA, NUEVA = f"{RAIZ}/BX718 Pérez, Juana 1.1", f"{RAIZ}/BX718 Peres Juana 1.1"
CASOS = {
    f"{VIEJA}/BX718-A00-A.pdf": "", f"{VIEJA}/BX718-A00-A-borrador.pdf": "",
    f"{NUEVA}/BX718-A00-A.pdf": "", f"{NUEVA}/BX718-B01-B.pdf": "",
    f"{RAIZ}/BX999 Otro 1.0/BX999-A00-A.pdf": "",
    # Un caso más abajo, con el mismo id: no cuelga del root, no es candidato.
    f"{RAIZ}/archivo/BX718 vieja/BX718-A00-A.pdf": "",
}
FECHAS = {VIEJA: 1789410586.0, NUEVA: 1789575049.0}  # la nueva es 2 días posterior
ENTREGABLE = r"^[A-Z]{2}\d{3}-[AB]\d{2}-[A-Z]\.pdf$"


def _en_casos(log=None, **params):
    reg = ToolRegistry(adapters={"fs": FsConFechas(files=CASOS, fechas=FECHAS)})
    reg._add_plugin("archivos", "plugins.archivos:PLUGIN", build_plugin())
    anotar = (lambda m, level="info": log((level, m))) if log else (lambda *_a, **_k: None)

    def factory(declaracion, ports=None):
        declarados, extras = declaracion.split_params({"carpeta": RAIZ, **params}, {})
        return ToolContext(
            run_id="run-test", case_id="BX718", params=declarados, extras=extras,
            config={}, context={}, log=anotar, ports=ports or {},
        )

    return reg.execute("archivos.buscar", factory)


def test_con_subcarpeta_busca_solo_adentro_del_caso():
    r = _en_casos(subcarpeta="BX718", patron=ENTREGABLE)
    assert r.status == "ok"
    # Sólo los de la carpeta elegida: ni los del otro caso ni los de la vieja.
    assert all(p.startswith(NUEVA) for p in r.outputs["rutas"]), r.outputs["rutas"]
    assert r.outputs["carpeta"] == NUEVA


def test_con_dos_carpetas_del_mismo_caso_gana_la_mas_nueva_y_lo_dice():
    registro = []
    r = _en_casos(subcarpeta="BX718", patron=ENTREGABLE, log=registro.append)
    assert r.outputs["carpeta"] == NUEVA
    # Warning y no error: hay una respuesta razonable, pero elegirla en silencio
    # sería decidir por el operador.
    assert any(
        nivel == "warning" and "2 subcarpetas" in m and "Peres Juana 1.1" in m
        and "deja afuera" in m and "Pérez" in m
        for nivel, m in registro
    ), registro


def test_solo_mira_las_subcarpetas_directas():
    # `archivo/BX718 vieja` tiene el id en el nombre pero cuelga un nivel más
    # abajo. La suposición es "el caso cuelga del root", no "está en algún lado".
    r = _en_casos(subcarpeta="BX718", patron=ENTREGABLE)
    assert not any("/archivo/" in p for p in r.outputs["rutas"])


def test_sin_subcarpeta_sigue_recorriendo_todo_como_antes():
    # Lo aditivo: los flujos que ya andan no cambian de conducta.
    r = _en_casos(patron=ENTREGABLE)
    assert r.status == "ok"
    assert len(r.outputs["carpetas"]) == 4  # las dos del caso, el otro, y el de archivo/


def test_un_caso_que_no_esta_se_distingue_de_un_caso_vacio():
    # Dos errores distintos donde antes había uno: el flujo puede ramificar.
    r = _en_casos(subcarpeta="BX000", patron=ENTREGABLE)
    assert r.status == "err"
    assert "ninguna subcarpeta" in r.message and "BX000" in r.message
    assert r.outputs == {"rutas": [], "cantidad": 0, "primera": "", "carpeta": "", "carpetas": []}

    r = _en_casos(subcarpeta="BX999", patron=r"^no-existe$")
    assert r.status == "err"
    assert "ningún archivo" in r.message and "BX999 Otro 1.0" in r.message


def test_usa_walk_con_max_depth_cuando_el_nucleo_lo_tiene():
    # core#33: pide los hijos directos sin statearlos. Lo que se verifica es que
    # el argumento viaje — el costo no se ve desde un fake.
    class Espia(FsConFechas):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.walks, self.list_dirs = [], []

        def walk(self, path, max_depth=None):
            self.walks.append(max_depth)
            yield from super().walk(path, max_depth=max_depth)

        def list_dir(self, path):
            self.list_dirs.append(path)
            return super().list_dir(path)

    fs = Espia(files=CASOS, fechas=FECHAS)
    reg = ToolRegistry(adapters={"fs": fs})
    reg._add_plugin("archivos", "plugins.archivos:PLUGIN", build_plugin())

    def factory(declaracion, ports=None):
        d, e = declaracion.split_params(
            {"carpeta": RAIZ, "subcarpeta": "BX718", "patron": ENTREGABLE}, {})
        return ToolContext(
            run_id="r", case_id="BX718", params=d, extras=e, config={}, context={},
            log=lambda *_a, **_k: None, ports=ports or {})

    r = reg.execute("archivos.buscar", factory)
    assert r.status == "ok"
    assert fs.walks[0] == 1          # la fase 1 no baja más de un nivel
    assert fs.list_dirs == []        # y no paga el listado con stat


def test_contra_un_nucleo_sin_max_depth_sigue_andando_y_avisa():
    # Un Bot que todavía no actualizó. El fake tiene que ser fiel en esto: el
    # núcleo viejo no declara `max_depth`, así que el TypeError sale al LIGAR
    # los argumentos, antes de entrar al generador. Un fake que lo levante
    # adentro del cuerpo lo tira recién al consumirlo, que es otro momento, y
    # el try/except —puesto a propósito alrededor de la llamada y no del
    # consumo, para no tragarse un TypeError de adentro del walk— no lo vería.
    class Viejo(FsConFechas):
        def walk(self, path):
            yield from FakeFs.walk(self, path)

    registro = []
    reg = ToolRegistry(adapters={"fs": Viejo(files=CASOS, fechas=FECHAS)})
    reg._add_plugin("archivos", "plugins.archivos:PLUGIN", build_plugin())

    def factory(declaracion, ports=None):
        d, e = declaracion.split_params(
            {"carpeta": RAIZ, "subcarpeta": "BX718", "patron": ENTREGABLE}, {})
        return ToolContext(
            run_id="r", case_id="BX718", params=d, extras=e, config={}, context={},
            log=lambda m, level="info": registro.append((level, m)), ports=ports or {})

    r = reg.execute("archivos.buscar", factory)
    assert r.status == "ok"
    assert r.outputs["carpeta"] == NUEVA   # mismo resultado que por el camino rápido
    assert any(nivel == "warning" and "core#33" in m for nivel, m in registro), registro
