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

* `generar_entregas`          -- INSERT ... SELECT idempotente; devuelve el conteo.
* `ENTREGA_STATUS_VALIDOS`    -- espejo del CHECK de `entregas_asociado.status`.
* `actualizar_status_entrega` -- avanza el ciclo de vida de una entrega.
* `EntregaError`              -- error de dominio de las entregas.
* `StatusEntregaInvalidoError` -- status fuera del CHECK del esquema.
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


class StatusEntregaInvalidoError(EntregaError):
    """El status pedido no esta en `ENTREGA_STATUS_VALIDOS` (R2).

    Cuelga de `EntregaError` -- y no directamente de `CoreError` -- porque es un
    fallo del dominio de entregas: quien ya capturaba `EntregaError` alrededor de
    la generacion atrapa tambien este sin cambiar nada.
    """


#: Ciclo de vida de una entrega, en el orden en que ocurre. Espeja literalmente
#: el `CHECK (status IN (...))` de `entregas_asociado.status`, y es la **fuente
#: unica** tanto de la guarda de `actualizar_status_entrega` como del combobox de
#: la GUI (R1, R7): un valor nuevo se anade aqui y en el DDL, en ningun otro
#: sitio. Es una tupla -- inmutable y ordenada -- para que la UI pueda ofrecer las
#: opciones en secuencia sin poder mutar la constante por accidente.
ENTREGA_STATUS_VALIDOS: Final[tuple[str, ...]] = (
    "Pendiente de recoger",
    "Recogido - no pagado",
    "Pagado",
)

#: `UPDATE` parametrizado del ciclo de estado. Toca **solo** la columna `status`:
#: `asociados.saldo_pendiente` es propiedad exclusiva de los triggers
#: `trg_pago_insert` / `trg_pago_delete` (ADR-3, R3/R4), y ajustarlo tambien desde
#: aqui seria doble contabilidad (riesgo RT-3).
UPDATE_STATUS_SQL: Final[str] = "UPDATE entregas_asociado SET status = ? WHERE id = ?"

_MSG_STATUS: Final[str] = (
    "Status de entrega no valido: {status!r}. Los permitidos son: {permitidos}."
)


def actualizar_status_entrega(
    conn: sqlite3.Connection, entrega_id: int, status: str
) -> None:
    """Cambia el status de una entrega tras validarlo contra el esquema (R1, R2).

    La guarda corre **antes de cualquier SQL**: un status desconocido nunca llega
    al CHECK de la tabla, de modo que el llamador recibe un error de dominio con
    las opciones validas en vez de un `IntegrityError` opaco. La escritura va en
    su propia transaccion (`with conn:` -> commit al salir, rollback ante error),
    como el resto de las funciones de escritura del proyecto.

    No lee ni escribe `asociados.saldo_pendiente`: el saldo lo mueven unicamente
    los triggers de `entrega_pagos` (ADR-3). Marcar una entrega como "Pagado" es
    un cambio de estado, no un abono.

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        entrega_id: id de la fila de `entregas_asociado`. Un id inexistente
            actualiza cero filas y no es un error.
        status: uno de `ENTREGA_STATUS_VALIDOS`.

    Raises:
        StatusEntregaInvalidoError: si `status` no esta en la constante; se lanza
            sin haber tocado la base.
        EntregaError: si SQLite rechaza la actualizacion.

    Time: O(log n) por el indice de la clave primaria | Space: O(1)
    """
    if status not in ENTREGA_STATUS_VALIDOS:
        raise StatusEntregaInvalidoError(
            _MSG_STATUS.format(
                status=status, permitidos=", ".join(ENTREGA_STATUS_VALIDOS)
            )
        )
    try:
        with conn:
            conn.execute(UPDATE_STATUS_SQL, (status, entrega_id))
    except sqlite3.Error as exc:
        raise EntregaError(
            f"No se pudo actualizar el status de la entrega: {exc}"
        ) from exc


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


#: Listado que consume la pestana Entregas. Un unico JOIN resuelve folio,
#: producto y asociado; los agregados de pago los calcula la capa de pagos por
#: fila (`core_pagos.total_pagado`), no esta consulta, para no duplicar la
#: semantica de redondeo que ya vive alli.
SELECT_ENTREGAS_SQL: Final[str] = """
SELECT e.id,
       e.fecha_entrega,
       ped.folio_pedido,
       pd.codigo_articulo,
       pr.descripcion,
       e.cantidad_entregada,
       e.monto_que_debe,
       e.status,
       e.asociado_id,
       a.nombre AS asociado
FROM   entregas_asociado e
JOIN   pedido_detalle pd ON pd.id = e.pedido_detalle_id
JOIN   pedidos ped       ON ped.id = pd.pedido_id
JOIN   productos pr      ON pr.codigo_articulo = pd.codigo_articulo
JOIN   asociados a       ON a.id = e.asociado_id
ORDER  BY e.fecha_entrega DESC, e.id DESC
"""

#: Claves que expone `listar_entregas`, contrato de la GUI.
CAMPOS_ENTREGA: Final[tuple[str, ...]] = (
    "id",
    "fecha_entrega",
    "folio_pedido",
    "codigo_articulo",
    "descripcion",
    "cantidad_entregada",
    "monto_que_debe",
    "status",
    "asociado_id",
    "asociado",
)


def listar_entregas(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Devuelve las entregas con su contexto, de la mas reciente a la mas antigua.

    Existe porque la GUI nunca ejecuta SQL (ADR-2): la pestana Entregas necesita
    folio, producto y asociado junto a cada entrega, y esa lectura tiene que
    salir de la capa core. Un solo JOIN los resuelve, sin N+1.

    Los agregados de pago (`pagado`, `saldo`) **no** se calculan aqui: los da
    `core_pagos.total_pagado` / `saldo_pendiente` sobre `entrega_pagos`, que es
    donde vive la semantica de redondeo. Duplicarla en esta consulta seria la
    forma segura de que las dos cifras acabaran divergiendo.

    Todos los JOIN son INNER: las FK de `entregas_asociado` son NOT NULL, asi
    que cada entrega tiene siempre detalle, pedido, producto y asociado.

    Args:
        conn: conexion inyectada por el call-site.

    Returns:
        Lista de diccionarios con las claves de `CAMPOS_ENTREGA`.

    Raises:
        EntregaError: si SQLite rechaza la lectura.

    Time: O(n log n) por el ORDER BY | Space: O(n)
    """
    try:
        filas = conn.execute(SELECT_ENTREGAS_SQL).fetchall()
    except sqlite3.Error as exc:
        raise EntregaError(f"No se pudieron leer las entregas: {exc}") from exc
    return [{campo: fila[campo] for campo in CAMPOS_ENTREGA} for fila in filas]
