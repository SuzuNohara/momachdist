"""Suite del directorio de clientes (`core_clientes`).

La fixture levanta el esquema real con `db.init_db(":memory:")`: la tabla
`clientes`, su `NOT NULL` sobre `nombre`, la FK nullable de `ventas` y la FK
`ON DELETE RESTRICT` de `encargos` son las de produccion. Eso importa sobre todo
para R6: sin `PRAGMA foreign_keys = ON` -- que pone `db.get_conn` -- el DELETE no
levantaria `IntegrityError` y la guarda no seria comprobable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

import core_clientes
import db
from core_comun import CoreError

NOMBRE_A: str = "Ana Lucia Torres"
NOMBRE_B: str = "Beatriz Mendoza"
NOMBRE_Z: str = "Zulema Ibarra"

TELEFONO_A: str = "5512345678"
DIRECCION_A: str = "Av. Reforma 100"
NOTAS_A: str = "Prefiere entregas por la tarde"

BLANCOS: tuple[str, ...] = ("", "   ", "\t", "\n  \n")


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def contar_clientes(conexion: sqlite3.Connection) -> int:
    """Filas actuales en `clientes`."""
    return int(conexion.execute("SELECT COUNT(*) AS n FROM clientes").fetchone()["n"])


def contar_asociados(conexion: sqlite3.Connection) -> int:
    """Filas actuales en `asociados`."""
    return int(conexion.execute("SELECT COUNT(*) AS n FROM asociados").fetchone()["n"])


def fila_cliente(conexion: sqlite3.Connection, cliente_id: int) -> sqlite3.Row | None:
    """Fila cruda de un cliente, o `None` si ya no existe."""
    return conexion.execute(
        "SELECT * FROM clientes WHERE id = ?", (cliente_id,)
    ).fetchone()


def alta_venta(conexion: sqlite3.Connection, cliente_id: int | None) -> None:
    """Registra una venta apuntando (o no) a un cliente."""
    conexion.execute("INSERT INTO ventas (cliente_id) VALUES (?)", (cliente_id,))
    conexion.commit()


def alta_encargo(conexion: sqlite3.Connection, cliente_id: int) -> None:
    """Registra un encargo ligado a un cliente (FK ON DELETE RESTRICT)."""
    conexion.execute("INSERT INTO encargos (cliente_id) VALUES (?)", (cliente_id,))
    conexion.commit()


# --------------------------------------------------------------------------
# T1 -- error de dominio y aislamiento del modulo (R7)
# --------------------------------------------------------------------------


def test_clienteerror_is_exception() -> None:
    """R7: `ClienteError` es una excepcion de dominio, no un tipo suelto."""
    # Arrange / Act
    error = core_clientes.ClienteError("fallo")

    # Assert
    assert isinstance(error, Exception)
    assert isinstance(error, CoreError)
    assert issubclass(core_clientes.ClienteError, CoreError)


def test_clienteerror_conserva_el_mensaje() -> None:
    """El mensaje de dominio llega intacto a la GUI."""
    # Arrange / Act
    error = core_clientes.ClienteError("mensaje claro")

    # Assert
    assert str(error) == "mensaje claro"


def test_crear_cliente_does_not_touch_asociados(conn: sqlite3.Connection) -> None:
    """Clientes y asociados son directorios distintos: no se contaminan."""
    # Arrange
    assert contar_asociados(conn) == 0

    # Act
    core_clientes.crear_cliente(conn, NOMBRE_A)
    core_clientes.crear_cliente(conn, NOMBRE_B)

    # Assert
    assert contar_clientes(conn) == 2
    assert contar_asociados(conn) == 0


# --------------------------------------------------------------------------
# T2 -- guarda de nombre (R3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blanco", BLANCOS)
def test_validar_nombre_rejects_blank(blanco: str) -> None:
    """R3: un nombre en blanco nunca llega al INSERT."""
    # Arrange / Act / Assert
    with pytest.raises(core_clientes.ClienteError):
        core_clientes._validar_nombre(blanco)


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("  Ana Lucia Torres  ", "Ana Lucia Torres"),
        ("Ana Lucia Torres", "Ana Lucia Torres"),
        ("\tAna Lucia Torres\n", "Ana Lucia Torres"),
        (" A ", "A"),
    ],
)
def test_validar_nombre_strips(crudo: str, esperado: str) -> None:
    """R3: el nombre se guarda recortado, sin espacios de sobra."""
    # Arrange / Act
    resultado = core_clientes._validar_nombre(crudo)

    # Assert
    assert resultado == esperado


# --------------------------------------------------------------------------
# T3 -- alta (R2, R3)
# --------------------------------------------------------------------------


def test_crear_cliente_inserts_and_returns_id(conn: sqlite3.Connection) -> None:
    """R2: el alta persiste la fila y devuelve el id generado."""
    # Arrange
    assert contar_clientes(conn) == 0

    # Act
    cliente_id = core_clientes.crear_cliente(
        conn,
        NOMBRE_A,
        telefono=TELEFONO_A,
        direccion=DIRECCION_A,
        notas=NOTAS_A,
    )

    # Assert
    assert isinstance(cliente_id, int)
    assert cliente_id > 0
    assert contar_clientes(conn) == 1
    fila = fila_cliente(conn, cliente_id)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_A
    assert fila["telefono"] == TELEFONO_A
    assert fila["direccion"] == DIRECCION_A
    assert fila["notas"] == NOTAS_A


def test_crear_cliente_guarda_el_nombre_recortado(conn: sqlite3.Connection) -> None:
    """R3: el alta normaliza el nombre antes de escribirlo."""
    # Arrange / Act
    cliente_id = core_clientes.crear_cliente(conn, f"   {NOMBRE_A}  ")

    # Assert
    fila = fila_cliente(conn, cliente_id)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_A


def test_crear_cliente_sin_opcionales_deja_null(conn: sqlite3.Connection) -> None:
    """R2: telefono, direccion y notas son opcionales y quedan en NULL."""
    # Arrange / Act
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A)

    # Assert
    fila = fila_cliente(conn, cliente_id)
    assert fila is not None
    assert fila["telefono"] is None
    assert fila["direccion"] is None
    assert fila["notas"] is None
    assert fila["fecha_alta"] is not None


def test_crear_cliente_devuelve_ids_distintos(conn: sqlite3.Connection) -> None:
    """R2: cada alta es un cliente nuevo, aunque el nombre se repita."""
    # Arrange / Act
    primero = core_clientes.crear_cliente(conn, NOMBRE_A)
    segundo = core_clientes.crear_cliente(conn, NOMBRE_A)

    # Assert
    assert primero != segundo
    assert contar_clientes(conn) == 2


@pytest.mark.parametrize("blanco", BLANCOS)
def test_crear_cliente_blank_name_raises(
    conn: sqlite3.Connection, blanco: str
) -> None:
    """R3: sin nombre no hay cliente, ni fila fantasma en la tabla."""
    # Arrange / Act / Assert
    with pytest.raises(core_clientes.ClienteError):
        core_clientes.crear_cliente(conn, blanco)
    assert contar_clientes(conn) == 0


# --------------------------------------------------------------------------
# T4 -- lectura (R1)
# --------------------------------------------------------------------------


def test_listar_clientes_returns_all_ordered(conn: sqlite3.Connection) -> None:
    """R1: la lista sale completa y ordenada por nombre."""
    # Arrange
    core_clientes.crear_cliente(conn, NOMBRE_Z, telefono=TELEFONO_A)
    core_clientes.crear_cliente(conn, NOMBRE_A)
    core_clientes.crear_cliente(conn, NOMBRE_B)

    # Act
    clientes = core_clientes.listar_clientes(conn)

    # Assert
    assert [cliente["nombre"] for cliente in clientes] == [
        NOMBRE_A,
        NOMBRE_B,
        NOMBRE_Z,
    ]
    assert set(clientes[0]) == set(core_clientes.CAMPOS_CLIENTE)
    assert clientes[2]["telefono"] == TELEFONO_A


def test_listar_clientes_expone_las_claves_del_contrato(
    conn: sqlite3.Connection,
) -> None:
    """R1: la GUI depende de estas seis claves exactas."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A, notas=NOTAS_A)

    # Act
    clientes = core_clientes.listar_clientes(conn)

    # Assert
    assert len(clientes) == 1
    assert set(clientes[0]) == {
        "id",
        "nombre",
        "telefono",
        "direccion",
        "notas",
        "fecha_alta",
    }
    assert clientes[0]["id"] == cliente_id
    assert clientes[0]["notas"] == NOTAS_A


def test_listar_clientes_empty(conn: sqlite3.Connection) -> None:
    """R1: sin clientes la lista es vacia, no `None`."""
    # Arrange / Act
    clientes = core_clientes.listar_clientes(conn)

    # Assert
    assert clientes == []


# --------------------------------------------------------------------------
# T5 -- edicion (R4, R3)
# --------------------------------------------------------------------------


def test_editar_cliente_updates_fields(conn: sqlite3.Connection) -> None:
    """R4: la edicion reescribe los cuatro campos del formulario."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(
        conn, NOMBRE_A, telefono=TELEFONO_A, direccion=DIRECCION_A, notas=NOTAS_A
    )

    # Act
    core_clientes.editar_cliente(
        conn,
        cliente_id,
        nombre=NOMBRE_B,
        telefono="5599998888",
        direccion="Calle Nueva 7",
        notas="Cambio de domicilio",
    )

    # Assert
    fila = fila_cliente(conn, cliente_id)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_B
    assert fila["telefono"] == "5599998888"
    assert fila["direccion"] == "Calle Nueva 7"
    assert fila["notas"] == "Cambio de domicilio"


def test_editar_cliente_limpia_los_opcionales_omitidos(
    conn: sqlite3.Connection,
) -> None:
    """R4: el formulario viaja completo, un campo omitido se borra."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(
        conn, NOMBRE_A, telefono=TELEFONO_A, direccion=DIRECCION_A, notas=NOTAS_A
    )

    # Act
    core_clientes.editar_cliente(conn, cliente_id, nombre=NOMBRE_A)

    # Assert
    fila = fila_cliente(conn, cliente_id)
    assert fila is not None
    assert fila["telefono"] is None
    assert fila["direccion"] is None
    assert fila["notas"] is None


def test_editar_cliente_no_afecta_a_los_demas(conn: sqlite3.Connection) -> None:
    """R4: el UPDATE es parametrizado y toca una sola fila."""
    # Arrange
    objetivo = core_clientes.crear_cliente(conn, NOMBRE_A)
    intacto = core_clientes.crear_cliente(conn, NOMBRE_B, telefono=TELEFONO_A)

    # Act
    core_clientes.editar_cliente(conn, objetivo, nombre=NOMBRE_Z)

    # Assert
    fila = fila_cliente(conn, intacto)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_B
    assert fila["telefono"] == TELEFONO_A


@pytest.mark.parametrize("blanco", BLANCOS)
def test_editar_cliente_blank_name_raises(
    conn: sqlite3.Connection, blanco: str
) -> None:
    """R3: la guarda de nombre vale igual en el alta que en la edicion."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A)

    # Act / Assert
    with pytest.raises(core_clientes.ClienteError):
        core_clientes.editar_cliente(conn, cliente_id, nombre=blanco)
    fila = fila_cliente(conn, cliente_id)
    assert fila is not None
    assert fila["nombre"] == NOMBRE_A


# --------------------------------------------------------------------------
# T6 -- baja sin movimientos (R5)
# --------------------------------------------------------------------------


def test_eliminar_cliente_without_movimientos_deletes(
    conn: sqlite3.Connection,
) -> None:
    """R5: un cliente sin ventas ni encargos se borra sin ruido."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A)

    # Act
    core_clientes.eliminar_cliente(conn, cliente_id)

    # Assert
    assert fila_cliente(conn, cliente_id) is None
    assert contar_clientes(conn) == 0


def test_eliminar_cliente_no_toca_a_los_demas(conn: sqlite3.Connection) -> None:
    """R5: el DELETE parametrizado borra exactamente un cliente."""
    # Arrange
    objetivo = core_clientes.crear_cliente(conn, NOMBRE_A)
    superviviente = core_clientes.crear_cliente(conn, NOMBRE_B)

    # Act
    core_clientes.eliminar_cliente(conn, objetivo)

    # Assert
    assert fila_cliente(conn, superviviente) is not None
    assert contar_clientes(conn) == 1


def test_eliminar_cliente_con_venta_de_mostrador_si_borra(
    conn: sqlite3.Connection,
) -> None:
    """Una venta con `cliente_id` en NULL no apunta a nadie: no bloquea."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A)
    alta_venta(conn, None)

    # Act
    core_clientes.eliminar_cliente(conn, cliente_id)

    # Assert
    assert contar_clientes(conn) == 0


# --------------------------------------------------------------------------
# T7 -- baja bloqueada por movimientos (R6)
# --------------------------------------------------------------------------


def test_eliminar_cliente_with_ventas_raises_clienteerror(
    conn: sqlite3.Connection,
) -> None:
    """R6: la FK de ventas se traduce a `ClienteError`, no a `IntegrityError`."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A)
    alta_venta(conn, cliente_id)

    # Act / Assert
    with pytest.raises(core_clientes.ClienteError) as excinfo:
        core_clientes.eliminar_cliente(conn, cliente_id)
    assert "ventas o encargos" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert fila_cliente(conn, cliente_id) is not None


def test_eliminar_cliente_con_encargos_raises_clienteerror(
    conn: sqlite3.Connection,
) -> None:
    """R6: el RESTRICT de encargos (ADR-5) produce el mismo mensaje."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A)
    alta_encargo(conn, cliente_id)

    # Act / Assert
    with pytest.raises(core_clientes.ClienteError):
        core_clientes.eliminar_cliente(conn, cliente_id)
    assert fila_cliente(conn, cliente_id) is not None


def test_eliminar_cliente_bloqueado_deja_la_venta_intacta(
    conn: sqlite3.Connection,
) -> None:
    """R6: la baja rechazada no destruye el historial que la bloqueo."""
    # Arrange
    cliente_id = core_clientes.crear_cliente(conn, NOMBRE_A)
    alta_venta(conn, cliente_id)

    # Act / Assert
    with pytest.raises(core_clientes.ClienteError):
        core_clientes.eliminar_cliente(conn, cliente_id)
    ventas = conn.execute(
        "SELECT COUNT(*) AS n FROM ventas WHERE cliente_id = ?", (cliente_id,)
    ).fetchone()
    assert int(ventas["n"]) == 1


def test_editar_cliente_inexistente_error(conn: sqlite3.Connection) -> None:
    """Un id que no existe no se edita en silencio: se reporta como error.

    Criterio unificado con `core_asociados.editar_asociado`, para que la GUI no
    muestre exito de una edicion que no toco ninguna fila.
    """
    # Act / Assert
    with pytest.raises(core_clientes.ClienteError):
        core_clientes.editar_cliente(conn, 9999, nombre=NOMBRE_A)


def test_eliminar_cliente_inexistente_error(conn: sqlite3.Connection) -> None:
    """Una baja sobre un id inexistente se reporta, no se ignora."""
    # Act / Assert
    with pytest.raises(core_clientes.ClienteError):
        core_clientes.eliminar_cliente(conn, 9999)
