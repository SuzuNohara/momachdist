"""Dominio de ventas: canasta multi-producto atomica e historial (CLI-02, CLI-05).

Sustituye la venta mono-producto de la epoca Excel por una **canasta**: la GUI
arma una lista de lineas `{codigo, cantidad, precio_publico}` y `registrar_venta`
las escribe todas o ninguna dentro de una sola transaccion. `VentaError` (hija de
`CoreError`) es el unico error que sale de aqui.

Decisiones que conviene no re-descubrir:

* El stock **nunca** se mantiene a mano: se lee de la vista `vw_existencias`
  (ADR-3), asi que tras insertar el detalle la existencia baja sola.
* La validacion de stock **agrega la cantidad por codigo antes de consultar**:
  dos lineas del mismo producto no pueden sobrevender, y ademas evita el
  anti-patron N+1 (una sola consulta con `IN` para toda la canasta).
* Los pagos son de otro dominio (`venta_pagos`): aqui solo se crean `ventas` y
  `venta_detalle`. El historial si **lee** los pagos, con una subconsulta
  agregada dentro de la misma consulta unica.

Grafo de imports: solo `core_comun` y la stdlib, de modo que la fachada `core`
puede re-exportar este modulo sin ciclos.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from core_comun import CoreError, _texto

#: Claves de cada fila del historial, en el orden del contrato que lee la GUI.
CAMPOS_HISTORIAL: Final[tuple[str, ...]] = (
    "venta_id", "fecha", "cliente", "codigo", "descripcion", "cantidad",
    "precio_costo", "precio_publico", "total", "ganancia", "total_venta",
    "num_productos", "total_pagado", "saldo_pendiente",
)

#: Nombre que toma una venta sin cliente registrado (venta de mostrador).
CLIENTE_MOSTRADOR: Final[str] = "Mostrador"

_MSG_CANASTA_VACIA: Final[str] = "La venta no tiene lineas: agrega al menos un producto."
_MSG_LINEA_INVALIDA: Final[str] = "Cada linea de la venta debe ser un diccionario."
_MSG_CODIGO_VACIO: Final[str] = "Cada linea de la venta necesita un codigo de articulo."
_MSG_CLIENTE_INEXISTENTE: Final[str] = "El cliente indicado no existe."
_MSG_CANTIDAD: Final[str] = "La cantidad de '{codigo}' debe ser un entero mayor que cero."
_MSG_PRECIO: Final[str] = "El precio de '{codigo}' debe ser un numero mayor o igual a cero."

# La lista de marcadores `?` se arma en ejecucion segun cuantos codigos distintos
# traiga la canasta; los valores siguen viajando ligados, nunca interpolados.
_SQL_STOCK_PREFIJO: Final[str] = (
    "SELECT codigo_articulo, descripcion, "
    "COALESCE(piezas_disponibles, 0) AS piezas_disponibles, "
    "COALESCE(precio_unitario_costo, 0) AS precio_costo "
    "FROM vw_existencias WHERE codigo_articulo IN ("
)
_SQL_STOCK_SUFIJO: Final[str] = ")"

_SQL_INSERT_VENTA: Final[str] = "INSERT INTO ventas (cliente_id, observaciones) VALUES (?, ?)"

_SQL_INSERT_DETALLE: Final[str] = (
    "INSERT INTO venta_detalle (venta_id, codigo_articulo, cantidad, precio_costo, "
    "precio_publico, total, ganancia) VALUES (?, ?, ?, ?, ?, ?, ?)"
)

#: Historial completo en **una sola consulta** (CLI-05 R5): el nombre del cliente
#: sale de un `LEFT JOIN` y los tres agregados por venta de subconsultas
#: correlacionadas, de modo que no hay ninguna lectura por fila (sin N+1).
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


class VentaError(CoreError):
    """Error de dominio del registro y la consulta de ventas."""


def _marcadores(cuantos: int) -> str:
    """Devuelve `cuantos` marcadores `?` separados por coma.

    Es la unica parte del SQL que se arma en ejecucion, y solo puede producir
    signos de interrogacion: ningun dato del usuario entra en la sentencia.
    Time: O(n) | Space: O(n)
    """
    return ", ".join(["?"] * cuantos)


def _numero(valor: Any) -> float | None:
    """Convierte `valor` a flotante, o `None` si no es un numero.

    Acepta entero, flotante y texto numerico (lo que puede llegar de un campo de
    la GUI); el booleano no cuenta como numero.

    Time: O(n) sobre la longitud del texto | Space: O(1)
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        return None
    try:
        return float(valor)
    except ValueError:
        return None


def _cantidad_valida(valor: Any, codigo: str) -> int:
    """Exige que la cantidad de una linea sea un entero mayor que cero (R3).

    Raises `VentaError` si no lo es. Time: O(1) | Space: O(1)
    """
    numero = _numero(valor)
    if numero is None or numero <= 0 or numero != int(numero):
        raise VentaError(_MSG_CANTIDAD.format(codigo=codigo))
    return int(numero)


def _precio_valido(valor: Any, codigo: str) -> float:
    """Exige que el precio publico de una linea sea un numero >= 0 (R4).

    Raises `VentaError` si no es numerico o es negativo. Time: O(1) | Space: O(1)
    """
    numero = _numero(valor)
    if numero is None or numero < 0:
        raise VentaError(_MSG_PRECIO.format(codigo=codigo))
    return numero


def _validar_forma(lineas: list[dict]) -> list[dict[str, Any]]:
    """Valida la forma de la canasta y devuelve las lineas normalizadas (R1-R4).

    No toca la base: si algo esta mal, la venta se rechaza antes de escribir.

    Raises:
        VentaError: canasta vacia, linea que no es dict, codigo en blanco,
            cantidad no entera positiva o precio negativo.
    Time: O(n) sobre el numero de lineas | Space: O(n)
    """
    if not lineas:
        raise VentaError(_MSG_CANASTA_VACIA)
    normalizadas: list[dict[str, Any]] = []
    for linea in lineas:
        if not isinstance(linea, dict):
            raise VentaError(_MSG_LINEA_INVALIDA)
        codigo = _texto(linea.get("codigo"))
        if not codigo:
            raise VentaError(_MSG_CODIGO_VACIO)
        normalizadas.append({
            "codigo": codigo,
            "cantidad": _cantidad_valida(linea.get("cantidad"), codigo),
            "precio_publico": _precio_valido(linea.get("precio_publico"), codigo),
        })
    return normalizadas


def _pedido_por_codigo(lineas: list[dict[str, Any]]) -> dict[str, int]:
    """Suma la cantidad pedida por codigo conservando el orden de aparicion (R5).

    Time: O(n) | Space: O(k) codigos distintos
    """
    pedido: dict[str, int] = {}
    for linea in lineas:
        codigo = str(linea["codigo"])
        pedido[codigo] = pedido.get(codigo, 0) + int(linea["cantidad"])
    return pedido


def _leer_existencias(conn: sqlite3.Connection, codigos: list[str]) -> dict[str, sqlite3.Row]:
    """Lee de `vw_existencias` todos los codigos de la canasta en una consulta.

    Una sola sentencia con `IN (?, ?, ...)`: nunca se consulta dentro del bucle.
    Time: O(k log n) | Space: O(k)
    """
    sql = _SQL_STOCK_PREFIJO + _marcadores(len(codigos)) + _SQL_STOCK_SUFIJO
    try:
        filas = conn.execute(sql, tuple(codigos)).fetchall()
    except sqlite3.Error as exc:
        raise VentaError(f"No se pudieron leer las existencias: {exc}") from exc
    return {str(fila["codigo_articulo"]): fila for fila in filas}


def _validar_stock_canasta(
    conn: sqlite3.Connection, lineas: list[dict[str, Any]]
) -> dict[str, dict]:
    """Valida el stock **agregado por codigo** contra `vw_existencias` (R5-R7).

    Agrupar antes de consultar es lo que impide el oversell dentro de la misma
    canasta: dos lineas de 2 piezas sobre un stock de 3 se rechazan aunque
    ninguna de las dos, por si sola, supere lo disponible. Devuelve por codigo
    `{descripcion, disponibles, precio_costo}`.
    Raises:
        VentaError: si un codigo no existe en el inventario (R6) o si la suma
            pedida supera lo disponible (R5).

    Time: O(n + k log m) | Space: O(k)
    """
    pedido = _pedido_por_codigo(lineas)
    existencias = _leer_existencias(conn, list(pedido))
    datos: dict[str, dict] = {}
    for codigo, cantidad in pedido.items():
        fila = existencias.get(codigo)
        if fila is None:
            raise VentaError(f"El articulo '{codigo}' no existe en el inventario.")
        disponibles = int(fila["piezas_disponibles"])
        if cantidad > disponibles:
            raise VentaError(
                f"Stock insuficiente de '{fila['descripcion']}' ({codigo}): "
                f"pediste {cantidad} y hay {disponibles} disponibles."
            )
        datos[codigo] = {
            "descripcion": fila["descripcion"],
            "disponibles": disponibles,
            "precio_costo": float(fila["precio_costo"]),
        }
    return datos


def _calcular_linea(cantidad: int, precio_publico: float, precio_costo: float) -> dict:
    """Total y ganancia de una linea, redondeados a dos decimales (R8).

    Time: O(1) | Space: O(1)
    """
    total = round(cantidad * precio_publico, 2)
    return {
        "cantidad": cantidad, "precio_costo": precio_costo,
        "precio_publico": precio_publico, "total": total,
        "ganancia": round(total - cantidad * precio_costo, 2),
    }


def _calcular_canasta(
    lineas: list[dict[str, Any]], datos: dict[str, dict]
) -> list[dict[str, Any]]:
    """Aplica `_calcular_linea` a cada linea y le pega codigo y descripcion (R8).

    Time: O(n) | Space: O(n)
    """
    calculadas: list[dict[str, Any]] = []
    for linea in lineas:
        codigo = str(linea["codigo"])
        info = datos[codigo]
        calculo = _calcular_linea(
            int(linea["cantidad"]), float(linea["precio_publico"]), float(info["precio_costo"])
        )
        calculadas.append({"codigo": codigo, "descripcion": info["descripcion"], **calculo})
    return calculadas


def _insertar_venta(conn: sqlite3.Connection, cliente_id: int | None,
                    observaciones: str, lineas: list[dict[str, Any]]) -> int:
    """Inserta encabezado y detalle dentro de una unica transaccion (R9, R10).

    El `with conn:` hace commit al salir y rollback ante cualquier excepcion, de
    modo que nunca queda media canasta registrada. Un `cliente_id` inexistente
    levanta `sqlite3.IntegrityError` (la FK esta activa por `db.get_conn`) y se
    traduce a `VentaError`, igual que cualquier otro fallo de escritura.

    Time: O(n) sobre el numero de lineas | Space: O(n)
    """
    try:
        with conn:
            try:
                cursor = conn.execute(_SQL_INSERT_VENTA, (cliente_id, observaciones))
            except sqlite3.IntegrityError as exc:
                raise VentaError(_MSG_CLIENTE_INEXISTENTE) from exc
            venta_id = int(cursor.lastrowid or 0)
            filas = [
                (venta_id, linea["codigo"], linea["cantidad"], linea["precio_costo"],
                 linea["precio_publico"], linea["total"], linea["ganancia"])
                for linea in lineas
            ]
            conn.executemany(_SQL_INSERT_DETALLE, filas)
    except sqlite3.Error as exc:
        raise VentaError(f"No se pudo registrar la venta: {exc}") from exc
    return venta_id


def _construir_resumen(venta_id: int, cliente_id: int | None,
                       lineas: list[dict[str, Any]], datos: dict[str, dict]) -> dict:
    """Arma el resumen de retorno con los totales y el stock restante (R11).

    `disponibles_restantes` es la existencia del codigo **despues** de descontar
    todo lo vendido de ese codigo en esta canasta, asi que dos lineas del mismo
    articulo muestran el mismo restante.

    Time: O(n) | Space: O(n)
    """
    pedido = _pedido_por_codigo(lineas)
    detalle = [
        {**linea, "disponibles_restantes": (
            datos[linea["codigo"]]["disponibles"] - pedido[linea["codigo"]]
        )}
        for linea in lineas
    ]
    return {
        "venta_id": venta_id,
        "cliente_id": cliente_id,
        "total": round(sum(linea["total"] for linea in lineas), 2),
        "ganancia": round(sum(linea["ganancia"] for linea in lineas), 2),
        "num_lineas": len(lineas),
        "lineas": detalle,
    }


def registrar_venta(conn: sqlite3.Connection, cliente_id: int | None,
                    lineas: list[dict], observaciones: str = "") -> dict:
    """Registra una venta multi-producto de forma atomica (R1-R11).

    Cinco fases: validar la forma de la canasta, validar el stock agregado por
    codigo, calcular totales y ganancias, insertar encabezado y detalle en una
    sola transaccion, y devolver el resumen. Los pagos quedan fuera (CLI-03):
    aqui solo se escriben `ventas` y `venta_detalle`.

    Args:
        conn: conexion inyectada, con `PRAGMA foreign_keys = ON`.
        cliente_id: id del cliente, o `None` para una venta de mostrador.
        lineas: canasta `[{codigo, cantidad, precio_publico}, ...]`.
        observaciones: nota libre del encabezado.

    Returns:
        `{venta_id, cliente_id, total, ganancia, num_lineas, lineas}`, con
        `disponibles_restantes` por linea.
    Raises:
        VentaError: canasta invalida, stock insuficiente, codigo inexistente,
            cliente inexistente o fallo de escritura (con rollback completo).

    Time: O(n + k log m) | Space: O(n)
    """
    normalizadas = _validar_forma(lineas)
    datos = _validar_stock_canasta(conn, normalizadas)
    calculadas = _calcular_canasta(normalizadas, datos)
    venta_id = _insertar_venta(conn, cliente_id, _texto(observaciones), calculadas)
    return _construir_resumen(venta_id, cliente_id, calculadas, datos)


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
