# mime

Leer y armar mails en formato MIME desde un flujo. Las APIs de correo entregan
y reciben el mail entero codificado (Gmail con `format=raw`, un `.eml`
guardado): encabezados codificados, cuerpos en quoted-printable o base64,
partes texto/HTML, adjuntos. Eso no lo resuelve una llamada HTTP guardada, y
es lo único que hace este plugin: pedir el mail o mandarlo lo hace una
conexión de `connections`, así que sirve con cualquier proveedor.

Se instala solo, sin dependencias (librería estándar de Python). Pide sólo el
port `fs`, para guardar y leer adjuntos.

## Tools

Categoría **MIME**.

| Paso | Qué hace | Deja |
|---|---|---|
| `mime.leer \| mensaje={response}, carpeta_adjuntos=C:/casos/{case_id}` | lee el mail | `{asunto}`, `{de}`, `{de_email}`, `{de_nombre}`, `{para}`, `{cc}`, `{fecha}`, `{message_id}`, `{texto}`, `{html}`, `{adjuntos}`, `{cantidad_adjuntos}`, `{respuesta}` |
| `mime.armar \| texto=Recibido., responder={respuesta}` | arma un mail nuevo o una respuesta en el hilo | `{raw}` (base64url, lo que pide Gmail), `{mensaje}` (texto .eml), `{tamano}` |
| `mime.guardar_base64 \| datos={response}, ruta=C:/casos/{case_id}/{nombre}` | guarda en un archivo el `data` de la API de adjuntos | `{ruta}`, `{tamano}` |

- `mensaje` acepta el `raw` de Gmail, base64 común, el texto RFC 822, o la
  `{response}` entera de la conexión que lo trajo (con `raw` adentro). Un
  `.eml` del disco va en `archivo`.
- `{texto}` es el cuerpo en texto; si el mail sólo trae HTML, el HTML pasado a
  texto. Está listo para pasárselo a `laya` y clasificar el mail.
- Los adjuntos se guardan con nombres que no pisan a otro ni se salen de la
  carpeta (un adjunto llamado `../../x.exe` queda como `x.exe` adentro).
- `{respuesta}` trae el destinatario, el asunto con `Re:` y los encabezados
  `In-Reply-To`/`References`: `mime.armar | responder={respuesta}` contesta en
  el mismo hilo sin armar nada a mano.

## Con Gmail

Las llamadas son Actions de `connections`, con el token de `oauth` en una
variable de Config (`Authorization: Bearer {env.GOOGLE_TOKEN}`):

- **Traer un mail**: `GET https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}?format=raw`
  → `mime.leer | mensaje={response}`.
- **Responder**: `mime.armar | texto=..., responder={respuesta}` → `POST .../messages/send`
  con payload `{"raw": "{raw}", "threadId": "{thread_id}"}` (el `threadId` viene
  en la misma respuesta que trajo el mail).
- **Adjuntos grandes** (formato `full`): `GET .../messages/{id}/attachments/{adjunto}`
  → `mime.guardar_base64 | datos={response}, ruta=...`.

## Lo que no hace

- **Poner la fecha** al armar: sería leer el reloj por fuera del port `clock`,
  y el servidor de correo la pone al recibir el mail (Gmail lo hace).
- **Leer el formato `full` de Gmail** (el JSON con las partes ya separadas): es
  propio de un proveedor. Se lee `raw`, que es el mail tal cual.
