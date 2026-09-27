"""
Plugin `mime` — leer y armar mails en formato MIME (RFC 822) desde un flujo.

Las APIs de correo entregan y reciben el mail entero codificado: Gmail en
`format=raw` da el mensaje en base64url, y para enviar pide lo mismo; un
`.eml` guardado es el mismo formato en texto. Leer eso a mano —encabezados
codificados (`=?utf-8?q?...?=`), cuerpos en quoted-printable o base64, partes
alternativas texto/HTML, adjuntos anidados— no se puede hacer con una llamada
HTTP guardada. Este plugin es esa parte, y sólo esa: pedir el mail o mandarlo
lo hace `connections` (o quien sea), así que sirve igual para cualquier
proveedor que hable MIME.

Todo es cómputo sobre la librería estándar (`email`), sin dependencias. El
único I/O es escribir los adjuntos y leer los que se mandan, por el port `fs`.

Lo que NO hace, a propósito:

- **Poner la fecha** al armar un mail: sería leer el reloj por fuera del port
  `clock`, y el servidor de correo la pone al recibirlo (Gmail lo hace).
- **Leer el formato `full` de Gmail** (el JSON con las partes ya separadas):
  es propio de un proveedor. `raw` es el mail tal cual, y ése es el que se lee.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import binascii
import html as html_lib
import json
import mimetypes
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

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
    name="mime",
    label="Correo (MIME)",
    version="0.1.2",
    doc="Leer un mail crudo (asunto, remitente, cuerpo en texto, adjuntos a una carpeta) y armar uno para "
    "enviar o responder en el mismo hilo. Pedirlo y mandarlo lo hace una conexión.",
    ports=(port_names.FS,),
)



def _en_seco() -> dict:
    """
    `dry_run="run"` si el núcleo lo conoce (v0.3.1-beta.14, core#34): en un dry
    run el tool corre de verdad, con fs y http en modo lectura. Un núcleo
    anterior no tiene el campo y `ToolManifest(dry_run=...)` reventaría al
    importar: ahí no se pasa.
    """
    campos = {f.name for f in dataclasses.fields(ToolManifest)}
    return {"dry_run": "run"} if "dry_run" in campos else {}


# ── Del texto que llega al mensaje ────────────────────────────────────────

# Un encabezado RFC 822 al principio ("From: ...", "Received: ..."): así se
# distingue un mail en texto de uno codificado en base64, que nunca tiene ':'.
_ENCABEZADO = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:[ \t]")


def _base64(texto: str) -> bytes | None:
    """
    base64 o base64url, con o sin relleno, estricto: None si no lo es.

    Estricto a propósito: `urlsafe_b64decode` descarta en silencio lo que no es
    del alfabeto, y convertía cualquier texto en bytes basura "decodificados".
    """
    compacto = re.sub(r"\s+", "", texto).replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(compacto + "=" * (-len(compacto) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None


def _como_objeto(valor):
    """
    El dict, si lo que llegó es uno escrito como texto; si no, tal cual.

    Un param de texto que recibe `{nodo.response}` (un objeto) llega como
    `str(dict)`: el repr de Python, con comillas simples, que no es JSON. Pasa
    siempre que un flujo encadena la respuesta de una conexión, así que se lee
    igual que un JSON.
    """
    if isinstance(valor, str) and valor.strip()[:1] == "{":
        for leer in (json.loads, ast.literal_eval):
            try:
                leido = leer(valor.strip())
            except (ValueError, SyntaxError):
                continue
            if isinstance(leido, dict):
                return leido
    return valor


def _bytes_del_mensaje(crudo) -> bytes | str:
    """
    Los bytes del mail, o un mensaje de error.

    Acepta el `raw` de Gmail (base64url, sin relleno), base64 común, el texto
    RFC 822 tal cual, o el JSON entero de la respuesta de Gmail —con `raw`
    adentro—, que es lo que deja una conexión en `{response}`.
    """
    crudo = _como_objeto(crudo)
    if isinstance(crudo, dict):
        if "raw" not in crudo:
            if "payload" in crudo:
                return "llegó un mensaje de Gmail en format=full; pedirlo con format=raw"
            return "el objeto no trae 'raw' con el mail"
        crudo = crudo["raw"]
    if not isinstance(crudo, str) or not crudo.strip():
        return "'mensaje' vacío"
    texto = crudo.strip()
    if _ENCABEZADO.match(texto):
        return texto.encode("utf-8", errors="surrogateescape")
    datos = _base64(texto)
    if datos is not None and _ENCABEZADO.match(datos[:200].decode("latin-1")):
        return datos
    return "'mensaje' no es un mail: ni texto RFC 822 ni base64 de uno"


# ── De HTML a texto ───────────────────────────────────────────────────────

class _ATexto(HTMLParser):
    """Lo mínimo para leer un HTML de mail como texto: sin estilos ni scripts,
    con saltos donde el HTML corta un renglón."""

    _CORTAN = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._ignorar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._ignorar += 1
        elif tag in self._CORTAN:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._ignorar:
            self._ignorar -= 1
        elif tag in self._CORTAN:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self._ignorar:
            self.partes.append(data)


def _html_a_texto(contenido: str) -> str:
    lector = _ATexto()
    lector.feed(contenido)
    texto = html_lib.unescape("".join(lector.partes))
    texto = re.sub(r"[ \t\r\f\v]+", " ", texto)
    return re.sub(r"\n\s*\n+", "\n\n", texto).strip()


# ── Leer ──────────────────────────────────────────────────────────────────

def _direcciones(valor) -> list[str]:
    if not valor:
        return []
    return [email for _nombre, email in getaddresses([str(valor)]) if email]


def _nombre_seguro(nombre: str, usados: set[str]) -> str:
    """Un nombre de archivo que no escapa de la carpeta ni pisa a otro adjunto."""
    nombre = re.split(r"[\\/]", nombre)[-1]
    nombre = re.sub(r'[<>:"|?*\x00-\x1f]', "_", nombre).strip(" .") or "adjunto"
    base, punto, ext = nombre.rpartition(".")
    if not punto:
        base, ext = nombre, ""
    base = base[:120]
    candidato, n = f"{base}.{ext}" if ext else base, 2
    while candidato.lower() in usados:
        candidato = f"{base} ({n}).{ext}" if ext else f"{base} ({n})"
        n += 1
    usados.add(candidato.lower())
    return candidato


def _es_en_seco(exc: PortError) -> bool:
    """El PortError con que el núcleo frena una escritura en un dry run (core#34)."""
    return "dry run" in str(exc)


def _contenido_de(parte) -> bytes:
    if parte.get_content_type() == "message/rfc822":
        adjunto = parte.get_payload(0) if parte.is_multipart() else parte.get_content()
        return adjunto.as_bytes()
    datos = parte.get_payload(decode=True)
    return datos if datos is not None else b""


def _respuesta_para(msg, asunto: str, remitente: str, referencias: str, message_id: str) -> dict:
    """Lo que `mime.armar` necesita para contestar en el mismo hilo."""
    responder = _direcciones(msg.get("reply-to")) or ([remitente] if remitente else [])
    asunto_re = asunto if re.match(r"^\s*(re|rv|aw)\s*:", asunto, re.I) else f"Re: {asunto}".strip()
    return {"para": responder, "asunto": asunto_re, "en_respuesta_a": message_id, "referencias": referencias}


LEER = ToolManifest(
    **_en_seco(),
    id="mime.leer",
    label="leer un mail",
    category="MIME",
    doc=(
        "Lee un mail crudo: el 'raw' de Gmail (format=raw), base64, texto RFC 822, o un .eml en "
        "'archivo'. Deja asunto, remitente, destinatarios, fecha, el cuerpo en texto (listo para "
        "pasarle a Laya o a un log), el HTML, y los adjuntos —guardados en 'carpeta_adjuntos' si se "
        "da—. {respuesta} es lo que 'armar un mail' necesita para contestar en el mismo hilo."
    ),
    params=(
        Param("mensaje", doc="El mail crudo, o la {response} entera de la conexión que lo trajo (con 'raw' adentro)."),
        Param("archivo", ParamType.PATH, doc="Un .eml, en vez de 'mensaje'."),
        Param("carpeta_adjuntos", ParamType.PATH, doc="Dónde guardar los adjuntos. Vacío: sólo se listan."),
        Param("max_texto", ParamType.INT, default=20000, doc="Corta {texto} en esta cantidad de caracteres. 0: sin corte."),
    ),
    outputs=(
        Output("asunto", ParamType.STR),
        Output("de", ParamType.STR, doc="Tal cual: 'Nombre <mail>'."),
        Output("de_email", ParamType.STR),
        Output("de_nombre", ParamType.STR),
        Output("para", ParamType.JSON, doc="Lista de mails."),
        Output("cc", ParamType.JSON, doc="Lista de mails."),
        Output("fecha", ParamType.STR, doc="ISO 8601, con zona. Vacío si el mail no la trae o no se entiende."),
        Output("message_id", ParamType.STR),
        Output("texto", ParamType.STR, doc="El cuerpo en texto; si el mail sólo trae HTML, el HTML pasado a texto."),
        Output("html", ParamType.STR),
        Output("adjuntos", ParamType.JSON, doc="[{nombre, tipo, tamano, ruta}]; 'ruta' vacía si no se guardaron."),
        Output("cantidad_adjuntos", ParamType.INT),
        Output("respuesta", ParamType.JSON, doc="{para, asunto, en_respuesta_a, referencias}, para 'armar un mail'."),
    ),
)

_VACIOS_LEER = {
    "asunto": "", "de": "", "de_email": "", "de_nombre": "", "para": [], "cc": [], "fecha": "",
    "message_id": "", "texto": "", "html": "", "adjuntos": [], "cantidad_adjuntos": 0, "respuesta": {},
}


def _leer(ctx: ToolContext) -> ToolResult:
    fs = ctx.port(port_names.FS)
    archivo = (ctx.params.get("archivo") or "").strip()
    if archivo:
        try:
            datos = fs.read_bytes(archivo)
        except PortError as exc:
            return ToolResult.err(f"no se pudo leer '{archivo}': {exc}", **_VACIOS_LEER)
    else:
        datos = _bytes_del_mensaje(ctx.params.get("mensaje"))
        if isinstance(datos, str):
            return ToolResult.err(datos, **_VACIOS_LEER)

    msg = BytesParser(policy=policy.default).parsebytes(datos)
    if not any(msg.get(h) for h in ("from", "subject", "date", "message-id", "mime-version")):
        return ToolResult.err("no parece un mail: no tiene ningún encabezado conocido", **_VACIOS_LEER)

    asunto = str(msg.get("subject") or "").strip()
    de = str(msg.get("from") or "").strip()
    remitentes = getaddresses([de]) if de else []
    de_nombre, de_email = remitentes[0] if remitentes else ("", "")
    try:
        fecha = parsedate_to_datetime(str(msg["date"])).isoformat() if msg.get("date") else ""
    except (TypeError, ValueError):
        fecha = ""
    message_id = str(msg.get("message-id") or "").strip()
    referencias = " ".join(x for x in (str(msg.get("references") or "").strip(), message_id) if x)

    cuerpo_texto = msg.get_body(preferencelist=("plain",))
    cuerpo_html = msg.get_body(preferencelist=("html",))
    html = cuerpo_html.get_content() if cuerpo_html is not None else ""
    texto = cuerpo_texto.get_content() if cuerpo_texto is not None else (_html_a_texto(html) if html else "")
    texto = texto.replace("\r\n", "\n").strip()
    maximo = int(ctx.params.get("max_texto") or 0)
    if maximo and len(texto) > maximo:
        ctx.log(f"el cuerpo tiene {len(texto)} caracteres; {{texto}} queda cortado en {maximo}", "warning")
        texto = texto[:maximo]

    carpeta = (ctx.params.get("carpeta_adjuntos") or "").strip()
    adjuntos, usados = [], set()
    partes = list(msg.iter_attachments())
    if carpeta and partes:
        try:
            fs.make_dirs(carpeta)
        except PortError as exc:
            if not _es_en_seco(exc):
                return ToolResult.err(f"no se pudo crear '{carpeta}': {exc}", **_VACIOS_LEER)
            # En un dry run el núcleo no deja escribir. Leer el mail sí: se
            # listan los adjuntos sin guardarlos y el resto del flujo recibe
            # asunto, texto y demás de verdad, que es para lo que corre en seco.
            ctx.log(f"en seco: los adjuntos se listan pero no se guardan en {carpeta}", "warning")
            carpeta = ""
    for i, parte in enumerate(partes, 1):
        tipo = parte.get_content_type()
        nombre = parte.get_filename() or f"adjunto-{i}{mimetypes.guess_extension(tipo) or '.bin'}"
        nombre = _nombre_seguro(nombre, usados)
        contenido = _contenido_de(parte)
        ruta = ""
        if carpeta:
            ruta = fs.join(carpeta, nombre)
            try:
                fs.write_bytes(ruta, contenido)
            except PortError as exc:
                return ToolResult.err(f"no se pudo guardar el adjunto '{nombre}': {exc}", **_VACIOS_LEER)
        adjuntos.append({"nombre": nombre, "tipo": tipo, "tamano": len(contenido), "ruta": ruta})

    ctx.log(
        f"'{asunto}' de {de_email or de or '(sin remitente)'}"
        + (f" · {len(adjuntos)} adjunto(s)" + (f" en {carpeta}" if carpeta else "") if adjuntos else "")
    )
    return ToolResult.ok(
        asunto=asunto, de=de, de_email=de_email, de_nombre=de_nombre,
        para=_direcciones(msg.get("to")), cc=_direcciones(msg.get("cc")),
        fecha=fecha, message_id=message_id, texto=texto, html=html,
        adjuntos=adjuntos, cantidad_adjuntos=len(adjuntos),
        respuesta=_respuesta_para(msg, asunto, de_email, referencias, message_id),
    )


# ── Armar ─────────────────────────────────────────────────────────────────

def _lista_de(valor) -> list[str]:
    """Mails o rutas: lista JSON, o separados por coma o punto y coma."""
    if isinstance(valor, str) and valor.strip()[:1] == "[":
        try:
            valor = json.loads(valor)
        except ValueError:
            pass
    if isinstance(valor, list):
        return [str(v).strip() for v in valor if str(v).strip()]
    return [v.strip() for v in re.split(r"[;,]", str(valor or "")) if v.strip()]


def _objeto(valor) -> dict:
    leido = _como_objeto(valor)
    return leido if isinstance(leido, dict) else {}


ARMAR = ToolManifest(
    id="mime.armar",
    label="armar un mail",
    category="MIME",
    doc=(
        "Arma un mail para enviar: {raw} es lo que pide el envío de Gmail ({\"raw\": \"{raw}\"}). "
        "Con 'responder' (la {respuesta} de 'leer un mail') contesta en el mismo hilo: toma de ahí el "
        "destinatario y el asunto si no se dan, y pone In-Reply-To y References. Los adjuntos se leen "
        "del disco."
    ),
    params=(
        Param("para", doc="Mails separados por coma. Vacío si se responde: va al remitente del original."),
        Param("asunto", doc="Vacío si se responde: 'Re: ' y el del original."),
        Param("texto", doc="El cuerpo en texto."),
        Param("html", doc="Opcional: el cuerpo en HTML; va junto al texto, como alternativa."),
        Param("cc"),
        Param("cco"),
        Param("de", doc="Vacío: el servidor pone la cuenta con que se envía."),
        Param("adjuntos", doc="Rutas de archivos, separadas por coma o como lista JSON."),
        Param("responder", ParamType.JSON, default={}, doc="La {respuesta} de 'leer un mail', para contestar en el hilo."),
    ),
    outputs=(
        Output("raw", ParamType.STR, doc="El mail en base64url, como lo pide Gmail."),
        Output("mensaje", ParamType.STR, doc="El mail en texto RFC 822, por si hay que guardarlo como .eml."),
        Output("tamano", ParamType.INT, doc="Bytes del mail armado."),
    ),
)


def _armar(ctx: ToolContext) -> ToolResult:
    vacios = {"raw": "", "mensaje": "", "tamano": 0}
    responder = _objeto(ctx.params.get("responder"))
    para = _lista_de(ctx.params.get("para")) or _lista_de(responder.get("para"))
    if not para:
        return ToolResult.err("falta 'para' (o 'responder' con el remitente del original)", **vacios)
    malos = [p for p in para if "@" not in p]
    if malos:
        return ToolResult.err(f"no son mails: {', '.join(malos)}", **vacios)
    asunto = (ctx.params.get("asunto") or "").strip() or str(responder.get("asunto") or "").strip()

    msg = EmailMessage(policy=policy.SMTP)
    if (ctx.params.get("de") or "").strip():
        msg["From"] = ctx.params["de"].strip()
    msg["To"] = ", ".join(para)
    for campo, encabezado in (("cc", "Cc"), ("cco", "Bcc")):
        if lista := _lista_de(ctx.params.get(campo)):
            msg[encabezado] = ", ".join(lista)
    msg["Subject"] = asunto
    if responder.get("en_respuesta_a"):
        msg["In-Reply-To"] = str(responder["en_respuesta_a"])
        msg["References"] = str(responder.get("referencias") or responder["en_respuesta_a"])

    msg.set_content(ctx.params.get("texto") or "")
    if (ctx.params.get("html") or "").strip():
        msg.add_alternative(ctx.params["html"], subtype="html")

    fs = ctx.port(port_names.FS)
    for ruta in _lista_de(ctx.params.get("adjuntos")):
        try:
            datos = fs.read_bytes(ruta)
        except PortError as exc:
            return ToolResult.err(f"no se pudo leer el adjunto '{ruta}': {exc}", **vacios)
        nombre = fs.basename(ruta)
        tipo = mimetypes.guess_type(nombre)[0] or "application/octet-stream"
        principal, _, sub = tipo.partition("/")
        msg.add_attachment(datos, maintype=principal, subtype=sub, filename=nombre)

    crudo = msg.as_bytes()
    ctx.log(f"mail para {', '.join(para)}: '{asunto}' ({len(crudo)} bytes)")
    return ToolResult.ok(
        raw=base64.urlsafe_b64encode(crudo).decode("ascii"),
        mensaje=crudo.decode("utf-8", errors="replace"),
        tamano=len(crudo),
    )


# ── Guardar datos en base64 ───────────────────────────────────────────────

GUARDAR_BASE64 = ToolManifest(
    id="mime.guardar_base64",
    label="guardar datos en base64",
    category="MIME",
    doc=(
        "Guarda en un archivo datos que llegan en base64 o base64url, como el 'data' de la API de "
        "adjuntos de Gmail (messages/{id}/attachments/{adjunto})."
    ),
    params=(
        Param("datos", required=True, doc="El base64, o la {response} entera con 'data' adentro."),
        Param("ruta", ParamType.PATH, required=True, doc="El archivo a escribir, con su nombre. Se crea la carpeta si falta."),
    ),
    outputs=(Output("ruta", ParamType.PATH), Output("tamano", ParamType.INT)),
)


def _guardar_base64(ctx: ToolContext) -> ToolResult:
    vacios = {"ruta": "", "tamano": 0}
    datos = _objeto(ctx.params["datos"]).get("data") or ctx.params["datos"]
    if not isinstance(datos, str) or not datos.strip():
        return ToolResult.err("'datos' vacío", **vacios)
    contenido = _base64(datos)
    if contenido is None:
        return ToolResult.err("'datos' no es base64", **vacios)
    fs = ctx.port(port_names.FS)
    ruta = ctx.params["ruta"].strip()
    try:
        fs.make_dirs(fs.parent(ruta))
        fs.write_bytes(ruta, contenido)
    except PortError as exc:
        return ToolResult.err(f"no se pudo guardar '{ruta}': {exc}", **vacios)
    ctx.log(f"{len(contenido)} bytes en {ruta}")
    return ToolResult.ok(ruta=ruta, tamano=len(contenido))


def build_plugin() -> Plugin:
    return Plugin(
        manifest=MANIFEST,
        tools=[
            FunctionTool(manifest=LEER, fn=_leer),
            FunctionTool(manifest=ARMAR, fn=_armar),
            FunctionTool(manifest=GUARDAR_BASE64, fn=_guardar_base64),
        ],
    )


PLUGIN = build_plugin()

__all__ = ["MANIFEST", "PLUGIN", "build_plugin"]
