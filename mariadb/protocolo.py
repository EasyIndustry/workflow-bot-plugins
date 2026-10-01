"""
El protocolo de cable de MySQL/MariaDB, a mano, sobre `SocketPort`.

Por qué existe este módulo (workflow-bot-core#39): un plugin nunca importa
una librería externa (`pymysql`, `mysqlclient`), sólo pide ports. `SocketPort`
da TCP/TLS crudo y nada de protocolo de aplicación encima — igual que
`UrllibHttpAdapter` implementa HTTP sin librería curada, este módulo
implementa el protocolo cliente/servidor de MySQL en Python puro sobre
`send`/`recv`.

MariaDB es un fork de MySQL que mantiene compatibilidad con ese protocolo —
no es uno propio — así que lo mismo de acá habla con los dos motores sin
ramas distintas: el saludo inicial dice cuál es (`server_version`), pero el
cliente no necesita tratarlos distinto.

**Alcance deliberadamente chico (MVP, igual criterio que sqlite y http):**

- Sólo auth `mysql_native_password`. Si el servidor pide otra cosa
  (`caching_sha2_password` de MySQL 8+, `ed25519` de MariaDB) se levanta
  `ProtocoloError` con un mensaje claro, no un intento a ciegas.
- Protocolo de consulta **simple** (`COM_QUERY`), no prepared statements.
  Eso tiene un precio real: **no hay un canal de parámetros aparte del
  texto de la query**, a diferencia de `sqlite_file`/`StoragePort` (que sí
  tienen bind real). Por eso `escapar_valor` existe — para que un
  `{variable}` resuelto en el SQL salga citado y escapado, no crudo. Es más
  débil que un bind real: depende de que el escapado cubra todos los casos,
  no de que el motor nunca confunda dato con sintaxis. Se documenta en vez
  de fingir que es lo mismo.
- Un solo result set por consulta, y que entre en un paquete de hasta 16MB
  (`0xFFFFFF`). Sin soporte de paquetes encadenados para una fila gigante, ni
  de múltiples statements separados por `;`.
- Valores de fila siempre como texto (protocolo de texto, no binario): un
  número vuelve como el string que mandó el servidor, igual que hace
  cualquier cliente MySQL antes de que el driver lo tipe. Convertir es cosa
  del plugin o del flujo, no de este módulo.

Referencia: "MySQL Client/Server Protocol" (dev.mysql.com/doc/dev/mysql-server/latest/PAGE_PROTOCOL.html).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field


class ProtocoloError(Exception):
    """Fallo del protocolo o del servidor (auth, SQL, motor). No es `PortError`."""


# ── Framing: 3 bytes de largo + 1 de secuencia, por paquete ──────────────


@dataclass
class _Conexion:
    sock: object
    conn: object
    seq: int = 0
    timeout: float | None = field(default=10.0)

    def _recv_exact(self, n: int) -> bytes:
        """
        `recv` del port no promete el tamaño pedido (igual que `socket.recv`):
        acumula hasta tener exactamente `n` bytes, o `ProtocoloError` si el otro
        lado cerró antes de completar el paquete.
        """
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(self.conn, n - len(buf), timeout=self.timeout)
            if not chunk:
                raise ProtocoloError(f"el servidor cerró la conexión a mitad de un paquete ({len(buf)}/{n} bytes)")
            buf += chunk
        return buf

    def read_packet(self) -> bytes:
        header = self._recv_exact(4)
        largo = header[0] | (header[1] << 8) | (header[2] << 16)
        self.seq = header[3] + 1
        return self._recv_exact(largo)

    def write_packet(self, payload: bytes) -> None:
        largo = len(payload)
        if largo > 0xFFFFFF:
            raise ProtocoloError("el paquete supera 16MB: no soportado (sin paquetes encadenados)")
        header = bytes([largo & 0xFF, (largo >> 8) & 0xFF, (largo >> 16) & 0xFF, self.seq & 0xFF])
        self.sock.send(self.conn, header + payload)
        self.seq += 1


# ── Enteros y strings "length-encoded" del protocolo ──────────────────────


def _lenenc_int(buf: bytes, pos: int) -> tuple[int | None, int]:
    primero = buf[pos]
    if primero < 0xFB:
        return primero, pos + 1
    if primero == 0xFB:
        return None, pos + 1  # NULL, sólo tiene sentido como valor de columna
    if primero == 0xFC:
        return struct.unpack_from("<H", buf, pos + 1)[0], pos + 3
    if primero == 0xFD:
        return int.from_bytes(buf[pos + 1:pos + 4], "little"), pos + 4
    if primero == 0xFE:
        return struct.unpack_from("<Q", buf, pos + 1)[0], pos + 9
    raise ProtocoloError(f"entero length-encoded inválido: {primero:#x}")


def _lenenc_str(buf: bytes, pos: int) -> tuple[bytes | None, int]:
    n, pos = _lenenc_int(buf, pos)
    if n is None:
        return None, pos
    return buf[pos:pos + n], pos + n


def _cstr(buf: bytes, pos: int) -> tuple[bytes, int]:
    fin = buf.index(b"\x00", pos)
    return buf[pos:fin], fin + 1


# ── Capability flags que importan acá (protocolo 4.1) ──────────────────────

_CLIENT_LONG_PASSWORD = 1 << 0
_CLIENT_CONNECT_WITH_DB = 1 << 3
_CLIENT_PROTOCOL_41 = 1 << 9
_CLIENT_MULTI_RESULTS = 1 << 17
_CLIENT_PLUGIN_AUTH = 1 << 19
_CLIENT_SECURE_CONNECTION = 1 << 15

_MIS_CAPACIDADES = (
    _CLIENT_LONG_PASSWORD | _CLIENT_PROTOCOL_41 | _CLIENT_SECURE_CONNECTION
    | _CLIENT_CONNECT_WITH_DB | _CLIENT_PLUGIN_AUTH | _CLIENT_MULTI_RESULTS
)
_CHARSET_UTF8MB4 = 45


def _scramble_mysql_native(password: bytes, seed: bytes) -> bytes:
    """
    `mysql_native_password`: SHA1(password) XOR SHA1(seed + SHA1(SHA1(password))).

    Nunca manda la contraseña: sólo el resultado de este desafío-respuesta
    contra la semilla que mandó el servidor en el saludo.
    """
    if not password:
        return b""
    etapa1 = hashlib.sha1(password).digest()
    etapa2 = hashlib.sha1(etapa1).digest()
    etapa3 = hashlib.sha1(seed + etapa2).digest()
    return bytes(a ^ b for a, b in zip(etapa1, etapa3))


@dataclass(frozen=True)
class Saludo:
    server_version: str
    capabilities: int
    seed: bytes
    auth_plugin: str


def _leer_saludo(c: _Conexion) -> Saludo:
    """El primer paquete (Handshake v10) que manda el servidor, antes de pedir nada."""
    pkt = c.read_packet()
    pos = 1  # protocol_version, no se usa
    _server_version, pos = _cstr(pkt, pos)
    pos += 4  # connection_id
    semilla1 = pkt[pos:pos + 8]; pos += 8
    pos += 1  # filler
    cap_baja = struct.unpack_from("<H", pkt, pos)[0]; pos += 2
    pos += 1  # charset
    pos += 2  # status flags
    cap_alta = struct.unpack_from("<H", pkt, pos)[0]; pos += 2
    largo_auth = pkt[pos]; pos += 1
    pos += 10  # reservado
    capacidades = cap_baja | (cap_alta << 16)
    semilla2 = b""
    if capacidades & _CLIENT_SECURE_CONNECTION:
        n = max(13, largo_auth - 8)
        semilla2 = pkt[pos:pos + n].rstrip(b"\x00")
        pos += n
    plugin = b"mysql_native_password"
    if capacidades & _CLIENT_PLUGIN_AUTH:
        plugin, pos = _cstr(pkt, pos)
    return Saludo(
        server_version=_server_version.decode(errors="replace"),
        capabilities=capacidades, seed=semilla1 + semilla2,
        auth_plugin=plugin.decode(errors="replace"),
    )


def _leer_ok_o_error(c: _Conexion, *, contexto: str) -> bytes:
    pkt = c.read_packet()
    if pkt[0] == 0xFF:
        raise ProtocoloError(f"{contexto}: {_mensaje_error(pkt)}")
    return pkt


def _mensaje_error(pkt: bytes) -> str:
    codigo = struct.unpack_from("<H", pkt, 1)[0]
    # Protocolo 4.1: '#' + 5 bytes de sqlstate antes del mensaje.
    resto = pkt[9:] if len(pkt) > 9 and pkt[3:4] == b"#" else pkt[3:]
    return f"[{codigo}] {resto.decode(errors='replace')}"


def conectar(sock, *, host: str, port: int, usuario: str, clave: str, base: str,
             timeout: float = 10.0) -> _Conexion:
    """Handshake + auth `mysql_native_password`. `ProtocoloError` si algo no cierra."""
    conn = sock.connect(host, port, timeout=timeout)
    c = _Conexion(sock=sock, conn=conn, timeout=timeout)
    try:
        saludo = _leer_saludo(c)
        if saludo.auth_plugin != "mysql_native_password":
            raise ProtocoloError(
                f"el servidor pide el método de autenticación '{saludo.auth_plugin}'; "
                "este plugin sólo soporta 'mysql_native_password'"
            )
        token = _scramble_mysql_native(clave.encode(), saludo.seed)
        cuerpo = (
            struct.pack("<IIB23x", _MIS_CAPACIDADES, 1 << 24, _CHARSET_UTF8MB4)
            + usuario.encode() + b"\x00"
            + bytes([len(token)]) + token
            + base.encode() + b"\x00"
            + saludo.auth_plugin.encode() + b"\x00"
        )
        c.write_packet(cuerpo)
        resp = c.read_packet()
        if resp[0] == 0xFE:
            # AuthSwitchRequest: el servidor insiste en otro plugin incluso
            # después de ofrecer mysql_native_password en el saludo.
            nombre_plugin, pos = _cstr(resp, 1)
            raise ProtocoloError(
                f"el servidor pidió cambiar a '{nombre_plugin.decode(errors='replace')}' "
                "durante el login; este plugin sólo soporta 'mysql_native_password'"
            )
        if resp[0] == 0xFF:
            raise ProtocoloError(f"autenticación rechazada: {_mensaje_error(resp)}")
        if resp[0] != 0x00:
            raise ProtocoloError(f"respuesta de auth inesperada: {resp[0]:#x}")
    except Exception:
        sock.close(conn)
        raise
    return c


def consultar(c: _Conexion, sql: str) -> list[dict]:
    """
    `COM_QUERY`: una sola sentencia de sólo lectura. Filas como dicts, valores
    siempre como texto (protocolo de texto de MySQL).
    """
    c.seq = 0
    c.write_packet(bytes([0x03]) + sql.encode("utf-8"))
    primero = c.read_packet()
    if primero[0] == 0xFF:
        raise ProtocoloError(_mensaje_error(primero))
    if primero[0] == 0x00:
        return []  # OK_Packet: sentencia sin result set (ej. "use x", "set ...")

    cantidad_columnas, _ = _lenenc_int(primero, 0)
    columnas: list[str] = []
    for _ in range(cantidad_columnas):
        pkt = c.read_packet()
        pos = 0
        for _ in range(4):  # catalog, schema, table, org_table — no hacen falta
            _v, pos = _lenenc_str(pkt, pos)
        nombre, pos = _lenenc_str(pkt, pos)
        columnas.append(nombre.decode(errors="replace"))
    c.read_packet()  # EOF de fin de metadata de columnas

    filas: list[dict] = []
    while True:
        pkt = c.read_packet()
        # Heurística estándar del protocolo de texto (sin CLIENT_DEPRECATE_EOF,
        # que este cliente no declara): un EOF real pesa menos de 9 bytes.
        if pkt[0] == 0xFE and len(pkt) < 9:
            break
        if pkt[0] == 0xFF:
            raise ProtocoloError(_mensaje_error(pkt))
        pos = 0
        fila = {}
        for nombre in columnas:
            valor, pos = _lenenc_str(pkt, pos)
            fila[nombre] = None if valor is None else valor.decode(errors="replace")
        filas.append(fila)
    return filas


def cerrar(sock, c: _Conexion) -> None:
    """`COM_QUIT` (mejor modales que cortar la TCP de golpe) y cierre del socket."""
    try:
        c.seq = 0
        c.write_packet(bytes([0x01]))
    except Exception:  # noqa: BLE001 — ya nos vamos; lo único que importa es cerrar el socket
        pass
    sock.close(c.conn)


# ── Escapado de valores — el único "bind" que tiene el protocolo simple ───


def escapar_valor(valor) -> str:
    """
    Un valor de Python como literal SQL seguro para pegar en el texto de la
    query — lo más parecido a un bind real que da el protocolo simple (ver
    docstring del módulo: `COM_QUERY` no tiene canal de parámetros aparte).

    Mismas reglas que `mysql_real_escape_string`: escapa backslash, comillas
    simples/dobles, NUL, \\n, \\r y Ctrl-Z, y envuelve en comillas simples.
    """
    if valor is None:
        return "NULL"
    if isinstance(valor, bool):
        return "1" if valor else "0"
    if isinstance(valor, (int, float)):
        return repr(valor)
    texto = str(valor)
    escapado = (
        texto.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
        .replace("\x00", "\\0").replace("\n", "\\n").replace("\r", "\\r").replace("\x1a", "\\Z")
    )
    return f"'{escapado}'"


__all__ = ["ProtocoloError", "conectar", "consultar", "cerrar", "escapar_valor"]
