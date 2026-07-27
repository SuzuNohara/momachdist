"""Suite del directorio de asociados (`core.obtener_o_crear_asociado`).

La fixture levanta el esquema real con `db.init_db(":memory:")`: la tabla
`asociados`, su `NOT NULL` sobre `nombre` y la FK de `pedido_detalle` son las de
produccion, no una version simplificada. Asi los tests de cableado comprueban de
verdad que el id que se guarda existe.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

import core
import db

TIPO_NORMAL: str = "Normal (con descuento)"

FOLIO_A: str = "C001264"
FOLIO_B: str = "C001265"

NOMBRE_A: str = "Aura Jannet Ramirez"
NOMBRE_B: str = "Etnan Gamaliel Perez"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def fila_pdf(
    *,
    folio: str = FOLIO_A,
    codigo: str = "11111",
    nombre_asociado: str = NOMBRE_A,
    surtida: int = 3,
    asociado: int = 0,
    casa: int = 3,
    local: int = 0,
) -> dict[str, Any]:
    """Fila con las claves que entrega `pdf_extractor.procesar_pdf`."""
    return {
        "Fecha registro": "2026-07-22 09:00",
        "Semana": "01-2026",
        "Folio de pedido": folio,
        "Codigo nota": "8043",
        "Distribuidora": "C0001 DISTRIBUIDORA CENTRO",
        "Nombre asociado": nombre_asociado,
        "Archivo origen": f"{folio}_NOTA.pdf (pag. 1)",
        "Codigo articulo": codigo,
        "Descripcion": "Sarten antiadherente 24cm",
        "Cantidad solicitada": surtida,
        "Cantidad surtida": surtida,
        "Cantidad Asociado": asociado,
        "Cantidad Casa": casa,
        "Cantidad Local": local,
        "Precio catalogo": 249.0,
        "Precio con IVA": 288.84,
        "Precio que pagas": 199.0,
        "Valor total con IVA": 866.52,
        "Tipo": TIPO_NORMAL,
    }


def contar_asociados(conn: sqlite3.Connection) -> int:
    """Filas actuales en `asociados`."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM asociados").fetchone()["n"])


def asociados_de(conn: sqlite3.Connection, folio: str) -> set[Any]:
    """`asociado_id` distintos guardados en el detalle de un folio."""
    filas = conn.execute(
        """
        SELECT d.asociado_id AS asociado_id
        FROM pedido_detalle d
        JOIN pedidos p ON p.id = d.pedido_id
        WHERE p.folio_pedido = ?
        """,
        (folio,),
    ).fetchall()
    return {fila["asociado_id"] for fila in filas}


# --------------------------------------------------------------------------
# T1 -- normalizacion del nombre (R3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("  Aura   Jannet  ", "Aura Jannet"),
        ("Aura Jannet", "Aura Jannet"),
        ("Aura\tJannet", "Aura Jannet"),
        ("Aura\nJannet", "Aura Jannet"),
        ("Aura  Jannet  Ramirez", "Aura Jannet Ramirez"),
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("\t\n ", ""),
    ],
)
def test_normalizar_nombre_colapsa_espacios_y_recorta(
    crudo: str | None, esperado: str
) -> None:
    """R3: el espaciado del PDF no debe generar identidades distintas."""
    # Arrange / Act
    resultado = core._normalizar_nombre(crudo)

    # Assert
    assert resultado == esperado


def test_normalizar_nombre_conserva_la_capitalizacion_original() -> None:
    """La normalizacion no destruye informacion: quien ignora el caso es SQL."""
    # Arrange / Act
    resultado = core._normalizar_nombre("  AURA   jannet ")

    # Assert
    assert resultado == "AURA jannet"


# --------------------------------------------------------------------------
# T2 -- alta y reuso (R1, R2)
# --------------------------------------------------------------------------


def test_obtener_o_crear_asociado_nombre_nuevo_crea_una_fila(
    conn: sqlite3.Connection,
) -> None:
    """R2: un nombre desconocido se da de alta y devuelve su id."""
    # Arrange
    assert contar_asociados(conn) == 0

    # Act
    asociado_id = core.obtener_o_crear_asociado(conn, NOMBRE_A)

    # Assert
    assert isinstance(asociado_id, int)
    assert contar_asociados(conn) == 1
    fila = conn.execute(
        "SELECT nombre FROM asociados WHERE id = ?", (asociado_id,)
    ).fetchone()
    assert fila["nombre"] == NOMBRE_A


def test_obtener_o_crear_asociado_existente_devuelve_mismo_id(
    conn: sqlite3.Connection,
) -> None:
    """R1: el segundo llamado reutiliza la fila, no crea una nueva."""
    # Arrange
    primero = core.obtener_o_crear_asociado(conn, NOMBRE_A)

    # Act
    segundo = core.obtener_o_crear_asociado(conn, NOMBRE_A)

    # Assert
    assert segundo == primero
    assert contar_asociados(conn) == 1


@pytest.mark.parametrize(
    "variante",
    [
        "aura jannet ramirez",
        "AURA JANNET RAMIREZ",
        "  Aura Jannet Ramirez  ",
        "Aura   Jannet   Ramirez",
        "\tAura Jannet\nRamirez ",
    ],
)
def test_obtener_o_crear_asociado_reusa_id_pese_a_espacios_y_mayusculas(
    conn: sqlite3.Connection, variante: str
) -> None:
    """R3: mismo asociado escrito distinto no debe duplicarse."""
    # Arrange
    original = core.obtener_o_crear_asociado(conn, NOMBRE_A)

    # Act
    repetido = core.obtener_o_crear_asociado(conn, variante)

    # Assert
    assert repetido == original
    assert contar_asociados(conn) == 1


def test_obtener_o_crear_asociado_guarda_el_nombre_normalizado(
    conn: sqlite3.Connection,
) -> None:
    """El alta persiste el nombre ya recortado y con espacios colapsados."""
    # Arrange / Act
    asociado_id = core.obtener_o_crear_asociado(conn, "   Aura    Jannet   ")

    # Assert
    fila = conn.execute(
        "SELECT nombre FROM asociados WHERE id = ?", (asociado_id,)
    ).fetchone()
    assert fila["nombre"] == "Aura Jannet"


def test_obtener_o_crear_asociado_distingue_personas_distintas(
    conn: sqlite3.Connection,
) -> None:
    """R2: dos nombres realmente distintos son dos asociados distintos."""
    # Arrange / Act
    primero = core.obtener_o_crear_asociado(conn, NOMBRE_A)
    segundo = core.obtener_o_crear_asociado(conn, NOMBRE_B)

    # Assert
    assert primero != segundo
    assert contar_asociados(conn) == 2


# --------------------------------------------------------------------------
# T3 -- guarda de nombre en blanco (R4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blanco", [None, "", "   ", "\t", "\n  \n"])
def test_obtener_o_crear_asociado_nombre_vacio_devuelve_none_sin_insertar(
    conn: sqlite3.Connection, blanco: str | None
) -> None:
    """R4: sin nombre no hay asociado, y tampoco fila fantasma en la tabla."""
    # Arrange / Act
    resultado = core.obtener_o_crear_asociado(conn, blanco)

    # Assert
    assert resultado is None
    assert contar_asociados(conn) == 0


# --------------------------------------------------------------------------
# T4 -- cableado por nota en `confirmar_carga` (R5, R6)
# --------------------------------------------------------------------------


def test_confirmar_carga_asigna_asociado_por_nota(
    conn: sqlite3.Connection,
) -> None:
    """R5: todas las lineas de una nota apuntan al asociado de esa nota."""
    # Arrange
    filas = [
        fila_pdf(codigo="11111"),
        fila_pdf(codigo="22222"),
    ]

    # Act
    core.confirmar_carga(conn, filas)

    # Assert
    esperado = core.obtener_o_crear_asociado(conn, NOMBRE_A)
    assert asociados_de(conn, FOLIO_A) == {esperado}
    assert contar_asociados(conn) == 1


def test_dos_notas_dos_asociados(conn: sqlite3.Connection) -> None:
    """R6: la resolucion es por nota, no por lote ni por PDF."""
    # Arrange
    filas = [
        fila_pdf(folio=FOLIO_A, codigo="11111", nombre_asociado=NOMBRE_A),
        fila_pdf(folio=FOLIO_B, codigo="22222", nombre_asociado=NOMBRE_B),
    ]

    # Act
    core.confirmar_carga(conn, filas)

    # Assert
    ids_a = asociados_de(conn, FOLIO_A)
    ids_b = asociados_de(conn, FOLIO_B)
    assert len(ids_a) == 1
    assert len(ids_b) == 1
    assert ids_a != ids_b
    assert contar_asociados(conn) == 2


def test_dos_notas_mismo_nombre_un_asociado(conn: sqlite3.Connection) -> None:
    """R3 + R6: dos notas con el mismo nombre comparten una unica fila."""
    # Arrange
    filas = [
        fila_pdf(folio=FOLIO_A, codigo="11111", nombre_asociado="  AURA  jannet "),
        fila_pdf(folio=FOLIO_B, codigo="22222", nombre_asociado="Aura Jannet"),
    ]

    # Act
    core.confirmar_carga(conn, filas)

    # Assert
    assert asociados_de(conn, FOLIO_A) == asociados_de(conn, FOLIO_B)
    assert contar_asociados(conn) == 1


def test_confirmar_carga_deja_asociado_id_null_si_la_nota_no_trae_nombre(
    conn: sqlite3.Connection,
) -> None:
    """R4 visto desde el orquestador: nota sin nombre -> detalle en NULL."""
    # Arrange
    filas = [
        fila_pdf(folio=FOLIO_A, codigo="11111", nombre_asociado=NOMBRE_A),
        fila_pdf(folio=FOLIO_B, codigo="22222", nombre_asociado="   "),
    ]

    # Act
    core.confirmar_carga(conn, filas)

    # Assert
    assert asociados_de(conn, FOLIO_B) == {None}
    assert asociados_de(conn, FOLIO_A) != {None}
    assert contar_asociados(conn) == 1


def test_confirmar_carga_no_duplica_asociado_al_reprocesar_el_mismo_pdf(
    conn: sqlite3.Connection,
) -> None:
    """Recargar la misma nota reutiliza el asociado ya dado de alta."""
    # Arrange
    filas = [fila_pdf()]
    core.confirmar_carga(conn, filas)
    ids_primera_carga = asociados_de(conn, FOLIO_A)

    # Act
    core.confirmar_carga(conn, filas)

    # Assert
    assert asociados_de(conn, FOLIO_A) == ids_primera_carga
    assert contar_asociados(conn) == 1


def test_confirmar_carga_revierte_el_alta_del_asociado_si_falla_el_detalle(
    conn: sqlite3.Connection,
) -> None:
    """El alta vive en la transaccion unica de MERC-01: todo o nada."""
    # Arrange: reparto invalido -> el CHECK de `pedido_detalle` aborta la carga.
    filas = [fila_pdf(surtida=5, asociado=0, casa=0, local=0)]

    # Act / Assert
    with pytest.raises(core.CargaError):
        core.confirmar_carga(conn, filas)
    assert contar_asociados(conn) == 0
