"""Suite del dominio de ventas: canasta multi-producto atomica (CLI-02).

La fixture levanta el esquema real con `db.init_db(":memory:")`, de modo que la
vista `vw_existencias`, los CHECK del detalle y las FKs se ejercitan de verdad.

Detalle importante del seeding: `vw_existencias.piezas_recibidas` suma **solo**
`cantidad_casa + cantidad_local` (lo entregado al asociado no es stock propio),
asi que todo producto vendible se siembra con piezas en casa.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Final
from unittest import mock

import pytest

import core_existencias
import core_ventas
import db

RAIZ_PROYECTO: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent
VENTAS_PATH: Final[pathlib.Path] = RAIZ_PROYECTO / "core_ventas.py"

TIPO_NORMAL: Final[str] = "Normal (con descuento)"
_METODOS_SQL: Final[frozenset[str]] = frozenset({"execute", "executemany", "executescript"})


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def _seed_stock(
    conn: sqlite3.Connection,
    *,
    codigo: str,
    descripcion: str = "Articulo",
    piezas: int,
    costo_total: float,
    folio: str = "C001264",
) -> None:
    """Deja `piezas` disponibles del `codigo` en casa, con su costo real."""
    conn.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        (codigo, descripcion),
    )
    cur = conn.execute(
        "INSERT INTO pedidos (folio_pedido) VALUES (?)",
        (folio,),
    )
    pedido_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO pedido_detalle (
            pedido_id, codigo_articulo, ocurrencia, cantidad_solicitada,
            cantidad_surtida, cantidad_asociado, cantidad_casa, cantidad_local,
            precio_que_pagas, valor_total_con_iva, tipo
        ) VALUES (?, ?, 1, ?, ?, 0, ?, 0, ?, ?, ?)
        """,
        (pedido_id, codigo, piezas, piezas, piezas, costo_total, costo_total * 1.5, TIPO_NORMAL),
    )


def _contar(conn: sqlite3.Connection, tabla_sql: str) -> int:
    return int(conn.execute(tabla_sql).fetchone()[0])


# --- R1-R4 -- T1: validacion de forma


def test_registrar_venta_rechaza_canasta_vacia(conn: sqlite3.Connection) -> None:
    # Arrange
    lineas: list[dict] = []

    # Act / Assert
    with pytest.raises(core_ventas.VentaError):
        core_ventas.registrar_venta(conn, None, lineas)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


@pytest.mark.parametrize("cantidad", [0, -1, 2.5, "dos"])
def test_registrar_venta_rechaza_cantidad_no_positiva(
    conn: sqlite3.Connection, cantidad: object
) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", piezas=10, costo_total=1000.0)
    lineas = [{"codigo": "11111", "cantidad": cantidad, "precio_publico": 180.0}]

    # Act / Assert
    with pytest.raises(core_ventas.VentaError):
        core_ventas.registrar_venta(conn, None, lineas)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


def test_registrar_venta_rechaza_precio_negativo(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", piezas=10, costo_total=1000.0)
    lineas = [{"codigo": "11111", "cantidad": 1, "precio_publico": -0.5}]

    # Act / Assert
    with pytest.raises(core_ventas.VentaError):
        core_ventas.registrar_venta(conn, None, lineas)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


# --- R5, R6, R7 -- T2: validacion de stock


def test_validar_stock_bloquea_linea_insuficiente(conn: sqlite3.Connection) -> None:
    # Arrange: 3 disponibles, se piden 5
    _seed_stock(conn, codigo="11111", descripcion="Sarten 24cm", piezas=3, costo_total=300.0)
    lineas = [{"codigo": "11111", "cantidad": 5, "precio_publico": 180.0}]

    # Act / Assert
    with pytest.raises(core_ventas.VentaError) as error:
        core_ventas.registrar_venta(conn, None, lineas)

    assert "Sarten 24cm" in str(error.value)
    assert "3" in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


def test_validar_stock_codigo_inexistente(conn: sqlite3.Connection) -> None:
    # Arrange: la vista no tiene ninguna fila para 99999
    lineas = [{"codigo": "99999", "cantidad": 1, "precio_publico": 10.0}]

    # Act / Assert
    with pytest.raises(core_ventas.VentaError) as error:
        core_ventas.registrar_venta(conn, None, lineas)

    assert "99999" in str(error.value)


def test_validar_stock_devuelve_descripcion_disponibles_y_costo(
    conn: sqlite3.Connection,
) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", descripcion="Sarten 24cm", piezas=10, costo_total=1000.0)
    lineas = [{"codigo": "11111", "cantidad": 2, "precio_publico": 180.0}]

    # Act
    datos = core_ventas._validar_stock_canasta(conn, lineas)

    # Assert
    assert datos["11111"] == {
        "descripcion": "Sarten 24cm",
        "disponibles": 10,
        "precio_costo": pytest.approx(100.0),
    }


# --- R5 -- T3: agregacion de lineas repetidas (anti-oversell)


def test_validar_stock_agrega_lineas_repetidas(conn: sqlite3.Connection) -> None:
    # Arrange: 3 disponibles y dos lineas de 2 del mismo codigo (total 4)
    _seed_stock(conn, codigo="11111", descripcion="Sarten 24cm", piezas=3, costo_total=300.0)
    lineas = [
        {"codigo": "11111", "cantidad": 2, "precio_publico": 180.0},
        {"codigo": "11111", "cantidad": 2, "precio_publico": 180.0},
    ]

    # Act / Assert
    with pytest.raises(core_ventas.VentaError) as error:
        core_ventas.registrar_venta(conn, None, lineas)

    assert "4" in str(error.value)
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 0


# --- R8 -- T4: calculo de linea


@pytest.mark.parametrize(
    "cantidad, precio_publico, precio_costo, total, ganancia",
    [
        (2, 180.0, 100.0, 360.0, 160.0),
        (3, 33.33, 10.0, 99.99, 69.99),
        (1, 0.0, 100.0, 0.0, -100.0),
    ],
)
def test_calcular_linea_total_y_ganancia(
    cantidad: int, precio_publico: float, precio_costo: float, total: float, ganancia: float
) -> None:
    # Arrange / Act
    calculo = core_ventas._calcular_linea(cantidad, precio_publico, precio_costo)

    # Assert
    assert calculo["total"] == pytest.approx(total)
    assert calculo["ganancia"] == pytest.approx(ganancia)


# --- R9, R10 -- T5: insercion transaccional


def test_registrar_venta_atomica_rollback(conn: sqlite3.Connection) -> None:
    # Arrange: el detalle falla despues de insertar el encabezado
    _seed_stock(conn, codigo="11111", piezas=10, costo_total=1000.0)
    lineas = [{"codigo": "11111", "cantidad": 2, "precio_publico": 180.0}]
    sql_roto = (
        "INSERT INTO venta_detalle ("
        "venta_id, codigo_articulo, cantidad, precio_costo, precio_publico, total, columna_fake"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)"
    )

    # Act / Assert
    with mock.patch.object(core_ventas, "_SQL_INSERT_DETALLE", sql_roto):
        with pytest.raises(core_ventas.VentaError):
            core_ventas.registrar_venta(conn, None, lineas)

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0
    assert _contar(conn, "SELECT COUNT(*) FROM venta_detalle") == 0


def test_registrar_venta_cliente_inexistente_falla(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", piezas=10, costo_total=1000.0)
    lineas = [{"codigo": "11111", "cantidad": 1, "precio_publico": 180.0}]

    # Act / Assert
    with pytest.raises(core_ventas.VentaError) as error:
        core_ventas.registrar_venta(conn, 999, lineas)

    assert "cliente" in str(error.value).lower()
    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


# --- R11 -- T6: resumen de retorno


def test_registrar_venta_resumen_correcto(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", descripcion="Sarten 24cm", piezas=10, costo_total=1000.0)
    _seed_stock(conn, codigo="22222", descripcion="Olla 5L", piezas=4, costo_total=800.0,
                folio="C001265")
    lineas = [
        {"codigo": "11111", "cantidad": 2, "precio_publico": 180.0},
        {"codigo": "22222", "cantidad": 1, "precio_publico": 300.0},
    ]

    # Act
    resumen = core_ventas.registrar_venta(conn, None, lineas, "venta de prueba")

    # Assert
    assert set(resumen.keys()) == {
        "venta_id", "cliente_id", "total", "ganancia", "num_lineas", "lineas",
    }
    assert resumen["venta_id"] > 0
    assert resumen["cliente_id"] is None
    assert resumen["total"] == pytest.approx(660.0)      # 2*180 + 1*300
    assert resumen["ganancia"] == pytest.approx(260.0)   # 660 - (2*100 + 1*200)
    assert resumen["num_lineas"] == 2
    assert [linea["disponibles_restantes"] for linea in resumen["lineas"]] == [8, 3]
    assert resumen["lineas"][0]["descripcion"] == "Sarten 24cm"


# --- R11 -- T7: la vista refleja el descuento


def test_venta_descuenta_stock_en_vista(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", descripcion="Sarten 24cm", piezas=10, costo_total=1000.0)

    # Act
    core_ventas.registrar_venta(
        conn, None, [{"codigo": "11111", "cantidad": 3, "precio_publico": 180.0}]
    )

    # Assert: sin recalculo manual, la vista ya descuenta
    fila = core_existencias.obtener_existencias(conn)[0]
    assert fila["Piezas vendidas"] == 3
    assert fila["Piezas disponibles"] == 7


# --- R12 -- T8: SQL parametrizado (auditoria estatica por AST)


def _llamadas_sql(arbol: ast.Module) -> list[ast.Call]:
    """Toda llamada a `execute`/`executemany`/`executescript` del modulo."""
    return [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in _METODOS_SQL
    ]


def test_ventas_sql_parametrizado() -> None:
    # Arrange
    arbol = ast.parse(VENTAS_PATH.read_text(encoding="utf-8"))

    # Act
    llamadas = _llamadas_sql(arbol)

    # Assert: ninguna sentencia se arma con f-string ni con `%`
    assert llamadas
    for llamada in llamadas:
        sql = llamada.args[0]
        assert not isinstance(sql, ast.JoinedStr)
        assert not (isinstance(sql, ast.BinOp) and isinstance(sql.op, ast.Mod))
        assert isinstance(sql, (ast.Name, ast.Constant))


def test_registrar_venta_codigo_malicioso_no_ejecuta_sql(conn: sqlite3.Connection) -> None:
    # Arrange: el codigo viaja ligado, nunca interpolado
    _seed_stock(conn, codigo="11111", piezas=10, costo_total=1000.0)
    codigo = "11111'); DROP TABLE ventas;--"

    # Act / Assert: se rechaza como codigo inexistente, sin efecto lateral
    with pytest.raises(core_ventas.VentaError):
        core_ventas.registrar_venta(conn, None, [
            {"codigo": codigo, "cantidad": 1, "precio_publico": 10.0}
        ])

    assert _contar(conn, "SELECT COUNT(*) FROM ventas") == 0


# --- R13 -- T9: los pagos son de otro dominio


def test_registrar_venta_no_crea_pagos(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", piezas=10, costo_total=1000.0)

    # Act
    core_ventas.registrar_venta(
        conn, None, [{"codigo": "11111", "cantidad": 2, "precio_publico": 180.0}]
    )

    # Assert
    assert _contar(conn, "SELECT COUNT(*) FROM venta_pagos") == 0
    assert "INSERT INTO venta_pagos" not in VENTAS_PATH.read_text(encoding="utf-8")


# --- R1 -- T10: la firma Excel no sobrevive en la capa core


def test_no_referencia_registrar_venta_excel() -> None:
    # Arrange
    modulos_core = sorted(RAIZ_PROYECTO.glob("core*.py"))

    # Act
    parametros = list(inspect.signature(core_ventas.registrar_venta).parameters)

    # Assert: firma nueva sobre la conexion, sin rastro de la ruta de Excel
    assert parametros == ["conn", "cliente_id", "lineas", "observaciones"]
    for modulo in modulos_core:
        assert "ruta_excel" not in modulo.read_text(encoding="utf-8")


# --- R15 -- T11: historial sobre SQLite


def test_obtener_ventas_historial_desde_sqlite(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_stock(conn, codigo="11111", descripcion="Sarten 24cm", piezas=10, costo_total=1000.0)
    resumen = core_ventas.registrar_venta(
        conn, None, [{"codigo": "11111", "cantidad": 2, "precio_publico": 180.0}]
    )

    # Act
    historial = core_ventas.obtener_ventas_historial(conn)

    # Assert
    assert len(historial) == 1
    fila = historial[0]
    assert set(fila.keys()) == set(core_ventas.CAMPOS_HISTORIAL)
    assert fila["venta_id"] == resumen["venta_id"]
    assert fila["codigo"] == "11111"
    assert fila["descripcion"] == "Sarten 24cm"
    assert fila["cantidad"] == 2
    assert fila["total"] == pytest.approx(360.0)
    assert fila["ganancia"] == pytest.approx(160.0)
    assert fila["fecha"]


def test_obtener_ventas_historial_bd_vacia(conn: sqlite3.Connection) -> None:
    # Arrange: conexion recien inicializada
    # Act
    historial = core_ventas.obtener_ventas_historial(conn)

    # Assert
    assert historial == []


@pytest.mark.parametrize("cuantos", [1, 2, 5, 50])
def test_marcadores_solo_produce_interrogaciones(cuantos: int) -> None:
    """`_marcadores` es la unica parte del SQL armada en ejecucion (R12).

    La auditoria por AST mira el tipo del nodo en el call site, asi que no
    detectaria una f-string construida en un local y pasada despues. Este test
    fija el invariante en la fuente: pase lo que pase, esta funcion solo puede
    emitir marcadores y comas, nunca un dato.
    """
    # Act
    salida = core_ventas._marcadores(cuantos)

    # Assert
    assert set(salida) <= {"?", ",", " "}
    assert salida.count("?") == cuantos
