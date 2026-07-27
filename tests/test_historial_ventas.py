"""Suite del historial de ventas (CLI-05): cliente, agregados por venta y saldo.

`obtener_ventas_historial` no se rehace en esta actividad: se **extiende** la
consulta que introdujo CLI-02 para que cada fila traiga ademas el nombre del
cliente y los tres agregados por venta, todo en una sola sentencia.

Los abonos se insertan aqui con SQL directo (`INSERT INTO venta_pagos ...`)
porque el registro de pagos (`agregar_pago`) es de CLI-03 y todavia no existe:
la tabla `venta_pagos`, en cambio, existe desde FUND-02.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Final

import pytest

import core_ventas
import db

RAIZ_PROYECTO: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent
VENTAS_PATH: Final[pathlib.Path] = RAIZ_PROYECTO / "core_ventas.py"

TIPO_NORMAL: Final[str] = "Normal (con descuento)"

CLAVES_DETALLE: Final[frozenset[str]] = frozenset(
    {
        "venta_id",
        "fecha",
        "codigo",
        "descripcion",
        "cantidad",
        "precio_costo",
        "precio_publico",
        "total",
        "ganancia",
    }
)


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def _seed_producto(conn: sqlite3.Connection, codigo: str, descripcion: str) -> None:
    conn.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        (codigo, descripcion),
    )


def _seed_stock(
    conn: sqlite3.Connection,
    *,
    codigo: str,
    descripcion: str,
    piezas: int,
    costo_total: float,
    folio: str,
) -> None:
    """Producto con `piezas` disponibles en casa (lo del asociado no es stock)."""
    _seed_producto(conn, codigo, descripcion)
    cur = conn.execute("INSERT INTO pedidos (folio_pedido) VALUES (?)", (folio,))
    conn.execute(
        """
        INSERT INTO pedido_detalle (
            pedido_id, codigo_articulo, ocurrencia, cantidad_solicitada,
            cantidad_surtida, cantidad_asociado, cantidad_casa, cantidad_local,
            precio_que_pagas, valor_total_con_iva, tipo
        ) VALUES (?, ?, 1, ?, ?, 0, ?, 0, ?, ?, ?)
        """,
        (
            int(cur.lastrowid), codigo, piezas, piezas, piezas,
            costo_total, costo_total * 1.5, TIPO_NORMAL,
        ),
    )


def _seed_cliente(conn: sqlite3.Connection, nombre: str) -> int:
    cur = conn.execute("INSERT INTO clientes (nombre) VALUES (?)", (nombre,))
    return int(cur.lastrowid)


def _seed_venta(
    conn: sqlite3.Connection,
    *,
    cliente_id: int | None,
    fecha: str,
    lineas: list[tuple[str, int, float, float]],
) -> int:
    """Inserta una venta con sus lineas `(codigo, cantidad, costo, publico)`."""
    cur = conn.execute(
        "INSERT INTO ventas (cliente_id, fecha, observaciones) VALUES (?, ?, ?)",
        (cliente_id, fecha, None),
    )
    venta_id = int(cur.lastrowid)
    for codigo, cantidad, costo, publico in lineas:
        total = cantidad * publico
        conn.execute(
            """
            INSERT INTO venta_detalle (
                venta_id, codigo_articulo, cantidad, precio_costo,
                precio_publico, total, ganancia
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (venta_id, codigo, cantidad, costo, publico, total, total - cantidad * costo),
        )
    return venta_id


def _seed_abono(conn: sqlite3.Connection, venta_id: int, monto: float) -> None:
    """Abono parcial con SQL directo: `agregar_pago` es de CLI-03 (ver D4)."""
    conn.execute(
        "INSERT INTO venta_pagos (venta_id, forma_pago, monto) VALUES (?, ?, ?)",
        (venta_id, "Efectivo", monto),
    )


# ---------------------------------------------------------------------------
# R1 -- T1: nombre del cliente via LEFT JOIN
# ---------------------------------------------------------------------------


def test_historial_incluye_nombre_cliente(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_producto(conn, "11111", "Sarten 24cm")
    cliente_id = _seed_cliente(conn, "Ana Lopez")
    _seed_venta(conn, cliente_id=cliente_id, fecha="2026-07-20 10:00:00",
                lineas=[("11111", 1, 100.0, 180.0)])
    _seed_venta(conn, cliente_id=None, fecha="2026-07-19 10:00:00",
                lineas=[("11111", 1, 100.0, 180.0)])

    # Act
    historial = core_ventas.obtener_ventas_historial(conn)

    # Assert: sin cliente ligado, la venta es de mostrador
    assert [fila["cliente"] for fila in historial] == ["Ana Lopez", "Mostrador"]


# ---------------------------------------------------------------------------
# R2 -- T2: las 9 claves del detalle de linea
# ---------------------------------------------------------------------------


def test_historial_conserva_claves_detalle(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_producto(conn, "11111", "Sarten 24cm")
    _seed_venta(conn, cliente_id=None, fecha="2026-07-20 10:00:00",
                lineas=[("11111", 2, 100.0, 180.0)])

    # Act
    fila = core_ventas.obtener_ventas_historial(conn)[0]

    # Assert
    assert CLAVES_DETALLE <= set(fila)
    assert fila["codigo"] == "11111"
    assert fila["descripcion"] == "Sarten 24cm"
    assert fila["cantidad"] == 2
    assert fila["precio_costo"] == pytest.approx(100.0)
    assert fila["precio_publico"] == pytest.approx(180.0)
    assert fila["total"] == pytest.approx(360.0)
    assert fila["ganancia"] == pytest.approx(160.0)


# ---------------------------------------------------------------------------
# R3 -- T3: agregados por venta
# ---------------------------------------------------------------------------


def test_historial_agrega_total_y_num_productos(conn: sqlite3.Connection) -> None:
    # Arrange: una venta de dos lineas
    _seed_producto(conn, "11111", "Sarten 24cm")
    _seed_producto(conn, "22222", "Olla 5L")
    _seed_venta(
        conn, cliente_id=None, fecha="2026-07-20 10:00:00",
        lineas=[("11111", 2, 100.0, 180.0), ("22222", 1, 200.0, 300.0)],
    )

    # Act
    historial = core_ventas.obtener_ventas_historial(conn)

    # Assert: los agregados son iguales en todas las filas de la venta
    assert len(historial) == 2
    assert [fila["total_venta"] for fila in historial] == [pytest.approx(660.0)] * 2
    assert [fila["num_productos"] for fila in historial] == [2, 2]


# ---------------------------------------------------------------------------
# R4 -- T4: pagos y saldo pendiente
# ---------------------------------------------------------------------------


def test_historial_total_pagado_y_saldo(conn: sqlite3.Connection) -> None:
    # Arrange: una venta con abono parcial y otra sin pagos
    _seed_producto(conn, "11111", "Sarten 24cm")
    con_abono = _seed_venta(conn, cliente_id=None, fecha="2026-07-20 10:00:00",
                            lineas=[("11111", 2, 100.0, 180.0)])
    _seed_abono(conn, con_abono, 150.0)
    _seed_venta(conn, cliente_id=None, fecha="2026-07-19 10:00:00",
                lineas=[("11111", 1, 100.0, 180.0)])

    # Act
    pagada, sin_pagos = core_ventas.obtener_ventas_historial(conn)

    # Assert
    assert pagada["total_pagado"] == pytest.approx(150.0)
    assert pagada["saldo_pendiente"] == pytest.approx(210.0)
    assert sin_pagos["total_pagado"] == 0.0
    assert sin_pagos["saldo_pendiente"] == pytest.approx(180.0)


# ---------------------------------------------------------------------------
# R5 -- T5: una sola consulta parametrizada (sin N+1)
# ---------------------------------------------------------------------------


def _sql_del_historial() -> ast.Call:
    """Nodo de la unica llamada a `execute` dentro de `obtener_ventas_historial`."""
    arbol = ast.parse(VENTAS_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "obtener_ventas_historial":
            llamadas = [
                hijo
                for hijo in ast.walk(nodo)
                if isinstance(hijo, ast.Call)
                and isinstance(hijo.func, ast.Attribute)
                and hijo.func.attr in {"execute", "executemany", "executescript"}
            ]
            assert len(llamadas) == 1
            return llamadas[0]
    raise AssertionError("obtener_ventas_historial no encontrada en core_ventas.py")


def test_historial_sql_unico_parametrizado(conn: sqlite3.Connection) -> None:
    # Arrange: dos ventas con dos lineas cada una -> 4 filas, 1 sola sentencia
    _seed_producto(conn, "11111", "Sarten 24cm")
    _seed_producto(conn, "22222", "Olla 5L")
    for indice in (1, 2):
        venta_id = _seed_venta(
            conn, cliente_id=None, fecha=f"2026-07-2{indice} 10:00:00",
            lineas=[("11111", 1, 100.0, 180.0), ("22222", 1, 200.0, 300.0)],
        )
        _seed_abono(conn, venta_id, 50.0)
    sentencias: list[str] = []
    conn.set_trace_callback(sentencias.append)

    # Act
    try:
        historial = core_ventas.obtener_ventas_historial(conn)
    finally:
        conn.set_trace_callback(None)

    # Assert: una sola sentencia para 4 filas, sin f-string ni `%` en el SQL
    assert len(historial) == 4
    assert len(sentencias) == 1
    sql = _sql_del_historial().args[0]
    assert isinstance(sql, ast.Name)
    assert not isinstance(sql, ast.JoinedStr)


# ---------------------------------------------------------------------------
# R6, R7 -- T6: orden y base vacia
# ---------------------------------------------------------------------------


def test_historial_orden_reciente_primero(conn: sqlite3.Connection) -> None:
    # Arrange: dos ventas el mismo dia y una anterior
    _seed_producto(conn, "11111", "Sarten 24cm")
    vieja = _seed_venta(conn, cliente_id=None, fecha="2026-07-01 09:00:00",
                        lineas=[("11111", 1, 100.0, 180.0)])
    primera_hoy = _seed_venta(conn, cliente_id=None, fecha="2026-07-20 10:00:00",
                              lineas=[("11111", 1, 100.0, 180.0)])
    segunda_hoy = _seed_venta(
        conn, cliente_id=None, fecha="2026-07-20 10:00:00",
        lineas=[("11111", 1, 100.0, 180.0), ("11111", 2, 100.0, 180.0)],
    )

    # Act
    ids = [fila["venta_id"] for fila in core_ventas.obtener_ventas_historial(conn)]

    # Assert: mas reciente primero y lineas de una misma venta contiguas
    assert ids == [segunda_hoy, segunda_hoy, primera_hoy, vieja]


def test_historial_bd_vacia_lista_vacia(conn: sqlite3.Connection) -> None:
    # Arrange: conexion recien inicializada
    # Act
    historial = core_ventas.obtener_ventas_historial(conn)

    # Assert
    assert historial == []


# ---------------------------------------------------------------------------
# R2, R3, R4 -- T12: integracion completa sobre SQLite
# ---------------------------------------------------------------------------


def test_historial_integracion_cliente_detalle_pagos_saldo(conn: sqlite3.Connection) -> None:
    # Arrange: venta real multi-linea a un cliente, con abono parcial
    _seed_stock(conn, codigo="11111", descripcion="Sarten 24cm", piezas=10,
                costo_total=1000.0, folio="C001264")
    _seed_stock(conn, codigo="22222", descripcion="Olla 5L", piezas=4,
                costo_total=800.0, folio="C001265")
    cliente_id = _seed_cliente(conn, "Ana Lopez")
    venta = core_ventas.registrar_venta(
        conn,
        cliente_id,
        [
            {"codigo": "11111", "cantidad": 2, "precio_publico": 180.0},
            {"codigo": "22222", "cantidad": 1, "precio_publico": 300.0},
        ],
        "pago en dos partes",
    )
    _seed_abono(conn, venta["venta_id"], 400.0)
    core_ventas.registrar_venta(
        conn, None, [{"codigo": "11111", "cantidad": 1, "precio_publico": 190.0}]
    )

    # Act
    historial = core_ventas.obtener_ventas_historial(conn)

    # Assert
    del_cliente = [fila for fila in historial if fila["venta_id"] == venta["venta_id"]]
    assert len(del_cliente) == 2
    assert {fila["cliente"] for fila in del_cliente} == {"Ana Lopez"}
    assert {fila["codigo"] for fila in del_cliente} == {"11111", "22222"}
    assert {fila["num_productos"] for fila in del_cliente} == {2}
    assert del_cliente[0]["total_venta"] == pytest.approx(660.0)
    assert del_cliente[0]["total_pagado"] == pytest.approx(400.0)
    assert del_cliente[0]["saldo_pendiente"] == pytest.approx(260.0)
    assert sum(fila["ganancia"] for fila in del_cliente) == pytest.approx(260.0)

    mostrador = [fila for fila in historial if fila["venta_id"] != venta["venta_id"]]
    assert [fila["cliente"] for fila in mostrador] == ["Mostrador"]
    assert mostrador[0]["total_pagado"] == 0.0
    assert mostrador[0]["saldo_pendiente"] == pytest.approx(190.0)
