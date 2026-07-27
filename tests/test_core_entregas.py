"""Suite de `core_entregas.generar_entregas` contra SQLite en memoria.

Las fixtures usan `db.init_db(":memory:")` para que el esquema real -- con su
CHECK de reparto, la UNIQUE de `entregas_asociado`, el trigger
`trg_entrega_insert` y la vista `vw_saldo_asociados` -- este en juego. El estado
previo (pedidos + detalle) se crea llamando al flujo real de MERC-01/02/03
(`confirmar_carga`), no insertando filas a mano que podrian esquivar una
restriccion.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

import db
from core_entregas import EntregaError, generar_entregas
from core_pedidos import confirmar_carga

TIPO_NORMAL = "Normal (con descuento)"


def _fila(
    *,
    folio: str,
    nombre: str,
    codigo: str,
    surtida: int,
    asociado: int,
    precio: float,
    casa: int = 0,
    local: int = 0,
    descripcion: str = "Producto de prueba",
) -> dict[str, Any]:
    """Construye una fila del extractor lista para `confirmar_carga`.

    `asociado + casa + local` debe igualar `surtida` para respetar el CHECK de
    reparto de `pedido_detalle`.

    Time: O(1) | Space: O(1)
    """
    return {
        "Folio de pedido": folio,
        "Nombre asociado": nombre,
        "Codigo articulo": codigo,
        "Descripcion": descripcion,
        "Cantidad solicitada": surtida,
        "Cantidad surtida": surtida,
        "Cantidad Asociado": asociado,
        "Cantidad Casa": casa,
        "Cantidad Local": local,
        "Precio catalogo": precio,
        "Precio con IVA": precio,
        "Precio que pagas": precio,
        "Valor total con IVA": precio,
        "Tipo": TIPO_NORMAL,
    }


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Conexion en memoria con el esquema real (tablas + trigger + vistas).

    Time: O(1) | Space: O(1)
    """
    return db.init_db(":memory:")


def _pedido_id(conn: sqlite3.Connection, folio: str) -> int:
    """Recupera el `pedidos.id` de un folio ya cargado.

    Time: O(log m) | Space: O(1)
    """
    fila = conn.execute(
        "SELECT id FROM pedidos WHERE folio_pedido = ?", (folio,)
    ).fetchone()
    assert fila is not None
    return int(fila["id"])


def _saldo(conn: sqlite3.Connection, nombre: str) -> float:
    """Lee `asociados.saldo_pendiente` (mantenido por el trigger).

    Time: O(m) | Space: O(1)
    """
    fila = conn.execute(
        "SELECT saldo_pendiente FROM asociados WHERE nombre = ?", (nombre,)
    ).fetchone()
    assert fila is not None
    return float(fila["saldo_pendiente"])


def _saldo_vista(conn: sqlite3.Connection, nombre: str) -> float:
    """Lee `total_debe` de `vw_saldo_asociados` (fuente de conciliacion).

    Time: O(m) | Space: O(1)
    """
    fila = conn.execute(
        "SELECT total_debe FROM vw_saldo_asociados WHERE nombre = ?", (nombre,)
    ).fetchone()
    assert fila is not None
    return float(fila["total_debe"])


def test_generar_entregas_inserta_una_por_linea_asociado(
    conn: sqlite3.Connection,
) -> None:
    # Arrange
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="Ana Ruiz", codigo="A1", surtida=10,
               asociado=3, casa=7, precio=100.0)],
    )
    # Act
    creadas = generar_entregas(conn)
    # Assert
    filas = conn.execute("SELECT * FROM entregas_asociado").fetchall()
    assert creadas == 1
    assert len(filas) == 1
    assert filas[0]["cantidad_entregada"] == 3
    assert filas[0]["asociado_id"] is not None


def test_generar_entregas_saldo_sube_via_trigger_no_por_codigo(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: surtida=10, asociado=3, precio linea=100 -> monto 30.0
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="Ana Ruiz", codigo="A1", surtida=10,
               asociado=3, casa=7, precio=100.0)],
    )
    assert _saldo(conn, "Ana Ruiz") == 0.0  # aun sin entregas
    # Act
    generar_entregas(conn)
    # Assert: el trigger sumo el monto; coincide con la vista de conciliacion
    assert _saldo(conn, "Ana Ruiz") == 30.0
    assert _saldo_vista(conn, "Ana Ruiz") == 30.0


def test_generar_entregas_es_idempotente_en_segunda_corrida(
    conn: sqlite3.Connection,
) -> None:
    # Arrange
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="Ana Ruiz", codigo="A1", surtida=10,
               asociado=3, casa=7, precio=100.0)],
    )
    primera = generar_entregas(conn)
    saldo_tras_primera = _saldo(conn, "Ana Ruiz")
    # Act
    segunda = generar_entregas(conn)
    # Assert: la re-ejecucion no inserta ni mueve el saldo
    assert primera == 1
    assert segunda == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM entregas_asociado"
    ).fetchone()["n"] == 1
    assert _saldo(conn, "Ana Ruiz") == saldo_tras_primera


@pytest.mark.parametrize(
    ("surtida", "asociado", "precio", "esperado"),
    [
        (10, 3, 100.0, 30.0),
        (4, 2, 50.0, 25.0),
        (3, 1, 100.0, 33.33),   # ROUND(100/3, 2)
        (5, 5, 80.0, 80.0),     # linea completa al asociado
    ],
)
def test_generar_entregas_monto_proporcional_exacto(
    conn: sqlite3.Connection,
    surtida: int,
    asociado: int,
    precio: float,
    esperado: float,
) -> None:
    # Arrange
    casa = surtida - asociado
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="Ana Ruiz", codigo="A1", surtida=surtida,
               asociado=asociado, casa=casa, precio=precio)],
    )
    # Act
    generar_entregas(conn)
    # Assert
    fila = conn.execute(
        "SELECT monto_que_debe FROM entregas_asociado"
    ).fetchone()
    assert fila["monto_que_debe"] == esperado


def test_generar_entregas_ignora_linea_sin_reparto_asociado(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: toda la mercancia a Casa -> cantidad_asociado = 0
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="Ana Ruiz", codigo="A1", surtida=8,
               asociado=0, casa=8, precio=100.0)],
    )
    # Act
    creadas = generar_entregas(conn)
    # Assert
    assert creadas == 0


def test_generar_entregas_regalo_surtida_cero_es_seguro(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: producto de regalo (surtida=0 -> reparto 0/0/0), sin division
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="Ana Ruiz", codigo="G1", surtida=0,
               asociado=0, precio=0.0, descripcion="Regalo")],
    )
    # Act
    creadas = generar_entregas(conn)
    # Assert
    assert creadas == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM entregas_asociado"
    ).fetchone()["n"] == 0


def test_generar_entregas_salta_detalle_sin_asociado(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: nota sin nombre -> asociado_id NULL, aun con cantidad_asociado>0
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="", codigo="A1", surtida=5,
               asociado=5, precio=100.0)],
    )
    detalle = conn.execute(
        "SELECT asociado_id, cantidad_asociado FROM pedido_detalle"
    ).fetchone()
    assert detalle["asociado_id"] is None
    assert detalle["cantidad_asociado"] == 5
    # Act
    creadas = generar_entregas(conn)
    # Assert: entregas_asociado.asociado_id es NOT NULL -> se salta
    assert creadas == 0


def test_generar_entregas_filtra_por_pedido_id(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: dos pedidos, cada uno con reparto a asociado
    confirmar_carga(
        conn,
        [_fila(folio="F1", nombre="Ana Ruiz", codigo="A1", surtida=10,
               asociado=10, precio=100.0)],
    )
    confirmar_carga(
        conn,
        [_fila(folio="F2", nombre="Beto Paz", codigo="A1", surtida=4,
               asociado=4, precio=40.0)],
    )
    pid1 = _pedido_id(conn, "F1")
    # Act: solo el pedido 1
    creadas_uno = generar_entregas(conn, pedido_id=pid1)
    # Assert: una sola entrega, la del pedido 1
    assert creadas_uno == 1
    assert conn.execute(
        "SELECT pedido_detalle.pedido_id AS pid FROM entregas_asociado "
        "JOIN pedido_detalle ON pedido_detalle.id = "
        "entregas_asociado.pedido_detalle_id"
    ).fetchone()["pid"] == pid1
    # Act: None procesa el resto
    creadas_todos = generar_entregas(conn, pedido_id=None)
    # Assert
    assert creadas_todos == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM entregas_asociado"
    ).fetchone()["n"] == 2


def test_generar_entregas_retorno_igual_al_conteo(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: tres lineas con reparto a asociado + una solo a Casa
    confirmar_carga(
        conn,
        [
            _fila(folio="F1", nombre="Ana Ruiz", codigo="A1", surtida=10,
                  asociado=4, casa=6, precio=100.0),
            _fila(folio="F1", nombre="Ana Ruiz", codigo="A2", surtida=6,
                  asociado=6, precio=60.0),
            _fila(folio="F1", nombre="Ana Ruiz", codigo="A3", surtida=2,
                  asociado=0, casa=2, precio=20.0),
        ],
    )
    # Act
    creadas = generar_entregas(conn)
    # Assert: solo las dos con cantidad_asociado>0 producen entrega
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM entregas_asociado"
    ).fetchone()["n"]
    assert creadas == 2
    assert creadas == total


def test_generar_entregas_envuelve_error_de_sqlite(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: cerrar la conexion fuerza un sqlite3.Error al ejecutar
    conn.close()
    # Act / Assert
    with pytest.raises(EntregaError):
        generar_entregas(conn)
