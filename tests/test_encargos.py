"""Suite del CRUD de encargos (`core_encargos`, ENC-02).

La fixture levanta el esquema real con `db.init_db(":memory:")`, de modo que el
`CHECK` de `status`, el `CHECK (cantidad_solicitada > 0)`, la FK
`clientes ON DELETE RESTRICT` y la FK `productos ON DELETE RESTRICT` son las de
produccion. Sin `PRAGMA foreign_keys = ON` --que pone `db.get_conn`-- R4 y R10
no serian comprobables: las FK rotas no levantarian `IntegrityError`.

Tres cosas se prueban con insistencia porque son las que rompen si alguien
"simplifica" el modulo mas tarde:

* **La atomicidad** (R10 y R7): una linea mala no puede dejar cabecera huerfana,
  y una edicion fallida no puede dejar el detalle a medio reemplazar --el
  `DELETE` previo tiene que revertirse con todo lo demas.
* **La ausencia de N+1** (R5): `listar_encargos` se observa con
  `set_trace_callback` y debe ejecutar **exactamente una** sentencia, por muchos
  encargos, lineas y anticipos que haya.
* **El reuso del componente de pagos** (R9): el anticipo entra por
  `core_pagos.agregar_pago` sobre `encargo_pagos`, y `total_anticipado` tiene
  que coincidir al centavo con `core_pagos.total_pagado` en las dos vistas.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

import core_encargos
import core_pagos
import db
from core_comun import CoreError

CODIGO_A: str = "ART-001"
CODIGO_B: str = "ART-002"
CODIGO_FANTASMA: str = "NO-EXISTE"

NOMBRE_A: str = "Ana Lucia Torres"
NOMBRE_B: str = "Beatriz Mendoza"

FORMA_A: str = "Efectivo"
FORMA_B: str = "Transferencia"

OBSERVACIONES: str = "Lo necesita antes del viernes"

#: Cantidades que el `CHECK (cantidad_solicitada > 0)` y R3 rechazan.
CANTIDADES_INVALIDAS: tuple[Any, ...] = (0, -1, 1.5, "abc", "", None, True, [1])

#: Precios que R3 rechaza; el cero **si** es valido (aun no se sabe el precio).
PRECIOS_INVALIDOS: tuple[Any, ...] = (-0.01, -5, "abc", None, True, {})

#: Codigos en blanco: la linea no identifica ningun articulo.
CODIGOS_VACIOS: tuple[Any, ...] = ("", "   ", "\t", None)

#: `cliente_id` que R4 rechaza antes de tocar la base.
CLIENTES_INVALIDOS: tuple[Any, ...] = (None, 0, -1, "", "abc", 1.5, True)

#: Estados desde los que editar y cancelar deben rebotar (R7, R8).
STATUS_NO_PENDIENTE: tuple[str, ...] = ("Surtido", "Entregado", "Cancelado")


class ContadorSQL:
    """Registra cada sentencia que SQLite ejecuta, para delatar un N+1."""

    def __init__(self) -> None:
        self.sentencias: list[str] = []

    def __call__(self, sql: str) -> None:
        """`set_trace_callback` llama a esto una vez por sentencia."""
        self.sentencias.append(sql)


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico y el catalogo sembrado."""
    conexion = db.init_db(":memory:")
    alta_producto(conexion, CODIGO_A, "Organizador multiusos")
    alta_producto(conexion, CODIGO_B, "Tapete antiderrapante")
    try:
        yield conexion
    finally:
        conexion.close()


@pytest.fixture()
def cliente(conn: sqlite3.Connection) -> int:
    """Cliente por defecto de las pruebas."""
    return alta_cliente(conn, NOMBRE_A)


def alta_producto(conexion: sqlite3.Connection, codigo: str, descripcion: str) -> None:
    """Siembra un articulo del catalogo (destino de la FK del detalle)."""
    conexion.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        (codigo, descripcion),
    )
    conexion.commit()


def alta_cliente(conexion: sqlite3.Connection, nombre: str) -> int:
    """Da de alta un cliente y devuelve su id."""
    cursor = conexion.execute("INSERT INTO clientes (nombre) VALUES (?)", (nombre,))
    conexion.commit()
    return int(cursor.lastrowid or 0)


def linea(codigo: str, cantidad: Any = 1, precio: Any = 10.0) -> dict[str, Any]:
    """Construye una linea de encargo con el contrato de `CAMPOS_LINEA`."""
    return {
        "codigo_articulo": codigo,
        "cantidad_solicitada": cantidad,
        "precio_estimado": precio,
    }


def contar(conexion: sqlite3.Connection, tabla: str) -> int:
    """Filas de una de las tres tablas de encargos (nombre literal del test)."""
    sentencias = {
        "encargos": "SELECT COUNT(*) AS n FROM encargos",
        "encargo_detalle": "SELECT COUNT(*) AS n FROM encargo_detalle",
        "encargo_pagos": "SELECT COUNT(*) AS n FROM encargo_pagos",
    }
    return int(conexion.execute(sentencias[tabla]).fetchone()["n"])


def status_de(conexion: sqlite3.Connection, encargo_id: int) -> str:
    """Status crudo del encargo, leido sin pasar por el modulo bajo prueba."""
    return str(
        conexion.execute(
            "SELECT status FROM encargos WHERE id = ?", (encargo_id,)
        ).fetchone()["status"]
    )


def fijar_status(conexion: sqlite3.Connection, encargo_id: int, status: str) -> None:
    """Mueve el status a mano: simula lo que hara ENC-03 al surtir o entregar."""
    conexion.execute("UPDATE encargos SET status = ? WHERE id = ?", (status, encargo_id))
    conexion.commit()


def fijar_fecha(conexion: sqlite3.Connection, encargo_id: int, fecha: str) -> None:
    """Fija `fecha_encargo` para poder comprobar el orden de R5."""
    conexion.execute(
        "UPDATE encargos SET fecha_encargo = ? WHERE id = ?", (fecha, encargo_id)
    )
    conexion.commit()


# --------------------------------------------------------------------------
# T1 -- error de dominio y contrato con el componente de pagos (D3, R9)
# --------------------------------------------------------------------------


def test_encargoerror_hereda_de_coreerror() -> None:
    """D3: la base real de la capa core es `CoreError`, no `DomainError`."""
    # Arrange / Act
    error = core_encargos.EncargoError("fallo")

    # Assert
    assert issubclass(core_encargos.EncargoError, CoreError)
    assert isinstance(error, CoreError)
    assert str(error) == "fallo"


def test_tabla_pagos_esta_en_la_whitelist_de_core_pagos() -> None:
    """R9: el anticipo se delega a `core_pagos`, que ya conoce `encargo_pagos`."""
    # Arrange / Act / Assert
    assert core_encargos.TABLA_PAGOS == "encargo_pagos"
    assert core_encargos.TABLA_PAGOS in core_pagos.PAGO_TABLAS


def test_status_validos_espeja_el_check_del_ddl() -> None:
    """ADR-5: los cuatro estados del `CHECK` y ninguno mas."""
    # Arrange / Act / Assert
    assert core_encargos.STATUS_VALIDOS == {
        "Pendiente", "Surtido", "Entregado", "Cancelado",
    }


# --------------------------------------------------------------------------
# T2 -- alta de encargo (R1)
# --------------------------------------------------------------------------


def test_crear_encargo_persiste_cabecera_y_detalle(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R1: una cabecera `Pendiente` y una fila de detalle por linea."""
    # Arrange
    lineas = [linea(CODIGO_A, 2, 150.0), linea(CODIGO_B, 1, 99.5)]

    # Act
    encargo_id = core_encargos.crear_encargo(conn, cliente, lineas, OBSERVACIONES)

    # Assert
    cabecera = conn.execute(
        "SELECT * FROM encargos WHERE id = ?", (encargo_id,)
    ).fetchone()
    assert isinstance(encargo_id, int) and encargo_id > 0
    assert cabecera["cliente_id"] == cliente
    assert cabecera["status"] == core_encargos.STATUS_PENDIENTE
    assert cabecera["observaciones"] == OBSERVACIONES
    assert cabecera["fecha_encargo"] is not None
    assert cabecera["venta_id"] is None
    assert contar(conn, "encargo_detalle") == 2


def test_crear_encargo_guarda_los_valores_de_cada_linea(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R1: codigo, cantidad y precio estimado llegan intactos al detalle."""
    # Arrange / Act
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 3, 25.5)])

    # Assert
    fila = conn.execute(
        "SELECT * FROM encargo_detalle WHERE encargo_id = ?", (encargo_id,)
    ).fetchone()
    assert fila["codigo_articulo"] == CODIGO_A
    assert fila["cantidad_solicitada"] == 3
    assert fila["precio_estimado"] == pytest.approx(25.5)


def test_crear_encargo_devuelve_ids_distintos(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R1: cada alta es un encargo nuevo, aunque repita cliente y lineas."""
    # Arrange / Act
    primero = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])
    segundo = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])

    # Assert
    assert primero != segundo
    assert contar(conn, "encargos") == 2


def test_crear_encargo_sin_observaciones_guarda_texto_vacio(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R1: las observaciones son opcionales y nunca quedan en `None`."""
    # Arrange / Act
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])

    # Assert
    assert core_encargos.obtener_encargo(conn, encargo_id)["observaciones"] == ""


# --------------------------------------------------------------------------
# T3 -- peticion sin lineas (R2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lineas", [[], (), None])
def test_crear_encargo_sin_lineas_no_escribe(
    conn: sqlite3.Connection, cliente: int, lineas: Any
) -> None:
    """R2: un encargo sin articulos se rechaza sin tocar la base."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.crear_encargo(conn, cliente, lineas)
    assert contar(conn, "encargos") == 0


# --------------------------------------------------------------------------
# T4 -- lineas invalidas y rechazo atomico (R3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cantidad", CANTIDADES_INVALIDAS)
def test_crear_encargo_cantidad_invalida_no_escribe(
    conn: sqlite3.Connection, cliente: int, cantidad: Any
) -> None:
    """R3: la cantidad debe ser un entero estrictamente mayor que cero."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, cantidad)])
    assert contar(conn, "encargos") == 0


@pytest.mark.parametrize("precio", PRECIOS_INVALIDOS)
def test_crear_encargo_precio_invalido_no_escribe(
    conn: sqlite3.Connection, cliente: int, precio: Any
) -> None:
    """R3: el precio estimado no puede ser negativo ni no numerico."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 1, precio)])
    assert contar(conn, "encargos") == 0


@pytest.mark.parametrize("codigo", CODIGOS_VACIOS)
def test_crear_encargo_codigo_vacio_no_escribe(
    conn: sqlite3.Connection, cliente: int, codigo: Any
) -> None:
    """R3: una linea sin codigo de articulo no identifica nada."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.crear_encargo(conn, cliente, [linea(codigo)])
    assert contar(conn, "encargos") == 0


@pytest.mark.parametrize("linea_rara", ["texto", 42, None, ["ART-001", 1]])
def test_crear_encargo_linea_no_diccionario_no_escribe(
    conn: sqlite3.Connection, cliente: int, linea_rara: Any
) -> None:
    """R3: cada linea tiene que ser un diccionario con el contrato acordado."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.crear_encargo(conn, cliente, [linea_rara])
    assert contar(conn, "encargos") == 0


def test_crear_encargo_una_linea_mala_tumba_la_peticion_completa(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R3: el rechazo es de la peticion entera, no solo de la linea culpable."""
    # Arrange
    lineas = [linea(CODIGO_A, 2, 10.0), linea(CODIGO_B, 0, 10.0), linea(CODIGO_A)]

    # Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.crear_encargo(conn, cliente, lineas)
    assert contar(conn, "encargos") == 0
    assert contar(conn, "encargo_detalle") == 0


def test_crear_encargo_acepta_precio_estimado_cero(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R3: cero es legitimo -- al levantar el encargo puede no saberse el precio."""
    # Arrange / Act
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 1, 0)])

    # Assert
    assert core_encargos.obtener_encargo(conn, encargo_id)["total_estimado"] == 0.0


# --------------------------------------------------------------------------
# T5 -- cliente ausente o inexistente (R4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cliente_id", CLIENTES_INVALIDOS)
def test_crear_encargo_cliente_invalido_no_escribe(
    conn: sqlite3.Connection, cliente_id: Any
) -> None:
    """R4: sin un cliente identificable no hay encargo."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.crear_encargo(conn, cliente_id, [linea(CODIGO_A)])
    assert contar(conn, "encargos") == 0


def test_crear_encargo_cliente_inexistente_envuelve_la_integrityerror(
    conn: sqlite3.Connection,
) -> None:
    """R4: la FK rota sale como `EncargoError` en espanol, no como `sqlite3`."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError) as excinfo:
        core_encargos.crear_encargo(conn, 9999, [linea(CODIGO_A)])
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert "cliente" in str(excinfo.value).lower()
    assert contar(conn, "encargos") == 0


# --------------------------------------------------------------------------
# T6 -- rollback completo del alta (R10)
# --------------------------------------------------------------------------


def test_crear_encargo_articulo_inexistente_no_deja_cabecera_huerfana(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R10: si falla una linea, la cabecera ya insertada se revierte tambien."""
    # Arrange
    lineas = [linea(CODIGO_A, 2, 10.0), linea(CODIGO_FANTASMA, 1, 10.0)]

    # Act / Assert
    with pytest.raises(core_encargos.EncargoError) as excinfo:
        core_encargos.crear_encargo(conn, cliente, lineas)
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert contar(conn, "encargos") == 0
    assert contar(conn, "encargo_detalle") == 0


# --------------------------------------------------------------------------
# T7 -- listado con totales, filtro y orden (R5)
# --------------------------------------------------------------------------


def test_listar_encargos_sin_datos_devuelve_lista_vacia(
    conn: sqlite3.Connection,
) -> None:
    """R5: una base sin encargos devuelve `[]`, nunca `None`."""
    # Arrange / Act / Assert
    assert core_encargos.listar_encargos(conn) == []


def test_listar_encargos_expone_el_contrato_completo(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R5: campos de `CAMPOS_ENCARGO`, con el nombre del cliente y los totales."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(
        conn, cliente, [linea(CODIGO_A, 2, 150.0), linea(CODIGO_B, 1, 99.5)], OBSERVACIONES
    )
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, FORMA_A, 100.0)

    # Act
    fila = core_encargos.listar_encargos(conn)[0]

    # Assert
    assert set(fila) == set(core_encargos.CAMPOS_ENCARGO)
    assert fila["id"] == encargo_id
    assert fila["cliente_id"] == cliente
    assert fila["cliente_nombre"] == NOMBRE_A
    assert fila["status"] == core_encargos.STATUS_PENDIENTE
    assert fila["observaciones"] == OBSERVACIONES
    assert fila["total_estimado"] == 399.5
    assert fila["total_anticipado"] == 100.0


def test_listar_encargos_ordena_por_fecha_descendente(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R5: lo mas reciente arriba, sin importar el orden de creacion."""
    # Arrange
    viejo = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])
    nuevo = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])
    fijar_fecha(conn, viejo, "2026-01-01 08:00:00")
    fijar_fecha(conn, nuevo, "2026-07-27 19:30:00")

    # Act
    ids = [fila["id"] for fila in core_encargos.listar_encargos(conn)]

    # Assert
    assert ids == [nuevo, viejo]


def test_listar_encargos_filtra_por_status(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R5: `status` acota el listado a los encargos en ese estado."""
    # Arrange
    pendiente = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])
    cancelado = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])
    core_encargos.cancelar_encargo(conn, cancelado)

    # Act
    pendientes = core_encargos.listar_encargos(conn, core_encargos.STATUS_PENDIENTE)
    cancelados = core_encargos.listar_encargos(conn, core_encargos.STATUS_CANCELADO)

    # Assert
    assert [fila["id"] for fila in pendientes] == [pendiente]
    assert [fila["id"] for fila in cancelados] == [cancelado]
    assert len(core_encargos.listar_encargos(conn)) == 2


@pytest.mark.parametrize("status", ["pendiente", "PENDIENTE", "Surtida", "", 7])
def test_listar_encargos_status_desconocido_raises(
    conn: sqlite3.Connection, status: Any
) -> None:
    """R5: una errata en el filtro se delata, no devuelve un listado vacio."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.listar_encargos(conn, status)


def test_listar_encargos_resuelve_los_totales_en_una_sola_consulta(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R5: prohibido el N+1 -- los dos agregados salen del mismo SELECT."""
    # Arrange
    for _ in range(4):
        encargo_id = core_encargos.crear_encargo(
            conn, cliente, [linea(CODIGO_A, 2, 50.0), linea(CODIGO_B, 1, 30.0)]
        )
        core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, FORMA_A, 10.0)
    contador = ContadorSQL()

    # Act
    conn.set_trace_callback(contador)
    try:
        encargos = core_encargos.listar_encargos(conn)
    finally:
        conn.set_trace_callback(None)

    # Assert
    assert len(encargos) == 4
    assert all(fila["total_estimado"] == 130.0 for fila in encargos)
    assert all(fila["total_anticipado"] == 10.0 for fila in encargos)
    assert len(contador.sentencias) == 1


def test_listar_encargos_no_mezcla_los_totales_de_dos_encargos(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R5: cada subconsulta se correlaciona con su propio encargo."""
    # Arrange
    primero = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 1, 100.0)])
    segundo = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_B, 4, 25.0)])
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, primero, FORMA_A, 40.0)

    # Act
    por_id = {fila["id"]: fila for fila in core_encargos.listar_encargos(conn)}

    # Assert
    assert por_id[primero]["total_estimado"] == 100.0
    assert por_id[primero]["total_anticipado"] == 40.0
    assert por_id[segundo]["total_estimado"] == 100.0
    assert por_id[segundo]["total_anticipado"] == 0.0


# --------------------------------------------------------------------------
# T8 -- lectura de un encargo con su detalle (R6)
# --------------------------------------------------------------------------


def test_obtener_encargo_incluye_cabecera_lineas_y_anticipo(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R6: cabecera + `lineas` + `total_anticipado` en un solo diccionario."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(
        conn, cliente, [linea(CODIGO_A, 2, 150.0), linea(CODIGO_B, 1, 99.5)]
    )
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, FORMA_B, 200.0)

    # Act
    encargo = core_encargos.obtener_encargo(conn, encargo_id)

    # Assert
    assert set(encargo) == set(core_encargos.CAMPOS_ENCARGO) | {"lineas"}
    assert encargo["cliente_nombre"] == NOMBRE_A
    assert encargo["total_estimado"] == 399.5
    assert encargo["total_anticipado"] == 200.0
    assert [ln["codigo_articulo"] for ln in encargo["lineas"]] == [CODIGO_A, CODIGO_B]
    assert set(encargo["lineas"][0]) == set(core_encargos.CAMPOS_LINEA)
    assert encargo["lineas"][0]["cantidad_solicitada"] == 2
    assert encargo["lineas"][0]["precio_estimado"] == pytest.approx(150.0)


def test_obtener_encargo_inexistente_raises(conn: sqlite3.Connection) -> None:
    """R6: pedir un encargo que no existe es un error de dominio, no `None`."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.obtener_encargo(conn, 9999)


# --------------------------------------------------------------------------
# T9 -- edicion solo en `Pendiente` (R7)
# --------------------------------------------------------------------------


def test_editar_encargo_actualiza_cabecera_y_reemplaza_detalle(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R7: el detalle nuevo sustituye al anterior, no se acumula."""
    # Arrange
    otro = alta_cliente(conn, NOMBRE_B)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente, [linea(CODIGO_A, 5, 10.0), linea(CODIGO_B, 2, 10.0)], "vieja"
    )

    # Act
    core_encargos.editar_encargo(
        conn, encargo_id, otro, [linea(CODIGO_B, 3, 20.0)], "nueva"
    )

    # Assert
    encargo = core_encargos.obtener_encargo(conn, encargo_id)
    assert encargo["cliente_id"] == otro
    assert encargo["cliente_nombre"] == NOMBRE_B
    assert encargo["observaciones"] == "nueva"
    assert encargo["total_estimado"] == 60.0
    assert len(encargo["lineas"]) == 1
    assert contar(conn, "encargo_detalle") == 1


@pytest.mark.parametrize("status", STATUS_NO_PENDIENTE)
def test_editar_encargo_no_pendiente_no_cambia_nada(
    conn: sqlite3.Connection, cliente: int, status: str
) -> None:
    """R7: fuera de `Pendiente` la edicion rebota sin tocar una sola fila."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 5, 10.0)])
    fijar_status(conn, encargo_id, status)

    # Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.editar_encargo(conn, encargo_id, cliente, [linea(CODIGO_B, 1, 1.0)])
    encargo = core_encargos.obtener_encargo(conn, encargo_id)
    assert encargo["status"] == status
    assert encargo["lineas"] == [
        {"codigo_articulo": CODIGO_A, "cantidad_solicitada": 5, "precio_estimado": 10.0}
    ]


def test_editar_encargo_inexistente_raises(conn: sqlite3.Connection, cliente: int) -> None:
    """R7: no se puede editar lo que no existe."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.editar_encargo(conn, 9999, cliente, [linea(CODIGO_A)])


def test_editar_encargo_lineas_invalidas_conserva_el_detalle_previo(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R7: la validacion precede a la escritura; el detalle viejo sobrevive."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 5, 10.0)])

    # Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.editar_encargo(conn, encargo_id, cliente, [linea(CODIGO_B, 0, 1.0)])
    assert core_encargos.obtener_encargo(conn, encargo_id)["total_estimado"] == 50.0


def test_editar_encargo_articulo_inexistente_revierte_el_borrado(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R7: si el detalle nuevo falla, el `DELETE` previo se revierte con el."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 5, 10.0)])
    nuevas = [linea(CODIGO_B, 1, 1.0), linea(CODIGO_FANTASMA, 1, 1.0)]

    # Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.editar_encargo(conn, encargo_id, cliente, nuevas)
    encargo = core_encargos.obtener_encargo(conn, encargo_id)
    assert len(encargo["lineas"]) == 1
    assert encargo["lineas"][0]["codigo_articulo"] == CODIGO_A


# --------------------------------------------------------------------------
# T10 -- cancelacion (R8)
# --------------------------------------------------------------------------


def test_cancelar_encargo_pendiente_cambia_el_status(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R8: de `Pendiente` a `Cancelado`, sin tocar el detalle."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 2, 10.0)])

    # Act
    core_encargos.cancelar_encargo(conn, encargo_id)

    # Assert
    assert status_de(conn, encargo_id) == core_encargos.STATUS_CANCELADO
    assert contar(conn, "encargo_detalle") == 1


def test_cancelar_encargo_no_mueve_el_dinero_anticipado(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R8: los abonos siguen registrados; devolverlos no es de este ciclo."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 2, 10.0)])
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, FORMA_A, 15.0)

    # Act
    core_encargos.cancelar_encargo(conn, encargo_id)

    # Assert
    assert contar(conn, "encargo_pagos") == 1
    assert core_encargos.obtener_encargo(conn, encargo_id)["total_anticipado"] == 15.0


def test_cancelar_encargo_dos_veces_raises(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R8: la segunda cancelacion ya no encuentra el encargo en `Pendiente`."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])
    core_encargos.cancelar_encargo(conn, encargo_id)

    # Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.cancelar_encargo(conn, encargo_id)


@pytest.mark.parametrize("status", ["Surtido", "Entregado"])
def test_cancelar_encargo_ya_surtido_no_cambia_nada(
    conn: sqlite3.Connection, cliente: int, status: str
) -> None:
    """R8: lo que ya salio del almacen no se cancela desde aqui."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A)])
    fijar_status(conn, encargo_id, status)

    # Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.cancelar_encargo(conn, encargo_id)
    assert status_de(conn, encargo_id) == status


def test_cancelar_encargo_inexistente_raises(conn: sqlite3.Connection) -> None:
    """R8: cancelar un id que no existe es un error de dominio."""
    # Arrange / Act / Assert
    with pytest.raises(core_encargos.EncargoError):
        core_encargos.cancelar_encargo(conn, 9999)


# --------------------------------------------------------------------------
# T11 -- anticipos delegados al componente de pagos (R9)
# --------------------------------------------------------------------------


def test_encargo_sin_anticipos_reporta_cero(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R9: cero abonos es un caso normal, no una ausencia de dato."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 1, 80.0)])

    # Act
    encargo = core_encargos.obtener_encargo(conn, encargo_id)

    # Assert
    assert encargo["total_anticipado"] == 0.0
    assert core_encargos.listar_encargos(conn)[0]["total_anticipado"] == 0.0


@pytest.mark.parametrize(
    ("abonos", "esperado"),
    [((), 0.0), ((50.0,), 50.0), ((50.0, 25.5), 75.5), ((33.33, 33.33, 33.34), 100.0)],
)
def test_total_anticipado_suma_los_abonos_registrados(
    conn: sqlite3.Connection, cliente: int, abonos: tuple[float, ...], esperado: float
) -> None:
    """R9: 0..N abonos, sumados con la misma semantica que `core_pagos`."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 1, 200.0)])
    for abono in abonos:
        core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, FORMA_A, abono)

    # Act
    encargo = core_encargos.obtener_encargo(conn, encargo_id)

    # Assert
    assert encargo["total_anticipado"] == esperado
    assert contar(conn, "encargo_pagos") == len(abonos)


def test_total_anticipado_coincide_en_listado_lectura_y_core_pagos(
    conn: sqlite3.Connection, cliente: int
) -> None:
    """R9: una sola cifra para el mismo encargo, la vea quien la vea."""
    # Arrange
    encargo_id = core_encargos.crear_encargo(conn, cliente, [linea(CODIGO_A, 1, 500.0)])
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, FORMA_A, 111.11)
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, FORMA_B, 100.05)

    # Act
    listado = core_encargos.listar_encargos(conn)[0]
    lectura = core_encargos.obtener_encargo(conn, encargo_id)
    componente = core_pagos.total_pagado(conn, core_encargos.TABLA_PAGOS, encargo_id)

    # Assert
    assert listado["total_anticipado"] == componente
    assert lectura["total_anticipado"] == componente
    assert componente == 211.16


def test_anticipo_sobre_encargo_inexistente_lo_rechaza_la_fk(
    conn: sqlite3.Connection,
) -> None:
    """R9: la guarda es de `core_pagos`; este modulo no la duplica."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.PagoError):
        core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, 9999, FORMA_A, 10.0)
    assert contar(conn, "encargo_pagos") == 0
