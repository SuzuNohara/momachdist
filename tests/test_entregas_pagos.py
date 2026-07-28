"""Entregas a asociado sobre el componente de pagos agnostico de tabla (CLI-04).

CLI-03 dejo `core_pagos` en verde, pero solo ejercito `entrega_pagos` **por la
whitelist**: su fixture no montaba pedido + detalle + asociado, asi que ninguna
prueba llego a insertar un abono real de entrega ni a ver moverse el saldo. Esta
suite cierra ese hueco: es el primer recorrido end-to-end del camino
`entrega_pagos`, y por eso siembra siempre con el flujo real
(`confirmar_carga` -> `generar_entregas`) en vez de insertar entregas a mano.
Sembrar por el flujo real importa porque es `trg_entrega_insert` quien fija el
saldo inicial del asociado; una entrega insertada a mano lo dejaria en cero y las
aserciones de saldo medirian otra cosa que produccion.

Invariante que estas pruebas vigilan (ADR-3, riesgo RT-3): **el codigo nunca
escribe `asociados.saldo_pendiente`**. Cada delta de esa columna que se afirma
aqui lo produjo un trigger -- `trg_pago_insert` al abonar, `trg_pago_delete` al
borrar. Si alguien "arreglara" la aplicacion para ajustar tambien el saldo, los
totales se contarian dos veces y estos tests caerian.

`actualizar_status_entrega` se prueba en el mismo archivo porque comparte el
seeding: el ciclo de estado y los abonos son las dos mitades de la misma pantalla
de entregas, y ninguna de las dos puede tocar el saldo.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any, Final

import pytest

import core_pagos
import db
from core_comun import CoreError
from core_entregas import (
    CAMPOS_ENTREGA,
    ENTREGA_STATUS_VALIDOS,
    EntregaError,
    StatusEntregaInvalidoError,
    actualizar_status_entrega,
    generar_entregas,
    listar_entregas,
)
from core_pedidos import confirmar_carga

TABLA: Final[str] = "entrega_pagos"
TIPO_NORMAL: Final[str] = "Normal (con descuento)"
ASOCIADA: Final[str] = "Ana Ruiz"
FORMA_A: Final[str] = "Efectivo"
FORMA_B: Final[str] = "Transferencia"

#: Linea sembrada por defecto: los 10 articulos surtidos van al asociado, con un
#: precio de linea de 300.0 -> `monto_que_debe` = ROUND(300*10/10, 2) = 300.0.
#: Un monto redondo hace que cualquier descuadre de centavos salte a la vista.
MONTO_QUE_DEBE: Final[float] = 300.0


def _fila(
    *,
    folio: str = "F1",
    nombre: str = ASOCIADA,
    codigo: str = "A1",
    surtida: int = 10,
    asociado: int = 10,
    precio: float = MONTO_QUE_DEBE,
    casa: int = 0,
) -> dict[str, Any]:
    """Construye una fila del extractor lista para `confirmar_carga`.

    `asociado + casa` debe igualar `surtida` para respetar el CHECK de reparto de
    `pedido_detalle`.

    Time: O(1) | Space: O(1)
    """
    return {
        "Folio de pedido": folio,
        "Nombre asociado": nombre,
        "Codigo articulo": codigo,
        "Descripcion": "Producto de prueba",
        "Cantidad solicitada": surtida,
        "Cantidad surtida": surtida,
        "Cantidad Asociado": asociado,
        "Cantidad Casa": casa,
        "Cantidad Local": 0,
        "Precio catalogo": precio,
        "Precio con IVA": precio,
        "Precio que pagas": precio,
        "Valor total con IVA": precio,
        "Tipo": TIPO_NORMAL,
    }


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema real: CHECK, FK, vistas y triggers.

    Time: O(1) | Space: O(1)
    """
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


@pytest.fixture()
def entrega_id(conn: sqlite3.Connection) -> int:
    """Siembra una entrega por el flujo real y devuelve su id.

    Pasa por `confirmar_carga` + `generar_entregas` para que `trg_entrega_insert`
    deje el saldo del asociado en `MONTO_QUE_DEBE`, igual que en produccion.

    Time: O(1) sobre una linea | Space: O(1)
    """
    confirmar_carga(conn, [_fila()])
    creadas = generar_entregas(conn)
    assert creadas == 1
    fila = conn.execute("SELECT id FROM entregas_asociado").fetchone()
    return int(fila["id"])


def _saldo(conexion: sqlite3.Connection, nombre: str = ASOCIADA) -> float:
    """Lee `asociados.saldo_pendiente`, la columna que mantienen los triggers.

    Time: O(m) sobre los asociados | Space: O(1)
    """
    fila = conexion.execute(
        "SELECT saldo_pendiente FROM asociados WHERE nombre = ?", (nombre,)
    ).fetchone()
    assert fila is not None
    return round(float(fila["saldo_pendiente"]), 2)


def _status(conexion: sqlite3.Connection, entrega: int) -> str:
    """Lee el status persistido de una entrega.

    Time: O(log n) | Space: O(1)
    """
    fila = conexion.execute(
        "SELECT status FROM entregas_asociado WHERE id = ?", (entrega,)
    ).fetchone()
    assert fila is not None
    return str(fila["status"])


# --------------------------------------------------------------------------
# T1 -- la constante espeja el CHECK del esquema (R1, R2, R7)
# --------------------------------------------------------------------------


def test_entrega_status_validos_matches_schema(conn: sqlite3.Connection) -> None:
    """R1/R7: la constante es el espejo exacto del CHECK de `status`.

    No se compara contra una lista escrita a mano sino contra el DDL que SQLite
    guarda en `sqlite_master`: si alguien anade un estado al esquema y olvida la
    constante (o al reves), la GUI ofreceria opciones que la base rechaza.
    """
    # Arrange
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("entregas_asociado",),
    ).fetchone()["sql"]

    # Act
    declarados = [estado for estado in ENTREGA_STATUS_VALIDOS if f"'{estado}'" in ddl]

    # Assert
    assert ENTREGA_STATUS_VALIDOS == (
        "Pendiente de recoger",
        "Recogido - no pagado",
        "Pagado",
    )
    assert declarados == list(ENTREGA_STATUS_VALIDOS)
    assert isinstance(ENTREGA_STATUS_VALIDOS, tuple)


def test_status_entrega_invalido_error_es_un_error_de_entrega() -> None:
    """R2: el error nuevo cuelga de `EntregaError`, no de una jerarquia paralela."""
    # Arrange / Act
    jerarquia = StatusEntregaInvalidoError.__mro__

    # Assert
    assert EntregaError in jerarquia
    assert CoreError in jerarquia


def test_default_del_esquema_es_el_primer_status_valido(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R1: una entrega recien generada nace en el primer estado del ciclo."""
    # Arrange / Act
    inicial = _status(conn, entrega_id)

    # Assert
    assert inicial == ENTREGA_STATUS_VALIDOS[0]


# --------------------------------------------------------------------------
# T2 / T8 -- ciclo de estado: valida antes del SQL y persiste (R1, R2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ENTREGA_STATUS_VALIDOS)
def test_actualizar_status_entrega_valida_y_persiste(
    conn: sqlite3.Connection, entrega_id: int, status: str
) -> None:
    """R1: cada estado del ciclo se acepta y queda escrito en la fila."""
    # Arrange
    assert _status(conn, entrega_id) == ENTREGA_STATUS_VALIDOS[0]

    # Act
    resultado = actualizar_status_entrega(conn, entrega_id, status)

    # Assert
    assert resultado is None
    assert _status(conn, entrega_id) == status


@pytest.mark.parametrize(
    "status",
    ["Enviado", "pagado", "PAGADO", "Pagado ", "", "Recogido", None, 3],
)
def test_actualizar_status_entrega_valida_y_rechaza(
    conn: sqlite3.Connection, entrega_id: int, status: Any
) -> None:
    """R2: un status fuera del CHECK rebota y deja la fila intacta.

    El valor invalido nunca alcanza la base: la guarda corre antes del SQL, de
    modo que el llamador ve un error de dominio y no un `IntegrityError`.
    """
    # Arrange
    antes = _status(conn, entrega_id)
    saldo_antes = _saldo(conn)

    # Act / Assert
    with pytest.raises(StatusEntregaInvalidoError):
        actualizar_status_entrega(conn, entrega_id, status)

    assert _status(conn, entrega_id) == antes
    assert _saldo(conn) == saldo_antes


def test_actualizar_status_entrega_no_toca_el_saldo(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R3: marcar "Pagado" es un cambio de estado, no un abono (ADR-3, RT-3).

    Si el helper ajustara tambien `saldo_pendiente`, un usuario que marcara la
    entrega como pagada y ademas registrara el abono descontaria el monto dos
    veces. El saldo solo baja cuando entra una fila en `entrega_pagos`.
    """
    # Arrange
    saldo_antes = _saldo(conn)

    # Act
    actualizar_status_entrega(conn, entrega_id, "Pagado")

    # Assert
    assert saldo_antes == MONTO_QUE_DEBE
    assert _saldo(conn) == MONTO_QUE_DEBE
    assert core_pagos.total_pagado(conn, TABLA, entrega_id) == 0.0


def test_actualizar_status_entrega_id_inexistente_no_altera_nada(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """Un id que no existe actualiza cero filas sin romper ni tocar las demas."""
    # Arrange
    antes = _status(conn, entrega_id)

    # Act
    actualizar_status_entrega(conn, entrega_id + 999, "Pagado")

    # Assert
    assert _status(conn, entrega_id) == antes


# --------------------------------------------------------------------------
# T6 -- varios abonos: el saldo lo baja el trigger (R3, R8)
# --------------------------------------------------------------------------


def test_pagos_entrega_bajan_saldo_por_trigger(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R3/R8: tres abonos suman en `total_pagado` y el trigger baja el saldo.

    Ninguna linea de la aplicacion escribe `asociados.saldo_pendiente`: los 175.75
    que desaparecen del saldo los descuenta `trg_pago_insert`, una vez por INSERT.
    """
    # Arrange
    abonos = (100.0, 50.5, 25.25)
    suma = round(sum(abonos), 2)
    assert _saldo(conn) == MONTO_QUE_DEBE

    # Act
    for indice, monto in enumerate(abonos):
        core_pagos.agregar_pago(conn, TABLA, entrega_id, FORMA_A, monto)
        parcial = round(sum(abonos[: indice + 1]), 2)
        assert _saldo(conn) == round(MONTO_QUE_DEBE - parcial, 2)

    # Assert
    assert core_pagos.total_pagado(conn, TABLA, entrega_id) == suma
    assert _saldo(conn) == round(MONTO_QUE_DEBE - suma, 2)
    assert _saldo(conn) == 124.25


def test_pago_total_deja_el_saldo_en_cero_por_trigger(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R3: liquidar la entrega de una vez deja el saldo exactamente en cero."""
    # Arrange
    assert _saldo(conn) == MONTO_QUE_DEBE

    # Act
    core_pagos.agregar_pago(conn, TABLA, entrega_id, FORMA_B, MONTO_QUE_DEBE)

    # Assert
    assert core_pagos.total_pagado(conn, TABLA, entrega_id) == MONTO_QUE_DEBE
    assert _saldo(conn) == 0.0


def test_pagos_de_una_entrega_no_mueven_el_saldo_de_otro_asociado(
    conn: sqlite3.Connection,
) -> None:
    """R3: el trigger resuelve el asociado por la entrega, no por el pago."""
    # Arrange: dos pedidos, un asociado distinto en cada uno
    confirmar_carga(conn, [_fila(folio="F1", nombre=ASOCIADA)])
    confirmar_carga(conn, [_fila(folio="F2", nombre="Beto Paz", precio=80.0,
                                 surtida=4, asociado=4)])
    generar_entregas(conn)
    entrega_ana = int(
        conn.execute(
            "SELECT e.id AS id FROM entregas_asociado e "
            "JOIN asociados a ON a.id = e.asociado_id WHERE a.nombre = ?",
            (ASOCIADA,),
        ).fetchone()["id"]
    )

    # Act
    core_pagos.agregar_pago(conn, TABLA, entrega_ana, FORMA_A, 100.0)

    # Assert
    assert _saldo(conn, ASOCIADA) == 200.0
    assert _saldo(conn, "Beto Paz") == 80.0


# --------------------------------------------------------------------------
# T7 -- borrar un abono restituye el saldo (R4)
# --------------------------------------------------------------------------


def test_borrar_pago_entrega_restaura_saldo(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R4: `trg_pago_delete` devuelve al saldo el monto del abono borrado.

    El DELETE es SQL directo a proposito: `core_pagos` no expone borrado todavia,
    y lo que se prueba aqui es el trigger, no una funcion de la aplicacion.
    """
    # Arrange: dos abonos, 220 pagados -> saldo 80
    pago_a = core_pagos.agregar_pago(conn, TABLA, entrega_id, FORMA_A, 120.0)
    core_pagos.agregar_pago(conn, TABLA, entrega_id, FORMA_B, 100.0)
    assert _saldo(conn) == 80.0

    # Act
    with conn:
        conn.execute("DELETE FROM entrega_pagos WHERE id = ?", (pago_a,))

    # Assert: el saldo sube justo los 120 borrados y el total refleja lo que queda
    assert _saldo(conn) == 200.0
    assert core_pagos.total_pagado(conn, TABLA, entrega_id) == 100.0
    assert len(core_pagos.listar_pagos(conn, TABLA, entrega_id)) == 1


def test_borrar_todos_los_pagos_devuelve_el_saldo_original(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R4: insertar y borrar todos los abonos es una operacion neutra."""
    # Arrange
    for monto in (30.0, 45.5):
        core_pagos.agregar_pago(conn, TABLA, entrega_id, FORMA_A, monto)
    assert _saldo(conn) == round(MONTO_QUE_DEBE - 75.5, 2)

    # Act
    with conn:
        conn.execute("DELETE FROM entrega_pagos WHERE entrega_id = ?", (entrega_id,))

    # Assert
    assert _saldo(conn) == MONTO_QUE_DEBE
    assert core_pagos.total_pagado(conn, TABLA, entrega_id) == 0.0


# --------------------------------------------------------------------------
# T9 -- el componente de CLI-03, reusado tal cual sobre `entrega_pagos` (R8)
# --------------------------------------------------------------------------


def test_componente_pago_reusa_entrega_pagos(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R8: las cuatro funciones de CLI-03 sirven a entregas sin codigo nuevo.

    Se comprueba de una pieza el contrato completo del reuso: la whitelist
    resuelve `entrega_id` como columna padre, `listar_pagos` devuelve las claves
    de `CAMPOS_PAGO` ordenadas por id, `total_pagado` suma redondeado y
    `saldo_pendiente` es `round(monto_que_debe - suma, 2)`.
    """
    # Arrange
    abonos = ((FORMA_A, 100.0, "2026-07-01"), (FORMA_B, 33.335, "2026-07-02"),
              (FORMA_A, 66.675, "2026-07-03"))

    # Act
    ids = [
        core_pagos.agregar_pago(conn, TABLA, entrega_id, forma, monto, fecha)
        for forma, monto, fecha in abonos
    ]
    listados = core_pagos.listar_pagos(conn, TABLA, entrega_id)
    total = core_pagos.total_pagado(conn, TABLA, entrega_id)
    saldo = core_pagos.saldo_pendiente(conn, TABLA, entrega_id, MONTO_QUE_DEBE)

    # Assert: la whitelist es la que puso `entrega_id` en el SQL
    assert core_pagos.PAGO_TABLAS[TABLA] == "entrega_id"
    assert [fila["entrega_id"] for fila in conn.execute(
        "SELECT entrega_id FROM entrega_pagos"
    )] == [entrega_id] * len(abonos)

    # Assert: listado ordenado por id con el contrato de claves de CLI-03
    assert [fila["id"] for fila in listados] == sorted(ids)
    assert [tuple(fila) for fila in listados] == [core_pagos.CAMPOS_PAGO] * len(abonos)
    assert [fila["forma_pago"] for fila in listados] == [
        forma for forma, _, _ in abonos
    ]
    assert [fila["fecha"] for fila in listados] == [
        fecha for _, _, fecha in abonos
    ]

    # Assert: totales redondeados a dos decimales y saldo consistente
    assert total == round(sum(monto for _, monto, _ in abonos), 2)
    assert total == 200.01
    assert saldo == round(MONTO_QUE_DEBE - total, 2)
    assert saldo == 99.99


def test_saldo_pendiente_del_componente_coincide_con_la_vista_de_conciliacion(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R8: la cifra del componente cuadra con `vw_saldo_asociados` y el trigger.

    Tres fuentes distintas -- el calculo en Python, la columna que mantienen los
    triggers y la vista que recalcula desde cero -- deben dar el mismo numero. Es
    la deteccion mas barata de la doble contabilidad de RT-3.
    """
    # Arrange
    core_pagos.agregar_pago(conn, TABLA, entrega_id, FORMA_A, 120.75)

    # Act
    del_componente = core_pagos.saldo_pendiente(
        conn, TABLA, entrega_id, MONTO_QUE_DEBE
    )
    de_la_vista = float(
        conn.execute(
            "SELECT saldo_pendiente FROM vw_saldo_asociados WHERE nombre = ?",
            (ASOCIADA,),
        ).fetchone()["saldo_pendiente"]
    )

    # Assert
    assert del_componente == 179.25
    assert de_la_vista == del_componente
    assert _saldo(conn) == del_componente


def test_listar_pagos_de_entrega_sin_abonos_es_vacio(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R8: una entrega recien generada no tiene abonos ni saldo consumido."""
    # Arrange / Act
    listados = core_pagos.listar_pagos(conn, TABLA, entrega_id)

    # Assert
    assert listados == []
    assert core_pagos.total_pagado(conn, TABLA, entrega_id) == 0.0
    assert core_pagos.saldo_pendiente(
        conn, TABLA, entrega_id, MONTO_QUE_DEBE
    ) == MONTO_QUE_DEBE


def test_abonar_a_una_entrega_inexistente_no_mueve_el_saldo(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """R8: la FK rechaza el abono huerfano antes de que el trigger corra."""
    # Arrange
    saldo_antes = _saldo(conn)

    # Act / Assert
    with pytest.raises(core_pagos.PagoError):
        core_pagos.agregar_pago(conn, TABLA, entrega_id + 999, FORMA_A, 50.0)

    assert _saldo(conn) == saldo_antes


def test_listar_entregas_resuelve_folio_producto_y_asociado(
    conn: sqlite3.Connection, entrega_id: int
) -> None:
    """La pestana Entregas lee por aqui: la GUI nunca ejecuta SQL (ADR-2)."""
    # Act
    entregas = listar_entregas(conn)

    # Assert
    assert len(entregas) == 1
    fila = entregas[0]
    assert set(fila) == set(CAMPOS_ENTREGA)
    assert fila["id"] == entrega_id
    assert fila["folio_pedido"]
    assert fila["descripcion"]
    assert fila["asociado"]
    assert fila["status"] == ENTREGA_STATUS_VALIDOS[0]


def test_listar_entregas_base_vacia_lista_vacia(conn: sqlite3.Connection) -> None:
    """Sin entregas generadas, la lista es vacia y no lanza."""
    # Act / Assert
    assert listar_entregas(conn) == []
