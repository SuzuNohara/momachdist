"""Capa de conexion y de inicializacion de esquema para SQLite.

Este modulo es la unica frontera con `sqlite3`. Expone:

* `ruta_base()`     -- directorio base de la aplicacion (soporta PyInstaller).
* `get_conn()`      -- conexion lista para usar (row_factory + foreign_keys ON).
* `init_db()`       -- crea/actualiza el esquema de forma idempotente.
* `DbError`         -- error de dominio base de la capa de persistencia.

El esquema canonico vive en `reference/db_schema.sql` (unica fuente de verdad,
ADR-4) y se lee desde disco; solo las tablas de encargos (ADR-5) y la
armonizacion de `venta_pagos` (ADR-6) se declaran aqui.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Final

logger: Final[logging.Logger] = logging.getLogger(__name__)

DB_FILENAME: Final[str] = "inventario.db"
SCHEMA_PATH: Final[Path] = Path(__file__).parent / "reference" / "db_schema.sql"

#: DDL de encargos (ADR-5). Se le agrega `IF NOT EXISTS` a cada tabla para que
#: `init_db` sea idempotente (R4); columnas, FKs y CHECKs son verbatim.
ENCARGOS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS encargos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id    INTEGER NOT NULL REFERENCES clientes(id) ON DELETE RESTRICT,
    fecha_encargo TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status        TEXT NOT NULL DEFAULT 'Pendiente'
                  CHECK (status IN ('Pendiente','Surtido','Entregado','Cancelado')),
    venta_id      INTEGER REFERENCES ventas(id),
    observaciones TEXT
);

CREATE TABLE IF NOT EXISTS encargo_detalle (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    encargo_id          INTEGER NOT NULL REFERENCES encargos(id) ON DELETE CASCADE,
    codigo_articulo     TEXT NOT NULL REFERENCES productos(codigo_articulo) ON DELETE RESTRICT,
    cantidad_solicitada INTEGER NOT NULL CHECK (cantidad_solicitada > 0),
    precio_estimado     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS encargo_pagos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    encargo_id INTEGER NOT NULL REFERENCES encargos(id) ON DELETE CASCADE,
    forma_pago TEXT NOT NULL CHECK (forma_pago IN ('Efectivo','Transferencia','Tarjeta','Otro')),
    monto      REAL NOT NULL CHECK (monto > 0),
    fecha_pago TEXT NOT NULL DEFAULT (date('now','localtime'))
);
"""

_VENTA_PAGOS_TABLE: Final[str] = "venta_pagos"
_FECHA_PAGO_COL: Final[str] = "fecha_pago"
_ADD_FECHA_PAGO_SQL: Final[str] = (
    "ALTER TABLE venta_pagos ADD COLUMN fecha_pago TEXT "
    "DEFAULT (date('now','localtime'))"
)
_ADD_FECHA_PAGO_FALLBACK_SQL: Final[str] = (
    "ALTER TABLE venta_pagos ADD COLUMN fecha_pago TEXT"
)


class DbError(Exception):
    """Error base de dominio de la capa de persistencia."""


def ruta_base() -> Path:
    """Devuelve el directorio base de la aplicacion.

    Bajo PyInstaller (`sys.frozen`) es el directorio del ejecutable; en
    ejecucion normal es el directorio de este modulo.

    Time: O(1) | Space: O(1)
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _load_schema_sql() -> str:
    """Lee el SQL canonico desde `SCHEMA_PATH`.

    Raises:
        DbError: si el archivo de esquema no existe o no puede leerse.

    Time: O(n) sobre el tamano del archivo | Space: O(n)
    """
    try:
        return SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise DbError(f"No se pudo leer el esquema en {SCHEMA_PATH}: {exc}") from exc


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Abre una conexion SQLite lista para usar.

    Fija `row_factory = sqlite3.Row` y ejecuta `PRAGMA foreign_keys = ON`
    inmediatamente: el pragma es por conexion, no lo aporta el esquema (R3, R5).

    Args:
        db_path: ruta al archivo `.db`. Si es `None` se usa
            `ruta_base() / DB_FILENAME`.

    Raises:
        DbError: si `sqlite3` no puede abrir la base.

    Time: O(1) | Space: O(1)
    """
    target: str | Path = db_path if db_path is not None else ruta_base() / DB_FILENAME
    try:
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as exc:
        raise DbError(f"No se pudo abrir la base de datos {target!s}: {exc}") from exc
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Indica si `table` ya tiene la columna `column`.

    Usa la funcion tabular `pragma_table_info` para poder parametrizar la
    consulta (nunca se interpola SQL).

    Time: O(c) sobre el numero de columnas | Space: O(1)
    """
    row = conn.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
        (table, column),
    ).fetchone()
    return row is not None


def _harmonize_venta_pagos(conn: sqlite3.Connection) -> None:
    """Agrega `venta_pagos.fecha_pago` si falta (armonizacion ADR-6).

    Las tres tablas de pagos deben compartir forma para que el componente de
    pagos sea generico. SQLite no tiene `ADD COLUMN IF NOT EXISTS`, asi que se
    inspecciona `pragma_table_info` primero. Si el motor rechaza el DEFAULT no
    constante `date('now','localtime')` se agrega la columna sin DEFAULT y se
    deja constancia en el log.

    Time: O(c) | Space: O(1)
    """
    if not _has_column(conn, _VENTA_PAGOS_TABLE, _FECHA_PAGO_COL):
        try:
            conn.execute(_ADD_FECHA_PAGO_SQL)
        except sqlite3.OperationalError:
            logger.warning(
                "SQLite rechazo el DEFAULT no constante en "
                "venta_pagos.fecha_pago; se agrega la columna sin DEFAULT."
            )
            conn.execute(_ADD_FECHA_PAGO_FALLBACK_SQL)


def init_db(
    target: sqlite3.Connection | str | Path | None = None,
) -> sqlite3.Connection:
    """Crea o actualiza el esquema completo y devuelve la conexion.

    Ejecuta el esquema canonico, luego `ENCARGOS_DDL` y por ultimo la
    armonizacion de `venta_pagos`. Todo el DDL usa `IF NOT EXISTS`, por lo que
    reejecutar es un no-op (R1, R2, R4, R7).

    Args:
        target: conexion existente (se reutiliza) o ruta/`None` para `get_conn`.

    Raises:
        DbError: si el esquema no puede leerse o el DDL falla.

    Time: O(n) sobre el tamano del esquema | Space: O(n)
    """
    conn = target if isinstance(target, sqlite3.Connection) else get_conn(target)
    schema_sql = _load_schema_sql()
    try:
        conn.executescript(schema_sql)
        conn.executescript(ENCARGOS_DDL)
        _harmonize_venta_pagos(conn)
        conn.commit()
    except sqlite3.Error as exc:
        raise DbError(f"Fallo la inicializacion del esquema: {exc}") from exc
    return conn
