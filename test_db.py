"""Pruebas de la capa de persistencia (`db.py`).

Cubre R1..R7 de la actividad momachdist-FUND-02. Estructura AAA.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

import db
from db import DbError, ENCARGOS_DDL, _load_schema_sql, get_conn, init_db, ruta_base

#: Captura el nombre de cada objeto declarado por un `CREATE` en un script SQL.
#: Tolera `IF NOT EXISTS`, `UNIQUE` y el nombre entre comillas o corchetes.
_CREATE_RE: Final[re.Pattern[str]] = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?(?:TABLE|VIEW|INDEX|TRIGGER)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)",
    re.IGNORECASE,
)


def _objetos_declarados(script_sql: str) -> frozenset[str]:
    """Nombres de los objetos que un script SQL declara con `CREATE`.

    Derivar el conjunto esperado del propio esquema (en vez de fijarlo a mano)
    hace que R1 cubra *todo* lo declarado — incluidos los indices — y que
    cualquier objeto agregado despues a `db_schema.sql` quede cubierto solo.

    Time: O(n) | Space: O(k) con n = largo del script y k = objetos declarados.
    """
    return frozenset(_CREATE_RE.findall(script_sql))


#: R1 — derivado de `reference/db_schema.sql`, no escrito a mano.
SCHEMA_OBJECTS: Final[frozenset[str]] = _objetos_declarados(_load_schema_sql())

#: R2 — derivado del DDL de encargos que `init_db` agrega sobre el esquema.
ENCARGOS_OBJECTS: Final[frozenset[str]] = _objetos_declarados(ENCARGOS_DDL)

#: Indices nombrados del esquema. Sirven de canario: si el parser se rompe y
#: devuelve un conjunto vacio o parcial, el test de sanidad falla en vez de
#: pasar de forma vacua.
INDICES_ESPERADOS: Final[frozenset[str]] = frozenset(
    {
        "idx_detalle_asociado",
        "idx_detalle_producto",
        "idx_entrega_pagos_entrega",
        "idx_venta_detalle_producto",
        "idx_venta_detalle_venta",
        "idx_venta_pagos_venta",
    }
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Ruta a un archivo `.db` nuevo dentro de `tmp_path`."""
    return tmp_path / "test_inventario.db"


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Conexion ya inicializada sobre un archivo temporal."""
    connection = init_db(db_path)
    yield connection
    connection.close()


def _object_names(connection: sqlite3.Connection) -> set[str]:
    """Nombres de todos los objetos declarados en `sqlite_master`."""
    rows = connection.execute("SELECT name FROM sqlite_master").fetchall()
    return {row["name"] for row in rows}


def _column_names(connection: sqlite3.Connection, table: str) -> list[str]:
    """Columnas de `table` en orden de declaracion."""
    rows = connection.execute(
        "SELECT name FROM pragma_table_info(?)", (table,)
    ).fetchall()
    return [row["name"] for row in rows]


# --------------------------------------------------------------------------
# R5 - ruta_base
# --------------------------------------------------------------------------


def test_ruta_base_returns_existing_directory() -> None:
    base = ruta_base()

    assert base.is_dir()
    assert (base / "db.py").exists()


def test_ruta_base_returns_executable_dir_when_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_exe = tmp_path / "app" / "inventario.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)

    base = ruta_base()

    assert base == fake_exe.parent


# --------------------------------------------------------------------------
# R1 - carga del esquema canonico
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment",
    [
        "CREATE TABLE IF NOT EXISTS productos",
        "CREATE TABLE IF NOT EXISTS pedido_detalle",
        "CREATE VIEW IF NOT EXISTS vw_existencias",
        "CREATE TRIGGER IF NOT EXISTS trg_entrega_insert",
    ],
)
def test_load_schema_sql_contains_expected_tables(fragment: str) -> None:
    sql = db._load_schema_sql()

    assert fragment in sql


def test_load_schema_sql_missing_file_raises_dberror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(db, "SCHEMA_PATH", tmp_path / "no_existe.sql")

    with pytest.raises(DbError):
        db._load_schema_sql()


# --------------------------------------------------------------------------
# R2 - DDL de encargos
# --------------------------------------------------------------------------


def test_encargos_ddl_declares_three_tables() -> None:
    occurrences = ENCARGOS_DDL.count("IF NOT EXISTS")

    assert occurrences == 3
    for table in ENCARGOS_OBJECTS:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ENCARGOS_DDL


# --------------------------------------------------------------------------
# R3 / R5 - get_conn
# --------------------------------------------------------------------------


def test_get_conn_enables_foreign_keys(db_path: Path) -> None:
    connection = get_conn(db_path)

    enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert enabled == 1
    connection.close()


def test_get_conn_uses_row_factory(db_path: Path) -> None:
    connection = get_conn(db_path)

    row = connection.execute("SELECT 1 AS uno").fetchone()

    assert isinstance(row, sqlite3.Row)
    assert row["uno"] == 1
    connection.close()


def test_get_conn_default_path_uses_ruta_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(db, "ruta_base", lambda: tmp_path)

    connection = get_conn()
    connection.close()

    assert (tmp_path / db.DB_FILENAME).exists()


def test_get_conn_unreachable_path_raises_dberror(tmp_path: Path) -> None:
    unreachable = tmp_path / "no" / "existe" / "inventario.db"

    with pytest.raises(DbError):
        get_conn(unreachable)


# --------------------------------------------------------------------------
# R1 / R2 / R4 / R7 - init_db
# --------------------------------------------------------------------------


def test_schema_objects_derivation_is_not_vacuous() -> None:
    """Canario del parser: sin esto, un regex roto haria pasar R1 en vacio."""
    assert len(SCHEMA_OBJECTS) >= 18
    assert INDICES_ESPERADOS.issubset(SCHEMA_OBJECTS)
    assert ENCARGOS_OBJECTS == frozenset({"encargos", "encargo_detalle", "encargo_pagos"})


def test_init_db_creates_all_objects(conn: sqlite3.Connection) -> None:
    names = _object_names(conn)

    assert SCHEMA_OBJECTS.issubset(names)


def test_init_db_creates_every_declared_index(conn: sqlite3.Connection) -> None:
    """R1 dice "every table, view, INDEX and trigger" — los indices tambien."""
    names = _object_names(conn)

    assert INDICES_ESPERADOS.issubset(names)


def test_init_db_creates_encargos(conn: sqlite3.Connection) -> None:
    names = _object_names(conn)

    assert ENCARGOS_OBJECTS.issubset(names)


def test_init_db_is_idempotent(db_path: Path) -> None:
    first = init_db(db_path)
    before = _object_names(first)
    first.close()

    second = init_db(db_path)
    after = _object_names(second)

    assert before == after
    second.close()


def test_init_db_accepts_existing_connection(db_path: Path) -> None:
    existing = get_conn(db_path)

    returned = init_db(existing)

    assert returned is existing
    assert "productos" in _object_names(returned)
    existing.close()


def test_init_db_on_closed_connection_raises_dberror(db_path: Path) -> None:
    closed = get_conn(db_path)
    closed.close()

    with pytest.raises(DbError):
        init_db(closed)


# --------------------------------------------------------------------------
# ADR-6 - armonizacion de venta_pagos
# --------------------------------------------------------------------------


def test_init_db_harmonizes_venta_pagos_fecha_pago(conn: sqlite3.Connection) -> None:
    columns = _column_names(conn, "venta_pagos")

    assert "fecha_pago" in columns


def test_init_db_does_not_duplicate_fecha_pago_on_reinit(db_path: Path) -> None:
    first = init_db(db_path)
    first.close()

    second = init_db(db_path)
    columns = _column_names(second, "venta_pagos")

    assert columns.count("fecha_pago") == 1
    second.close()


def test_venta_pagos_shares_shape_with_entrega_pagos(conn: sqlite3.Connection) -> None:
    venta_cols = set(_column_names(conn, "venta_pagos"))
    entrega_cols = set(_column_names(conn, "entrega_pagos"))

    common = {"id", "forma_pago", "monto", "fecha_pago"}

    assert common.issubset(venta_cols)
    assert common.issubset(entrega_cols)


def test_venta_pagos_fecha_pago_defaults_to_current_date(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("INSERT INTO ventas (cliente_id) VALUES (NULL)")
    venta_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    conn.execute(
        "INSERT INTO venta_pagos (venta_id, forma_pago, monto) VALUES (?, ?, ?)",
        (venta_id, "Efectivo", 100.0),
    )
    row = conn.execute(
        "SELECT fecha_pago FROM venta_pagos WHERE venta_id = ?", (venta_id,)
    ).fetchone()
    expected = conn.execute("SELECT date('now','localtime') AS hoy").fetchone()["hoy"]

    assert row["fecha_pago"] == expected


def test_harmonize_venta_pagos_falls_back_when_default_rejected(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    monkeypatch.setattr(
        db,
        "_ADD_FECHA_PAGO_SQL",
        "ALTER TABLE venta_pagos ADD COLUMN fecha_pago TEXT DEFAULT (no_such_fn())",
    )

    connection = init_db(db_path)
    columns = _column_names(connection, "venta_pagos")

    assert "fecha_pago" in columns
    connection.close()


# --------------------------------------------------------------------------
# R6 - integridad referencial
# --------------------------------------------------------------------------


def test_get_conn_rejects_fk_violation(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO encargo_detalle "
            "(encargo_id, codigo_articulo, cantidad_solicitada) VALUES (?, ?, ?)",
            (9999, "NO-EXISTE", 1),
        )


def test_encargo_detalle_rejects_non_positive_quantity(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("INSERT INTO clientes (nombre) VALUES ('Ana')")
    cliente_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        ("ART-1", "Producto de prueba"),
    )
    conn.execute("INSERT INTO encargos (cliente_id) VALUES (?)", (cliente_id,))
    encargo_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO encargo_detalle "
            "(encargo_id, codigo_articulo, cantidad_solicitada) VALUES (?, ?, ?)",
            (encargo_id, "ART-1", 0),
        )
