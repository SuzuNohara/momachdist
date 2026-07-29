"""Suite de la conversion `encargo -> venta` (`core_conversion`, ENC-03 + ENC-04 R1-R3).

Esta es la suite que sostiene **RT-2**, el riesgo mas alto del proyecto. El spike
ENC-01 reprodujo y cerro sus tres vectores; aqui se conservan los tres, ya sobre
el codigo de produccion:

* **Doble descuento de stock.** El vector real no es una resta mal hecha
  --`vw_existencias` es derivada, no hay contador que mantener-- sino
  **reconvertir el mismo encargo**, que crearia una segunda venta con su propio
  detalle. Se prueba por los dos caminos (status y `venta_id`) y se verifica el
  descuento unico distinguiendo **7 de 4**: con 10 disponibles y un encargo de 3,
  la vista tiene que quedar en 7.
* **Anticipo perdido o duplicado.** `SUM(encargo_pagos)` antes ==
  `SUM(venta_pagos)` despues, en cada caso convertido.
* **Commit parcial.** Se rompe de verdad el `UPDATE` final (paso 4) y se
  comprueba que la venta, su detalle **y** los pagos traspasados tampoco quedan
  escritos.

La fixture levanta el esquema **real** con `db.init_db(":memory:")`, de modo que
la vista, los CHECK y las FK son los de produccion.

Trampa de `vw_existencias` (confirmada por el spike): `piezas_recibidas` suma
**solo** `cantidad_casa + cantidad_local`, y el reparto por defecto manda todo al
asociado. Sembrar sin poner esas columnas da `piezas_disponibles = 0` y hace
parecer que toda validacion de stock falla; `_seed_stock` las pone explicitas.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any, Final
from unittest import mock

import pytest

import core_conversion
import core_encargos
import core_pagos
import db
from core_encargos import EncargoError
from core_ventas import VentaError

RAIZ_PROYECTO: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent
CONVERSION_PATH: Final[pathlib.Path] = RAIZ_PROYECTO / "core_conversion.py"

TIPO_NORMAL: Final[str] = "Normal (con descuento)"
_METODOS_SQL: Final[frozenset[str]] = frozenset({"execute", "executemany", "executescript"})

CODIGO_A: Final[str] = "ART-001"
CODIGO_B: Final[str] = "ART-002"
#: Producto del catalogo **sin** ninguna entrada de pedido: no aparece en
#: `vw_existencias`, asi que cuenta como 0 disponibles.
CODIGO_FANTASMA: Final[str] = "ART-FANTASMA"

#: Estados desde los que surtir debe rebotar (R3).
STATUS_NO_PENDIENTE: Final[tuple[str, ...]] = ("Surtido", "Entregado", "Cancelado")


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema real y el catalogo sembrado."""
    conexion = db.init_db(":memory:")
    conexion.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        (CODIGO_FANTASMA, "Articulo sin existencias"),
    )
    conexion.commit()
    try:
        yield conexion
    finally:
        conexion.close()


def _seed_stock(
    conexion: sqlite3.Connection,
    *,
    codigo: str,
    descripcion: str = "Articulo",
    piezas: int,
    costo_total: float,
    folio: str = "C001264",
) -> None:
    """Deja `piezas` disponibles del `codigo` **en casa**, con su costo real."""
    conexion.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        (codigo, descripcion),
    )
    cursor = conexion.execute("INSERT INTO pedidos (folio_pedido) VALUES (?)", (folio,))
    conexion.execute(
        """
        INSERT INTO pedido_detalle (
            pedido_id, codigo_articulo, ocurrencia, cantidad_solicitada,
            cantidad_surtida, cantidad_asociado, cantidad_casa, cantidad_local,
            precio_que_pagas, valor_total_con_iva, tipo
        ) VALUES (?, ?, 1, ?, ?, 0, ?, 0, ?, ?, ?)
        """,
        (
            int(cursor.lastrowid or 0),
            codigo,
            piezas,
            piezas,
            piezas,
            costo_total,
            costo_total * 1.5,
            TIPO_NORMAL,
        ),
    )
    conexion.commit()


def _seed_cliente(conexion: sqlite3.Connection, nombre: str = "Ana Lucia Torres") -> int:
    """Alta de cliente: `encargos.cliente_id` es NOT NULL con RESTRICT."""
    cursor = conexion.execute("INSERT INTO clientes (nombre) VALUES (?)", (nombre,))
    conexion.commit()
    return int(cursor.lastrowid or 0)


def _linea(codigo: str, cantidad: int, precio: float) -> dict[str, Any]:
    """Linea de encargo con el contrato de `core_encargos.CAMPOS_LINEA`."""
    return {
        "codigo_articulo": codigo,
        "cantidad_solicitada": cantidad,
        "precio_estimado": precio,
    }


def _contar(conexion: sqlite3.Connection, sql: str) -> int:
    """Escalar de un `SELECT COUNT(*)` literal escrito en el propio test."""
    return int(conexion.execute(sql).fetchone()[0])


def _disponibles(conexion: sqlite3.Connection, codigo: str) -> int:
    """`vw_existencias.piezas_disponibles` del codigo, o 0 si no esta en la vista."""
    fila = conexion.execute(
        "SELECT piezas_disponibles FROM vw_existencias WHERE codigo_articulo = ?",
        (codigo,),
    ).fetchone()
    return 0 if fila is None else int(fila["piezas_disponibles"])


def _cabecera(conexion: sqlite3.Connection, encargo_id: int) -> sqlite3.Row:
    """Status y `venta_id` crudos del encargo."""
    return conexion.execute(
        "SELECT status, venta_id FROM encargos WHERE id = ?", (encargo_id,)
    ).fetchone()


def _suma(conexion: sqlite3.Connection, sql: str, parametro: int) -> float:
    """Agregado de un solo valor; `sql` es una constante escrita en el test."""
    return round(float(conexion.execute(sql, (parametro,)).fetchone()[0]), 2)


_SQL_SUMA_ANTICIPOS: Final[str] = (
    "SELECT COALESCE(SUM(monto), 0) FROM encargo_pagos WHERE encargo_id = ?"
)
_SQL_SUMA_PAGOS_VENTA: Final[str] = (
    "SELECT COALESCE(SUM(monto), 0) FROM venta_pagos WHERE venta_id = ?"
)


def _forzar_status(conexion: sqlite3.Connection, encargo_id: int, status: str) -> None:
    """Pone un status a mano: ENC-02 no expone transiciones distintas de cancelar."""
    conexion.execute("UPDATE encargos SET status = ? WHERE id = ?", (status, encargo_id))
    conexion.commit()


# --- R1, R7, R9, R11, R14 -- caso de negocio 1: sin anticipo


def test_surtir_encargo_sin_anticipo_deja_la_venta_sin_pagos(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, descripcion="Sarten 24cm", piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 2, 180.0)])

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert
    assert resumen["total"] == pytest.approx(360.0)
    assert resumen["anticipo_transferido"] == pytest.approx(0.0)
    assert resumen["saldo"] == pytest.approx(360.0)
    assert _contar(conn, "SELECT COUNT(*) FROM venta_pagos") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 1


# --- R8, R10 -- caso de negocio 2: anticipo parcial


def test_surtir_encargo_anticipo_parcial_conserva_el_monto_y_deja_saldo(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: 8 disponibles, encargo de 3 @ 300, anticipo de 400
    _seed_stock(conn, codigo=CODIGO_A, descripcion="Olla 5L", piezas=8, costo_total=1600.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 300.0)])
    core_pagos.agregar_pago(
        conn, core_encargos.TABLA_PAGOS, encargo_id, "Transferencia", 400.0, "2026-07-01"
    )
    antes = _suma(conn, _SQL_SUMA_ANTICIPOS, encargo_id)

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: ni perdido ni duplicado, y el saldo queda pendiente
    assert resumen["total"] == pytest.approx(900.0)
    assert resumen["anticipo_transferido"] == pytest.approx(400.0)
    assert resumen["saldo"] == pytest.approx(500.0)
    assert _suma(conn, _SQL_SUMA_PAGOS_VENTA, int(resumen["venta_id"])) == pytest.approx(antes)


def test_surtir_encargo_traspasa_cada_anticipo_como_su_propia_fila(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: dos anticipos de formas y fechas distintas
    _seed_stock(conn, codigo=CODIGO_A, piezas=8, costo_total=800.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 300.0)])
    core_pagos.agregar_pago(
        conn, core_encargos.TABLA_PAGOS, encargo_id, "Efectivo", 100.0, "2026-07-01"
    )
    core_pagos.agregar_pago(
        conn, core_encargos.TABLA_PAGOS, encargo_id, "Tarjeta", 250.0, "2026-07-15"
    )

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: 1:1, con forma y fecha preservadas (H5: la fecha viaja explicita)
    pagos = core_pagos.listar_pagos(conn, "venta_pagos", int(resumen["venta_id"]))
    assert [(pago["forma_pago"], pago["monto"], pago["fecha"]) for pago in pagos] == [
        ("Efectivo", 100.0, "2026-07-01"),
        ("Tarjeta", 250.0, "2026-07-15"),
    ]
    assert resumen["anticipo_transferido"] == pytest.approx(350.0)


def test_surtir_encargo_no_borra_los_anticipos_del_encargo(conn: sqlite3.Connection) -> None:
    # Arrange: H4 -- `encargo_pagos` queda como historico del encargo
    _seed_stock(conn, codigo=CODIGO_A, piezas=8, costo_total=800.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 2, 300.0)])
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, "Efectivo", 200.0)

    # Act
    core_conversion.surtir_encargo(conn, encargo_id)

    # Assert
    assert _contar(conn, "SELECT COUNT(*) FROM encargo_pagos") == 1
    assert _suma(conn, _SQL_SUMA_ANTICIPOS, encargo_id) == pytest.approx(200.0)


# --- R6, R12 -- caso de negocio 3: stock insuficiente


def test_surtir_encargo_stock_insuficiente_bloquea_y_no_muta_nada(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: 2 disponibles, se piden 5, con un anticipo de por medio
    _seed_stock(conn, codigo=CODIGO_A, descripcion="Vajilla 20pz", piezas=2, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 5, 900.0)])
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, "Efectivo", 300.0)

    # Act / Assert
    with pytest.raises(VentaError) as error:
        core_conversion.surtir_encargo(conn, encargo_id)

    assert "Vajilla 20pz" in str(error.value)
    assert "2" in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_pagos") == 0
    cabecera = _cabecera(conn, encargo_id)
    assert cabecera["venta_id"] is None
    assert cabecera["status"] == core_encargos.STATUS_PENDIENTE
    assert _suma(conn, _SQL_SUMA_ANTICIPOS, encargo_id) == pytest.approx(300.0)
    assert _disponibles(conn, CODIGO_A) == 2


def test_surtir_encargo_agrega_las_lineas_del_mismo_codigo(conn: sqlite3.Connection) -> None:
    # Arrange: 3 disponibles y dos lineas de 2 del mismo articulo (total 4)
    _seed_stock(conn, codigo=CODIGO_A, piezas=3, costo_total=300.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_A, 2, 180.0), _linea(CODIGO_A, 2, 180.0)]
    )

    # Act / Assert: ninguna linea sola sobrevende, pero juntas si
    with pytest.raises(VentaError) as error:
        core_conversion.surtir_encargo(conn, encargo_id)

    assert "4" in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


def test_surtir_encargo_articulo_fuera_del_inventario_bloquea(conn: sqlite3.Connection) -> None:
    # Arrange: el producto existe en el catalogo pero no en `vw_existencias`
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_FANTASMA, 1, 50.0)]
    )

    # Act / Assert
    with pytest.raises(VentaError) as error:
        core_conversion.surtir_encargo(conn, encargo_id)

    assert CODIGO_FANTASMA in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


# --- RT-2 riesgo 1: doble descuento de stock


def test_surtir_encargo_descuenta_el_stock_una_sola_vez(conn: sqlite3.Connection) -> None:
    # Arrange: 10 disponibles, encargo de 3 -- la vista debe quedar en 7, no en 4
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 180.0)])

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: `encargo_detalle` no afecta la vista; solo `venta_detalle` descuenta
    assert _disponibles(conn, CODIGO_A) == 7
    vendidas = conn.execute(
        "SELECT piezas_vendidas FROM vw_existencias WHERE codigo_articulo = ?", (CODIGO_A,)
    ).fetchone()
    assert int(vendidas["piezas_vendidas"]) == 3
    assert _suma(
        conn,
        "SELECT COALESCE(SUM(cantidad), 0) FROM venta_detalle WHERE venta_id = ?",
        int(resumen["venta_id"]),
    ) == pytest.approx(3.0)


def test_surtir_encargo_rechaza_la_segunda_conversion(conn: sqlite3.Connection) -> None:
    # Arrange: el vector real de doble descuento es reconvertir el mismo encargo
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 180.0)])
    core_conversion.surtir_encargo(conn, encargo_id)

    # Act / Assert
    with pytest.raises(EncargoError):
        core_conversion.surtir_encargo(conn, encargo_id)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 1
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 1
    assert _disponibles(conn, CODIGO_A) == 7


def test_surtir_encargo_rechaza_reconversion_aunque_el_status_siga_pendiente(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: fila `Pendiente` con `venta_id` puesto a mano -- el esquema lo
    # permite (H2: `venta_id` es nullable y sin UNIQUE), solo el dominio lo veta
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 180.0)])
    cursor = conn.execute("INSERT INTO ventas (cliente_id) VALUES (?)", (cliente_id,))
    conn.execute(
        "UPDATE encargos SET venta_id = ? WHERE id = ?",
        (int(cursor.lastrowid or 0), encargo_id),
    )
    conn.commit()

    # Act / Assert
    with pytest.raises(EncargoError) as error:
        core_conversion.surtir_encargo(conn, encargo_id)

    assert "ya se convirtio" in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 0
    assert _disponibles(conn, CODIGO_A) == 10


def test_encargo_surtible_rechaza_el_mismo_estado_que_rechaza_la_conversion(
    conn: sqlite3.Connection,
) -> None:
    """El chequeo del boton y la conversion tienen que coincidir siempre.

    Con `venta_id` ya puesto y el status aun `Pendiente`, `surtir_encargo`
    rechaza (guarda anti-reconversion). Si `encargo_surtible` no mirara
    `venta_id`, habilitaria el boton Surtir y el clic rebotaria: la GUI estaria
    prometiendo algo que el dominio veta.
    """
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 180.0)])
    assert core_conversion.encargo_surtible(conn, encargo_id) is True

    cursor = conn.execute("INSERT INTO ventas (cliente_id) VALUES (?)", (cliente_id,))
    conn.execute(
        "UPDATE encargos SET venta_id = ? WHERE id = ?",
        (int(cursor.lastrowid or 0), encargo_id),
    )
    conn.commit()

    # Act / Assert: los dos criterios coinciden
    assert core_conversion.encargo_surtible(conn, encargo_id) is False
    with pytest.raises(EncargoError):
        core_conversion.surtir_encargo(conn, encargo_id)


# --- RT-2 riesgo 3: commit parcial


def test_surtir_encargo_revierte_entero_si_falla_el_cierre(conn: sqlite3.Connection) -> None:
    # Arrange: se rompe de verdad el UPDATE final (paso 4 de la conversion)
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 180.0)])
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, "Efectivo", 200.0)
    sql_roto = "UPDATE encargos SET venta_id = ?, columna_fake = ? WHERE id = ?"

    # Act / Assert
    with mock.patch.object(core_conversion, "_SQL_CERRAR_ENCARGO", sql_roto):
        with pytest.raises(EncargoError):
            core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: ni la venta, ni su detalle, ni los pagos traspasados sobreviven
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_pagos") == 0
    cabecera = _cabecera(conn, encargo_id)
    assert cabecera["status"] == core_encargos.STATUS_PENDIENTE
    assert cabecera["venta_id"] is None
    assert _suma(conn, _SQL_SUMA_ANTICIPOS, encargo_id) == pytest.approx(200.0)
    assert _disponibles(conn, CODIGO_A) == 10


# --- R3, R4, R5 -- guardas de estado


@pytest.mark.parametrize("status", STATUS_NO_PENDIENTE)
def test_surtir_encargo_no_pendiente_es_rechazado(conn: sqlite3.Connection, status: str) -> None:
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 2, 180.0)])
    _forzar_status(conn, encargo_id, status)

    # Act / Assert
    with pytest.raises(EncargoError):
        core_conversion.surtir_encargo(conn, encargo_id)

    cabecera = _cabecera(conn, encargo_id)
    assert cabecera["status"] == status
    assert cabecera["venta_id"] is None
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


def test_surtir_encargo_inexistente_es_rechazado(conn: sqlite3.Connection) -> None:
    # Arrange: la base no tiene ningun encargo
    # Act / Assert
    with pytest.raises(EncargoError):
        core_conversion.surtir_encargo(conn, 999)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


def test_surtir_encargo_sin_lineas_es_rechazado(conn: sqlite3.Connection) -> None:
    # Arrange: cabecera sin detalle -- ENC-02 ya lo impide; esto es la defensa
    cliente_id = _seed_cliente(conn)
    cursor = conn.execute("INSERT INTO encargos (cliente_id) VALUES (?)", (cliente_id,))
    conn.commit()

    # Act / Assert
    with pytest.raises(EncargoError) as error:
        core_conversion.surtir_encargo(conn, int(cursor.lastrowid or 0))

    assert "lineas" in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


# --- R2, R7, R11, R14 -- mapeo de lineas, precio firme y resumen


def test_surtir_encargo_usa_el_precio_estimado_como_precio_publico(
    conn: sqlite3.Connection,
) -> None:
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, descripcion="Sarten 24cm", piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 2, 180.0)])

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: precio del encargo, costo fresco de la vista
    fila = conn.execute(
        "SELECT codigo_articulo, cantidad, precio_publico, precio_costo, total, ganancia "
        "FROM venta_detalle WHERE venta_id = ?",
        (int(resumen["venta_id"]),),
    ).fetchone()
    assert fila["codigo_articulo"] == CODIGO_A
    assert int(fila["cantidad"]) == 2
    assert float(fila["precio_publico"]) == pytest.approx(180.0)
    assert float(fila["precio_costo"]) == pytest.approx(100.0)
    assert float(fila["total"]) == pytest.approx(360.0)
    assert float(fila["ganancia"]) == pytest.approx(160.0)


def test_surtir_encargo_respeta_el_precio_pactado_aunque_la_ganancia_salga_negativa(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: el costo (200/pieza) subio por encima del precio pactado (150)
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=2000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 2, 150.0)])

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: H6 -- el precio es firme, la perdida se registra tal cual
    assert resumen["total"] == pytest.approx(300.0)
    assert resumen["ganancia"] == pytest.approx(-100.0)
    assert resumen["lineas"][0]["precio_publico"] == pytest.approx(150.0)


def test_surtir_encargo_con_precio_estimado_cero_vende_en_cero(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: regalo o precio pendiente -- intencional, no un error
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 1, 0.0)])

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert
    assert resumen["total"] == pytest.approx(0.0)
    assert resumen["ganancia"] == pytest.approx(-100.0)
    assert resumen["saldo"] == pytest.approx(0.0)


def test_surtir_encargo_fija_venta_id_y_lo_pasa_a_entregado(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 2, 180.0)])

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert
    cabecera = _cabecera(conn, encargo_id)
    assert int(cabecera["venta_id"]) == int(resumen["venta_id"])
    assert cabecera["status"] == core_encargos.STATUS_ENTREGADO
    assert resumen["status"] == core_encargos.STATUS_ENTREGADO


def test_surtir_encargo_marca_la_venta_con_la_trazabilidad_al_encargo(
    conn: sqlite3.Connection,
) -> None:
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_A, 2, 180.0)], "Lo necesita el viernes"
    )

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: la marca y la nota original de la clienta conviven
    fila = conn.execute(
        "SELECT cliente_id, observaciones FROM ventas WHERE id = ?",
        (int(resumen["venta_id"]),),
    ).fetchone()
    assert int(fila["cliente_id"]) == cliente_id
    assert f"#{encargo_id}" in fila["observaciones"]
    assert "Lo necesita el viernes" in fila["observaciones"]


def test_surtir_encargo_devuelve_el_resumen_completo(conn: sqlite3.Connection) -> None:
    # Arrange: dos articulos distintos, con anticipo
    _seed_stock(conn, codigo=CODIGO_A, descripcion="Sarten 24cm", piezas=10, costo_total=1000.0)
    _seed_stock(
        conn, codigo=CODIGO_B, descripcion="Olla 5L", piezas=4, costo_total=800.0, folio="C001265"
    )
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_A, 2, 180.0), _linea(CODIGO_B, 1, 300.0)]
    )
    core_pagos.agregar_pago(conn, core_encargos.TABLA_PAGOS, encargo_id, "Efectivo", 160.0)

    # Act
    resumen = core_conversion.surtir_encargo(conn, encargo_id)

    # Assert
    assert set(resumen) == set(core_conversion.CAMPOS_RESUMEN)
    assert resumen["encargo_id"] == encargo_id
    assert resumen["cliente_id"] == cliente_id
    assert resumen["num_lineas"] == 2
    assert resumen["total"] == pytest.approx(660.0)
    assert resumen["ganancia"] == pytest.approx(260.0)
    assert resumen["saldo"] == pytest.approx(500.0)
    assert set(resumen["lineas"][0]) >= {
        "codigo",
        "descripcion",
        "cantidad",
        "precio_costo",
        "precio_publico",
        "total",
        "ganancia",
    }
    assert resumen["lineas"][0]["descripcion"] == "Sarten 24cm"


# --- ENC-04 R1, R2, R3 -- `encargo_surtible`, el chequeo que habilita el boton


def test_encargo_surtible_true_con_stock_suficiente(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    _seed_stock(conn, codigo=CODIGO_B, piezas=4, costo_total=800.0, folio="C001265")
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_A, 10, 180.0), _linea(CODIGO_B, 4, 300.0)]
    )

    # Act / Assert: la frontera exacta (pedir todo lo disponible) es surtible
    assert core_conversion.encargo_surtible(conn, encargo_id) is True


@pytest.mark.parametrize("status", STATUS_NO_PENDIENTE)
def test_encargo_surtible_false_si_no_esta_pendiente(
    conn: sqlite3.Connection, status: str
) -> None:
    # Arrange: stock de sobra, para que lo unico que decida sea el status
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 1, 180.0)])
    _forzar_status(conn, encargo_id, status)

    # Act / Assert
    assert core_conversion.encargo_surtible(conn, encargo_id) is False


def test_encargo_surtible_false_con_stock_insuficiente(conn: sqlite3.Connection) -> None:
    # Arrange: la segunda linea pide una pieza mas de la que hay
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    _seed_stock(conn, codigo=CODIGO_B, piezas=2, costo_total=400.0, folio="C001265")
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_A, 1, 180.0), _linea(CODIGO_B, 3, 300.0)]
    )

    # Act / Assert
    assert core_conversion.encargo_surtible(conn, encargo_id) is False


def test_encargo_surtible_false_si_el_producto_no_esta_en_la_vista(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: producto del catalogo sin existencias -> cuenta como 0
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_FANTASMA, 1, 50.0)]
    )

    # Act / Assert
    assert core_conversion.encargo_surtible(conn, encargo_id) is False


def test_encargo_surtible_agrega_las_lineas_del_mismo_codigo(conn: sqlite3.Connection) -> None:
    # Arrange: 3 disponibles y dos lineas de 2 -- ninguna sola sobrevende
    _seed_stock(conn, codigo=CODIGO_A, piezas=3, costo_total=300.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(
        conn, cliente_id, [_linea(CODIGO_A, 2, 180.0), _linea(CODIGO_A, 2, 180.0)]
    )

    # Act / Assert: el aviso coincide con lo que haria `surtir_encargo`
    assert core_conversion.encargo_surtible(conn, encargo_id) is False
    with pytest.raises(VentaError):
        core_conversion.surtir_encargo(conn, encargo_id)


def test_encargo_surtible_false_si_el_encargo_no_existe(conn: sqlite3.Connection) -> None:
    # Arrange / Act / Assert: es una lectura para la GUI, no debe lanzar
    assert core_conversion.encargo_surtible(conn, 999) is False


def test_encargo_surtible_false_si_el_encargo_no_tiene_lineas(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: cabecera `Pendiente` sin detalle -- no hay nada que surtir
    cliente_id = _seed_cliente(conn)
    cursor = conn.execute("INSERT INTO encargos (cliente_id) VALUES (?)", (cliente_id,))
    conn.commit()

    # Act / Assert
    assert core_conversion.encargo_surtible(conn, int(cursor.lastrowid or 0)) is False


def test_encargo_surtible_false_si_la_consulta_falla(conn: sqlite3.Connection) -> None:
    # Arrange: la GUI no puede recibir una excepcion desde este chequeo
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 1, 180.0)])
    sql_roto = "SELECT columna_fake AS grupos, 0 AS faltantes FROM encargo_detalle WHERE id = ?"

    # Act
    with mock.patch.object(core_conversion, "_SQL_SURTIBLE", sql_roto):
        surtible = core_conversion.encargo_surtible(conn, encargo_id)

    # Assert
    assert surtible is False


def test_encargo_surtible_deja_de_serlo_tras_surtir(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo=CODIGO_A, piezas=10, costo_total=1000.0)
    cliente_id = _seed_cliente(conn)
    encargo_id = core_encargos.crear_encargo(conn, cliente_id, [_linea(CODIGO_A, 3, 180.0)])
    antes = core_conversion.encargo_surtible(conn, encargo_id)

    # Act
    core_conversion.surtir_encargo(conn, encargo_id)

    # Assert: el boton se apaga solo, sin depender de la GUI
    assert antes is True
    assert core_conversion.encargo_surtible(conn, encargo_id) is False


# --- R15 -- SQL parametrizado (auditoria estatica por AST)


def _llamadas_sql(arbol: ast.Module) -> list[ast.Call]:
    """Toda llamada a `execute`/`executemany`/`executescript` del modulo."""
    return [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in _METODOS_SQL
    ]


def test_conversion_sql_parametrizado() -> None:
    # Arrange
    arbol = ast.parse(CONVERSION_PATH.read_text(encoding="utf-8"))

    # Act
    llamadas = _llamadas_sql(arbol)

    # Assert: ninguna sentencia se arma con f-string ni con `%`
    assert llamadas
    for llamada in llamadas:
        sql = llamada.args[0]
        assert not isinstance(sql, ast.JoinedStr)
        assert not (isinstance(sql, ast.BinOp) and isinstance(sql.op, ast.Mod))
        assert isinstance(sql, (ast.Name, ast.Constant))


def test_conversion_no_duplica_el_sql_de_venta_ni_de_pagos() -> None:
    # Arrange: DEUDA-05 existe para que este modulo componga, no copie
    fuente = CONVERSION_PATH.read_text(encoding="utf-8")

    # Act / Assert
    assert "INSERT INTO venta_detalle" not in fuente
    assert "INSERT INTO venta_pagos" not in fuente
    assert "INSERT INTO ventas" not in fuente
