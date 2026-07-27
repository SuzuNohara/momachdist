"""Existencias y resumen del Dashboard leidos de las vistas SQL (ADR-3).

El stock nunca se mantiene a mano: la unica fuente es la vista `vw_existencias`
(recibidas/vendidas/disponibles y costo unitario ya calculados con `COALESCE`
y guardas de division en `db_schema.sql`). El Dashboard agrega sobre esa vista
mas `ventas`/`venta_detalle`, `pedidos`, `entregas_asociado` y la vista
`vw_saldo_asociados`. Este modulo solo lee: cada consulta es parametrizada y
cada agregado va envuelto en `COALESCE(...,0)` para el caso de base vacia.

Se conservan las claves de dict en espanol capitalizado que la GUI ya consume
(`"Codigo articulo"`, `"Piezas disponibles"`, ...): el mapeo columna->clave se
hace aqui, dentro de la capa core, para no reescribir las pestanas por dentro.

Dependencias del grafo de imports: solo `core_comun` (raiz), asi que no hay
ciclos y la direccion sigue siendo hacia abajo.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from core_comun import CoreError

# Umbral unico de bajo stock, compartido por Dashboard e Inventario (R8).
STOCK_BAJO_UMBRAL: Final = 3

SELECT_EXISTENCIAS_SQL: Final[str] = """
SELECT
    codigo_articulo,
    descripcion,
    COALESCE(piezas_recibidas, 0)      AS piezas_recibidas,
    COALESCE(piezas_vendidas, 0)       AS piezas_vendidas,
    COALESCE(piezas_disponibles, 0)    AS piezas_disponibles,
    COALESCE(precio_unitario_costo, 0) AS precio_unitario_costo,
    COALESCE(total_pagado_real, 0)     AS total_pagado_real,
    COALESCE(valor_catalogo_total, 0)  AS valor_catalogo_total
FROM vw_existencias
ORDER BY codigo_articulo
"""

SELECT_AGREGADOS_EXISTENCIAS_SQL: Final[str] = """
SELECT
    COUNT(*)                                                     AS productos_distintos,
    COALESCE(SUM(piezas_disponibles), 0)                         AS piezas_disponibles,
    COALESCE(SUM(piezas_disponibles * precio_unitario_costo), 0) AS valor_inventario_costo
FROM vw_existencias
"""

SELECT_AGREGADOS_VENTAS_SQL: Final[str] = """
SELECT
    (SELECT COUNT(*) FROM ventas)                          AS num_ventas,
    COALESCE((SELECT SUM(total) FROM venta_detalle), 0)    AS total_vendido,
    COALESCE((SELECT SUM(ganancia) FROM venta_detalle), 0) AS ganancia_total
"""

CONTAR_PEDIDOS_SQL: Final[str] = "SELECT COUNT(*) FROM pedidos"

CONTAR_ENTREGAS_PENDIENTES_SQL: Final[str] = (
    "SELECT COUNT(*) FROM entregas_asociado WHERE status != 'Pagado'"
)

MONTO_PENDIENTE_SQL: Final[str] = (
    "SELECT COALESCE(SUM(saldo_pendiente), 0) FROM vw_saldo_asociados "
    "WHERE saldo_pendiente > 0"
)

SELECT_BAJO_STOCK_SQL: Final[str] = """
SELECT codigo_articulo, descripcion, piezas_disponibles
FROM vw_existencias
WHERE piezas_disponibles <= ?
ORDER BY codigo_articulo
"""


def _fila_a_existencia(fila: sqlite3.Row) -> dict:
    """Renombra una fila de `vw_existencias` a las claves que lee la GUI (R1).

    Time: O(1) | Space: O(1)
    """
    return {
        "Codigo articulo": fila["codigo_articulo"],
        "Descripcion": fila["descripcion"],
        "Piezas recibidas": fila["piezas_recibidas"],
        "Piezas vendidas": fila["piezas_vendidas"],
        "Piezas disponibles": fila["piezas_disponibles"],
        "Precio unitario costo": fila["precio_unitario_costo"],
        "Total pagado real": fila["total_pagado_real"],
        "Valor catalogo total": fila["valor_catalogo_total"],
    }


def obtener_existencias(conn: sqlite3.Connection) -> list[dict]:
    """Lee `vw_existencias` ordenada por codigo y mapea a las claves GUI (R1-R3).

    Reemplaza a `construir_existencias`/`obtener_catalogo` (ADR-3/ADR-4): el
    stock sale siempre de la vista, nunca de una tabla mantenida a mano. Base
    sin filas de `pedido_detalle` -> lista vacia.

    Time: O(n) sobre el numero de productos | Space: O(n)
    """
    try:
        filas = conn.execute(SELECT_EXISTENCIAS_SQL).fetchall()
    except sqlite3.Error as exc:
        raise CoreError(f"No se pudieron leer las existencias: {exc}") from exc
    return [_fila_a_existencia(fila) for fila in filas]


def _escalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    """Ejecuta una consulta de un solo valor y lo devuelve (0 si no hay fila).

    Time: O(1) amortizado | Space: O(1)
    """
    fila = conn.execute(sql, params).fetchone()
    if fila is None:
        return 0
    valor = fila[0]
    return valor if valor is not None else 0


def _agregados_existencias(conn: sqlite3.Connection) -> tuple[int, int, float]:
    """Totales de stock del Dashboard sobre `vw_existencias` (R5).

    Time: O(n) sobre el numero de productos | Space: O(1)
    """
    fila = conn.execute(SELECT_AGREGADOS_EXISTENCIAS_SQL).fetchone()
    return (
        fila["productos_distintos"],
        fila["piezas_disponibles"],
        fila["valor_inventario_costo"],
    )


def _agregados_ventas(conn: sqlite3.Connection) -> tuple[int, float, float]:
    """Totales de ventas del Dashboard sobre `ventas`/`venta_detalle` (R5).

    Time: O(n) sobre el numero de lineas de venta | Space: O(1)
    """
    fila = conn.execute(SELECT_AGREGADOS_VENTAS_SQL).fetchone()
    return fila["num_ventas"], fila["total_vendido"], fila["ganancia_total"]


def _productos_bajo_stock(conn: sqlite3.Connection) -> list[dict]:
    """Productos con `piezas_disponibles <= STOCK_BAJO_UMBRAL` (R6).

    Time: O(n) sobre el numero de productos | Space: O(k) bajo umbral
    """
    filas = conn.execute(SELECT_BAJO_STOCK_SQL, (STOCK_BAJO_UMBRAL,)).fetchall()
    return [
        {
            "Codigo articulo": fila["codigo_articulo"],
            "Descripcion": fila["descripcion"],
            "Piezas disponibles": fila["piezas_disponibles"],
        }
        for fila in filas
    ]


def _construir_resumen(conn: sqlite3.Connection) -> dict:
    """Ensambla el dict del Dashboard a partir de las consultas agregadas.

    Time: O(n) sobre productos y lineas de venta | Space: O(n)
    """
    productos_distintos, piezas_disponibles, valor_inventario = _agregados_existencias(conn)
    num_ventas, total_vendido, ganancia_total = _agregados_ventas(conn)
    return {
        "productos_distintos": productos_distintos,
        "piezas_disponibles": piezas_disponibles,
        "valor_inventario_costo": valor_inventario,
        "productos_bajo_stock": _productos_bajo_stock(conn),
        "num_ventas": num_ventas,
        "total_vendido": total_vendido,
        "ganancia_total": ganancia_total,
        "num_pedidos_distintos": _escalar(conn, CONTAR_PEDIDOS_SQL),
        "entregas_pendientes_cobro": _escalar(conn, CONTAR_ENTREGAS_PENDIENTES_SQL),
        "monto_pendiente_asociados": _escalar(conn, MONTO_PENDIENTE_SQL),
    }


def obtener_resumen_dashboard(conn: sqlite3.Connection) -> dict:
    """Resumen del Dashboard: 10 claves, cada SUM `COALESCE`d a 0 (R4-R7).

    Base vacia -> todos los numericos 0/0.0 y `productos_bajo_stock == []`.

    Time: O(n) sobre productos y lineas de venta | Space: O(n)
    """
    try:
        return _construir_resumen(conn)
    except sqlite3.Error as exc:
        raise CoreError(f"No se pudo construir el resumen del dashboard: {exc}") from exc
