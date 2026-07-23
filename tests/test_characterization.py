"""Tests de caracterizacion del comportamiento actual de `procesar_pdf`.

Red de seguridad (SP-2 / RT-1) del refactor: congelan la salida de
`reference/inventario_core.py` ANTES de tocar codigo. Cualquier cambio de
comportamiento debe hacer fallar `test_output_matches_golden_baseline`.

Ademas se verifica que los dos artefactos que acompañan al golden no se
desincronicen de el en silencio:

- el generador (`tests/generate_baseline.py`) sigue reproduciendolo byte a byte;
- el documento de caracterizacion (R6) sigue describiendo los mismos datos.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Final

import pytest
from inventario_core import procesar_pdf

from tests import generate_baseline
from tests.baseline_util import BASELINE_JSON, normalize_rows

EXPECTED_ASSOCIATE_FRAGMENTS: Final[tuple[str, ...]] = ("ETNAN", "Aura Jannet")
EXPECTED_MIN_FOLIOS: Final[int] = 5
TYPE_8COL: Final[str] = "Normal (con descuento)"
TYPE_9COL: Final[str] = "Sin descuento"
EXPECTED_TYPES: Final[frozenset[str]] = frozenset({TYPE_8COL, TYPE_9COL})

#: R3 exige preservar estos cinco campos de metadata en cada nota.
NOTE_METADATA_FIELDS: Final[tuple[str, ...]] = (
    "Semana",
    "Folio de pedido",
    "Codigo nota",
    "Distribuidora",
    "Nombre asociado",
)

#: Artefacto de documentacion de R6, verificado contra el golden.
CHARACTERIZATION_DOC: Final[pathlib.Path] = (
    BASELINE_JSON.parent / "C001264_NOTA.characterization.md"
)
PER_NOTE_SECTION: Final[str] = "## Desglose por nota"
SUMMARY_SECTION: Final[str] = "## Resumen observado"

#: (prefijo de la etiqueta en el doc, clave de la metrica derivada del golden)
HEADLINE_METRICS: Final[tuple[tuple[str, str], ...]] = (
    ("Notas (pedidos)", "notes"),
    ("Paginas del PDF", "pages"),
    ("Folios distintos", "folios"),
    ("Asociados distintos", "associates"),
    ("Total de filas de producto", "product_rows"),
    ("Filas rama 8-col", "rows_8col"),
    ("Filas rama 9-col", "rows_9col"),
    ("Filas de regalo", "gift_rows"),
    ("Filas con Ocurrencia > 1", "repeated_rows"),
    ("Campos por fila", "fields_per_row"),
)

_PAGE_RE: Final[re.Pattern[str]] = re.compile(r"\(pag\.\s*(\d+)\)")
_LEADING_INT_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _extract_rows(pdf_path: pathlib.Path) -> list[dict[str, Any]]:
    """Corre `procesar_pdf` sobre `pdf_path` y normaliza campos volatiles.

    Time: O(p * t) sobre paginas/tablas | Space: O(n) filas
    """
    return normalize_rows(procesar_pdf(str(pdf_path)))


def _load_golden() -> list[dict[str, Any]]:
    """Carga el snapshot golden versionado.

    Time: O(n) | Space: O(n)
    """
    golden: list[dict[str, Any]] = json.loads(
        BASELINE_JSON.read_text(encoding="utf-8")
    )
    return golden


def _page_of(row: dict[str, Any]) -> int:
    """Devuelve el numero de pagina codificado en `Archivo origen`.

    Time: O(k) sobre la longitud del campo | Space: O(1)
    """
    match = _PAGE_RE.search(str(row["Archivo origen"]))
    assert match is not None, row["Archivo origen"]
    return int(match.group(1))


def _golden_per_note(golden: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Deriva del golden la ficha de cada nota, indexada por pagina.

    Cada ficha lleva los valores distintos de folio / codigo nota / asociado
    (conjuntos, para detectar mezclas) y el desglose 8-col / 9-col / total.

    Time: O(n) | Space: O(p) paginas
    """
    per_note: dict[int, dict[str, Any]] = {}
    for row in golden:
        note = per_note.setdefault(
            _page_of(row),
            {
                "Folio de pedido": set(),
                "Codigo nota": set(),
                "Nombre asociado": set(),
                "rows_8col": 0,
                "rows_9col": 0,
                "total": 0,
            },
        )
        for field in ("Folio de pedido", "Codigo nota", "Nombre asociado"):
            note[field].add(str(row[field]))
        note["total"] += 1
        if row["Tipo"] == TYPE_8COL:
            note["rows_8col"] += 1
        elif row["Tipo"] == TYPE_9COL:
            note["rows_9col"] += 1
    return per_note


def _golden_headline_metrics(golden: list[dict[str, Any]]) -> dict[str, int]:
    """Deriva del golden las cifras de cabecera que documenta R6.

    Time: O(n) | Space: O(n)
    """
    return {
        "notes": len({row["Folio de pedido"] for row in golden}),
        "pages": len({_page_of(row) for row in golden}),
        "folios": len({row["Folio de pedido"] for row in golden}),
        "associates": len({row["Nombre asociado"] for row in golden}),
        "product_rows": len(golden),
        "rows_8col": sum(1 for row in golden if row["Tipo"] == TYPE_8COL),
        "rows_9col": sum(1 for row in golden if row["Tipo"] == TYPE_9COL),
        "gift_rows": sum(1 for row in golden if row["Precio que pagas"] == 0),
        "repeated_rows": sum(1 for row in golden if int(row["Ocurrencia"]) > 1),
        "fields_per_row": len(golden[0]),
    }


def _clean_cell(cell: str) -> str:
    """Quita el marcado markdown decorativo de una celda.

    Time: O(k) | Space: O(k)
    """
    return cell.replace("`", "").replace("*", "").strip()


def _doc_table_rows(section_title: str) -> list[list[str]]:
    """Filas de datos de la primera tabla markdown bajo `section_title`.

    Descarta el encabezado y la fila separadora; devuelve el resto de filas
    con las celdas ya limpias.

    Time: O(L) sobre las lineas del documento | Space: O(f * c)
    """
    lines = CHARACTERIZATION_DOC.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == section_title)

    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [_clean_cell(cell) for cell in stripped.strip("|").split("|")]
        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    return rows[1:]


def _documented_headline_metrics() -> dict[str, int]:
    """Lee del documento R6 las cifras de cabecera, por prefijo de etiqueta.

    Time: O(m * f) etiquetas x filas | Space: O(m)
    """
    rows = _doc_table_rows(SUMMARY_SECTION)

    documented: dict[str, int] = {}
    for prefix, key in HEADLINE_METRICS:
        cells = next((row for row in rows if row[0].startswith(prefix)), None)
        assert cells is not None, f"metrica ausente en el doc: {prefix}"
        match = _LEADING_INT_RE.match(cells[1])
        assert match is not None, f"valor no numerico para {prefix}: {cells[1]}"
        documented[key] = int(match.group(1))
    return documented


# --------------------------------------------------------------------------- #
# R2 / R7 — snapshot golden
# --------------------------------------------------------------------------- #
def test_normalize_drops_volatile() -> None:
    rows = [{"Fecha registro": "2026-07-23 10:00", "Codigo articulo": "12345"}]

    normalized = normalize_rows(rows)

    assert "Fecha registro" not in normalized[0]
    assert normalized[0]["Codigo articulo"] == "12345"


def test_output_matches_golden_baseline(sample_pdf_path: pathlib.Path) -> None:
    golden = _load_golden()

    rows = _extract_rows(sample_pdf_path)

    assert rows == golden


def test_generate_baseline_reproduces_committed_golden(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El generador (R2) sigue produciendo exactamente el golden versionado."""
    target = tmp_path / "C001264_NOTA.baseline.json"
    monkeypatch.setattr(generate_baseline, "BASELINE_JSON", target)

    generate_baseline.main()

    assert target.read_bytes() == BASELINE_JSON.read_bytes()


# --------------------------------------------------------------------------- #
# R3 — notas, folios, asociados y metadata
# --------------------------------------------------------------------------- #
def test_notes_folios_and_associates(sample_pdf_path: pathlib.Path) -> None:
    rows = _extract_rows(sample_pdf_path)

    folios = {row["Folio de pedido"] for row in rows}
    associates = {row["Nombre asociado"] for row in rows}

    assert len(folios) >= EXPECTED_MIN_FOLIOS
    assert len(associates) > 1
    for fragment in EXPECTED_ASSOCIATE_FRAGMENTS:
        assert any(fragment in name for name in associates), fragment


@pytest.mark.parametrize("field", NOTE_METADATA_FIELDS)
def test_every_note_preserves_metadata_field(
    sample_pdf_path: pathlib.Path, field: str
) -> None:
    """R3: los cinco campos de metadata existen, no van vacios y son unicos por nota."""
    rows = _extract_rows(sample_pdf_path)

    by_note: dict[int, set[str]] = {}
    for row in rows:
        assert field in row, (field, row.get("Archivo origen"))
        by_note.setdefault(_page_of(row), set()).add(str(row[field]).strip())

    assert len(by_note) == EXPECTED_MIN_FOLIOS
    for page, values in by_note.items():
        assert "" not in values, (field, page)
        assert len(values) == 1, (field, page, values)


# --------------------------------------------------------------------------- #
# R4 / R5 — ramas de columnas y regalos
# --------------------------------------------------------------------------- #
def test_both_column_branches_present(sample_pdf_path: pathlib.Path) -> None:
    rows = _extract_rows(sample_pdf_path)

    types = {row["Tipo"] for row in rows}

    assert EXPECTED_TYPES <= types


def test_gift_product_zero_price(sample_pdf_path: pathlib.Path) -> None:
    rows = _extract_rows(sample_pdf_path)

    gifts = [
        row
        for row in rows
        if row["Precio que pagas"] == 0 or row["Precio catalogo"] == 0
    ]

    assert len(gifts) >= 1


# --------------------------------------------------------------------------- #
# R6 — el documento de caracterizacion no puede desviarse del golden
# --------------------------------------------------------------------------- #
def test_characterization_doc_per_note_table_matches_golden() -> None:
    """La tabla "Desglose por nota" describe exactamente lo que dice el golden."""
    expected = _golden_per_note(_load_golden())

    documented = {
        int(cells[0]): cells
        for cells in _doc_table_rows(PER_NOTE_SECTION)
        if cells[0].isdigit()
    }

    assert set(documented) == set(expected)
    for page, cells in documented.items():
        note = expected[page]
        assert {cells[1]} == note["Folio de pedido"], page
        assert {cells[2]} == note["Codigo nota"], page
        assert {cells[3]} == note["Nombre asociado"], page
        assert int(cells[4]) == note["rows_8col"], page
        assert int(cells[5]) == note["rows_9col"], page
        assert int(cells[6]) == note["total"], page


def test_characterization_doc_headline_counts_match_golden() -> None:
    """Las cifras de "Resumen observado" se derivan realmente del golden."""
    expected = _golden_headline_metrics(_load_golden())

    documented = _documented_headline_metrics()

    assert documented == expected
