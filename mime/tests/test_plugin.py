"""
Tests del plugin `mime`, con mails armados acá con la librería estándar y un
filesystem en memoria que guarda bytes de verdad (el `FakeFs` del núcleo los
guarda como texto y rompería un adjunto binario).
Necesita el núcleo (`backend/`) del repo de la app: se busca en la variable
`WORKFLOW_BOT_APP` o en `../workflow-bot-app`, al lado de este catálogo.
Corre con `python -m pytest mime` desde la raíz del catálogo.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

from backend.core.contract import ToolContext  # noqa: E402
from backend.core.registry import ToolRegistry  # noqa: E402
from backend.tests.fakes import FakeFs, _norm  # noqa: E402

from mime.plugin import build_plugin  # noqa: E402

PDF = b"%PDF-1.4\n\x00\xff\xfe binario \x89PNG\r\n"


class FsBytes(FakeFs):
    def __init__(self, archivos: dict | None = None):
        super().__init__()
        self.bytes: dict[str, bytes] = {_norm(k): v for k, v in (archivos or {}).items()}

    def write_bytes(self, path, content):
        self.bytes[_norm(path)] = bytes(content)
        self.calls.append(("write_bytes", _norm(path)))

    def read_bytes(self, path):
        from backend.core.ports import PortError
        if _norm(path) not in self.bytes:
            raise PortError(f"no existe {path}")
        return self.bytes[_norm(path)]


def _correr(tool, params, fs=None):
    fs = fs or FsBytes()
    reg = ToolRegistry(adapters={"fs": fs})
    reg._add_plugin("mime", "mime:PLUGIN", build_plugin())
    registro = []

    def factory(declaracion, ports=None):
        d, e = declaracion.split_params(params, {})
        return ToolContext(run_id="r", case_id="c", params=d, extras=e, config={}, context={},
                           log=lambda m, level="info": registro.append((level, m)), ports=ports or {})

    return reg.execute(tool, factory), fs, registro


def _mail() -> EmailMessage:
    m = EmailMessage()
    m["From"] = "José Pérez <jose@ejemplo.com>"
    m["To"] = "Soporte <soporte@empresa.com>, otra@empresa.com"
    m["Cc"] = "jefe@empresa.com"
    m["Subject"] = "La máquina dejó de andar — urgente"
    m["Date"] = "Fri, 25 Sep 2026 10:30:00 -0300"
    m["Message-ID"] = "<abc123@ejemplo.com>"
    m["References"] = "<previo@ejemplo.com>"
    m.set_content("Hola, la máquina no arranca.\nEs la tercera vez.\n")
    m.add_alternative("<p>Hola, la <b>máquina</b> no arranca.</p>", subtype="html")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="informe técnico.pdf")
    m.add_attachment(b"uno", maintype="text", subtype="plain", filename="notas.txt")
    m.add_attachment(b"dos", maintype="text", subtype="plain", filename="notas.txt")
    m.add_attachment(b"malo", maintype="application", subtype="octet-stream", filename="../../fuera.exe")
    return m


def _raw(m: EmailMessage) -> str:
    """Como lo da Gmail en format=raw: base64url sin relleno."""
    return base64.urlsafe_b64encode(m.as_bytes()).decode().rstrip("=")


def test_leer_el_raw_de_gmail_saca_encabezados_cuerpo_y_respuesta():
    r, _, registro = _correr("mime.leer", {"mensaje": _raw(_mail())})
    assert r.status == "ok", r.message
    o = r.outputs
    assert o["asunto"] == "La máquina dejó de andar — urgente"
    assert (o["de_nombre"], o["de_email"]) == ("José Pérez", "jose@ejemplo.com")
    assert o["para"] == ["soporte@empresa.com", "otra@empresa.com"] and o["cc"] == ["jefe@empresa.com"]
    assert o["fecha"] == "2026-09-25T10:30:00-03:00"
    assert o["texto"] == "Hola, la máquina no arranca.\nEs la tercera vez."
    assert "<b>máquina</b>" in o["html"]
    assert o["respuesta"] == {
        "para": ["jose@ejemplo.com"], "asunto": "Re: La máquina dejó de andar — urgente",
        "en_respuesta_a": "<abc123@ejemplo.com>", "referencias": "<previo@ejemplo.com> <abc123@ejemplo.com>",
    }
    # Sin carpeta: se listan pero no se escribe nada.
    assert o["cantidad_adjuntos"] == 4 and all(a["ruta"] == "" for a in o["adjuntos"])


def test_acepta_texto_rfc822_base64_comun_y_la_response_entera_de_la_conexion():
    m = _mail()
    for mensaje in (m.as_string(), base64.b64encode(m.as_bytes()).decode(),
                    json.dumps({"id": "18a", "threadId": "18a", "raw": _raw(m)})):
        r, _, _ = _correr("mime.leer", {"mensaje": mensaje})
        assert r.status == "ok" and r.outputs["de_email"] == "jose@ejemplo.com"


def test_guarda_adjuntos_binarios_sin_pisarse_ni_escaparse_de_la_carpeta():
    r, fs, _ = _correr("mime.leer", {"mensaje": _raw(_mail()), "carpeta_adjuntos": "C:/casos/AB123/adjuntos"})
    assert r.status == "ok"
    nombres = [a["nombre"] for a in r.outputs["adjuntos"]]
    assert nombres == ["informe técnico.pdf", "notas.txt", "notas (2).txt", "fuera.exe"]
    assert fs.bytes["C:/casos/AB123/adjuntos/informe técnico.pdf"] == PDF  # los bytes, intactos
    assert fs.bytes["C:/casos/AB123/adjuntos/notas (2).txt"] == b"dos"
    assert all(p.startswith("C:/casos/AB123/adjuntos/") for p in fs.bytes)
    assert r.outputs["adjuntos"][0] == {
        "nombre": "informe técnico.pdf", "tipo": "application/pdf", "tamano": len(PDF),
        "ruta": "C:/casos/AB123/adjuntos/informe técnico.pdf",
    }


def test_un_mail_solo_html_en_latin1_quoted_printable_se_lee_como_texto():
    crudo = (
        "From: =?iso-8859-1?q?Mar=EDa?= <maria@ejemplo.com>\r\n"
        "Subject: =?iso-8859-1?q?Factura_de_agosto?=\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=iso-8859-1\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        "<html><head><style>p{color:red}</style></head><body><p>Buenas, =BFme pasan la factura?</p>"
        "<p>Gracias &amp; saludos</p></body></html>\r\n"
    )
    r, _, _ = _correr("mime.leer", {"mensaje": crudo})
    assert r.status == "ok"
    assert r.outputs["de_nombre"] == "María" and r.outputs["asunto"] == "Factura de agosto"
    assert r.outputs["texto"] == "Buenas, ¿me pasan la factura?\n\nGracias & saludos"
    assert "color" not in r.outputs["texto"]


def test_leer_un_eml_del_disco_y_cortar_el_texto():
    fs = FsBytes({"D:/mails/uno.eml": _mail().as_bytes()})
    r, _, registro = _correr("mime.leer", {"archivo": "D:/mails/uno.eml", "max_texto": 10}, fs)
    assert r.status == "ok" and r.outputs["texto"] == "Hola, la m"
    assert any(nivel == "warning" and "cortado" in m for nivel, m in registro)


def test_lo_que_no_es_un_mail_es_err_con_salidas_vacias():
    for mensaje, pista in (("", "vacío"), ("hola que tal", "no es un mail"),
                           (json.dumps({"id": "1", "payload": {}}), "format=raw"),
                           (json.dumps({"id": "1"}), "'raw'")):
        r, _, _ = _correr("mime.leer", {"mensaje": mensaje})
        assert r.status == "err" and pista in r.message, (mensaje, r.message)
        assert r.outputs["adjuntos"] == [] and r.outputs["respuesta"] == {}


# ── armar ─────────────────────────────────────────────────────────────────

def _parsear_raw(raw: str):
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def test_armar_un_mail_nuevo_con_html_y_adjunto():
    fs = FsBytes({"C:/out/informe.pdf": PDF})
    r, _, _ = _correr("mime.armar", {
        "para": "a@x.com; b@x.com", "cc": "c@x.com", "cco": "d@x.com", "asunto": "Informe — septiembre",
        "texto": "Va el informe.", "html": "<p>Va el <b>informe</b>.</p>", "adjuntos": "C:/out/informe.pdf",
    }, fs)
    assert r.status == "ok", r.message
    m = _parsear_raw(r.outputs["raw"])
    assert m["To"] == "a@x.com, b@x.com" and m["Cc"] == "c@x.com" and m["Bcc"] == "d@x.com"
    assert m["Subject"] == "Informe — septiembre" and m["From"] is None
    assert m.get_body(("plain",)).get_content().strip() == "Va el informe."
    assert "<b>informe</b>" in m.get_body(("html",)).get_content()
    [adj] = list(m.iter_attachments())
    assert adj.get_filename() == "informe.pdf" and adj.get_content_type() == "application/pdf"
    assert adj.get_payload(decode=True) == PDF
    assert r.outputs["tamano"] == len(base64.urlsafe_b64decode(r.outputs["raw"]))


def test_responder_con_lo_que_dejo_leer_queda_en_el_mismo_hilo():
    leido, _, _ = _correr("mime.leer", {"mensaje": _raw(_mail())})
    r, _, _ = _correr("mime.armar", {"texto": "Ya mandamos un técnico.", "responder": leido.outputs["respuesta"]})
    assert r.status == "ok", r.message
    m = _parsear_raw(r.outputs["raw"])
    assert m["To"] == "jose@ejemplo.com"
    assert m["Subject"] == "Re: La máquina dejó de andar — urgente"
    assert m["In-Reply-To"] == "<abc123@ejemplo.com>"
    assert m["References"] == "<previo@ejemplo.com> <abc123@ejemplo.com>"
    # Leído de nuevo, el asunto no se vuelve "Re: Re:".
    otra, _, _ = _correr("mime.leer", {"mensaje": r.outputs["raw"]})
    assert otra.outputs["respuesta"]["asunto"] == "Re: La máquina dejó de andar — urgente"


def test_armar_valida_destinatarios_y_adjuntos():
    r, _, _ = _correr("mime.armar", {"texto": "x"})
    assert r.status == "err" and "para" in r.message
    r, _, _ = _correr("mime.armar", {"para": "no-es-mail", "texto": "x"})
    assert r.status == "err" and "no-es-mail" in r.message
    r, _, _ = _correr("mime.armar", {"para": "a@x.com", "texto": "x", "adjuntos": "C:/no/existe.pdf"})
    assert r.status == "err" and "existe.pdf" in r.message and r.outputs["raw"] == ""


# ── guardar_base64 ────────────────────────────────────────────────────────

def test_guardar_base64_desde_la_response_de_adjuntos_de_gmail():
    data = base64.urlsafe_b64encode(PDF).decode().rstrip("=")
    r, fs, _ = _correr("mime.guardar_base64", {"datos": json.dumps({"size": len(PDF), "data": data}),
                                                "ruta": "C:/casos/AB123/informe.pdf"})
    assert r.status == "ok" and r.outputs == {"ruta": "C:/casos/AB123/informe.pdf", "tamano": len(PDF)}
    assert fs.bytes["C:/casos/AB123/informe.pdf"] == PDF

    r, _, _ = _correr("mime.guardar_base64", {"datos": "no es base64 !!!", "ruta": "C:/x.bin"})
    assert r.status == "err"


def test_en_un_dry_run_lee_el_mail_y_lista_los_adjuntos_sin_guardarlos():
    from backend.core.ports import PortError

    class FsEnSeco(FsBytes):
        def make_dirs(self, path):
            raise PortError("escritura en dry run: fs.make_dirs")

        def write_bytes(self, path, content):
            raise PortError("escritura en dry run: fs.write_bytes")

    r, fs, registro = _correr("mime.leer", {"mensaje": _raw(_mail()), "carpeta_adjuntos": "C:/x"}, FsEnSeco())
    assert r.status == "ok" and r.outputs["asunto"].startswith("La máquina")
    assert r.outputs["cantidad_adjuntos"] == 4 and all(a["ruta"] == "" for a in r.outputs["adjuntos"])
    assert any(nivel == "warning" and "en seco" in m for nivel, m in registro)

    # Cualquier otro error al escribir sigue siendo err.
    class FsRoto(FsBytes):
        def make_dirs(self, path):
            raise PortError("ruta fuera del árbol permitido")

    r, _, _ = _correr("mime.leer", {"mensaje": _raw(_mail()), "carpeta_adjuntos": "C:/x"}, FsRoto())
    assert r.status == "err" and "fuera del árbol" in r.message


def test_leer_se_declara_para_correr_en_seco_si_el_nucleo_lo_sabe():
    import dataclasses
    from backend.core.contract import ToolManifest
    from mime.plugin import LEER, ARMAR, GUARDAR_BASE64
    if "dry_run" not in {f.name for f in dataclasses.fields(ToolManifest)}:
        return  # núcleo anterior a core#34: se instala igual, sin el campo
    assert LEER.dry_run == "run"
    assert ARMAR.dry_run == "skip" and GUARDAR_BASE64.dry_run == "skip"


def test_la_response_de_una_conexion_llega_como_repr_de_python_y_se_lee_igual():
    """`mensaje={L.response}` en un param de texto: el núcleo pasa str(dict), con
    comillas simples. Es lo que manda un flujo real que encadena 'Gmail - leer'."""
    respuesta = {"id": "18a", "threadId": "18a", "labelIds": ["INBOX", "UNREAD"], "raw": _raw(_mail()), "sizeEstimate": 1234}
    r, _, _ = _correr("mime.leer", {"mensaje": str(respuesta)})
    assert r.status == "ok", r.message
    assert r.outputs["de_email"] == "jose@ejemplo.com"

    data = base64.urlsafe_b64encode(PDF).decode().rstrip("=")
    r, fs, _ = _correr("mime.guardar_base64", {"datos": str({"size": len(PDF), "data": data}), "ruta": "C:/a/b.pdf"})
    assert r.status == "ok" and fs.bytes["C:/a/b.pdf"] == PDF

    # `responder` es ParamType.JSON: ahí el núcleo pasa el objeto tal cual, nunca str(dict).
