# momachdist — Inventario Betterware

App de escritorio local para administrar una distribución Betterware: carga de
remisiones en PDF, inventario, ventas a clientes, entregas y saldos por
asociado, encargos con anticipo y el ciclo de semanas/puntos Betterware.

Todo el estado vive en un solo archivo **SQLite** (`inventario.db`) junto al
programa. No usa red, no tiene autenticación y no necesita servidor.

> Documento para quien desarrolla o mantiene el proyecto.
> Si sólo vas a **usar** el programa, lee `LEEME_INSTRUCCIONES.txt`.

---

## 1. Ejecución en Ubuntu (Python)

Probado en Ubuntu 24.04 LTS con Python 3.13.5, sesión X11 (`DISPLAY=:1`).

### 1.1 Requisito del sistema: tkinter

`tkinter` **no** viene con `pip` — es un paquete del sistema. Sin él la app no
arranca:

```bash
sudo apt install python3-tk
```

El resto de dependencias (`pdfplumber`, `openpyxl`) son wheels puros; no hacen
falta compiladores ni librerías de sistema.

### 1.2 Preparar el entorno virtual (una sola vez)

Desde la raíz del repo:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

Para desarrollar (incluye los tests):

```bash
./.venv/bin/python -m pip install -r requirements-dev.txt
```

### 1.3 Arrancar la app

```bash
./ejecutar.sh
```

o, equivalente, sin el script:

```bash
./.venv/bin/python gui_inventario.py
```

El primer arranque crea `inventario.db` con todas las tablas, vistas y triggers
(`init_db` es idempotente: el segundo arranque no destruye nada).

### 1.4 Verificar el entorno antes de usarlo

Comprobación rápida de que las tres piezas críticas están en su lugar:

```bash
./.venv/bin/python - <<'PY'
import tkinter, pdfplumber, sqlite3
print("tkinter    OK")
print("pdfplumber", pdfplumber.__version__)
print("sqlite     ", sqlite3.sqlite_version)
PY
```

### 1.5 Sin entorno gráfico (SSH, servidor, CI)

La app es tkinter: **necesita un display**. Por SSH usa `ssh -X`, o un display
virtual:

```bash
sudo apt install xvfb
xvfb-run -a ./.venv/bin/python gui_inventario.py
```

La suite de tests, en cambio, **no** necesita display: verifica la GUI por AST y
importando el módulo, nunca abriendo ventanas.

### 1.6 Empaquetado

Hoy **no hay** empaquetado en el repo: ni `generar_ejecutable.bat`, ni `.spec`,
ni `pyinstaller` en los requirements. La forma soportada de ejecutar es Python
directo, como arriba. `db.ruta_base()` ya contempla `sys.frozen`, así que el
código está listo para PyInstaller cuando se decida armar el `.exe` de Windows.

---

## 2. Stack verificado

| Aspecto | Detalle |
|---|---|
| Lenguaje | Python 3.13.5 (mínimo 3.12; el código usa `X \| Y`, `from __future__ import annotations`, `datetime.UTC`) |
| Almacenamiento | SQLite 3.45.3 vía `sqlite3` de stdlib — **sin ORM** (ADR-1) |
| GUI | tkinter / ttk (`python3-tk` del sistema) |
| Parsing PDF | `pdfplumber` 0.11.10 (ADR-4) |
| Reportes | `openpyxl` 3.1.5 — sólo para "Exportar a Excel" |
| Tests | `pytest` 9.1.1 — **1034 tests, ~27 s** |
| Estándar de código | `.langs/python.md` (obligatorio) |

### Dependencias — por qué cada una

| Paquete | Dónde se usa | Archivo |
|---|---|---|
| `pdfplumber` | única lectura de PDF | `pdf_extractor.py` (runtime) |
| `openpyxl` | genera el `.xlsx` de reportes | `export_excel.py` (runtime) |
| `pytest` | suite de tests | dev |
| `pandas` | **sólo** para regenerar el golden del baseline: `tests/generate_baseline.py` importa `reference/inventario_core.py`, que sí usa pandas | dev |

`pandas` **no se importa en ningún módulo de producción ni en ningún test** — por
eso está en `requirements-dev.txt` y no en `requirements.txt`. No lo quites de
dev sin sustituir antes al generador del baseline.

---

## 3. Arquitectura (ADR-2)

```
GUI (tkinter, gui_inventario.py)     ← NUNCA ejecuta SQL
        │ llama funciones de dominio
core.py (fachada, 95 exports)        ← validaciones de negocio + SQL parametrizado
        │ 12 submódulos de dominio
   ┌────┴─────────────┐
pdf_extractor.py    db.py            ← extracción pura   /   conexión + schema
(pdfplumber)        (sqlite3)
                        │
                  inventario.db
```

**Reglas de capa no negociables:**

- La GUI nunca ejecuta SQL. Sólo llama a `core`.
- `pdf_extractor` no importa nada de almacenamiento. Devuelve `list[dict]`.
- `core` recibe la `conn` **inyectada**; no abre conexiones por su cuenta.
  Quien la abre es `App.__init__` (`db.init_db(DB_PATH)`), una por sesión.
- Todo SQL es parametrizado — nunca f-strings ni concatenación.
  Lo vigila `tests/test_sql_parametrizado.py`.

`core.py` es una **fachada**: superó el límite de 400 líneas de `.langs/python.md`
y se dividió por dominio. `core_comun` es la raíz del grafo de imports (define
`CoreError`) y los submódulos nunca importan la fachada — así no hay ciclos.

| Submódulo | Dominio |
|---|---|
| `core_comun` | raíz: `CoreError`, utilidades compartidas |
| `core_productos` | catálogo (upsert desde remisión) |
| `core_asociados` | directorio, saldos, WhatsApp |
| `core_reparto` | reparto Asociado / Casa / Local |
| `core_pedidos` | pedidos y detalle desde PDF |
| `core_existencias` | lectura de `vw_existencias` |
| `core_entregas` | entregas a asociado |
| `core_clientes` | CRM de clientes finales |
| `core_ventas` | registro de venta (escritura) |
| `core_historial` | historial de ventas (lectura) |
| `core_pagos` | pagos N sobre las 3 tablas de pago |
| `core_encargos` | encargos y su ciclo de status |
| `core_semanas` | semanas de catálogo y puntos BW |
| `core_conversion` | encargo → venta (compone 3 dominios) |

---

## 4. Modelo de datos

Fuente de verdad: **`reference/db_schema.sql`**. `db.py` lo ejecuta tal cual y le
**añade** el DDL de encargos (ADR-5: `encargos`, `encargo_detalle`,
`encargo_pagos`), que no está en el schema de referencia.

> ⚠️ **Dato de empaquetado:** `db.SCHEMA_PATH` apunta a
> `reference/db_schema.sql` y se lee **en cada arranque**. `reference/` no es
> sólo documentación: es un artefacto de runtime y tiene que viajar con la app.

**Invariantes:**

- `existencias` es una **vista** (`vw_existencias`), nunca una tabla (ADR-3).
  Era el bug raíz del Excel: una tabla de existencias se desincroniza, una vista no.
- `asociados.saldo_pendiente` lo mantienen triggers; `vw_saldo_asociados` es la
  fuente de verdad para reconciliar.
- Pagos = tablas hijas 1:N (`venta_pagos`, `entrega_pagos`, `encargo_pagos`):
  N abonos con fecha, sin límite (ADR-6).
- `PRAGMA foreign_keys = ON` es **por conexión** — lo pone `get_conn()`, no el schema.
- `encargos.venta_id` traza el encargo hasta la venta que lo surtió.

**Trampa conocida:** `vw_existencias.piezas_recibidas` suma sólo
`cantidad_casa + cantidad_local` — lo que se va al asociado **no** es tu
inventario. Y el reparto por defecto manda todo al asociado. Si siembras stock
sin poner esas dos columnas, `piezas_disponibles` sale 0 y parece que todo falla.

---

## 5. Detalles no funcionales

### 5.1 Objetivos (proporcionales a app local de 1 usuario)

| NFR | Target | Estado |
|---|---|---|
| Performance | Operaciones < 100 ms al volumen real (decenas–cientos de filas) | Sin instrumentar; el volumen lo hace trivial |
| Escalabilidad | Años de operación → miles de filas; índices ya en el schema | Cubierto por diseño |
| Concurrencia | Ninguna: 1 usuaria, 1 proceso, sin locking | Por diseño |
| Disponibilidad | App local, sin SLA | — |
| Seguridad | Sin red, sin auth, sin datos sensibles expuestos. SQL siempre parametrizado | Vigilado por test |
| Observabilidad | Errores a `inventario.log` | Implementado (`backup.setup_logging`) |
| Backup | Copia con timestamp del `.db` en cada arranque | Implementado (`backup.backup_db`) |
| Cobertura de tests | ≥ 80% (umbral de `.localsettings`) | ⚠️ **nunca medida** — falta `pytest-cov` |

### 5.2 Archivos que la app crea en runtime

Todos junto al programa, en `db.ruta_base()` (bajo PyInstaller: el directorio del
ejecutable). Los tres están en `.gitignore`.

| Archivo | Qué es |
|---|---|
| `inventario.db` | La base. **Es todo tu negocio: es el archivo a respaldar.** |
| `inventario.log` | Bitácora de errores (parsing, guardado). Sólo se escribe en fallos |
| `backups/inventario-AAAAMMDD-HHMMSS.db` | Copia automática, una por arranque |

### 5.3 Resiliencia: respaldo y restauración

`backup.startup()` corre **antes** de abrir la base, en `App.__init__`, y nunca
propaga fallos: si el respaldo no se puede hacer, lo registra en el log y la app
arranca igual.

**Restaurar** (el "rollback" del proyecto) es un paso manual — no hay función de
restore en el código:

```bash
# 1. Cierra la app.
# 2. Guarda la base actual por si acaso.
mv inventario.db inventario-mala.db
# 3. Elige el respaldo y ponlo en su lugar.
ls -lt backups/
cp backups/inventario-20260729-101500.db inventario.db
# 4. Vuelve a arrancar.
```

Cada arranque genera un respaldo nuevo, así que `backups/` crece sin límite; hoy
nadie lo purga. Bórralos a mano cuando estorben.

### 5.4 Manejo de errores

`core` levanta `CoreError` y `db` levanta `DbError` ante cualquier violación de
negocio o de persistencia; la GUI las traduce a un diálogo y las manda al log. El
patrón `errores` del parsing se conserva: un PDF ilegible se reporta a la usuaria
**sin abortar** la carga de los demás.

---

## 6. Tests

```bash
./.venv/bin/python -m pytest -q          # toda la suite (~27 s)
./.venv/bin/python -m pytest tests/test_conversion_encargo.py -v
```

No requieren display ni red; cada test corre contra un SQLite temporal.

**Guards de arquitectura** — tests que fallan ante violaciones estructurales, no
ante bugs de lógica. Si tocas algo cerca, entiéndelos antes:

| Test | Qué protege |
|---|---|
| `tests/test_characterization.py` | Golden del parsing PDF (`tests/baseline/*.json`). Cualquier cambio de comportamiento en la extracción lo rompe |
| `tests/test_gui_cableado.py` | Importa la GUI **y** compara por AST todas sus referencias `core.*` contra la fachada. Allowlist `PENDIENTES_CLI04`, hoy **vacía** |
| `tests/test_sql_parametrizado.py` | Ningún SQL de la capa de datos se interpola |
| `tests/test_backup.py` | Toda operación de dominio llamada desde la GUI está protegida con bitácora + aviso (`MODULOS_DOMINIO`) |

Las allowlists de estos guards **sólo pueden encoger**. Hay tests que fallan si
dejas ahí algo ya resuelto.

**Regenerar el golden del baseline** (sólo ante un cambio intencionado del parsing):

```bash
./.venv/bin/python -m tests.generate_baseline
```

### El fallo que más veces se repitió en este proyecto

**La GUI llamando a un `core.X` que no existe** — pasó cuatro veces, una de ellas
en la carga de remisiones, el camino diario principal. Siempre por lo mismo: la
suite verificaba la GUI sin importarla, y en ejecución un `os.path.exists` cortaba
antes de que reventara. `test_gui_cableado.py` es la red permanente contra eso.
**Toda oleada que toque la GUI debe dejarla importable.**

---

## 7. Layout

| Path | Rol |
|---|---|
| `gui_inventario.py` | App tkinter — 8 pestañas. Punto de entrada |
| `core.py` + `core_*.py` | Fachada y 12 submódulos de dominio |
| `db.py` | `get_conn()`, `init_db()`, `ruta_base()`, `DbError` |
| `pdf_extractor.py` | Parsing de remisiones (pdfplumber) |
| `backup.py` | `setup_logging()`, `backup_db()`, `startup()` — stdlib only |
| `export_excel.py` | Reporte `.xlsx` de existencias y ventas |
| `tests/` | Suite pytest + `tests/baseline/` (golden) |
| `conftest.py` | Inserta `reference/` y la raíz en `sys.path` |
| `test_db.py` | Tests de `db.py` — **quedó en la raíz**, no en `tests/` |
| `spikes/` | Spikes de investigación (ENC-01). No es código de producción |
| `reference/` | **READ-ONLY.** Diseño y código originales. Nunca se modifica |
| `ejecutar.sh` | Lanzador para Ubuntu |

---

## 8. Limitaciones conocidas

Registradas como deuda en NOVA (`.activities/active.md`), ninguna bloquea el uso:

| Deuda | Qué |
|---|---|
| DEUDA-01 | La **cobertura nunca se midió**: falta `pytest-cov`. 4 tests están anclados al layout de pdfplumber, de ahí que los requirements estén pineados |
| DEUDA-03 | `core_reparto.aplicar_default_post_extraccion` no tiene call-site |
| DEUDA-06 | `encargos.venta_id` sin `UNIQUE`: el guard contra reconvertir un encargo (que causaría doble descuento de stock) vive sólo en la capa de dominio |
| DEUDA-07 | Dos `except Exception` en `gui_inventario.py` violan `.langs/python.md` §6 |
| DEUDA-08 | `core_semanas.py` en 399/400 líneas, sin holgura |
| DEUDA-09 | `core_pagos.agregar_pago` no valida el estado del padre: un abono contra un encargo `Cancelado` se escribe. Hoy la única barrera es el gating del botón en la GUI |

| DEUDA-10 | El botón "📁 Abrir Excel" es código muerto y su mensaje engaña: apunta a `inventario_betterware.xlsx`, que ya nadie escribe tras la migración, así que siempre responde *"Todavía no existe el Excel. Primero procesa al menos un PDF"* — y procesar un PDF no lo va a crear. Para reportes el botón correcto es "📤 Exportar a Excel". Falta decidir si se retira el botón o se corrige el mensaje |

**Además, sin registrar:**

- No hay lockfile ni pineado por hash; sólo versiones exactas.
- `backups/` no se purga nunca — crece un archivo por arranque.
- Sin empaquetado en el repo (ver §1.6).

## 9. Decisiones de negocio ya codificadas

No las cambies sin hablar con la dueña — son decisiones suyas, no defaults técnicos:

- **El precio de un encargo es firme.** No se re-cotiza al surtir: si el costo
  subió, la ganancia sale negativa y así se registra.
- **Tras convertir un encargo, `venta_pagos` es la fuente de verdad del cobro.**
  Los reportes de caja deben excluir `encargo_pagos` de encargos ya convertidos,
  o cuentan el anticipo dos veces.
- **Los puntos Betterware se toman por máximo**, no por la primera lectura de la semana.

---

Plan maestro, ADRs y actividades: `.projects/momachdist.md` y `.activities/` en la
raíz de NOVA. Contexto de arquitectura para agentes: `.nova.md`.
