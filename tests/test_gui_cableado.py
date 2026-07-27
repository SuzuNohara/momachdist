"""Cableado de `gui_inventario` contra la capa core sobre SQLite.

El resto de la suite verifica la GUI de forma **estatica** (`ast.parse`), que es
lo correcto para aserciones sobre widgets: importar tkinter y construir la
ventana en CI no aporta y es fragil. Pero esa eleccion dejo un hueco: un modulo
que ni siquiera importa pasaba en verde, porque nadie lo importaba nunca.

Este archivo cierra ese hueco con lo minimo que si se puede comprobar sin
abrir una ventana:

* que `gui_inventario` **importa** y que su alias `core` es la capa nueva
  (durante W2 apuntaba a `inventario_core`, el modulo Excel que ya no existe
  en el arbol);
* que `App.__init__` abre la conexion de la sesion (`self.conn`), de la que
  dependen `TabDashboard`, `TabInventario` y `VentanaVenta`;
* que la confirmacion de la carga llama a `core.confirmar_carga` con la firma
  nueva `(conn, filas)` y no con la vieja de Excel `(filas, EXCEL_PATH)`.

No se instancia `App`: eso exige un servidor X. Las tres aserciones son de
import y de AST.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import core
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"


def _metodo(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


def test_gui_importa_sin_errores() -> None:
    """El modulo de la GUI se importa: sus dependencias existen en el arbol."""
    assert gui_inventario.APP_TITLE


def test_alias_core_apunta_a_la_capa_sqlite() -> None:
    """`core` dentro de la GUI es la capa nueva, no el modulo Excel."""
    assert gui_inventario.core is core
    assert hasattr(gui_inventario.core, "confirmar_carga")
    assert hasattr(gui_inventario.core, "obtener_existencias")


def test_app_abre_la_conexion_de_la_sesion() -> None:
    """`App.__init__` asigna `self.conn` desde `db.init_db` (ADR-2)."""
    fuente = ast.unparse(_metodo("App", "__init__"))
    assert "self.conn = db.init_db(DB_PATH)" in fuente


def test_confirmar_carga_usa_la_firma_nueva() -> None:
    """`al_confirmar_carga` pasa la conexion primero, sin ruta de Excel."""
    fuente = ast.unparse(_metodo("App", "al_confirmar_carga"))
    assert "core.confirmar_carga(self.conn, filas_confirmadas)" in fuente
    assert "EXCEL_PATH" not in fuente
