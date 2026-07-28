"""Suite de `pdf_extractor` — el modulo de extraccion pura (FUND-03).

Cubre tres ejes:

1. **Verbatim** — cada funcion movida es byte-identica a su original en
   `reference/inventario_core.py` (ADR-4: riesgo de parseo = 0).
2. **Aislamiento** — el modulo no arrastra pandas/openpyxl ni ningun
   acoplamiento de almacenamiento.
3. **Equivalencia con el golden de FUND-01** — la salida de
   `procesar_pdf` sobre `reference/C001264_NOTA.pdf` es identica al
   snapshot `tests/baseline/C001264_NOTA.baseline.json`.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
from typing import Any, Final

import pdfplumber
import pytest

import pdf_extractor
from tests.baseline_util import BASELINE_JSON, SAMPLE_PDF, normalize_rows

PROJECT_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent
EXTRACTOR_PATH: Final[pathlib.Path] = PROJECT_ROOT / "pdf_extractor.py"
REFERENCE_MODULE: Final[pathlib.Path] = PROJECT_ROOT / "reference" / "inventario_core.py"

#: Funciones que el plan de FUND-03 manda mover, en orden de dependencia.
MOVED_FUNCTIONS: Final[tuple[str, ...]] = (
    "_clean_money",
    "_clean_int",
    "extraer_metadata",
    "extraer_productos_de_tabla",
    "procesar_pdf",
    "preparar_filas_desde_pdfs",
    "limpiar_telefono",
    "link_whatsapp",
)

#: Funciones anadidas al modulo DESPUES del movimiento verbatim de FUND-03.
#: BW-02 sumo la extraccion de puntos Betterware, que es legitimamente parte de
#: la capa de extraccion. Declararlas aqui conserva la garantia original —el
#: bloque verbatim sigue completo, en orden y al principio— sin degradarla a
#: "cualquier cosa vale detras": una funcion nueva no declarada rompe el test,
#: que es exactamente lo que FUND-03 queria proteger.
FUNCIONES_AGREGADAS: Final[tuple[str, ...]] = (
    "extraer_puntos_bw",
    "extraer_semana_cierre_bw",
    "extraer_puntos_de_paginas",
)

#: Tokens que delatarian acoplamiento con la capa de almacenamiento.
STORAGE_TOKENS: Final[tuple[str, ...]] = (
    "pandas",
    "openpyxl",
    "DataFrame",
    "read_excel",
    "ExcelWriter",
    "load_workbook",
    "MOV_COLUMNS",
    "STOCK_COLUMNS",
    "VENTAS_COLUMNS",
    "ASOCIADOS_COLUMNS",
    "DIRECTORIO_COLUMNS",
    "SHEET_MOV",
    "SHEET_STOCK",
    "SHEET_VENTAS",
    "SHEET_ASOCIADOS",
    "SHEET_DIRECTORIO",
    "actualizar_excel_maestro",
)


def _function_sources(path: pathlib.Path) -> dict[str, str]:
    """Mapea nombre -> codigo fuente exacto de cada `def` de nivel superior.

    Time: O(n) | Space: O(n)  (n = bytes del archivo)
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _load_baseline() -> list[dict[str, Any]]:
    """Carga el snapshot golden generado por FUND-01.

    Time: O(n) | Space: O(n)
    """
    return json.loads(BASELINE_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# T1 / T6(R6) — scaffold del modulo: imports minimos, sin almacenamiento
# ---------------------------------------------------------------------

def test_module_imports_only_the_four_allowed_top_level_dependencies() -> None:
    tree = ast.parse(EXTRACTOR_PATH.read_text(encoding="utf-8"))

    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")

    assert sorted(top_level) == ["datetime", "os", "pdfplumber", "re"]


def test_module_has_no_storage_coupled_token() -> None:
    source = EXTRACTOR_PATH.read_text(encoding="utf-8")

    found = [token for token in STORAGE_TOKENS if token in source]

    assert found == []


def test_module_defines_no_module_level_constant() -> None:
    tree = ast.parse(EXTRACTOR_PATH.read_text(encoding="utf-8"))

    assignments = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]

    assert assignments == []


# ---------------------------------------------------------------------
# R1 — las 8 funciones existen y su cuerpo es VERBATIM
# ---------------------------------------------------------------------

@pytest.mark.parametrize("nombre", MOVED_FUNCTIONS)
def test_moved_function_source_is_verbatim_copy_of_reference(nombre: str) -> None:
    origen = _function_sources(REFERENCE_MODULE)
    destino = _function_sources(EXTRACTOR_PATH)

    assert nombre in destino
    assert destino[nombre] == origen[nombre]


def test_extractor_defines_exactly_the_planned_functions() -> None:
    destino = tuple(_function_sources(EXTRACTOR_PATH))

    assert destino[: len(MOVED_FUNCTIONS)] == MOVED_FUNCTIONS
    assert destino == MOVED_FUNCTIONS + FUNCIONES_AGREGADAS


# ---------------------------------------------------------------------
# T2 (R2) — helpers de limpieza
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("$1,234.50", 1234.5),
        ("$0.00", 0.0),
        ("  59.90 ", 59.9),
        ("", 0.0),
        (None, 0.0),
        ("N/A", 0.0),
        (49.9, 49.9),
    ],
)
def test_clean_money_returns_float_for_every_boundary_input(
    entrada: object, esperado: float
) -> None:
    resultado = pdf_extractor._clean_money(entrada)

    assert resultado == esperado


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("3", 3),
        ("  12 ", 12),
        ("", 0),
        (None, 0),
        ("1.5", 0),
        ("abc", 0),
    ],
)
def test_clean_int_returns_zero_for_unparseable_input(entrada: object, esperado: int) -> None:
    resultado = pdf_extractor._clean_int(entrada)

    assert resultado == esperado


# ---------------------------------------------------------------------
# T3 (R4) — metadata por pagina
# ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def page_texts() -> list[str]:
    """Texto plano de cada pagina del PDF de muestra.

    Time: O(p) | Space: O(p)  (p = paginas)
    """
    with pdfplumber.open(SAMPLE_PDF) as pdf:
        return [pagina.extract_text() or "" for pagina in pdf.pages]


def test_extraer_metadata_returns_per_note_asociado_on_first_page(page_texts: list[str]) -> None:
    meta = pdf_extractor.extraer_metadata(page_texts[0])

    assert meta["Nombre asociado"] == "ETNAN JEZREEL LOPEZ TORRES"
    assert meta["Semana"] == "30 - 2026"
    assert meta["Folio de pedido"].startswith("OV-")
    assert meta["Codigo nota"].isdigit()


def test_extraer_metadata_returns_a_different_asociado_on_second_page(
    page_texts: list[str],
) -> None:
    meta = pdf_extractor.extraer_metadata(page_texts[1])

    assert meta["Nombre asociado"].startswith("Aura Jannet")


def test_extraer_metadata_agrees_with_baseline_for_every_header_page(
    page_texts: list[str],
) -> None:
    baseline = _load_baseline()

    for indice, texto in enumerate(page_texts, start=1):
        meta = pdf_extractor.extraer_metadata(texto)
        if not meta["Folio de pedido"]:
            continue
        esperados = {
            fila["Nombre asociado"]
            for fila in baseline
            if fila["Archivo origen"].endswith(f"(pag. {indice})")
        }
        assert esperados in ({meta["Nombre asociado"]}, set())


def test_extraer_metadata_returns_empty_fields_for_page_without_header() -> None:
    meta = pdf_extractor.extraer_metadata("texto sin encabezado alguno")

    assert meta == {
        "Semana": "",
        "Folio de pedido": "",
        "Codigo nota": "",
        "Distribuidora": "",
        "Nombre asociado": "",
    }


# ---------------------------------------------------------------------
# T4 (R2, R4) — ramas de 8 y 9 columnas
# ---------------------------------------------------------------------

FILA_8_COLS: Final[list[str]] = [
    "25444", "ATRAPA CABELLOS DÚO", "12", "2", "2", "43.02", "$49.90", "$99.80",
]
FILA_9_COLS: Final[list[str]] = [
    "23103", "SET SARTENES", "1", "1", "$399.00", "$310.00", "$89.00", "$359.60", "$359.60",
]
FILA_9_COLS_REGALO: Final[list[str]] = [
    "24110", "CEPI PRENDAS REGALO", "1", "1", "$399.00", "$0.00", "$0.00", "$0.00", "$0.00",
]
FILA_8_COLS_REGALO: Final[list[str]] = [
    "23819", "ORGANI RACK BLACK", "", "1", "1", "$0.00", "$0.00", "$0.00",
]


def test_extraer_productos_de_tabla_marks_eight_column_row_as_normal_con_descuento() -> None:
    tabla = [["Artículo", "Descripción"], FILA_8_COLS]

    productos = pdf_extractor.extraer_productos_de_tabla(tabla)

    assert len(productos) == 1
    assert productos[0]["Tipo"] == "Normal (con descuento)"
    assert productos[0]["Precio catalogo"] == 43.02
    assert productos[0]["Precio con IVA"] == 49.9
    # El 18% se aplica al TOTAL de la linea, no al precio unitario.
    assert productos[0]["Precio que pagas"] == round(49.9 * 2 * (1 - 0.18), 2)
    assert productos[0]["Precio que pagas"] == 81.84


def test_extraer_productos_de_tabla_marks_nine_column_row_as_sin_descuento() -> None:
    tabla = [FILA_9_COLS]

    productos = pdf_extractor.extraer_productos_de_tabla(tabla)

    assert len(productos) == 1
    assert productos[0]["Tipo"] == "Sin descuento"
    assert productos[0]["Precio catalogo"] == 399.0
    assert productos[0]["Precio que pagas"] == 310.0
    assert productos[0]["Precio con IVA"] == 359.6


def test_extraer_productos_de_tabla_keeps_catalogo_price_on_zero_priced_gift_row() -> None:
    tabla = [FILA_9_COLS_REGALO]

    productos = pdf_extractor.extraer_productos_de_tabla(tabla)

    assert len(productos) == 1
    # Una fila de regalo NO implica "Precio catalogo == 0".
    assert productos[0]["Precio catalogo"] == 399.0
    assert productos[0]["Precio que pagas"] == 0.0
    assert productos[0]["Valor total con IVA"] == 0.0


def test_extraer_productos_de_tabla_handles_zero_priced_eight_column_row_without_error() -> None:
    tabla = [FILA_8_COLS_REGALO]

    productos = pdf_extractor.extraer_productos_de_tabla(tabla)

    assert len(productos) == 1
    assert productos[0]["Precio que pagas"] == 0.0
    assert productos[0]["Cantidad Casa"] == 1


@pytest.mark.parametrize(
    "fila",
    [
        ["Artículo", "Descripción", "Pag", "Sol", "Sur", "SinIVA", "ConIVA", "Total"],
        ["Articulo", "Descripción", "Pag", "Sol", "Sur", "SinIVA", "ConIVA", "Total"],
        ["Total general", "", "", "", "", "", "", "999.00"],
        ["ABC", "no es codigo", "", "1", "1", "1", "1", "1"],
        ["123", "codigo demasiado corto", "", "1", "1", "1", "1", "1"],
        [None, None, None, None, None, None, None, None],
    ],
)
def test_extraer_productos_de_tabla_skips_non_product_row(fila: list[str | None]) -> None:
    productos = pdf_extractor.extraer_productos_de_tabla([fila])

    assert productos == []


def test_extraer_productos_de_tabla_ignores_row_with_unexpected_column_count() -> None:
    tabla = [["25444", "SOLO TRES", "1"]]

    productos = pdf_extractor.extraer_productos_de_tabla(tabla)

    assert productos == []


# ---------------------------------------------------------------------
# T5 / T9 (R4) — equivalencia con el golden de FUND-01
# ---------------------------------------------------------------------

def test_procesar_pdf_matches_fund01_baseline(sample_pdf_path: pathlib.Path) -> None:
    esperado = _load_baseline()

    obtenido = normalize_rows(pdf_extractor.procesar_pdf(str(sample_pdf_path)))

    assert obtenido == esperado


def test_procesar_pdf_reproduces_baseline_shape(sample_pdf_path: pathlib.Path) -> None:
    baseline = _load_baseline()

    filas = normalize_rows(pdf_extractor.procesar_pdf(str(sample_pdf_path)))

    assert len(filas) == len(baseline) == 42
    assert sum(1 for f in filas if f["Tipo"] == "Normal (con descuento)") == 29
    assert sum(1 for f in filas if f["Tipo"] == "Sin descuento") == 13
    assert sum(1 for f in filas if f["Precio que pagas"] == 0) == 9
    assert sum(1 for f in filas if f["Ocurrencia"] > 1) == 3
    assert len({f["Folio de pedido"] for f in filas}) == 5
    assert len({f["Nombre asociado"] for f in filas}) == 5
    assert {f["Semana"] for f in filas} == {"30 - 2026"}


def test_procesar_pdf_keeps_catalogo_price_on_zero_priced_gift_rows(
    sample_pdf_path: pathlib.Path,
) -> None:
    filas = pdf_extractor.procesar_pdf(str(sample_pdf_path))

    regalos = [f for f in filas if f["Precio que pagas"] == 0]
    con_catalogo = [f for f in regalos if f["Precio catalogo"] > 0]

    assert len(regalos) == 9
    assert len(con_catalogo) == 4
    assert {f["Tipo"] for f in con_catalogo} == {"Sin descuento"}


def test_procesar_pdf_numbers_repeated_products_with_ocurrencia(
    sample_pdf_path: pathlib.Path,
) -> None:
    filas = pdf_extractor.procesar_pdf(str(sample_pdf_path))

    repetidas = [f for f in filas if f["Ocurrencia"] > 1]

    assert len(repetidas) == 3
    for fila in repetidas:
        clave = (fila["Folio de pedido"], fila["Codigo articulo"], fila["Tipo"])
        previas = [
            f for f in filas
            if (f["Folio de pedido"], f["Codigo articulo"], f["Tipo"]) == clave
        ]
        assert len(previas) >= fila["Ocurrencia"]


def test_procesar_pdf_does_not_sort_folios_by_page_order(
    sample_pdf_path: pathlib.Path,
) -> None:
    filas = pdf_extractor.procesar_pdf(str(sample_pdf_path))

    folios_en_orden = list(dict.fromkeys(f["Folio de pedido"] for f in filas))

    # El orden de folios del PDF real no es ascendente: es el dato de
    # origen, no un defecto del parser. No se debe ordenar.
    assert folios_en_orden != sorted(folios_en_orden)


# ---------------------------------------------------------------------
# T6 (R1, R4) — preparar_filas_desde_pdfs
# ---------------------------------------------------------------------

def test_preparar_filas_desde_pdfs_returns_rows_and_empty_errors_for_valid_pdf(
    sample_pdf_path: pathlib.Path,
) -> None:
    filas, errores = pdf_extractor.preparar_filas_desde_pdfs([str(sample_pdf_path)])

    assert len(filas) == 42
    assert errores == []


def test_preparar_filas_desde_pdfs_collects_unreadable_path_in_errores(
    tmp_path: pathlib.Path,
) -> None:
    inexistente = tmp_path / "no_existe.pdf"

    filas, errores = pdf_extractor.preparar_filas_desde_pdfs([str(inexistente)])

    assert filas == []
    assert len(errores) == 1
    assert errores[0].startswith("no_existe.pdf: error al leer el archivo (")


def test_preparar_filas_desde_pdfs_keeps_valid_rows_when_one_path_fails(
    sample_pdf_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    rotos = tmp_path / "roto.pdf"
    rotos.write_bytes(b"esto no es un pdf")

    filas, errores = pdf_extractor.preparar_filas_desde_pdfs(
        [str(sample_pdf_path), str(rotos)]
    )

    assert len(filas) == 42
    assert len(errores) == 1
    assert errores[0].startswith("roto.pdf: ")


class _FakePage:
    """Pagina de pdfplumber sin encabezado y sin tabla de productos."""

    def extract_text(self) -> str:
        """Time: O(1) | Space: O(1)"""
        return "Documento sin encabezado de remision"

    def extract_tables(self) -> list[list[list[str]]]:
        """Time: O(1) | Space: O(1)"""
        # Primera tabla vacia (hueco), segunda sin columna "Artículo".
        return [[], [["Concepto", "Importe"], ["Envio", "$0.00"]]]


class _FakePdf:
    """Contexto minimo compatible con `pdfplumber.open`."""

    pages: list[_FakePage] = [_FakePage()]

    def __enter__(self) -> _FakePdf:
        """Time: O(1) | Space: O(1)"""
        return self

    def __exit__(self, *_exc: object) -> bool:
        """Time: O(1) | Space: O(1)"""
        return False


def test_procesar_pdf_returns_no_rows_when_pdf_has_no_product_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf_extractor.pdfplumber, "open", lambda _ruta: _FakePdf())

    filas = pdf_extractor.procesar_pdf("cualquiera.pdf")

    assert filas == []


def test_preparar_filas_desde_pdfs_reports_pdf_without_products_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf_extractor.pdfplumber, "open", lambda _ruta: _FakePdf())

    filas, errores = pdf_extractor.preparar_filas_desde_pdfs(["/tmp/vacio.pdf"])

    assert filas == []
    assert errores == [
        "vacio.pdf: no se encontraron productos (revisa que sea una remision valida)"
    ]


def test_preparar_filas_desde_pdfs_returns_empty_tuple_for_empty_input() -> None:
    filas, errores = pdf_extractor.preparar_filas_desde_pdfs([])

    assert filas == []
    assert errores == []


# ---------------------------------------------------------------------
# T7 (R1, R3) — telefono / whatsapp
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("5512345678", "525512345678"),
        ("55 1234 5678", "525512345678"),
        ("(55) 1234-5678", "525512345678"),
        ("525512345678", "525512345678"),
        ("", ""),
        (None, ""),
        ("12345", "12345"),
    ],
)
def test_limpiar_telefono_prefixes_country_code_only_for_ten_digit_numbers(
    entrada: object, esperado: str
) -> None:
    resultado = pdf_extractor.limpiar_telefono(entrada)

    assert resultado == esperado


def test_link_whatsapp_returns_none_for_empty_phone() -> None:
    resultado = pdf_extractor.link_whatsapp("", "")

    assert resultado is None


def test_link_whatsapp_returns_plain_link_when_message_is_empty() -> None:
    resultado = pdf_extractor.link_whatsapp("5512345678")

    assert resultado == "https://wa.me/525512345678"


def test_link_whatsapp_url_encodes_the_message() -> None:
    resultado = pdf_extractor.link_whatsapp("5512345678", "Hola ¿que tal? a&b")

    assert resultado == "https://wa.me/525512345678?text=Hola%20%C2%BFque%20tal%3F%20a%26b"


def test_link_whatsapp_keeps_quote_import_local_to_the_function() -> None:
    fuentes = _function_sources(EXTRACTOR_PATH)

    assert "from urllib.parse import quote" in fuentes["link_whatsapp"]
    assert "urllib" not in EXTRACTOR_PATH.read_text(encoding="utf-8").split("def _clean_money")[0]


# ---------------------------------------------------------------------
# T8 (R3, R5) — aislamiento de imports
# ---------------------------------------------------------------------

def test_import_pdf_extractor_without_pandas() -> None:
    fuente = EXTRACTOR_PATH.read_text(encoding="utf-8")

    completado = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pdf_extractor, sys; "
            "assert 'pandas' not in sys.modules, 'pandas fue importado'; "
            "assert 'openpyxl' not in sys.modules, 'openpyxl fue importado'",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert "pandas" not in fuente
    assert "openpyxl" not in fuente
    assert completado.returncode == 0, completado.stderr
