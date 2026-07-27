"""Catalogo de productos: upsert idempotente y lectura ordenada.

Depende unicamente de `core_comun`. No abre conexiones: la
`sqlite3.Connection` siempre viene inyectada desde el call-site (ADR-2).

* `upsert_producto`   -- alta/actualizacion idempotente de un producto.
* `upsert_productos`  -- lote deduplicado y atomico; devuelve codigos distintos.
* `obtener_catalogo`  -- lectura del catalogo ordenada por codigo.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from core_comun import CoreError, _es_cero, _texto

#: Claves tal como llegan del extractor de PDF (`pdf_extractor`), R4.
CLAVE_CODIGO: Final[str] = "Codigo articulo"
CLAVE_DESCRIPCION: Final[str] = "Descripcion"
CLAVE_PRECIO_PAGAS: Final[str] = "Precio que pagas"
CLAVE_VALOR_TOTAL: Final[str] = "Valor total con IVA"

#: Upsert idempotente (R1, R2, R3, R5, R7).
#:
#: * El `DO UPDATE` no lista `fecha_creacion`, por lo que el valor original
#:   sobrevive a cualquier reprocesamiento del mismo codigo (R2).
#: * `MAX(...)` impide degradar un `es_regalo_o_promo` ya marcado en 1 (R5).
#: * El `WHERE` convierte en no-op las cargas repetidas sin cambios (R3).
UPSERT_PRODUCTO_SQL: Final[str] = """
INSERT INTO productos (codigo_articulo, descripcion, es_regalo_o_promo)
VALUES (?, ?, ?)
ON CONFLICT(codigo_articulo) DO UPDATE SET
    descripcion = excluded.descripcion,
    es_regalo_o_promo = MAX(productos.es_regalo_o_promo,
                            excluded.es_regalo_o_promo)
WHERE productos.descripcion <> excluded.descripcion
   OR productos.es_regalo_o_promo <> excluded.es_regalo_o_promo
"""

#: Lectura del catalogo completo (R6).
SELECT_CATALOGO_SQL: Final[str] = """
SELECT codigo_articulo, descripcion, categoria, es_regalo_o_promo, fecha_creacion
FROM productos
ORDER BY codigo_articulo
"""

_COLUMNAS_CATALOGO: Final[tuple[str, ...]] = (
    "codigo_articulo",
    "descripcion",
    "categoria",
    "es_regalo_o_promo",
    "fecha_creacion",
)


def _mapear_fila(fila: dict[str, Any]) -> tuple[str, str, int]:
    """Traduce una fila del PDF a las columnas de `productos` (R4, R5).

    `categoria` queda fuera a proposito: no viene del PDF en este ciclo y debe
    permanecer NULL. `es_regalo_o_promo` vale 1 unicamente cuando
    `"Precio que pagas"` y `"Valor total con IVA"` son ambos 0 (R5); el precio
    de catalogo no participa de la condicion.

    Args:
        fila: registro crudo del extractor de PDF.

    Returns:
        Tupla `(codigo_articulo, descripcion, es_regalo_o_promo)`.

    Raises:
        CoreError: si la fila no trae `codigo_articulo` o `descripcion`.

    Time: O(n) sobre la longitud de los textos | Space: O(1)
    """
    codigo = _texto(fila.get(CLAVE_CODIGO))
    descripcion = _texto(fila.get(CLAVE_DESCRIPCION))
    if not codigo:
        raise CoreError(f"Fila sin '{CLAVE_CODIGO}': {fila!r}")
    if not descripcion:
        raise CoreError(f"Fila sin '{CLAVE_DESCRIPCION}' para el codigo {codigo}")

    es_regalo = int(
        _es_cero(fila.get(CLAVE_PRECIO_PAGAS))
        and _es_cero(fila.get(CLAVE_VALOR_TOTAL))
    )
    return codigo, descripcion, es_regalo


def _ejecutar_upsert(
    conn: sqlite3.Connection, parametros: tuple[str, str, int]
) -> None:
    """Aplica el upsert ya mapeado y traduce el error de sqlite3 a dominio.

    Time: O(log m) sobre el indice de la PK | Space: O(1)
    """
    try:
        conn.execute(UPSERT_PRODUCTO_SQL, parametros)
    except sqlite3.Error as exc:
        raise CoreError(
            f"No se pudo guardar el producto {parametros[0]}: {exc}"
        ) from exc


def _fusionar(
    previo: tuple[str, str, int] | None, actual: tuple[str, str, int]
) -> tuple[str, str, int]:
    """Combina dos apariciones del mismo codigo dentro de un lote (R5, R7).

    La ultima descripcion gana (semantica de dedup del plan), pero el flag de
    regalo/promocion es *pegajoso*: basta con que una sola aparicion del codigo
    en el lote sea regalo para que `es_regalo_o_promo` quede en 1, sin importar
    su posicion. Sin esta fusion la fila de regalo se perderia antes de llegar
    al SQL cuando no es la ultima ocurrencia.

    Time: O(1) | Space: O(1)
    """
    if previo is None:
        return actual
    codigo, descripcion, es_regalo = actual
    return codigo, descripcion, max(es_regalo, previo[2])


def upsert_producto(conn: sqlite3.Connection, fila: dict[str, Any]) -> None:
    """Inserta o actualiza un producto a partir de una fila del PDF.

    No abre ni cierra transacciones: la atomicidad del lote la gobierna
    `upsert_productos`. Reejecutar con los mismos datos es un no-op (R3) y
    `fecha_creacion` nunca se reescribe (R2).

    Args:
        conn: conexion inyectada por el call-site.
        fila: registro crudo del extractor de PDF.

    Raises:
        CoreError: si la fila es invalida o si SQLite rechaza la escritura.

    Time: O(log m) sobre el indice de la PK | Space: O(1)
    """
    _ejecutar_upsert(conn, _mapear_fila(fila))


def _aplicar_upsert_productos(
    conn: sqlite3.Connection, filas: list[dict[str, Any]]
) -> int:
    """Nucleo de `upsert_productos`, sin transaccion propia.

    Existe porque `with conn:` de `sqlite3` hace COMMIT al salir: si
    `confirmar_carga` invocara `upsert_productos` dentro de su propio
    `with conn:`, el commit interno cortaria el lote y romperia el
    todo-o-nada de R7. La logica de dedup y fusion es identica.

    Time: O(n) | Space: O(k) con k = codigos distintos
    """
    unicas: dict[str, tuple[str, str, int]] = {}
    for fila in filas:
        parametros = _mapear_fila(fila)
        unicas[parametros[0]] = _fusionar(unicas.get(parametros[0]), parametros)
    for parametros in unicas.values():
        _ejecutar_upsert(conn, parametros)
    return len(unicas)


def upsert_productos(conn: sqlite3.Connection, filas: list[dict[str, Any]]) -> int:
    """Procesa un lote de filas del PDF en una sola transaccion (R1, R2, R7).

    Deduplica por `codigo_articulo` conservando la ultima descripcion, pero
    fusionando `es_regalo_o_promo` con `max(...)` sobre todas las apariciones
    del codigo: una fila de regalo en cualquier posicion del lote marca el flag
    (R5, primera clausula). Aplica un upsert por codigo distinto dentro de
    `with conn:`, de modo que un error a mitad del lote no deja filas parciales.

    Args:
        conn: conexion inyectada por el call-site.
        filas: registros crudos del extractor de PDF.

    Returns:
        Numero de codigos distintos procesados.

    Raises:
        CoreError: si alguna fila es invalida o si SQLite rechaza la escritura.

    Time: O(n) | Space: O(k) con k = codigos distintos
    """
    if not filas:
        return 0
    try:
        with conn:
            return _aplicar_upsert_productos(conn, filas)
    except sqlite3.Error as exc:
        raise CoreError(f"Fallo el guardado del lote de productos: {exc}") from exc


def obtener_catalogo(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Devuelve el catalogo completo ordenado por `codigo_articulo` (R6).

    Reemplaza a la version homonima de `inventario_core.py`, que leia el Excel:
    se reescribe la persistencia, no el parsing (ADR-4).

    Args:
        conn: conexion inyectada por el call-site.

    Returns:
        Lista de dicts con las cinco columnas de `productos`; `[]` si no hay
        productos registrados.

    Raises:
        CoreError: si SQLite rechaza la lectura.

    Time: O(m) | Space: O(m)
    """
    try:
        filas = conn.execute(SELECT_CATALOGO_SQL).fetchall()
    except sqlite3.Error as exc:
        raise CoreError(f"No se pudo leer el catalogo de productos: {exc}") from exc
    return [{col: fila[col] for col in _COLUMNAS_CATALOGO} for fila in filas]
