"""Generador (idempotente) del snapshot golden de extraccion PDF.

Uso:

    <PY> -m tests.generate_baseline

Corre `inventario_core.procesar_pdf` sobre el PDF de muestra, elimina los
campos volatiles y escribe `tests/baseline/C001264_NOTA.baseline.json`.
Se vuelve a ejecutar SOLO cuando el comportamiento cambie de forma
intencionada; cualquier otra divergencia debe hacer fallar el test de
caracterizacion.
"""

from __future__ import annotations

import json
from typing import Any

# `conftest` se importa por su efecto secundario: inserta `reference/` y la
# raiz del proyecto en `sys.path` (excepcion documentada a python.md #15).
# Centralizar ahi la manipulacion evita repetirla en este script.
import conftest as _conftest  # noqa: F401  (import por efecto secundario)
from inventario_core import procesar_pdf

from tests.baseline_util import BASELINE_JSON, SAMPLE_PDF, normalize_rows


def main() -> None:
    """Regenera el golden JSON a partir del PDF de muestra.

    Time: O(p * t) sobre paginas/tablas del PDF | Space: O(n) filas
    """
    rows: list[dict[str, Any]] = procesar_pdf(str(SAMPLE_PDF))
    normalized = normalize_rows(rows)

    BASELINE_JSON.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_JSON.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
