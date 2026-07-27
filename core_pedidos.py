"""Entrada de mercancia: remisiones PDF -> `pedidos` + `pedido_detalle`.

Depende de `core_comun` (coercion de valores), de `core_productos` (upsert del
catalogo, necesario para satisfacer la FK del detalle) y de `core_asociados`
(resolucion del asociado de cada nota). No abre conexiones: la
`sqlite3.Connection` siempre viene inyectada desde el call-site (ADR-2).

* `guardar_pedido`          -- cabecera idempotente por folio.
* `guardar_pedido_detalle`  -- lineas del pedido, sin duplicar.
* `confirmar_carga`         -- orquestador transaccional del lote completo.
* `obtener_movimientos`     -- lectura del historial para la pestana de pedidos.
* `CargaError`              -- error de dominio de la carga de remisiones.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from core_asociados import CLAVE_NOMBRE_ASOCIADO, obtener_o_crear_asociado
from core_comun import CoreError, _entero, _real, _texto
from core_productos import (
    CLAVE_CODIGO,
    CLAVE_PRECIO_PAGAS,
    CLAVE_VALOR_TOTAL,
    _aplicar_upsert_productos,
)
from core_reparto import estampar_asociado_id

CLAVE_FOLIO: Final[str] = "Folio de pedido"

#: Cabecera del pedido (R1, R5, R8). `semana_id` queda NULL a proposito: la
#: vinculacion con `semanas_catalogo` es responsabilidad de BW-01. El
#: `DO NOTHING` sobre `UNIQUE(folio_pedido)` hace la insercion idempotente.
INSERT_PEDIDO_SQL: Final[str] = """
INSERT INTO pedidos (folio_pedido, semana_id, codigo_nota, distribuidora,
                     nombre_asociado_pdf, archivo_origen)
VALUES (?, NULL, ?, ?, ?, ?)
ON CONFLICT(folio_pedido) DO NOTHING
"""

SELECT_PEDIDO_ID_SQL: Final[str] = "SELECT id FROM pedidos WHERE folio_pedido = ?"

CONTAR_PEDIDOS_SQL: Final[str] = "SELECT COUNT(*) AS n FROM pedidos"

#: Linea de detalle (R2, R4). Se usa `ON CONFLICT (...) DO NOTHING` y NO
#: `INSERT OR IGNORE`: `OR IGNORE` silenciaria tambien la violacion del CHECK
#: de reparto, que R3 exige que aflore como `IntegrityError`. Al declarar el
#: conflict target explicito solo se ignora el choque con la tupla UNIQUE.
#: `asociado_id` es parametro desde MERC-02: lo resuelve una sola vez por nota
#: `obtener_o_crear_asociado` y queda NULL solo si la nota no trae nombre.
INSERT_DETALLE_SQL: Final[str] = """
INSERT INTO pedido_detalle (
    pedido_id, codigo_articulo, ocurrencia, cantidad_solicitada,
    cantidad_surtida, cantidad_asociado, asociado_id, cantidad_casa,
    cantidad_local, precio_catalogo, precio_con_iva, precio_que_pagas,
    valor_total_con_iva, tipo)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (pedido_id, codigo_articulo, tipo, ocurrencia) DO NOTHING
"""

#: Historial de movimientos para la pestana de pedidos de la GUI (MERC-06).
#:
#: Sentencia estatica y sin parametros: el filtrado sigue siendo client-side en
#: `_aplicar_filtro`, de modo que nunca se interpola entrada de usuario en SQL.
#: Los alias reproducen literalmente las claves que el Treeview ya consumia del
#: Excel, para que el cuerpo del filtro no cambie.
#:
#: `productos` va con INNER JOIN porque la FK `RESTRICT` del detalle garantiza
#: el match; `asociados` y `semanas_catalogo` van con LEFT JOIN porque
#: `asociado_id` y `semana_id` son nullable. El `COALESCE` del nombre degrada al
#: texto del PDF y, en ultima instancia, a cadena vacia.
SELECT_MOVIMIENTOS_SQL: Final[str] = """
SELECT
    COALESCE(sc.semana_texto, '')                    AS "Semana",
    ped.folio_pedido                                 AS "Folio de pedido",
    pd.codigo_articulo                               AS "Codigo articulo",
    pr.descripcion                                   AS "Descripcion",
    COALESCE(a.nombre, ped.nombre_asociado_pdf, '')  AS "Nombre asociado",
    pd.cantidad_surtida                              AS "Cantidad surtida",
    pd.cantidad_asociado                             AS "Cantidad Asociado",
    pd.cantidad_casa                                 AS "Cantidad Casa",
    pd.cantidad_local                                AS "Cantidad Local",
    pd.precio_que_pagas                              AS "Precio que pagas"
FROM pedido_detalle pd
JOIN pedidos ped              ON ped.id = pd.pedido_id
JOIN productos pr             ON pr.codigo_articulo = pd.codigo_articulo
LEFT JOIN asociados a         ON a.id = pd.asociado_id
LEFT JOIN semanas_catalogo sc ON sc.id = ped.semana_id
ORDER BY ped.fecha_registro DESC, ped.folio_pedido, pd.id
"""


class CargaError(CoreError):
    """Fallo al persistir una remision (cabecera o detalle).

    Hereda de `CoreError` en vez de introducir una jerarquia paralela: la capa
    core ya tenia su base de dominio antes de esta actividad.
    """


def _agrupar_por_folio(
    filas: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Agrupa las filas del extractor por `"Folio de pedido"` conservando orden.

    Time: O(n) | Space: O(n)
    """
    grupos: dict[str, list[dict[str, Any]]] = {}
    for fila in filas:
        folio = _texto(fila.get(CLAVE_FOLIO))
        if not folio:
            raise CargaError(f"Fila sin '{CLAVE_FOLIO}': {fila!r}")
        grupos.setdefault(folio, []).append(fila)
    return grupos


def _contar_pedidos(conn: sqlite3.Connection) -> int:
    """Numero de filas en `pedidos`.

    Time: O(m) | Space: O(1)
    """
    return int(conn.execute(CONTAR_PEDIDOS_SQL).fetchone()["n"])


def guardar_pedido(conn: sqlite3.Connection, meta: dict[str, Any]) -> int:
    """Inserta (o recupera) la cabecera del pedido y devuelve su id (R1, R5, R8).

    Idempotente sobre `UNIQUE(folio_pedido)`: un folio ya presente no genera una
    segunda fila y se devuelve el id existente. `semana_id` queda NULL. No abre
    transaccion: la gobierna `confirmar_carga`.

    Args:
        conn: conexion inyectada por el call-site.
        meta: dict con las claves de cabecera del extractor (una fila sirve).

    Returns:
        `pedidos.id` del folio, nuevo o preexistente.

    Raises:
        CargaError: si `meta` no trae folio o si el id no se puede recuperar.

    Time: O(log m) sobre el indice de `folio_pedido` | Space: O(1)
    """
    folio = _texto(meta.get(CLAVE_FOLIO))
    if not folio:
        raise CargaError(f"Cabecera sin '{CLAVE_FOLIO}': {meta!r}")
    conn.execute(
        INSERT_PEDIDO_SQL,
        (
            folio,
            _texto(meta.get("Codigo nota")),
            _texto(meta.get("Distribuidora")),
            _texto(meta.get(CLAVE_NOMBRE_ASOCIADO)),
            _texto(meta.get("Archivo origen")),
        ),
    )
    fila = conn.execute(SELECT_PEDIDO_ID_SQL, (folio,)).fetchone()
    if fila is None:
        raise CargaError(f"No se pudo recuperar el pedido del folio {folio}")
    return int(fila["id"])


def _parametros_detalle(
    pedido_id: int, fila: dict[str, Any], asociado_id: int | None = None
) -> tuple[Any, ...]:
    """Traduce una fila del PDF a los parametros de `INSERT_DETALLE_SQL` (R2).

    El orden replica exactamente el de la sentencia, `asociado_id` incluido: se
    resuelve una vez por nota y se repite en todas sus lineas (R5, R6).

    Time: O(n) sobre la longitud de los textos | Space: O(1)
    """
    return (
        pedido_id,
        _texto(fila.get(CLAVE_CODIGO)),
        _entero(fila.get("Ocurrencia"), 1),
        _entero(fila.get("Cantidad solicitada")),
        _entero(fila.get("Cantidad surtida")),
        _entero(fila.get("Cantidad Asociado")),
        asociado_id,
        _entero(fila.get("Cantidad Casa")),
        _entero(fila.get("Cantidad Local")),
        _real(fila.get("Precio catalogo")),
        _real(fila.get("Precio con IVA")),
        _real(fila.get(CLAVE_PRECIO_PAGAS)),
        _real(fila.get(CLAVE_VALOR_TOTAL)),
        _texto(fila.get("Tipo")),
    )


def guardar_pedido_detalle(
    conn: sqlite3.Connection,
    pedido_id: int,
    filas: list[dict[str, Any]],
    asociado_id: int | None = None,
) -> int:
    """Inserta las lineas del pedido, sin duplicar (R2, R4).

    El choque con `UNIQUE(pedido_id, codigo_articulo, tipo, ocurrencia)` se
    ignora, pero el CHECK de reparto sigue abortando con `sqlite3.IntegrityError`
    (R3) y la FK a `productos` tambien. No abre transaccion.

    Args:
        conn: conexion inyectada por el call-site.
        pedido_id: cabecera a la que pertenecen las lineas.
        filas: registros crudos del extractor para ese folio.
        asociado_id: asociado de la nota, ya resuelto; `None` si no trae nombre.

    Returns:
        Numero de lineas realmente insertadas (los duplicados no cuentan).

    Time: O(n log m) | Space: O(1)
    """
    antes = conn.total_changes
    for fila in filas:
        conn.execute(
            INSERT_DETALLE_SQL,
            _parametros_detalle(pedido_id, fila, asociado_id),
        )
    return conn.total_changes - antes


def confirmar_carga(
    conn: sqlite3.Connection, filas: list[dict[str, Any]]
) -> dict[str, Any]:
    """Persiste un lote confirmado de remisiones (R6, R7, R9).

    Sustituye a `actualizar_excel_maestro` / `_guardar_excel_completo`: la carga
    diaria ya no escribe ningun `.xlsx`. Agrupa por folio y, dentro de un unico
    `with conn:`, aplica productos (para satisfacer la FK), cabecera, asociado y
    detalle. Ese `with` es el unico punto de commit: cualquier fallo deja cero
    filas, incluidas las altas de asociados.

    La resolucion del asociado es por nota, no por lote (R5, R6): un mismo PDF
    con varias notas produce un `asociado_id` por nota, y dos notas con nombres
    equivalentes reutilizan la misma fila de `asociados`. Ese id se sella
    ademas en cada fila del folio (`estampar_asociado_id`), de modo que el
    lote confirmado sale sabiendo a que asociado pertenece cada linea.

    Args:
        conn: conexion inyectada por el call-site.
        filas: registros crudos del extractor, de uno o varios PDF.

    Returns:
        `{"pedidos": nuevas cabeceras, "detalle": lineas insertadas,
        "folios": folios procesados}`.

    Raises:
        CargaError: si SQLite rechaza cualquier parte del lote.

    Time: O(n log m) | Space: O(n)
    """
    grupos = _agrupar_por_folio(filas)
    if not grupos:
        return {"pedidos": 0, "detalle": 0, "folios": []}

    pedidos_antes = _contar_pedidos(conn)
    detalle = 0
    try:
        with conn:
            for filas_folio in grupos.values():
                _aplicar_upsert_productos(conn, filas_folio)
                pedido_id = guardar_pedido(conn, filas_folio[0])
                asociado_id = obtener_o_crear_asociado(
                    conn, filas_folio[0].get(CLAVE_NOMBRE_ASOCIADO)
                )
                estampar_asociado_id(filas_folio, asociado_id)
                detalle += guardar_pedido_detalle(
                    conn, pedido_id, filas_folio, asociado_id
                )
    except sqlite3.Error as exc:
        raise CargaError(f"Carga rechazada por la base de datos: {exc}") from exc

    return {
        "pedidos": _contar_pedidos(conn) - pedidos_antes,
        "detalle": detalle,
        "folios": list(grupos),
    }


def obtener_movimientos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Devuelve el historial de lineas de pedido para la GUI (R1..R4).

    Sustituye a la version que leia el maestro de Excel
    (`cargar_existente(...).to_dict("records")`): una sola consulta con los
    JOIN necesarios, sin anti-patron N+1. Las claves de cada dict son las mismas
    que consumia el Treeview, asi que el filtrado client-side de la pestana no
    cambia (R1, R2).

    El nombre del asociado se resuelve por join contra `asociados` y degrada al
    nombre impreso en el PDF cuando la linea no tiene `asociado_id` (R3); la
    semana llega por LEFT JOIN y queda en `""` mientras BW-01 no la vincule.
    Una base sin movimientos devuelve `[]`, nunca `None` (R4).

    Args:
        conn: conexion inyectada por el call-site (ADR-2), con
            `row_factory = sqlite3.Row` tal y como la entrega `db.get_conn`.

    Returns:
        Lista de dicts con las diez claves del Treeview, ordenada por fecha de
        registro descendente y, dentro de cada folio, por orden de captura.

    Raises:
        CoreError: si SQLite rechaza la lectura.

    Time: O(n log n) por el ORDER BY | Space: O(n)
    """
    try:
        filas = conn.execute(SELECT_MOVIMIENTOS_SQL).fetchall()
    except sqlite3.Error as exc:
        raise CoreError(f"No se pudo leer el historial de pedidos: {exc}") from exc
    return [dict(fila) for fila in filas]
