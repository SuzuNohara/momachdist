"""Suite del catalogo de productos (core.upsert_* / core.obtener_catalogo).

La fixture levanta el esquema real desde `db.init_db(":memory:")`: las pruebas
corren contra las mismas constraints que produccion, no contra un CREATE TABLE
simplificado.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

import core
import db

FECHA_SENTINELA: str = "2000-01-01 00:00:00"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def fila_pdf(
    codigo: str,
    descripcion: str,
    precio_pagas: float = 199.0,
    valor_total: float = 230.84,
    precio_catalogo: float = 249.0,
) -> dict[str, Any]:
    """Construye una fila con las claves que entrega el extractor de PDF."""
    return {
        "Codigo articulo": codigo,
        "Descripcion": descripcion,
        "Precio catalogo": precio_catalogo,
        "Precio que pagas": precio_pagas,
        "Valor total con IVA": valor_total,
    }


def contar_productos(conn: sqlite3.Connection) -> int:
    """Numero de filas en `productos`."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM productos").fetchone()["n"])


def leer_producto(conn: sqlite3.Connection, codigo: str) -> sqlite3.Row:
    """Fila completa de `productos` para `codigo`."""
    row = conn.execute(
        "SELECT * FROM productos WHERE codigo_articulo = ?", (codigo,)
    ).fetchone()
    assert row is not None
    return row


# --------------------------------------------------------------------------
# R1 - alta de codigos nuevos
# --------------------------------------------------------------------------
def test_upsert_productos_inserta_codigo_nuevo(conn: sqlite3.Connection) -> None:
    """Un codigo inedito crea exactamente una fila con su descripcion."""
    filas = [fila_pdf("11111", "Sarten antiadherente 24cm")]

    procesados = core.upsert_productos(conn, filas)

    assert procesados == 1
    assert contar_productos(conn) == 1
    assert leer_producto(conn, "11111")["descripcion"] == "Sarten antiadherente 24cm"


# --------------------------------------------------------------------------
# R2 - idempotencia y preservacion de fecha_creacion
# --------------------------------------------------------------------------
def test_upsert_productos_mismo_codigo_no_duplica(conn: sqlite3.Connection) -> None:
    """Reprocesar el mismo codigo no duplica filas ni reescribe fecha_creacion."""
    core.upsert_productos(conn, [fila_pdf("22222", "Juego de vasos")])
    conn.execute(
        "UPDATE productos SET fecha_creacion = ? WHERE codigo_articulo = ?",
        (FECHA_SENTINELA, "22222"),
    )
    conn.commit()

    core.upsert_productos(conn, [fila_pdf("22222", "Juego de vasos")])

    assert contar_productos(conn) == 1
    assert leer_producto(conn, "22222")["fecha_creacion"] == FECHA_SENTINELA


def test_upsert_productos_conserva_fecha_creacion_al_actualizar(
    conn: sqlite3.Connection,
) -> None:
    """El DO UPDATE cambia la descripcion pero nunca la fecha_creacion."""
    core.upsert_productos(conn, [fila_pdf("22223", "Nombre viejo")])
    conn.execute(
        "UPDATE productos SET fecha_creacion = ? WHERE codigo_articulo = ?",
        (FECHA_SENTINELA, "22223"),
    )
    conn.commit()

    core.upsert_productos(conn, [fila_pdf("22223", "Nombre nuevo")])

    fila = leer_producto(conn, "22223")
    assert fila["descripcion"] == "Nombre nuevo"
    assert fila["fecha_creacion"] == FECHA_SENTINELA


# --------------------------------------------------------------------------
# R3 - actualizacion de descripcion y no-op
# --------------------------------------------------------------------------
def test_upsert_productos_actualiza_descripcion_cambiada(
    conn: sqlite3.Connection,
) -> None:
    """Una descripcion distinta se propaga a la fila existente."""
    core.upsert_productos(conn, [fila_pdf("33333", "Descripcion original")])

    core.upsert_productos(conn, [fila_pdf("33333", "Descripcion corregida")])

    assert contar_productos(conn) == 1
    assert leer_producto(conn, "33333")["descripcion"] == "Descripcion corregida"


def test_upsert_productos_descripcion_identica_es_no_op(
    conn: sqlite3.Connection,
) -> None:
    """Una carga identica no modifica ninguna fila (WHERE anti no-op)."""
    core.upsert_productos(conn, [fila_pdf("33334", "Sin cambios")])
    antes = dict(leer_producto(conn, "33334"))
    cambios_antes = conn.total_changes

    core.upsert_productos(conn, [fila_pdf("33334", "Sin cambios")])

    assert dict(leer_producto(conn, "33334")) == antes
    assert conn.total_changes == cambios_antes


# --------------------------------------------------------------------------
# R4 - mapeo de claves del PDF
# --------------------------------------------------------------------------
def test_upsert_producto_mapea_claves_pdf(conn: sqlite3.Connection) -> None:
    """Las claves del PDF se mapean a columnas y se recortan los espacios."""
    fila = fila_pdf("  44444  ", "   Set de tuppers 5 piezas   ")

    core.upsert_producto(conn, fila)

    guardado = leer_producto(conn, "44444")
    assert guardado["descripcion"] == "Set de tuppers 5 piezas"
    assert guardado["categoria"] is None


@pytest.mark.parametrize(
    "fila",
    [
        {"Descripcion": "Sin codigo"},
        {"Codigo articulo": "   ", "Descripcion": "Codigo en blanco"},
        {"Codigo articulo": "55555"},
    ],
)
def test_upsert_producto_rechaza_fila_incompleta(
    conn: sqlite3.Connection, fila: dict[str, Any]
) -> None:
    """Faltar codigo o descripcion es un error de dominio, no un INSERT roto."""
    with pytest.raises(core.CoreError):
        core.upsert_producto(conn, fila)

    assert contar_productos(conn) == 0


# --------------------------------------------------------------------------
# R5 - flag de regalo/promocion
# --------------------------------------------------------------------------
def test_upsert_producto_regalo_marca_flag(conn: sqlite3.Connection) -> None:
    """Precio que pagas == 0 y valor total == 0 marcan es_regalo_o_promo = 1."""
    fila = fila_pdf("66666", "Regalo por compra", precio_pagas=0, valor_total=0)

    core.upsert_producto(conn, fila)

    assert leer_producto(conn, "66666")["es_regalo_o_promo"] == 1


def test_upsert_producto_regalo_no_se_degrada(conn: sqlite3.Connection) -> None:
    """Un upsert normal posterior no baja a 0 un flag ya marcado en 1."""
    core.upsert_producto(
        conn, fila_pdf("66667", "Regalo", precio_pagas=0, valor_total=0)
    )

    core.upsert_producto(conn, fila_pdf("66667", "Ahora se vende", precio_pagas=99.0))

    fila = leer_producto(conn, "66667")
    assert fila["es_regalo_o_promo"] == 1
    assert fila["descripcion"] == "Ahora se vende"


@pytest.mark.parametrize(
    ("precio_pagas", "valor_total", "esperado"),
    [
        (0, 0, 1),
        (0.0, 0.0, 1),
        ("0", "0", 1),
        (0, 230.84, 0),
        (199.0, 0, 0),
        (199.0, 230.84, 0),
        (None, None, 0),
        ("gratis", "gratis", 0),
        (False, False, 0),
    ],
)
def test_upsert_producto_flag_regalo_por_combinacion_de_precios(
    conn: sqlite3.Connection,
    precio_pagas: Any,
    valor_total: Any,
    esperado: int,
) -> None:
    """Solo la combinacion de ambos precios en 0 activa el flag."""
    fila = fila_pdf(
        "77777", "Articulo", precio_pagas=precio_pagas, valor_total=valor_total
    )

    core.upsert_producto(conn, fila)

    assert leer_producto(conn, "77777")["es_regalo_o_promo"] == esperado


def test_upsert_producto_regalo_ignora_precio_catalogo(
    conn: sqlite3.Connection,
) -> None:
    """Un precio de catalogo distinto de 0 no impide marcar el regalo (R5)."""
    fila = fila_pdf(
        "77778",
        "Regalo con catalogo no cero",
        precio_pagas=0,
        valor_total=0,
        precio_catalogo=349.0,
    )

    core.upsert_producto(conn, fila)

    assert leer_producto(conn, "77778")["es_regalo_o_promo"] == 1


# --------------------------------------------------------------------------
# R5 a traves del lote — el dedup no puede perder el flag de regalo
#
# Los tests de arriba entran por `upsert_producto` (singular) y por eso no
# ejercitan el dedup de `upsert_productos`. La primera clausula de R5 ("cuando
# una fila representa un regalo, fijar es_regalo_o_promo = 1") se rompia de
# forma silenciosa y dependiente del orden: con dedup last-wins, una fila de
# regalo que no fuera la ultima aparicion de su codigo se descartaba antes de
# llegar al SQL, y el flag nunca se fijaba.
# --------------------------------------------------------------------------


def test_upsert_productos_regalo_primero_conserva_flag(
    conn: sqlite3.Connection,
) -> None:
    """Regalo primero, normal despues, mismo lote y codigo nuevo (R5)."""
    lote = [
        fila_pdf("70001", "Version regalo", precio_pagas=0, valor_total=0),
        fila_pdf("70001", "Version normal"),
    ]

    core.upsert_productos(conn, lote)

    fila = leer_producto(conn, "70001")
    assert fila["es_regalo_o_promo"] == 1
    assert fila["descripcion"] == "Version normal"


def test_upsert_productos_regalo_al_final_conserva_flag(
    conn: sqlite3.Connection,
) -> None:
    """El orden inverso da el mismo resultado — R5 no depende del orden."""
    lote = [
        fila_pdf("70002", "Version normal"),
        fila_pdf("70002", "Version regalo", precio_pagas=0, valor_total=0),
    ]

    core.upsert_productos(conn, lote)

    fila = leer_producto(conn, "70002")
    assert fila["es_regalo_o_promo"] == 1
    assert fila["descripcion"] == "Version regalo"


def test_upsert_productos_lote_sin_regalo_no_marca_flag(
    conn: sqlite3.Connection,
) -> None:
    """Contraparte: fusionar el flag no debe marcar regalos inexistentes."""
    lote = [fila_pdf("70003", "Primera"), fila_pdf("70003", "Segunda")]

    core.upsert_productos(conn, lote)

    assert leer_producto(conn, "70003")["es_regalo_o_promo"] == 0


def test_upsert_productos_regalo_en_lote_no_se_degrada_despues(
    conn: sqlite3.Connection,
) -> None:
    """El flag fijado por un lote sobrevive a una carga normal posterior (R5)."""
    core.upsert_productos(
        conn,
        [fila_pdf("70004", "Regalo", precio_pagas=0, valor_total=0)],
    )

    core.upsert_productos(conn, [fila_pdf("70004", "Ya no es regalo")])

    fila = leer_producto(conn, "70004")
    assert fila["es_regalo_o_promo"] == 1
    assert fila["descripcion"] == "Ya no es regalo"


# --------------------------------------------------------------------------
# R6 - lectura del catalogo
# --------------------------------------------------------------------------
def test_obtener_catalogo_devuelve_ordenado(conn: sqlite3.Connection) -> None:
    """Tres productos vuelven como tres dicts ordenados por codigo."""
    core.upsert_productos(
        conn,
        [
            fila_pdf("30003", "Tercero"),
            fila_pdf("10001", "Primero"),
            fila_pdf("20002", "Segundo"),
        ],
    )

    catalogo = core.obtener_catalogo(conn)

    assert [item["codigo_articulo"] for item in catalogo] == [
        "10001",
        "20002",
        "30003",
    ]
    assert all(isinstance(item, dict) for item in catalogo)
    assert set(catalogo[0]) == {
        "codigo_articulo",
        "descripcion",
        "categoria",
        "es_regalo_o_promo",
        "fecha_creacion",
    }


def test_obtener_catalogo_vacio_devuelve_lista_vacia(conn: sqlite3.Connection) -> None:
    """Sin productos registrados el catalogo es una lista vacia."""
    catalogo = core.obtener_catalogo(conn)

    assert catalogo == []


# --------------------------------------------------------------------------
# R7 - conteo de distintos, transaccion y SQL parametrizado
# --------------------------------------------------------------------------
def test_upsert_productos_retorna_conteo_distintos(conn: sqlite3.Connection) -> None:
    """Un lote con repetidos devuelve el numero de codigos distintos."""
    filas = [
        fila_pdf("80001", "Alfa"),
        fila_pdf("80002", "Beta"),
        fila_pdf("80001", "Alfa corregido"),
    ]

    procesados = core.upsert_productos(conn, filas)

    assert procesados == 2
    assert contar_productos(conn) == 2
    assert leer_producto(conn, "80001")["descripcion"] == "Alfa corregido"


def test_upsert_productos_lote_vacio_devuelve_cero(conn: sqlite3.Connection) -> None:
    """Un lote sin filas no escribe nada y devuelve 0."""
    procesados = core.upsert_productos(conn, [])

    assert procesados == 0
    assert contar_productos(conn) == 0


def test_upsert_productos_error_a_mitad_no_deja_filas_parciales(
    conn: sqlite3.Connection,
) -> None:
    """La transaccion es atomica: una fila invalida revierte todo el lote."""
    filas = [
        fila_pdf("90001", "Valida"),
        {"Codigo articulo": "90002"},
    ]

    with pytest.raises(core.CoreError):
        core.upsert_productos(conn, filas)

    assert contar_productos(conn) == 0


def test_upsert_producto_envuelve_error_de_sqlite(conn: sqlite3.Connection) -> None:
    """Un fallo de sqlite3 sale como CoreError, no como error de bajo nivel."""
    conn.close()

    with pytest.raises(core.CoreError):
        core.upsert_producto(conn, fila_pdf("99999", "Conexion cerrada"))


def test_obtener_catalogo_envuelve_error_de_sqlite(conn: sqlite3.Connection) -> None:
    """La lectura tambien traduce el error de sqlite3 a error de dominio."""
    conn.close()

    with pytest.raises(core.CoreError):
        core.obtener_catalogo(conn)


def test_upsert_producto_sql_es_parametrizado() -> None:
    """El SQL usa placeholders `?` y no interpolacion de cadenas."""
    sql = core.UPSERT_PRODUCTO_SQL

    assert "VALUES (?, ?, ?)" in sql
    assert "{" not in sql and "%" not in sql
    assert "{" not in core.SELECT_CATALOGO_SQL
