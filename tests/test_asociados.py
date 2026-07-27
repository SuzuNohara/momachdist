"""Suite del CRUD manual de asociados (MERC-07, R1-R8).

La fixture levanta el esquema real con `db.init_db(db.get_conn(":memory:"))`:
el CHECK de `status`, las dos FKs que protegen el borrado y los triggers que
mantienen `saldo_pendiente` son los de produccion. Eso importa especialmente
aqui: las guardas de R6 dependen de `PRAGMA foreign_keys = ON`, que fija
`db.get_conn`. R8 se verifica contra `pdf_extractor.link_whatsapp` (FUND-03).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Final

import pytest

import core_asociados
import db
import pdf_extractor

CODIGO_ARTICULO: Final[str] = "11111"
TIPO_NORMAL: Final[str] = "Normal (con descuento)"

NOMBRE_A: Final[str] = "Aura Jannet Ramirez"
NOMBRE_B: Final[str] = "Etnan Gamaliel Perez"

INSERT_PRODUCTO_SQL: Final[str] = (
    "INSERT OR IGNORE INTO productos (codigo_articulo, descripcion) VALUES (?, ?)"
)
INSERT_PEDIDO_SQL: Final[str] = "INSERT INTO pedidos (folio_pedido) VALUES (?)"
INSERT_DETALLE_SQL: Final[str] = (
    "INSERT INTO pedido_detalle (pedido_id, codigo_articulo, cantidad_solicitada,"
    " cantidad_surtida, cantidad_asociado, asociado_id, tipo)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)
INSERT_ENTREGA_SQL: Final[str] = (
    "INSERT INTO entregas_asociado (pedido_detalle_id, asociado_id,"
    " cantidad_entregada, monto_que_debe) VALUES (?, ?, ?, ?)"
)


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico y las FKs activas."""
    conexion = db.init_db(db.get_conn(":memory:"))
    try:
        yield conexion
    finally:
        conexion.close()


def crear_detalle(
    conn: sqlite3.Connection,
    asociado_id: int | None,
    folio: str,
    cantidad: int = 2,
) -> int:
    """Inserta producto + pedido + detalle y devuelve el `pedido_detalle.id`.

    `asociado_id` puede ir en `None` para aislar la FK de `entregas_asociado`
    de la de `pedido_detalle` al probar el borrado protegido.
    """
    conn.execute(INSERT_PRODUCTO_SQL, (CODIGO_ARTICULO, "Sarten antiadherente"))
    pedido = conn.execute(INSERT_PEDIDO_SQL, (folio,))
    fila = (pedido.lastrowid, CODIGO_ARTICULO, cantidad, cantidad, cantidad)
    detalle = conn.execute(INSERT_DETALLE_SQL, (*fila, asociado_id, TIPO_NORMAL))
    conn.commit()
    return int(detalle.lastrowid or 0)


def crear_entrega(
    conn: sqlite3.Connection,
    asociado_id: int,
    folio: str,
    monto: float = 250.0,
    cantidad: int = 2,
) -> int:
    """Inserta una entrega real (dispara `trg_entrega_insert` sobre el saldo)."""
    detalle_id = crear_detalle(conn, None, folio, cantidad)
    entrega = conn.execute(
        INSERT_ENTREGA_SQL, (detalle_id, asociado_id, cantidad, monto)
    )
    conn.commit()
    return int(entrega.lastrowid or 0)


def leer_fila(conn: sqlite3.Connection, asociado_id: int) -> sqlite3.Row | None:
    """Fila cruda de `asociados`, para afirmar contra la BD y no contra el API."""
    return conn.execute(
        "SELECT * FROM asociados WHERE id = ?", (asociado_id,)
    ).fetchone()


# --- T1: listado con saldo (R1) -------------------------------------------


def test_listar_asociados_vacio_lista_vacia(conn: sqlite3.Connection) -> None:
    """R1: un directorio recien creado lista vacio, no None ni error."""
    # Arrange / Act
    resultado = core_asociados.listar_asociados(conn)

    # Assert
    assert resultado == []


def test_listar_asociados_incluye_saldo(conn: sqlite3.Connection) -> None:
    """R1: el saldo que dejo el trigger de la entrega viaja en el listado."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A, telefono="5512345678")
    crear_entrega(conn, asociado_id, "C001264", monto=250.0)

    # Act
    filas = core_asociados.listar_asociados(conn)

    # Assert
    assert len(filas) == 1
    assert filas[0] == {
        "id": asociado_id,
        "nombre": NOMBRE_A,
        "telefono": "5512345678",
        "notas": "",
        "status": "Activo",
        "saldo_pendiente": 250.0,
    }


def test_listar_asociados_ordena_por_nombre(conn: sqlite3.Connection) -> None:
    """R1: el orden lo fija el SQL, no el orden de alta."""
    # Arrange
    core_asociados.crear_asociado(conn, NOMBRE_B)
    core_asociados.crear_asociado(conn, NOMBRE_A)

    # Act
    nombres = [fila["nombre"] for fila in core_asociados.listar_asociados(conn)]

    # Assert
    assert nombres == [NOMBRE_A, NOMBRE_B]


# --- T2: alta manual (R2, R3, R4) -----------------------------------------


def test_crear_asociado_devuelve_id(conn: sqlite3.Connection) -> None:
    """R2: el alta devuelve un id usable para releer la fila."""
    # Arrange / Act
    asociado_id = core_asociados.crear_asociado(
        conn, NOMBRE_A, telefono="5512345678", notas="Ruta centro"
    )

    # Assert
    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_A
    assert fila["telefono"] == "5512345678"
    assert fila["notas"] == "Ruta centro"


@pytest.mark.parametrize("blanco", ["", "   ", "\t", "\n  \n"])
def test_crear_asociado_nombre_vacio_error(
    conn: sqlite3.Connection, blanco: str
) -> None:
    """R3: sin nombre no hay alta, y tampoco fila fantasma en la tabla."""
    # Arrange / Act / Assert
    with pytest.raises(core_asociados.AsociadoError):
        core_asociados.crear_asociado(conn, blanco)

    assert core_asociados.listar_asociados(conn) == []


def test_crear_asociado_status_default_activo(conn: sqlite3.Connection) -> None:
    """R4: sin status explicito el asociado nace Activo y con saldo en cero."""
    # Arrange / Act
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)

    # Assert
    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["status"] == "Activo"
    assert fila["saldo_pendiente"] == 0.0


@pytest.mark.parametrize("invalido", ["Suspendido", "activo", "", "ACTIVO"])
def test_crear_asociado_status_invalido_error(
    conn: sqlite3.Connection, invalido: str
) -> None:
    """R4: el CHECK del esquema se traduce a error de dominio, no a sqlite3."""
    # Arrange / Act / Assert
    with pytest.raises(core_asociados.AsociadoError):
        core_asociados.crear_asociado(conn, NOMBRE_A, status=invalido)

    assert core_asociados.listar_asociados(conn) == []


def test_crear_asociado_normaliza_el_nombre(conn: sqlite3.Connection) -> None:
    """El alta manual usa la misma normalizacion que la carga de remisiones."""
    # Arrange / Act
    asociado_id = core_asociados.crear_asociado(conn, "  Aura   Jannet  ")

    # Assert
    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["nombre"] == "Aura Jannet"


# --- T3: edicion parcial (R4, R5) -----------------------------------------


def test_editar_asociado_actualiza_campos(conn: sqlite3.Connection) -> None:
    """R5: los cuatro campos editables se persisten en una sola llamada."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)

    # Act
    core_asociados.editar_asociado(
        conn,
        asociado_id,
        nombre=NOMBRE_B,
        telefono="5599887766",
        notas="Cambio de ruta",
        status="Inactivo",
    )

    # Assert
    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_B
    assert fila["telefono"] == "5599887766"
    assert fila["notas"] == "Cambio de ruta"
    assert fila["status"] == "Inactivo"


def test_editar_asociado_inexistente_error(conn: sqlite3.Connection) -> None:
    """R5: un id que no existe es error de dominio, no un UPDATE silencioso."""
    # Arrange / Act / Assert
    with pytest.raises(core_asociados.AsociadoError):
        core_asociados.editar_asociado(conn, 9999, nombre=NOMBRE_A)


def test_editar_asociado_solo_toca_campos_provistos(
    conn: sqlite3.Connection,
) -> None:
    """R5: `None` significa 'no tocar'; el resto de la fila queda intacto."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(
        conn, NOMBRE_A, telefono="5512345678", notas="Ruta centro"
    )

    # Act
    core_asociados.editar_asociado(conn, asociado_id, status="Inactivo")

    # Assert
    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["status"] == "Inactivo"
    assert fila["nombre"] == NOMBRE_A
    assert fila["telefono"] == "5512345678"
    assert fila["notas"] == "Ruta centro"


@pytest.mark.parametrize("blanco", ["", "   ", "\t"])
def test_editar_asociado_nombre_vacio_error(
    conn: sqlite3.Connection, blanco: str
) -> None:
    """R3 en la edicion: no se puede dejar la fila sin nombre."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)

    # Act / Assert
    with pytest.raises(core_asociados.AsociadoError):
        core_asociados.editar_asociado(conn, asociado_id, nombre=blanco)

    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_A


def test_editar_asociado_status_invalido_error(conn: sqlite3.Connection) -> None:
    """R4 en la edicion: el CHECK tambien se envuelve, y no deja rastro."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)

    # Act / Assert
    with pytest.raises(core_asociados.AsociadoError):
        core_asociados.editar_asociado(conn, asociado_id, status="Suspendido")

    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["status"] == "Activo"


def test_editar_asociado_no_altera_saldo_pendiente(
    conn: sqlite3.Connection,
) -> None:
    """ADR-3: el saldo es territorio de los triggers, la edicion no lo pisa."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)
    crear_entrega(conn, asociado_id, "C001264", monto=430.5)

    # Act
    core_asociados.editar_asociado(conn, asociado_id, notas="Recordar cobro")

    # Assert
    fila = leer_fila(conn, asociado_id)
    assert fila is not None
    assert fila["saldo_pendiente"] == 430.5


# --- T4: baja protegida por las FKs (R6, R7) ------------------------------


def test_eliminar_asociado_sin_entregas(conn: sqlite3.Connection) -> None:
    """R6: un asociado sin movimientos se borra y desaparece del listado."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)

    # Act
    core_asociados.eliminar_asociado(conn, asociado_id)

    # Assert
    assert leer_fila(conn, asociado_id) is None
    assert core_asociados.listar_asociados(conn) == []


def test_eliminar_asociado_con_entregas_rechazado(
    conn: sqlite3.Connection,
) -> None:
    """R6: la FK RESTRICT de `entregas_asociado` bloquea y avisa a la GUI."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)
    crear_entrega(conn, asociado_id, "C001264")

    # Act / Assert
    with pytest.raises(core_asociados.AsociadoError, match="entregas ligadas"):
        core_asociados.eliminar_asociado(conn, asociado_id)

    assert leer_fila(conn, asociado_id) is not None


def test_eliminar_asociado_con_detalle_ligado_rechazado(
    conn: sqlite3.Connection,
) -> None:
    """R6: la FK NO ACTION de `pedido_detalle` bloquea igual que la RESTRICT."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A)
    crear_detalle(conn, asociado_id, "C001265")

    # Act / Assert
    with pytest.raises(core_asociados.AsociadoError, match="entregas ligadas"):
        core_asociados.eliminar_asociado(conn, asociado_id)

    assert leer_fila(conn, asociado_id) is not None


def test_eliminar_asociado_inexistente_error(conn: sqlite3.Connection) -> None:
    """R7: borrar un id que no existe es error, no un no-op silencioso."""
    # Arrange / Act / Assert
    with pytest.raises(core_asociados.AsociadoError):
        core_asociados.eliminar_asociado(conn, 9999)


# --- T6: roundtrip y link de WhatsApp (R8) --------------------------------


def test_crud_asociado_roundtrip_completo(conn: sqlite3.Connection) -> None:
    """Alta -> listado -> edicion -> baja sobre la misma fila, de punta a punta."""
    # Arrange
    asociado_id = core_asociados.crear_asociado(conn, NOMBRE_A, telefono="5512345678")

    # Act
    core_asociados.editar_asociado(conn, asociado_id, status="Inactivo")
    tras_editar = core_asociados.listar_asociados(conn)
    core_asociados.eliminar_asociado(conn, asociado_id)

    # Assert
    assert [fila["status"] for fila in tras_editar] == ["Inactivo"]
    assert core_asociados.listar_asociados(conn) == []


def test_link_whatsapp_precargado() -> None:
    """R8: numero local de 10 digitos + mensaje -> link wa.me listo para abrir."""
    # Arrange / Act
    link = pdf_extractor.link_whatsapp("5512345678", "Hola")

    # Assert
    assert link == "https://wa.me/525512345678?text=Hola"


@pytest.mark.parametrize("sin_digitos", [None, "", "   ", "sin telefono"])
def test_link_whatsapp_sin_digitos_devuelve_none(sin_digitos: str | None) -> None:
    """R8: sin numero util el link es `None`, para que la GUI avise en vez de abrirlo."""
    # Arrange / Act
    link = pdf_extractor.link_whatsapp(sin_digitos, "Hola")

    # Assert
    assert link is None
