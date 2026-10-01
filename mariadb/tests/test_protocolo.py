"""
Tests de `protocolo.py` contra `FakeSocket` (scripted — nunca abre un socket
de verdad) y unidades puras (escapado, lenenc, etc.) sin red de ningún tipo.

Importante sobre `FakeSocket.recv`: devuelve el **próximo chunk entero** de
la lista guionada, sin importar cuánto se pidió (a diferencia de
`socket.recv`, que nunca entrega más de lo pedido). Por eso cada paquete se
guioná en DOS chunks separados —el header de 4 bytes, después el payload
exacto— para que `_recv_exact` (que sí respeta el tamaño pedido) reciba
cada uno de a uno, como pasaría de verdad.

El protocolo en sí ya se validó contra una MariaDB real (handshake, auth
`mysql_native_password`, `COM_QUERY` con filas, columnas, NULL y error, y
una contraseña incorrecta rechazada) — esto cubre la lógica de framing y
parseo con bytes armados a mano, para no depender de la red en CI.

Corre con `python -m pytest mariadb` desde la raíz del catálogo.
"""

from __future__ import annotations

import os
import pathlib
import sys

CATALOGO = pathlib.Path(__file__).resolve().parents[2]
APP = pathlib.Path(os.environ.get("WORKFLOW_BOT_APP") or CATALOGO.parent / "workflow-bot-app")
sys.path.insert(0, str(CATALOGO))
sys.path.insert(0, str(APP))

import pytest  # noqa: E402

from backend.core.ports import SocketConnection  # noqa: E402
from backend.tests.fakes import FakeSocket  # noqa: E402

from mariadb.protocolo import (  # noqa: E402
    ProtocoloError, cerrar, conectar, consultar, escapar_valor,
)

HOST, PORT = "db.test", 3306


def _lenenc(data: bytes) -> bytes:
    assert len(data) < 0xFB
    return bytes([len(data)]) + data


def _header(payload: bytes, seq: int = 0) -> bytes:
    largo = len(payload)
    return bytes([largo & 0xFF, (largo >> 8) & 0xFF, (largo >> 16) & 0xFF, seq & 0xFF])


def _en_chunks(*payloads: bytes) -> list[bytes]:
    """Header y payload como chunks separados, por cada paquete — ver el docstring del módulo."""
    partes: list[bytes] = []
    for p in payloads:
        partes.append(_header(p))
        partes.append(p)
    return partes


def _saludo(*, auth_plugin: bytes = b"mysql_native_password") -> bytes:
    """Un Handshake v10 mínimo pero real: CLIENT_SECURE_CONNECTION + CLIENT_PLUGIN_AUTH."""
    cap_low = 0x8000  # CLIENT_SECURE_CONNECTION
    cap_high = 0x0008  # CLIENT_PLUGIN_AUTH (1<<19, mitad alta)
    return (
        bytes([10])  # protocol_version
        + b"5.5.5-10.11.6-MariaDB\x00"  # server_version
        + (42).to_bytes(4, "little")  # connection_id
        + b"12345678"  # seed1 (8)
        + b"\x00"  # filler
        + cap_low.to_bytes(2, "little")
        + bytes([33])  # charset
        + (2).to_bytes(2, "little")  # status flags
        + cap_high.to_bytes(2, "little")
        + bytes([21])  # auth_data_len (8 + 13)
        + b"\x00" * 10  # reservado
        + b"123456789012\x00"  # seed2: 13 bytes, el último NUL (se recorta al parsear)
        + auth_plugin + b"\x00"
    )


_OK = b"\x00\x00\x00\x02\x00\x00\x00"  # OK_Packet mínimo: sólo el primer byte importa acá
_ERR_ACCESO = b"\xff\x15\x04#28000Access denied for user"


def _resultset_una_fila(columna: str, valor: str) -> list[bytes]:
    col_def = (
        _lenenc(b"def") + _lenenc(b"") + _lenenc(b"") + _lenenc(b"") + _lenenc(columna.encode())
    )
    eof = b"\xfe\x00\x00\x00\x00"
    fila = _lenenc(valor.encode())
    return [bytes([1]), col_def, eof, fila, eof]


# ── conectar(): handshake + auth ─────────────────────────────────────────


def test_conectar_hace_el_handshake_y_autentica_ok():
    chunks = _en_chunks(_saludo(), _OK)
    sock = FakeSocket({(HOST, PORT): chunks})

    c = conectar(sock, host=HOST, port=PORT, usuario="lector", clave="s3cr3t", base="memoria")

    assert c.conn.handle is not None
    # El segundo paquete mandado es la HandshakeResponse41, con el token de 20 bytes
    # (SHA1) del auth_response -- nunca la contraseña en claro.
    enviados = [l["data"] for l in sock.calls if l["op"] == "send"]
    cuerpo_enviado = enviados[0][4:]  # sin el header de 4 bytes
    assert b"s3cr3t" not in cuerpo_enviado
    assert b"lector\x00" in cuerpo_enviado
    assert b"mysql_native_password" in cuerpo_enviado


def test_conectar_sin_contrasena_no_manda_token():
    chunks = _en_chunks(_saludo(), _OK)
    sock = FakeSocket({(HOST, PORT): chunks})

    conectar(sock, host=HOST, port=PORT, usuario="anon", clave="", base="memoria")

    enviado = [l["data"] for l in sock.calls if l["op"] == "send"][0][4:]
    # Largo de auth_response = 0: el byte de longitud, en 0, pegado justo
    # después del nombre de usuario con su NUL.
    assert b"anon\x00\x00" in enviado


def test_conectar_rechaza_un_plugin_de_auth_no_soportado():
    chunks = _en_chunks(_saludo(auth_plugin=b"caching_sha2_password"))
    sock = FakeSocket({(HOST, PORT): chunks})

    with pytest.raises(ProtocoloError, match="caching_sha2_password"):
        conectar(sock, host=HOST, port=PORT, usuario="u", clave="p", base="memoria")

    # Y cierra el socket aunque la auth no haya arrancado -- no deja la conexión colgada.
    assert sock._abiertas == {}


def test_conectar_password_incorrecta_es_protocoloerror_no_crash():
    chunks = _en_chunks(_saludo(), _ERR_ACCESO)
    sock = FakeSocket({(HOST, PORT): chunks})

    with pytest.raises(ProtocoloError, match="Access denied"):
        conectar(sock, host=HOST, port=PORT, usuario="u", clave="mal", base="memoria")


def test_conectar_destino_no_guionado_es_porterror_del_fake():
    from backend.core.ports import PortError

    sock = FakeSocket()
    with pytest.raises(PortError):
        conectar(sock, host="otro.host", port=9999, usuario="u", clave="p", base="x")


# ── consultar(): result set, OK sin filas, y error ───────────────────────


def _conectado() -> tuple[FakeSocket, object]:
    chunks = _en_chunks(_saludo(), _OK)
    sock = FakeSocket({(HOST, PORT): chunks})
    c = conectar(sock, host=HOST, port=PORT, usuario="u", clave="p", base="memoria")
    return sock, c


def test_consultar_devuelve_filas_como_dicts():
    sock, c = _conectado()
    sock.respuestas[(HOST, PORT)] = _en_chunks(*_resultset_una_fila("uno", "1"))

    filas = consultar(c, "select 1 as uno")

    assert filas == [{"uno": "1"}]


def test_consultar_sin_result_set_devuelve_lista_vacia():
    """Un OK_Packet (ej. tras un `USE x`) no es un result set."""
    sock, c = _conectado()
    sock.respuestas[(HOST, PORT)] = _en_chunks(_OK)

    assert consultar(c, "use memoria") == []


def test_consultar_sql_invalido_levanta_protocoloerror_con_el_mensaje_del_motor():
    sock, c = _conectado()
    err = b"\xff\x5a\x04#42S02Table 'memoria.no_existe' doesn't exist"
    sock.respuestas[(HOST, PORT)] = _en_chunks(err)

    with pytest.raises(ProtocoloError, match="no_existe"):
        consultar(c, "select * from no_existe")


def test_consultar_valor_null_vuelve_none():
    sock, c = _conectado()
    col_def = _lenenc(b"def") + _lenenc(b"") + _lenenc(b"") + _lenenc(b"") + _lenenc(b"x")
    eof = b"\xfe\x00\x00\x00\x00"
    fila_con_null = b"\xfb"  # 0xFB como valor de columna: NULL
    sock.respuestas[(HOST, PORT)] = _en_chunks(bytes([1]), col_def, eof, fila_con_null, eof)

    assert consultar(c, "select null as x") == [{"x": None}]


# ── cerrar(): manda COM_QUIT y cierra el socket igual si falla ───────────


def test_cerrar_cierra_el_socket_aunque_el_quit_falle():
    sock, c = _conectado()

    class _RompeAlMandar(FakeSocket):
        def send(self, conn, data):
            raise RuntimeError("la conexión ya está cortada")

    # Reemplaza el send sólo para este objeto, sin perder lo ya guionado/abierto.
    sock.send = _RompeAlMandar.send.__get__(sock)
    cerrar(sock, c)

    assert sock._abiertas == {}


# ── escapar_valor: el único "bind" que tiene el protocolo simple ────────


def test_escapar_valor_none_es_null():
    assert escapar_valor(None) == "NULL"


def test_escapar_valor_numeros_van_crudos():
    assert escapar_valor(42) == "42"
    assert escapar_valor(3.5) == "3.5"


def test_escapar_valor_booleanos():
    assert escapar_valor(True) == "1"
    assert escapar_valor(False) == "0"


def test_escapar_valor_comillas_y_backslash():
    assert escapar_valor("o'brien") == r"'o\'brien'"
    assert escapar_valor('dijo "hola"') == r"""'dijo \"hola\"'"""
    assert escapar_valor("a\\b") == r"'a\\b'"


def test_escapar_valor_intento_de_inyeccion_queda_contenido():
    """El caso que motiva que exista esta función en vez de interpolar crudo."""
    maligno = "x' OR '1'='1"
    citado = escapar_valor(maligno)

    # Toda comilla del valor original sale escapada (\') — nunca cruda — así
    # que las únicas dos comillas *sin* backslash delante son las que esta
    # función agregó para abrir y cerrar el literal entero.
    sin_escapadas = citado.replace("\\'", "")
    assert sin_escapadas.count("'") == 2
    assert sin_escapadas.startswith("'") and sin_escapadas.endswith("'")
