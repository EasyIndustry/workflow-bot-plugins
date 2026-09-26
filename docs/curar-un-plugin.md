# Cómo curar un plugin propuesto

Esta guía es para quien revisa un PR que agrega o actualiza un `plugins/<name>.json` — persona humana o agente bajo supervisión humana. El CI (`validate.yml`) ya confirmó que el JSON cumple el schema; esto es la parte que CI no puede ni debe hacer automáticamente, porque instalar un paquete propuesto en un PR ejecuta código arbitrario.

No mergees nada de esto sin haber completado todos los pasos.

## Pasos

### a) Leer el manifest del PR

Abrí el `plugins/<name>.json` que agrega/modifica el PR. Anotá:

- `source` (qué vas a instalar)
- `compatible_core` (contra qué versión de `bot-core` probar)
- `ports` que declara — vas a verificar que sean exactamente esos, ni más ni menos.

Revisá también el `repo_url`: navegá el código del plugin antes de instalar nada. Un manifest con `ports: ["fs"]` pero un repo que hace llamadas HTTP directas (sin pasar por el port `http`) es una señal de alerta, no un detalle menor.

### b) Armar un venv limpio

Aislado del resto del sistema, descartable:

```bash
python -m venv /tmp/curate-<name>
source /tmp/curate-<name>/bin/activate
```

No reutilices un venv de otro proyecto ni el entorno donde corre CI/producción.

### c) Instalar el release de `bot-core` que declara `compatible_core`

- Si `workflow-bot-core` está publicado en PyPI, instalá la versión más reciente que satisface el constraint:

  ```bash
  pip install "workflow-bot-core<constraint de compatible_core>"
  ```

- Si no está en PyPI (o querés el código exacto del release), descargá el tarball del release de GitHub correspondiente:

  ```bash
  pip install "https://github.com/EasyIndustry/workflow-bot-core/archive/refs/tags/<tag>.tar.gz"
  ```

  Elegí `<tag>` como el release más reciente que cae dentro del constraint declarado.

### d) Instalar el paquete propuesto

Usando el `source` del manifest:

```bash
# PyPI
pip install <source>

# git+https
pip install "<source>"
```

Si falla acá (dependencias rotas, build errors), es motivo de rechazo o de pedir cambios en el PR — no lo arregles vos silenciosamente en el paquete del plugin.

### d-bis) Si el plugin trae `requirements.txt` (dependencias de cómputo)

Convención de `EasyIndustry/workflow-bot-plugins#1`: un plugin puede declarar librerías de cómputo puro (numpy, trimesh, etc. — no las que hacen I/O, esas van por port) en un `requirements.txt` junto a su `__init__.py`, con `==versión` y `--hash=sha256:...` por línea. La app las instala con `pip install --only-binary :all: --require-hashes`, así que:

- Bajá la wheel de cada dependencia vos mismo desde PyPI (nunca confíes en el hash que trae el PR — un hash correcto solo garantiza que la wheel no cambió *después*, no que sea la que corresponde):

  ```bash
  pip download --only-binary :all: --platform win_amd64 \
      --python-version <la de runtime-release.json> --implementation cp --abi cp<versión> \
      -d /tmp/wheels-<name> <cada dependencia declarada>
  pip hash /tmp/wheels-<name>/*.whl
  ```

- Confirmá que el `requirements.txt` no le falta ninguna transitiva: revisá `Requires-Dist` en el `METADATA` de cada wheel (`pip download` sin `--no-deps` ya las resuelve; comprobalo mirando qué bajó).
- Fijate que `compatible_runtime` en `plugins/<name>.json` sea el tag del release de `workflow-bot-app` cuyo `runtime-release.json` usaste (Python + plataforma).
- Si el plugin trae una carpeta `wheels/` para instalaciones sin internet, no debería estar commiteada en un repo — se espera como asset de un release, no en el árbol de `git` (un binario de varios MB por bump de versión infla el historial para siempre).

### e) Cargar el plugin y confirmar el manifest real

Cargalo como lo haría `workflow-bot-core` (vía su mecanismo de entry points) e inspeccioná el `PluginManifest` que expone en runtime. Confirmá:

- Carga sin errores ni excepciones.
- Los `ports` que declara en runtime son **exactamente** los que dice `plugins/<name>.json` en este repo — ni un port de más (superficie de acceso no documentada) ni de menos (manifest desactualizado).
- El `name` del manifest coincide con el `name` del archivo del índice.

Un snippet mínimo de humo para esto:

```python
from workflow_bot_core.contract import load_plugin  # ajustar al import real del core

manifest = load_plugin("<entry-point-o-modulo-del-plugin>")
assert set(manifest.ports) == {"fs"}  # los ports declarados en plugins/<name>.json
```

(ajustá el import y el mecanismo de carga al que use la versión real de `bot-core` que estés probando.)

### f) Si no trae tests propios, escribir un smoke test mínimo

Si el repo del plugin no tiene su propia suite de tests, no aprobés a ciegas. Escribí (localmente, en tu entorno de curaduría) un test mínimo que:

- Instancie el plugin con fakes/mocks de los ports que declara (nunca contra recursos reales de producción).
- Ejecute un flujo de ejemplo simple que use al menos una de sus tools/actions principales.
- Confirme que el resultado es el esperado y que no hay excepciones no manejadas.

Este test no se commitea en `plugins-registry` (acá no vive código) — es una verificación tuya antes de aprobar. Si te parece que el plugin lo amerita, proponéselo al mantenedor del plugin para que lo sume a su propio repo.

## Criterio de aprobación

Aprobá y mergeá el PR solo si:

- [ ] El JSON pasó la validación de schema en CI.
- [ ] Instalaste `bot-core` en la versión de `compatible_core` y el paquete propuesto, ambos sin errores, en un entorno aislado.
- [ ] El plugin carga y sus `ports` en runtime coinciden exactamente con los declarados.
- [ ] Corriste su suite de tests (o tu smoke test mínimo) y pasó.
- [ ] Si trae `requirements.txt`: generaste vos mismo los hashes desde PyPI, no faltan transitivas, y `compatible_runtime` corresponde al release usado.
- [ ] El plugin es genérico, no atado a un negocio o cliente puntual.
- [ ] La licencia declarada es real y está en el repo del plugin.

Si algo de esto falla, pedí cambios en el PR en vez de mergear con reservas.
