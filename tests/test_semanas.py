"""Suite de la semana de catalogo (core_semanas + su cableado en guardar_pedido).

La fixture levanta el esquema real con `db.init_db(":memory:")`, de modo que el
`UNIQUE(semana_texto)` de `semanas_catalogo` y la FK `pedidos.semana_id` son los
de produccion y se ejercitan de verdad -- no se simulan con dobles.

Las filas de PDF se construyen con el helper `fila_pdf` que ya mantiene
`tests/test_core_pedidos.py`: la forma del registro del extractor tiene una sola
definicion en la suite.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any, Final

import pytest

import core_pedidos
import core_semanas
import db
from tests.test_core_pedidos import FOLIO_A, FOLIO_B, fila_pdf

SEMANA_A: Final[str] = "30 - 2026"
SEMANA_B: Final[str] = "31 - 2026"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def fila_con_semana(semana: str, **kwargs: Any) -> dict[str, Any]:
    """Fila del extractor con la semana sobreescrita.

    `fila_pdf` fija la semana a un valor de ejemplo; esta suite necesita
    controlarla caso a caso sin duplicar el resto del registro.
    """
    fila = fila_pdf(**kwargs)
    fila["Semana"] = semana
    return fila


def filas_semanas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Contenido completo de `semanas_catalogo` ordenado por id."""
    return conn.execute(
        "SELECT id, semana_texto, numero_semana, anio FROM semanas_catalogo "
        "ORDER BY id"
    ).fetchall()


def semana_de_folio(conn: sqlite3.Connection, folio: str) -> sqlite3.Row:
    """Cabecera del folio junto al texto de la semana a la que quedo ligada."""
    return conn.execute(
        "SELECT p.semana_id AS semana_id, sc.semana_texto AS semana_texto "
        "FROM pedidos p "
        "LEFT JOIN semanas_catalogo sc ON sc.id = p.semana_id "
        "WHERE p.folio_pedido = ?",
        (folio,),
    ).fetchone()


# --------------------------------------------------------------------------
# T1 / R1 - parseo del texto de la semana
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("30 - 2026", (30, 2026)),
        ("30-2026", (30, 2026)),
        ("  30   -   2026  ", (30, 2026)),
        ("Semana 30 - 2026", (30, 2026)),
        ("1 - 2026", (1, 2026)),
        ("52 - 2025", (52, 2025)),
    ],
)
def test_parsear_semana_extracts_numero_and_anio(
    texto: str, esperado: tuple[int, int]
) -> None:
    """R1: el par (numero, anio) sale del texto y tolera el espaciado del PDF."""
    resultado = core_semanas._parsear_semana(texto)

    assert resultado == esperado


@pytest.mark.parametrize("texto", ["", "   ", "\t\n", None])
def test_parsear_semana_blank_returns_none_pair(texto: str | None) -> None:
    """R1: sin texto util no hay semana que parsear, y la funcion no lanza."""
    resultado = core_semanas._parsear_semana(texto)

    assert resultado == (None, None)


@pytest.mark.parametrize("texto", ["foo", "30 - 26", "2026", "- -", "semana"])
def test_parsear_semana_malformed_returns_none_pair(texto: str) -> None:
    """R1: un texto que no case con el patron degrada a `(None, None)`, sin lanzar."""
    resultado = core_semanas._parsear_semana(texto)

    assert resultado == (None, None)


# --------------------------------------------------------------------------
# T2 / R2, R3, R4, R6 - upsert de la semana
# --------------------------------------------------------------------------
def test_obtener_o_crear_semana_creates_new_with_parsed_columns(
    conn: sqlite3.Connection,
) -> None:
    """R2: la semana inedita se da de alta con su numero y anio ya derivados."""
    semana_id = core_semanas.obtener_o_crear_semana(conn, SEMANA_A)

    filas = filas_semanas(conn)
    assert len(filas) == 1
    assert filas[0]["id"] == semana_id
    assert filas[0]["semana_texto"] == SEMANA_A
    assert filas[0]["numero_semana"] == 30
    assert filas[0]["anio"] == 2026


def test_obtener_o_crear_semana_same_twice_returns_same_id_one_row(
    conn: sqlite3.Connection,
) -> None:
    """R3: el mismo texto es idempotente sobre `UNIQUE(semana_texto)`."""
    primero = core_semanas.obtener_o_crear_semana(conn, SEMANA_A)

    segundo = core_semanas.obtener_o_crear_semana(conn, SEMANA_A)

    assert primero == segundo
    assert len(filas_semanas(conn)) == 1


@pytest.mark.parametrize("texto", ["", "   ", None])
def test_obtener_o_crear_semana_blank_returns_none_no_row(
    conn: sqlite3.Connection, texto: str | None
) -> None:
    """R4: una semana en blanco devuelve `None` y no toca la base."""
    resultado = core_semanas.obtener_o_crear_semana(conn, texto)

    assert resultado is None
    assert filas_semanas(conn) == []


def test_obtener_o_crear_semana_malformed_stores_row_null_columns(
    conn: sqlite3.Connection,
) -> None:
    """R6: un texto no parseable se persiste igual, con las columnas en NULL."""
    semana_id = core_semanas.obtener_o_crear_semana(conn, "semana rara")

    filas = filas_semanas(conn)
    assert len(filas) == 1
    assert filas[0]["id"] == semana_id
    assert filas[0]["semana_texto"] == "semana rara"
    assert filas[0]["numero_semana"] is None
    assert filas[0]["anio"] is None


# --------------------------------------------------------------------------
# T3 / R5 - cableado en la cabecera del pedido
# --------------------------------------------------------------------------
def test_guardar_pedido_sets_semana_id_from_meta(conn: sqlite3.Connection) -> None:
    """R5: la cabecera queda ligada a la fila de `semanas_catalogo` de su texto."""
    pedido_id = core_pedidos.guardar_pedido(conn, fila_con_semana(SEMANA_A))

    fila = conn.execute(
        "SELECT semana_id FROM pedidos WHERE id = ?", (pedido_id,)
    ).fetchone()
    filas = filas_semanas(conn)
    assert len(filas) == 1
    assert fila["semana_id"] == filas[0]["id"]


def test_guardar_pedido_blank_semana_leaves_semana_id_null(
    conn: sqlite3.Connection,
) -> None:
    """R5: sin semana en la nota la FK sigue siendo nullable y no se crea fila."""
    pedido_id = core_pedidos.guardar_pedido(conn, fila_con_semana(""))

    fila = conn.execute(
        "SELECT semana_id FROM pedidos WHERE id = ?", (pedido_id,)
    ).fetchone()
    assert fila["semana_id"] is None
    assert filas_semanas(conn) == []


# --------------------------------------------------------------------------
# T4 / R5, R7 - integracion a traves del orquestador transaccional
# --------------------------------------------------------------------------
def test_confirmar_carga_links_pedido_to_week(conn: sqlite3.Connection) -> None:
    """R5: el lote confirmado deja cada pedido apuntando al texto de su semana."""
    core_pedidos.confirmar_carga(conn, [fila_con_semana(SEMANA_A)])

    fila = semana_de_folio(conn, FOLIO_A)
    assert fila["semana_id"] is not None
    assert fila["semana_texto"] == SEMANA_A


def test_two_pedidos_same_week_share_one_semana_row(
    conn: sqlite3.Connection,
) -> None:
    """R3, R5: dos folios de la misma semana comparten exactamente una fila."""
    filas = [
        fila_con_semana(SEMANA_A, folio=FOLIO_A, codigo="11111"),
        fila_con_semana(SEMANA_A, folio=FOLIO_B, codigo="22222", descripcion="Vaso"),
    ]

    core_pedidos.confirmar_carga(conn, filas)

    assert len(filas_semanas(conn)) == 1
    primero = semana_de_folio(conn, FOLIO_A)
    segundo = semana_de_folio(conn, FOLIO_B)
    assert primero["semana_id"] == segundo["semana_id"]
    assert primero["semana_texto"] == SEMANA_A


def test_semanas_distintas_crean_una_fila_cada_una(
    conn: sqlite3.Connection,
) -> None:
    """R2: dos semanas distintas en el mismo lote son dos filas de catalogo."""
    filas = [
        fila_con_semana(SEMANA_A, folio=FOLIO_A, codigo="11111"),
        fila_con_semana(SEMANA_B, folio=FOLIO_B, codigo="22222", descripcion="Vaso"),
    ]

    core_pedidos.confirmar_carga(conn, filas)

    textos = [fila["semana_texto"] for fila in filas_semanas(conn)]
    assert textos == [SEMANA_A, SEMANA_B]
    assert semana_de_folio(conn, FOLIO_A)["semana_texto"] == SEMANA_A
    assert semana_de_folio(conn, FOLIO_B)["semana_texto"] == SEMANA_B


def test_semana_upsert_rolls_back_with_batch_on_failure(
    conn: sqlite3.Connection,
) -> None:
    """R7: el alta de la semana vive dentro del `with conn:` del orquestador.

    El segundo folio trae un reparto desbalanceado, que el CHECK del esquema
    rechaza: si `obtener_o_crear_semana` hiciera commit por su cuenta, la fila
    del primer folio sobreviviria al rollback del lote.
    """
    filas = [
        fila_con_semana(SEMANA_A, folio=FOLIO_A, codigo="11111"),
        fila_con_semana(
            SEMANA_B, folio=FOLIO_B, codigo="22222", surtida=5, casa=2
        ),
    ]

    with pytest.raises(core_pedidos.CargaError):
        core_pedidos.confirmar_carga(conn, filas)

    assert filas_semanas(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM pedidos").fetchone()["n"] == 0
