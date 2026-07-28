"""Suite del spike RT-2: conversion `encargo -> venta` (ENC-01).

Corre bajo la suite general de pytest. La fixture levanta el **esquema real**
con `db.init_db(":memory:")` (desviacion D7): `vw_existencias`, los CHECK de
`pedido_detalle` y las FKs de encargos se ejercitan de verdad, asi que lo que
estos tests prueban es lo que ENC-03 va a heredar.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Final

import pytest

import poc_encargo_venta as poc
from core_ventas import VentaError

POC_PATH: Final[pathlib.Path] = pathlib.Path(poc.__file__).resolve()
_METODOS_SQL: Final[frozenset[str]] = frozenset(
    {"execute", "executemany", "executescript"})


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico completo aplicado."""
    conexion = poc.nueva_bd()
    try:
        yield conexion
    finally:
        conexion.close()


def _contar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def _disponibles(conn: sqlite3.Connection, codigo: str) -> int:
    fila = conn.execute(
        "SELECT piezas_disponibles FROM vw_existencias WHERE codigo_articulo = ?",
        (codigo,)).fetchone()
    return int(fila["piezas_disponibles"])


def _cabecera(conn: sqlite3.Connection, encargo_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT status, venta_id FROM encargos WHERE id = ?", (encargo_id,)
    ).fetchone()


# --- R1 -- T1: scaffold sobre el esquema real


def test_poc_db_tiene_vw_existencias_y_encargos(conn: sqlite3.Connection) -> None:
    # Arrange
    poc._seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
    cliente_id = poc._seed_cliente(conn, "Ana")

    # Act
    encargo_id = poc._seed_encargo(
        conn, cliente_id, [{"codigo": "11111", "cantidad": 2, "precio": 180.0}])

    # Assert: el seed evita la trampa de `piezas_recibidas` (casa + local)
    assert _disponibles(conn, "11111") == 10
    assert _cabecera(conn, encargo_id)["status"] == "Pendiente"
    assert _cabecera(conn, encargo_id)["venta_id"] is None


# --- R2, R11 -- T2: el sketch de conversion


def test_convertir_crea_venta_detalle_y_marca_entregado(
    conn: sqlite3.Connection,
) -> None:
    # Arrange
    poc._seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
    poc._seed_stock(conn, "22222", "Olla 5L", 6, 200.0)
    cliente_id = poc._seed_cliente(conn, "Ana")
    encargo_id = poc._seed_encargo(conn, cliente_id, [
        {"codigo": "11111", "cantidad": 2, "precio": 180.0},
        {"codigo": "22222", "cantidad": 1, "precio": 300.0}])

    # Act
    resultado = poc.convertir_encargo_a_venta(conn, encargo_id)

    # Assert
    venta = conn.execute("SELECT cliente_id FROM ventas WHERE id = ?",
                         (resultado["venta_id"],)).fetchone()
    assert resultado["num_lineas"] == 2
    assert resultado["total"] == pytest.approx(660.0)
    assert _cabecera(conn, encargo_id)["status"] == "Entregado"
    assert _cabecera(conn, encargo_id)["venta_id"] == resultado["venta_id"]
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 1
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 2
    assert venta["cliente_id"] == cliente_id


# --- R3, R8 -- T3: descuento unico de stock


def test_stock_descuenta_una_sola_vez(conn: sqlite3.Connection) -> None:
    # Arrange: 10 disponibles, encargo de 3
    poc._seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
    encargo_id = poc._seed_encargo(conn, poc._seed_cliente(conn, "Ana"),
                                   [{"codigo": "11111", "cantidad": 3, "precio": 180.0}])

    # Act
    poc.convertir_encargo_a_venta(conn, encargo_id)

    # Assert: 7, no 4 -- 4 seria doble descuento
    assert _disponibles(conn, "11111") == 7


def test_convertir_rechaza_segunda_conversion_del_mismo_encargo(
    conn: sqlite3.Connection,
) -> None:
    # Arrange
    poc._seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
    encargo_id = poc._seed_encargo(conn, poc._seed_cliente(conn, "Ana"),
                                   [{"codigo": "11111", "cantidad": 3, "precio": 180.0}])
    poc.convertir_encargo_a_venta(conn, encargo_id)

    # Act / Assert: sin este guarda el stock se descontaria dos veces
    with pytest.raises(VentaError):
        poc.convertir_encargo_a_venta(conn, encargo_id)

    assert _disponibles(conn, "11111") == 7
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 1


# --- R4 -- T4: caso sin anticipo


def test_caso_sin_anticipo_venta_sin_pagos(conn: sqlite3.Connection) -> None:
    # Arrange
    poc._seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
    encargo_id = poc._seed_encargo(conn, poc._seed_cliente(conn, "Ana"),
                                   [{"codigo": "11111", "cantidad": 2, "precio": 180.0}])

    # Act
    resultado = poc.convertir_encargo_a_venta(conn, encargo_id)

    # Assert
    assert _contar(conn, "SELECT COUNT(*) FROM venta_pagos") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 1
    assert resultado["anticipo"] == pytest.approx(0.0)
    assert resultado["saldo"] == pytest.approx(360.0)
    assert resultado["status"] == "Entregado"
    assert _cabecera(conn, encargo_id)["venta_id"] == resultado["venta_id"]


# --- R5, R7 -- T5: caso anticipo parcial


def test_caso_anticipo_parcial_saldo_y_conservacion(conn: sqlite3.Connection) -> None:
    # Arrange: total 900, anticipo 400 -> saldo 500
    poc._seed_stock(conn, "22222", "Olla 5L", 8, 200.0)
    encargo_id = poc._seed_encargo(
        conn, poc._seed_cliente(conn, "Beto"),
        [{"codigo": "22222", "cantidad": 3, "precio": 300.0}],
        [{"forma_pago": "Transferencia", "monto": 400.0}])
    antes = conn.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM encargo_pagos WHERE encargo_id = ?",
        (encargo_id,)).fetchone()[0]

    # Act
    resultado = poc.convertir_encargo_a_venta(conn, encargo_id)

    # Assert
    pagos = conn.execute(
        "SELECT forma_pago, monto FROM venta_pagos WHERE venta_id = ?",
        (resultado["venta_id"],)).fetchall()
    assert len(pagos) == 1
    assert pagos[0]["forma_pago"] == "Transferencia"
    assert resultado["total"] == pytest.approx(900.0)
    assert resultado["saldo"] == pytest.approx(500.0)
    assert resultado["saldo"] > 0
    assert sum(float(p["monto"]) for p in pagos) == pytest.approx(float(antes))


def test_traspaso_preserva_cada_anticipo_por_separado(conn: sqlite3.Connection) -> None:
    # Arrange: dos anticipos de formas distintas
    poc._seed_stock(conn, "22222", "Olla 5L", 8, 200.0)
    encargo_id = poc._seed_encargo(
        conn, poc._seed_cliente(conn, "Beto"),
        [{"codigo": "22222", "cantidad": 3, "precio": 300.0}],
        [{"forma_pago": "Efectivo", "monto": 100.0},
         {"forma_pago": "Tarjeta", "monto": 250.0}])

    # Act
    resultado = poc.convertir_encargo_a_venta(conn, encargo_id)

    # Assert: 1:1, sin consolidar en un solo pago
    filas = conn.execute(
        "SELECT forma_pago, monto FROM venta_pagos WHERE venta_id = ? ORDER BY id",
        (resultado["venta_id"],)).fetchall()
    assert [(f["forma_pago"], f["monto"]) for f in filas] == [
        ("Efectivo", 100.0), ("Tarjeta", 250.0)]
    assert resultado["saldo"] == pytest.approx(550.0)


# --- R6, R9 -- T6: caso stock insuficiente


def test_caso_stock_insuficiente_bloquea_y_no_muta(conn: sqlite3.Connection) -> None:
    # Arrange: 2 disponibles, encargo de 5, con anticipo de por medio
    poc._seed_stock(conn, "33333", "Vajilla 20pz", 2, 500.0)
    encargo_id = poc._seed_encargo(
        conn, poc._seed_cliente(conn, "Carla"),
        [{"codigo": "33333", "cantidad": 5, "precio": 900.0}],
        [{"forma_pago": "Efectivo", "monto": 300.0}])

    # Act / Assert
    with pytest.raises(VentaError) as error:
        poc.convertir_encargo_a_venta(conn, encargo_id)

    assert "Vajilla 20pz" in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_pagos") == 0
    assert _cabecera(conn, encargo_id)["venta_id"] is None
    assert _cabecera(conn, encargo_id)["status"] == "Pendiente"
    assert _disponibles(conn, "33333") == 2
    assert _contar(conn, "SELECT COUNT(*) FROM encargo_pagos") == 1


def test_stock_agregado_por_codigo_bloquea_dos_lineas_del_mismo_articulo(
    conn: sqlite3.Connection,
) -> None:
    # Arrange: 3 disponibles repartidos en dos lineas de 2 (total 4)
    poc._seed_stock(conn, "33333", "Vajilla 20pz", 3, 500.0)
    encargo_id = poc._seed_encargo(conn, poc._seed_cliente(conn, "Carla"), [
        {"codigo": "33333", "cantidad": 2, "precio": 900.0},
        {"codigo": "33333", "cantidad": 2, "precio": 900.0}])

    # Act / Assert: la validacion agregada de CLI-02 lo bloquea
    with pytest.raises(VentaError):
        poc.convertir_encargo_a_venta(conn, encargo_id)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


# --- R7, R8 -- T7: aserciones transversales


def test_riesgos_stock_unico_y_anticipo_conservado(conn: sqlite3.Connection) -> None:
    # Arrange
    poc._seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
    encargo_id = poc._seed_encargo(
        conn, poc._seed_cliente(conn, "Ana"),
        [{"codigo": "11111", "cantidad": 4, "precio": 180.0}],
        [{"forma_pago": "Efectivo", "monto": 200.0}])

    # Act
    resultado = poc.convertir_encargo_a_venta(conn, encargo_id)
    riesgos = poc._resumen_riesgos(conn, encargo_id, resultado["venta_id"])

    # Assert
    assert riesgos["solicitado"] == 4
    assert riesgos["vendido_detalle"] == 4
    assert riesgos["vendido_vista"] == 4
    assert riesgos["descuento_unico"]
    assert riesgos["anticipo_encargo"] == pytest.approx(200.0)
    assert riesgos["pagos_venta"] == pytest.approx(200.0)
    assert riesgos["anticipo_conservado"]
    assert _disponibles(conn, "11111") == 6


def test_rollback_deja_el_encargo_intacto_si_falla_el_cierre(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: el UPDATE final falla despues de la venta y de los pagos
    poc._seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
    encargo_id = poc._seed_encargo(
        conn, poc._seed_cliente(conn, "Ana"),
        [{"codigo": "11111", "cantidad": 2, "precio": 180.0}],
        [{"forma_pago": "Efectivo", "monto": 100.0}])
    monkeypatch.setattr(
        poc, "_SQL_CERRAR_ENCARGO",
        "UPDATE encargos SET venta_id = ?, columna_fake = ? WHERE id = ?")

    # Act / Assert: la transaccion unica revierte tambien los pasos 2 y 3
    with pytest.raises(VentaError):
        poc.convertir_encargo_a_venta(conn, encargo_id)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_pagos") == 0
    assert _cabecera(conn, encargo_id)["venta_id"] is None
    assert _cabecera(conn, encargo_id)["status"] == "Pendiente"
    assert _disponibles(conn, "11111") == 10


# --- R11, R12 -- T8: POC ejecutable y auditoria de SQL


def _llamadas_sql(arbol: ast.Module) -> list[ast.Call]:
    """Toda llamada a `execute`/`executemany`/`executescript` del modulo."""
    return [nodo for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr in _METODOS_SQL]


def test_poc_sql_parametrizado() -> None:
    # Arrange
    arbol = ast.parse(POC_PATH.read_text(encoding="utf-8"))

    # Act
    llamadas = _llamadas_sql(arbol)

    # Assert: ninguna sentencia se arma con f-string ni con `%`
    assert llamadas
    for llamada in llamadas:
        sql = llamada.args[0]
        assert not isinstance(sql, ast.JoinedStr)
        assert not (isinstance(sql, ast.BinOp) and isinstance(sql.op, ast.Mod))
        assert isinstance(sql, (ast.Name, ast.Constant))


def test_poc_no_usa_print() -> None:
    # Arrange
    arbol = ast.parse(POC_PATH.read_text(encoding="utf-8"))

    # Act
    nombres = [nodo.func.id for nodo in ast.walk(arbol)
               if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)]

    # Assert
    assert "print" not in nombres


def test_main_devuelve_cero_con_los_tres_casos_en_verde() -> None:
    # Arrange / Act
    codigo = poc.main()

    # Assert
    assert codigo == 0
