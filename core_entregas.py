"""Generacion de entregas a asociado a partir del detalle del pedido.

Una vez confirmada la carga de una remision (MERC-01/02/03), cada linea de
`pedido_detalle` con reparto a asociado (`cantidad_asociado > 0`) debe
convertirse en una fila de `entregas_asociado`: lo que ese asociado concreto
recibio y, por tanto, debe. Este modulo concentra esa regla y **reemplaza** a
la vieja `construir_entregas_asociado`, que operaba sobre DataFrames/Excel.

Vive por debajo de `core_pedidos` en el grafo de imports: solo depende de
`core_comun` (error de dominio base). Nada de la capa core importa desde aqui
salvo la fachada `core`, de modo que las dependencias siguen apuntando hacia
abajo y no hay ciclos. No abre conexiones: la `sqlite3.Connection` siempre
viene inyectada desde el call-site (ADR-2).

Invariante de saldo (ADR-3): el codigo **nunca** escribe
`asociados.saldo_pendiente`. El trigger `trg_entrega_insert` es la unica fuente
que lo ajusta; `vw_saldo_asociados` es la fuente de verdad de la conciliacion.

* `generar_entregas`  -- INSERT ... SELECT idempotente; devuelve el conteo.
* `EntregaError`      -- error de dominio de la generacion de entregas.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from core_comun import CoreError


class EntregaError(CoreError):
    """Fallo al generar las entregas de un pedido.

    Hereda de `CoreError` en vez de abrir una jerarquia paralela: la capa core
    ya tenia su base de dominio antes de esta actividad.
    """


#: Generacion set-based e idempotente de entregas (R1-R8). Un unico
#: `INSERT ... SELECT` evita el anti-patron N+1:
#:
#: * El `SELECT` filtra los `pedido_detalle` elegibles -- reparto a asociado
#:   (`cantidad_asociado > 0`), con asociado resuelto (`asociado_id IS NOT NULL`,
#:   porque `entregas_asociado.asociado_id` es NOT NULL) y con surtido real
#:   (`cantidad_surtida > 0`, que ademas descarta la division por cero de R3).
#: * `monto_que_debe` es proporcional: parte del total de la linea
#:   (`precio_que_pagas`) la fraccion que se llevo el asociado, con `ROUND(.,2)`
#:   en SQL (R2). El `* 1.0` fuerza aritmetica real y no entera.
#: * `cantidad_entregada = cantidad_asociado` y `asociado_id` del detalle (R6);
#:   `status` y `fecha_entrega` los pone el DEFAULT del esquema.
#: * `NOT EXISTS` + `UNIQUE(pedido_detalle_id)` hacen la operacion
#:   re-ejecutable sin duplicar (R5).
#: * `(:pedido_id IS NULL OR pd.pedido_id = :pedido_id)` restringe a un pedido o,
#:   con NULL, procesa todos (R7).
#:
#: NO se escribe `asociados.saldo_pendiente`: cada fila insertada dispara
#: `trg_entrega_insert`, que suma el `monto_que_debe` al saldo (R4).
INSERT_ENTREGAS_SQL: Final[str] = """
INSERT INTO entregas_asociado
    (pedido_detalle_id, asociado_id, cantidad_entregada, monto_que_debe)
SELECT pd.id, pd.asociado_id, pd.cantidad_asociado,
       ROUND(pd.precio_que_pagas * pd.cantidad_asociado * 1.0
             / pd.cantidad_surtida, 2)
FROM   pedido_detalle pd
WHERE  pd.cantidad_asociado > 0
  AND  pd.asociado_id IS NOT NULL
  AND  pd.cantidad_surtida > 0
  AND  (:pedido_id IS NULL OR pd.pedido_id = :pedido_id)
  AND  NOT EXISTS (
           SELECT 1 FROM entregas_asociado e
           WHERE e.pedido_detalle_id = pd.id
       )
"""


def generar_entregas(
    conn: sqlite3.Connection, pedido_id: int | None = None
) -> int:
    """Genera una entrega por linea con reparto a asociado (R1-R8).

    Inserta exactamente una fila en `entregas_asociado` por cada
    `pedido_detalle` elegible que aun no tenga entrega, dentro de una unica
    transaccion (`with conn:` -> commit al salir, rollback ante error): la
    generacion es todo-o-nada. Reejecutar sobre los mismos datos inserta cero
    filas (R5). El codigo nunca toca `asociados.saldo_pendiente`: lo ajusta el
    trigger `trg_entrega_insert` (R4, ADR-3).

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        pedido_id: restringe la generacion a ese pedido; `None` procesa todos
            los pedidos (R7).

    Returns:
        Numero de entregas realmente creadas en esta corrida (R8).

    Raises:
        EntregaError: si SQLite rechaza la insercion.

    Time: O(n) sobre las lineas candidatas | Space: O(1)
    """
    try:
        with conn:
            cursor = conn.execute(INSERT_ENTREGAS_SQL, {"pedido_id": pedido_id})
            creadas = cursor.rowcount
    except sqlite3.Error as exc:
        raise EntregaError(
            f"No se pudieron generar las entregas: {exc}"
        ) from exc
    return creadas
