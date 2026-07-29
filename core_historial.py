"""Historial de ventas: la lectura del dominio de ventas.

Separado de `core_ventas` cuando ese modulo supero las 400 lineas de
`.langs/python.md` §3 al incorporar la variante componible que necesita ENC-03.
El seam es natural: `core_ventas` escribe (validar canasta, insertar venta) y
este modulo lee (una sola consulta con los agregados por venta). No comparten
estado y sus consumidores son distintos -- la ventana de venta contra la
pestana de historial.

Solo depende de `core_comun`, asi que no introduce ciclos.

* `CAMPOS_HISTORIAL`         -- contrato de 14 claves que consume la GUI.
* `CLIENTE_MOSTRADOR`        -- valor de `cliente` cuando la venta no tiene uno.
* `obtener_ventas_historial` -- historial completo, sin N+1.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

# `VentaError` se queda en `core_ventas`: el historial es la cara de lectura del
# mismo dominio y comparte su error, en vez de introducir uno paralelo que la GUI
# tendria que capturar aparte. La dependencia va en un solo sentido -- `core_ventas`
# no importa este modulo -- asi que no hay ciclo.
from core_ventas import VentaError

CAMPOS_HISTORIAL: Final[tuple[str, ...]] = (
    "venta_id", "fecha", "cliente", "codigo", "descripcion", "cantidad",
    "precio_costo", "precio_publico", "total", "ganancia", "total_venta",
    "num_productos", "total_pagado", "saldo_pendiente",
)

#: Nombre que toma una venta sin cliente registrado (venta de mostrador).
CLIENTE_MOSTRADOR: Final[str] = "Mostrador"

_SQL_HISTORIAL: Final[str] = """
SELECT
    v.id AS venta_id,
    v.fecha AS fecha,
    COALESCE(c.nombre, 'Mostrador') AS cliente,
    d.codigo_articulo AS codigo,
    COALESCE(p.descripcion, d.codigo_articulo) AS descripcion,
    d.cantidad, d.precio_costo, d.precio_publico, d.total, d.ganancia,
    (SELECT COALESCE(SUM(d2.total), 0) FROM venta_detalle d2
      WHERE d2.venta_id = v.id) AS total_venta,
    (SELECT COUNT(*) FROM venta_detalle d3
      WHERE d3.venta_id = v.id) AS num_productos,
    (SELECT COALESCE(SUM(pg.monto), 0) FROM venta_pagos pg
      WHERE pg.venta_id = v.id) AS total_pagado
FROM ventas v
JOIN venta_detalle d ON d.venta_id = v.id
LEFT JOIN clientes c ON c.id = v.cliente_id
LEFT JOIN productos p ON p.codigo_articulo = d.codigo_articulo
ORDER BY v.fecha DESC, v.id DESC
"""




def _fila_historial(fila: sqlite3.Row) -> dict[str, Any]:
    """Mapea una fila cruda del historial al contrato de `CAMPOS_HISTORIAL`.

    `saldo_pendiente` se deriva aqui para redondear una sola vez, con la misma
    semantica que debera adoptar el registro de pagos (CLI-03).
    Time: O(1) | Space: O(1)
    """
    total_venta = round(float(fila["total_venta"]), 2)
    total_pagado = round(float(fila["total_pagado"]), 2)
    return {
        "venta_id": int(fila["venta_id"]),
        "fecha": fila["fecha"],
        "cliente": fila["cliente"],
        "codigo": fila["codigo"],
        "descripcion": fila["descripcion"],
        "cantidad": int(fila["cantidad"]),
        "precio_costo": float(fila["precio_costo"]),
        "precio_publico": float(fila["precio_publico"]),
        "total": float(fila["total"]),
        "ganancia": float(fila["ganancia"]),
        "total_venta": total_venta,
        "num_productos": int(fila["num_productos"]),
        "total_pagado": total_pagado,
        "saldo_pendiente": round(total_venta - total_pagado, 2),
    }


def obtener_ventas_historial(conn: sqlite3.Connection) -> list[dict]:
    """Historial de ventas: una fila por linea vendida (R15, CLI-05 R1-R7).

    Cada fila trae el detalle de la linea, el nombre del cliente (`'Mostrador'`
    cuando la venta no tiene cliente) y tres agregados por venta --`total_venta`,
    `num_productos` y `total_pagado`-- de los que sale `saldo_pendiente`. Todo se
    resuelve en **una sola consulta**: no hay lecturas por fila. Orden
    `fecha DESC, venta_id DESC`, de modo que lo mas reciente queda arriba y las
    lineas de una misma venta quedan contiguas. Base sin ventas -> lista vacia.

    Devuelve una lista de diccionarios con las claves de `CAMPOS_HISTORIAL`, y
    levanta `VentaError` si la consulta falla.

    Time: O(n log n) por el ORDER BY | Space: O(n)
    """
    try:
        filas = conn.execute(_SQL_HISTORIAL).fetchall()
    except sqlite3.Error as exc:
        raise VentaError(f"No se pudo leer el historial de ventas: {exc}") from exc
    return [_fila_historial(fila) for fila in filas]
