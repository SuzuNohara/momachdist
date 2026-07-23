"""Configuracion de pytest para la suite de caracterizacion.

Inserta `reference/` y la raiz del proyecto en `sys.path` para que
`import inventario_core` y `from tests import baseline_util` funcionen al
ejecutar pytest desde la raiz del proyecto.

NOTA (excepcion deliberada a `.langs/python.md`, item 15 — "Is `sys.path`
manipulation absent?"): el spike FUND-01 debe caracterizar el modulo
original `reference/inventario_core.py` *en su sitio*, sin copiarlo ni
empaquetarlo, por lo que la insercion en `sys.path` es requisito del plan.
Es la unica violacion permitida del estandar.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Final

import pytest

PROJECT_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent
REFERENCE_DIR: Final[pathlib.Path] = PROJECT_ROOT / "reference"
SAMPLE_PDF_NAME: Final[str] = "C001264_NOTA.pdf"

for _path in (REFERENCE_DIR, PROJECT_ROOT):
    _str_path = str(_path)
    if _str_path not in sys.path:
        sys.path.insert(0, _str_path)


@pytest.fixture()
def sample_pdf_path() -> pathlib.Path:
    """Ruta al PDF de muestra multi-nota usado como baseline.

    Time: O(1) | Space: O(1)
    """
    return REFERENCE_DIR / SAMPLE_PDF_NAME
