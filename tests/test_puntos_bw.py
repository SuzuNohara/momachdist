"""Suite de los puntos Betterware (BW-02): extraccion + persistencia por semana.

Tres ejes:

1. **Extraccion pura** (`pdf_extractor`) -- el texto de la nota entra, `int | None`
   sale, y nunca se lanza.
2. **Persistencia** (`core_semanas`) -- los puntos aterrizan en la semana de
   *cierre*, no en la del pedido, bajo la semantica de maximo de R6.
3. **Aislamiento de capa** (R9) -- `pdf_extractor` sigue sin tocar `pandas` /
   `openpyxl`.

La fixture levanta el esquema real con `db.init_db(":memory:")`, igual que
`tests/test_semanas.py` (BW-01), de modo que `UNIQUE(semana_texto)` y el
`DEFAULT 0` de `puntos_bw_acumulados` son los de produccion.

`reference/C001264_NOTA.pdf` se usa como entrada real siempre que se puede: es
la nota que motivo la decision de negocio de R6 (cuatro cortes distintos del
mismo cierre 29).
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Final

import pdfplumber
import pytest

import core_semanas
import db
import pdf_extractor
from tests.baseline_util import SAMPLE_PDF

PROJECT_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent
EXTRACTOR_PATH: Final[pathlib.Path] = PROJECT_ROOT / "pdf_extractor.py"

#: Funciones que BW-02 anade a `pdf_extractor`; el resto del modulo es el bloque
#: verbatim de FUND-03 y no se toca.
FUNCIONES_BW: Final[tuple[str, ...]] = (
    "extraer_puntos_bw",
    "extraer_semana_cierre_bw",
    "extraer_puntos_de_paginas",
)

#: Tokens que delatarian que la extraccion arrastro la capa de reportes (R9).
TOKENS_ALMACENAMIENTO: Final[tuple[str, ...]] = (
    "pandas",
    "openpyxl",
    "DataFrame",
    "read_excel",
    "ExcelWriter",
    "load_workbook",
)

SEMANA_PEDIDO: Final[str] = "27 - 2026"
SEMANA_CIERRE: Final[str] = "29 - 2026"
SEMANA_AJENA: Final[str] = "40 - 2026"
ANIO: Final[int] = 2026

#: Los cinco `Total PB acumulados` de `C001264_NOTA.pdf`, todos del cierre 29.
#: Son cortes distintos de un mismo corrido de temporada: el valor real de la
#: semana es el maximo (decision de negocio 2026-07-27 que reemplaza R6).
#: La quinta nota reporta `0`, que es un valor real y **no** una ausencia.
PUNTOS_MUESTRA: Final[tuple[int, ...]] = (20003, 6428, 22272, 8777, 0)
PUNTOS_MAXIMO: Final[int] = 22272

TEXTO_CON_PUNTOS: Final[str] = (
    "MIS PUNTOS BW al cierre de semana 29\n"
    "Total PB acumulados 20,003\n"
    "Puntos por facturacion 1,250"
)
TEXTO_SIN_PUNTOS: Final[str] = (
    "Semana 30 - 2026 Folio de pedido C001264\n"
    "Articulo Descripcion Cantidad\n"
    "1234567 Vaso termico 2"
)


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def puntos_de(conn: sqlite3.Connection, semana_texto: str) -> int | None:
    """Puntos almacenados en la semana cuyo texto es `semana_texto`."""
    fila = conn.execute(
        "SELECT puntos_bw_acumulados FROM semanas_catalogo WHERE semana_texto = ?",
        (semana_texto,),
    ).fetchone()
    return None if fila is None else fila["puntos_bw_acumulados"]


def leer_puntos(conn: sqlite3.Connection, semana_id: int) -> int | None:
    """Puntos almacenados en la semana con id `semana_id`."""
    fila = conn.execute(
        "SELECT puntos_bw_acumulados FROM semanas_catalogo WHERE id = ?",
        (semana_id,),
    ).fetchone()
    return None if fila is None else fila["puntos_bw_acumulados"]


def semana_nueva(conn: sqlite3.Connection, semana_texto: str) -> int:
    """Da de alta la semana y devuelve su id, fallando si BW-01 devuelve `None`."""
    semana_id = core_semanas.obtener_o_crear_semana(conn, semana_texto)
    assert semana_id is not None
    return semana_id


# --------------------------------------------------------------------------
# T7 / R1, R2 - extraer_puntos_bw
# --------------------------------------------------------------------------
def test_extraer_puntos_bw_thousands_separator() -> None:
    """R1: el separador de miles del PDF desaparece y el resultado es un `int`."""
    resultado = pdf_extractor.extraer_puntos_bw(TEXTO_CON_PUNTOS)

    assert resultado == 20003
    assert isinstance(resultado, int)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Total PB acumulados 20,003", 20003),
        ("Total PB acumulados 6,428", 6428),
        ("Total PB acumulados 0", 0),
        ("Total PB acumulados 1,234,567", 1234567),
        ("Total PB acumulados    22,272", 22272),
        ("Total PB acumulados\n8,777", 8777),
    ],
)
def test_extraer_puntos_bw_parses_every_boundary_number(
    texto: str, esperado: int
) -> None:
    """R1: cualquier forma del numero acumulado se normaliza a entero."""
    resultado = pdf_extractor.extraer_puntos_bw(texto)

    assert resultado == esperado


@pytest.mark.parametrize(
    "texto",
    [
        TEXTO_SIN_PUNTOS,
        "",
        "   ",
        "Total PB acumulados",
        "Total PB acumulados ,,",
        "Total PB acumuladosXX 20,003",
        None,
    ],
)
def test_extraer_puntos_bw_returns_none_without_raising(texto: str | None) -> None:
    """R2: sin un numero reconocible la funcion degrada a `None` y nunca lanza."""
    resultado = pdf_extractor.extraer_puntos_bw(texto)

    assert resultado is None


# --------------------------------------------------------------------------
# T2 / R3 - extraer_semana_cierre_bw
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("MIS PUNTOS BW al cierre de semana 29", 29),
        ("al cierre de semana 1", 1),
        ("al cierre de semana   52", 52),
        (TEXTO_CON_PUNTOS, 29),
    ],
)
def test_extraer_semana_cierre_bw_returns_closing_week(
    texto: str, esperado: int
) -> None:
    """R3: la semana de cierre sale del texto como entero."""
    resultado = pdf_extractor.extraer_semana_cierre_bw(texto)

    assert resultado == esperado


@pytest.mark.parametrize(
    "texto", [TEXTO_SIN_PUNTOS, "", "al cierre de semana", "cierre de semana 29", None]
)
def test_extraer_semana_cierre_bw_returns_none_without_raising(
    texto: str | None,
) -> None:
    """R3: sin la frase de cierre devuelve `None` y nunca lanza."""
    resultado = pdf_extractor.extraer_semana_cierre_bw(texto)

    assert resultado is None


# --------------------------------------------------------------------------
# T3 / R9, R7 - extraer_puntos_de_paginas sobre el PDF real
# --------------------------------------------------------------------------
def test_extraer_puntos_de_paginas_returns_sample_notes_with_closing_week() -> None:
    """R7: el PDF real entrega un corte por nota, todos del cierre 29."""
    resultado = pdf_extractor.extraer_puntos_de_paginas(str(SAMPLE_PDF))

    assert [puntos for puntos, _ in resultado] == list(PUNTOS_MUESTRA)
    assert {cierre for _, cierre in resultado} == {29}


def test_extraer_puntos_de_paginas_keeps_page_reporting_zero_points() -> None:
    """R2, R7: `0` es un corte real, no una ausencia, y no debe descartarse.

    La quinta nota del PDF de muestra reporta `Total PB acumulados 0`. La guarda
    del bucle compara contra `None` justamente por esto: un `if not puntos:`
    perderia esa pagina y con ella la unica evidencia de que la distincion entre
    "cero puntos" y "la pagina no los reporta" se respeta.
    """
    resultado = pdf_extractor.extraer_puntos_de_paginas(str(SAMPLE_PDF))

    assert (0, 29) in resultado


def test_extraer_puntos_de_paginas_covers_every_page_of_the_sample() -> None:
    """R7: en esta nota todas las paginas reportan puntos; ninguna se pierde."""
    with pdfplumber.open(SAMPLE_PDF) as pdf:
        total_paginas = len(pdf.pages)

    resultado = pdf_extractor.extraer_puntos_de_paginas(str(SAMPLE_PDF))

    assert len(resultado) == total_paginas


# --------------------------------------------------------------------------
# T8 / R4, R6 - actualizar_puntos_semana: manual gana, auto se queda con el maximo
# --------------------------------------------------------------------------
def test_actualizar_puntos_semana_manual_overrides_auto(
    conn: sqlite3.Connection,
) -> None:
    """R4, R6: auto aplica semantica de maximo; `manual=True` corrige siempre.

    Cubre las cuatro ramas de la decision de negocio del 2026-07-27 en el orden en
    que la usuaria las viviria: primer extract sobre el `DEFAULT 0`, un corte
    menor que no debe pisar, un corte mayor que si, y la correccion manual a la
    baja que manda sobre todo lo anterior.
    """
    semana_id = semana_nueva(conn, SEMANA_CIERRE)

    primero = core_semanas.actualizar_puntos_semana(conn, semana_id, 20003)
    menor = core_semanas.actualizar_puntos_semana(conn, semana_id, 6428)
    puntos_tras_menor = leer_puntos(conn, semana_id)
    mayor = core_semanas.actualizar_puntos_semana(conn, semana_id, 22272)
    puntos_tras_mayor = leer_puntos(conn, semana_id)
    correccion = core_semanas.actualizar_puntos_semana(
        conn, semana_id, 15000, manual=True
    )

    assert primero is True
    assert menor is False
    assert puntos_tras_menor == 20003
    assert mayor is True
    assert puntos_tras_mayor == 22272
    assert correccion is True
    assert leer_puntos(conn, semana_id) == 15000


@pytest.mark.parametrize(
    "orden",
    [
        (20003, 6428, 22272, 8777),
        (8777, 22272, 6428, 20003),
        (22272, 20003, 8777, 6428),
        (6428, 8777, 20003, 22272),
    ],
)
def test_actualizar_puntos_semana_result_is_order_independent(
    conn: sqlite3.Connection, orden: tuple[int, ...]
) -> None:
    """R6: la semantica de maximo hace el resultado independiente del orden.

    Es el punto de toda la decision de negocio: con la guarda de no-clobber
    original ganaba la primera pagina parseada, un valor arbitrario que dependia
    del orden de lectura del PDF.
    """
    semana_id = semana_nueva(conn, SEMANA_CIERRE)

    for puntos in orden:
        core_semanas.actualizar_puntos_semana(conn, semana_id, puntos)

    assert leer_puntos(conn, semana_id) == PUNTOS_MAXIMO


def test_actualizar_puntos_semana_auto_writes_over_null(
    conn: sqlite3.Connection,
) -> None:
    """R6: `NULL` es "sin valor", asi que el extract automatico si escribe."""
    semana_id = semana_nueva(conn, SEMANA_CIERRE)
    with conn:
        conn.execute(
            "UPDATE semanas_catalogo SET puntos_bw_acumulados = NULL WHERE id = ?",
            (semana_id,),
        )

    escribio = core_semanas.actualizar_puntos_semana(conn, semana_id, 20003)

    assert escribio is True
    assert leer_puntos(conn, semana_id) == 20003


def test_actualizar_puntos_semana_equal_value_is_not_rewritten(
    conn: sqlite3.Connection,
) -> None:
    """R6: la guarda es estricta (`>`), asi que repetir el mismo corte no escribe."""
    semana_id = semana_nueva(conn, SEMANA_CIERRE)
    core_semanas.actualizar_puntos_semana(conn, semana_id, 20003)

    repetido = core_semanas.actualizar_puntos_semana(conn, semana_id, 20003)

    assert repetido is False
    assert leer_puntos(conn, semana_id) == 20003


def test_actualizar_puntos_semana_unknown_week_writes_nothing(
    conn: sqlite3.Connection,
) -> None:
    """R4: un id inexistente no afecta ninguna fila y se reporta como no escrito."""
    escribio = core_semanas.actualizar_puntos_semana(conn, 9999, 20003, manual=True)

    assert escribio is False
    assert conn.execute("SELECT COUNT(*) AS n FROM semanas_catalogo").fetchone()["n"] == 0


def test_actualizar_puntos_semana_persists_across_connection_state(
    conn: sqlite3.Connection,
) -> None:
    """R4: la escritura queda confirmada por su propio `with conn:`, sin commit suelto."""
    semana_id = semana_nueva(conn, SEMANA_CIERRE)
    core_semanas.actualizar_puntos_semana(conn, semana_id, 20003, manual=True)

    conn.rollback()

    assert leer_puntos(conn, semana_id) == 20003


# --------------------------------------------------------------------------
# T9 / R2, R7 - paginas sin puntos no alteran la semana
# --------------------------------------------------------------------------
def test_procesar_puntos_bw_none_leaves_week_unchanged(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2, R7: sin puntos en el PDF ninguna semana se toca.

    El PDF de muestra si trae puntos, y el proyecto no tiene una nota sin ellos,
    asi que la ausencia se simula sustituyendo el extractor por el resultado que
    daria una nota asi: la lista vacia. La sustitucion usa la fixture
    `monkeypatch` de pytest -- que revierte sola al terminar el test -- sobre el
    modulo que `procesar_puntos_bw` importa localmente.
    """
    assert pdf_extractor.extraer_puntos_bw(TEXTO_SIN_PUNTOS) is None
    semana_id = semana_nueva(conn, SEMANA_PEDIDO)
    core_semanas.actualizar_puntos_semana(conn, semana_id, 500, manual=True)
    monkeypatch.setattr(pdf_extractor, "extraer_puntos_de_paginas", lambda _ruta: [])

    core_semanas.procesar_puntos_bw(conn, str(SAMPLE_PDF), semana_id, ANIO)

    assert leer_puntos(conn, semana_id) == 500
    assert len(conn.execute("SELECT id FROM semanas_catalogo").fetchall()) == 1


def test_procesar_puntos_bw_without_closing_week_falls_back_to_order_week(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: sin referencia de cierre los puntos se quedan en la semana del pedido."""
    semana_id = semana_nueva(conn, SEMANA_PEDIDO)
    monkeypatch.setattr(
        pdf_extractor, "extraer_puntos_de_paginas", lambda _ruta: [(20003, None)]
    )

    core_semanas.procesar_puntos_bw(conn, str(SAMPLE_PDF), semana_id, ANIO)

    assert puntos_de(conn, SEMANA_PEDIDO) == 20003
    assert len(conn.execute("SELECT id FROM semanas_catalogo").fetchall()) == 1


# --------------------------------------------------------------------------
# T10 / R3, R5 - los puntos van a la semana de cierre, no a la del pedido
# --------------------------------------------------------------------------
def test_procesar_puntos_bw_attaches_to_closing_week(
    conn: sqlite3.Connection,
) -> None:
    """R3, R5: pedido en la semana 27 y cierre 29 -> los puntos aterrizan en la 29.

    Entrada real: `reference/C001264_NOTA.pdf`, cuyas notas dicen todas
    `al cierre de semana 29`. La semana del pedido se fuerza a `27 - 2026` para
    que ambas no puedan confundirse.
    """
    semana_pedido_id = semana_nueva(conn, SEMANA_PEDIDO)

    core_semanas.procesar_puntos_bw(conn, str(SAMPLE_PDF), semana_pedido_id, ANIO)

    assert puntos_de(conn, SEMANA_CIERRE) == PUNTOS_MAXIMO
    assert puntos_de(conn, SEMANA_PEDIDO) == 0


def test_procesar_puntos_bw_keeps_max_regardless_of_page_order(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5, R6: el mismo PDF leido al reves deja exactamente los mismos puntos."""
    semana_pedido_id = semana_nueva(conn, SEMANA_PEDIDO)
    paginas = [(puntos, 29) for puntos in reversed(PUNTOS_MUESTRA)]
    monkeypatch.setattr(
        pdf_extractor, "extraer_puntos_de_paginas", lambda _ruta: paginas
    )

    core_semanas.procesar_puntos_bw(conn, str(SAMPLE_PDF), semana_pedido_id, ANIO)

    assert puntos_de(conn, SEMANA_CIERRE) == PUNTOS_MAXIMO


def test_procesar_puntos_bw_leaves_unrelated_weeks_untouched(
    conn: sqlite3.Connection,
) -> None:
    """R7: la carga solo escribe en la semana de cierre de la nota."""
    semana_pedido_id = semana_nueva(conn, SEMANA_PEDIDO)
    semana_ajena_id = semana_nueva(conn, SEMANA_AJENA)
    core_semanas.actualizar_puntos_semana(conn, semana_ajena_id, 777, manual=True)

    core_semanas.procesar_puntos_bw(conn, str(SAMPLE_PDF), semana_pedido_id, ANIO)

    assert leer_puntos(conn, semana_ajena_id) == 777


def test_procesar_puntos_bw_is_idempotent_on_reload(
    conn: sqlite3.Connection,
) -> None:
    """R6, R7: recargar la misma nota no cambia el valor ni duplica la semana."""
    semana_pedido_id = semana_nueva(conn, SEMANA_PEDIDO)
    core_semanas.procesar_puntos_bw(conn, str(SAMPLE_PDF), semana_pedido_id, ANIO)

    core_semanas.procesar_puntos_bw(conn, str(SAMPLE_PDF), semana_pedido_id, ANIO)

    assert puntos_de(conn, SEMANA_CIERRE) == PUNTOS_MAXIMO
    assert len(conn.execute("SELECT id FROM semanas_catalogo").fetchall()) == 2


# --------------------------------------------------------------------------
# T11 / R9 - la extraccion sigue sin depender de la capa de reportes
# --------------------------------------------------------------------------
def test_pdf_extractor_puntos_no_pandas() -> None:
    """R9: `pdf_extractor` importa y su texto no menciona `pandas`/`openpyxl`.

    La comprobacion es **estatica** a proposito: `pandas` esta instalado en el
    venv, asi que mirar `sys.modules` solo diria si algun otro test lo importo
    antes. Lo que R9 exige es que este modulo no lo referencie.
    """
    fuente = EXTRACTOR_PATH.read_text(encoding="utf-8")

    encontrados = [token for token in TOKENS_ALMACENAMIENTO if token in fuente]

    assert encontrados == []
    assert all(hasattr(pdf_extractor, nombre) for nombre in FUNCIONES_BW)


def test_pdf_extractor_top_level_imports_stay_within_the_allowed_set() -> None:
    """R9: BW-02 no anade ningun import de nivel de modulo a la extraccion."""
    tree = ast.parse(EXTRACTOR_PATH.read_text(encoding="utf-8"))

    importados: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            importados.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            importados.append(node.module or "")

    assert sorted(importados) == ["datetime", "os", "pdfplumber", "re"]


@pytest.mark.parametrize("nombre", FUNCIONES_BW)
def test_puntos_function_body_imports_nothing(nombre: str) -> None:
    """R9: las funciones de puntos no meten un import por la puerta de atras."""
    tree = ast.parse(EXTRACTOR_PATH.read_text(encoding="utf-8"))
    funcion = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == nombre
    )

    imports = [
        hijo
        for hijo in ast.walk(funcion)
        if isinstance(hijo, (ast.Import, ast.ImportFrom))
    ]

    assert imports == []


def test_core_semanas_does_not_import_pdf_extractor_at_module_level() -> None:
    """El grafo de imports sigue apuntando hacia abajo: sin ciclo ni `pdfplumber`.

    `procesar_puntos_bw` necesita el extractor, pero lo importa dentro de la
    funcion para que importar la capa de dominio no arrastre la de extraccion.
    """
    ruta = PROJECT_ROOT / "core_semanas.py"
    tree = ast.parse(ruta.read_text(encoding="utf-8"))

    importados: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            importados.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            importados.append(node.module or "")

    assert "pdf_extractor" not in importados
    assert "pdfplumber" not in importados


def test_listar_semanas_ordena_de_la_mas_reciente_a_la_mas_antigua(
    conn: sqlite3.Connection,
) -> None:
    """La GUI lee las semanas por aqui: nunca ejecuta SQL (ADR-2)."""
    # Arrange
    core_semanas.obtener_o_crear_semana(conn, "27 - 2026")
    id_reciente = core_semanas.obtener_o_crear_semana(conn, "30 - 2026")
    core_semanas.obtener_o_crear_semana(conn, "29 - 2026")

    # Act
    semanas = core_semanas.listar_semanas(conn)

    # Assert
    assert [s["semana_texto"] for s in semanas] == [
        "30 - 2026",
        "29 - 2026",
        "27 - 2026",
    ]
    assert semanas[0]["id"] == id_reciente
    assert set(semanas[0]) == set(core_semanas.CAMPOS_SEMANA)


def test_listar_semanas_degrada_puntos_nulos_a_cero(
    conn: sqlite3.Connection,
) -> None:
    """Una semana recien creada no tiene puntos; la GUI no debe recibir `None`."""
    # Arrange
    semana_id = core_semanas.obtener_o_crear_semana(conn, "31 - 2026")
    with conn:
        conn.execute(
            "UPDATE semanas_catalogo SET puntos_bw_acumulados = NULL WHERE id = ?",
            (semana_id,),
        )

    # Act
    semanas = core_semanas.listar_semanas(conn)

    # Assert
    assert semanas[0]["puntos_bw_acumulados"] == 0


def test_listar_semanas_base_vacia_lista_vacia(conn: sqlite3.Connection) -> None:
    """Sin semanas cargadas, la lista es vacia y no lanza."""
    # Act / Assert
    assert core_semanas.listar_semanas(conn) == []
