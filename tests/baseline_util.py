"""Utilidades del baseline de caracterizacion.

Normaliza la salida de `inventario_core.procesar_pdf` eliminando los
campos no deterministas, para que el snapshot golden sea reproducible.
"""

from __future__ import annotations

import pathlib
from typing import Any, Final

PROJECT_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent
REFERENCE_DIR: Final[pathlib.Path] = PROJECT_ROOT / "reference"
SAMPLE_PDF: Final[pathlib.Path] = REFERENCE_DIR / "C001264_NOTA.pdf"

BASELINE_DIR: Final[pathlib.Path] = PROJECT_ROOT / "tests" / "baseline"
BASELINE_JSON: Final[pathlib.Path] = BASELINE_DIR / "C001264_NOTA.baseline.json"

#: Campos cuyo valor cambia en cada ejecucion (`datetime.now()`), por lo
#: que no pueden formar parte de un snapshot golden.
VOLATILE_FIELDS: Final[frozenset[str]] = frozenset({"Fecha registro"})


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Devuelve `rows` sin los campos volatiles, preservando el orden.

    El orden de `procesar_pdf` es determinista (pagina -> tabla ->
    ocurrencia), asi que no se reordena nada. `Archivo origen` si es
    determinista (basename + numero de pagina) y se conserva.

    Time: O(n * k) | Space: O(n * k)  (n filas, k campos por fila)
    """
    return [
        {key: value for key, value in row.items() if key not in VOLATILE_FIELDS}
        for row in rows
    ]
