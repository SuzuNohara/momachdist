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


#: Nombres que la GUI todavia invoca como `core.X` y que la fachada aun no
#: expone, porque su migracion Excel->SQLite pertenece a una oleada posterior.
#: **Esta lista solo puede encoger**, y con CLI-04 quedo vacia: la pestana
#: Entregas ya lee de SQLite (`core.listar_entregas`), los abonos se capturan en
#: `VentanaPagos` y `VentanaDetalleEntrega` -- que era quien sostenia
#: `FORMA_PAGO_OPCIONES`, `STATUS_ASOCIADO_OPCIONES` y
#: `actualizar_entrega_asociado` -- salio del arbol.
#:
#: Vacia, la allowlist convierte `test_gui_no_referencia_core_inexistente` en lo
#: que siempre quiso ser: **todo** `core.X` de la GUI existe en la fachada, sin
#: excepciones toleradas.
PENDIENTES_CLI04: Final[frozenset[str]] = frozenset()


def _atributos_core_usados() -> set[str]:
    """Nombres accedidos como `core.<attr>` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    return {
        nodo.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Attribute)
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "core"
    }


def test_gui_no_referencia_core_inexistente() -> None:
    """Todo `core.X` que la GUI invoca existe en la fachada.

    Guard sistematico de una clase de fallo que ya se materializo tres veces
    (`confirmar_carga` con la firma vieja, `link_whatsapp` y
    `preparar_filas_desde_pdfs`): la GUI llama a un nombre que la capa core no
    expone, y nadie se entera porque la suite la verifica por AST sin
    importarla y el guard de Excel corta antes en tiempo de ejecucion.
    """
    # Act
    faltantes = {a for a in _atributos_core_usados() if not hasattr(core, a)}

    # Assert
    assert faltantes <= PENDIENTES_CLI04, (
        f"La GUI llama a core.{sorted(faltantes - PENDIENTES_CLI04)}, "
        "que la fachada no expone."
    )


def test_lista_de_pendientes_no_tiene_entradas_muertas() -> None:
    """Un pendiente ya resuelto debe salir de la lista, no quedarse."""
    # Act
    resueltos = {a for a in PENDIENTES_CLI04 if hasattr(core, a)}

    # Assert
    assert not resueltos, (
        f"Ya existen en core y sobran en PENDIENTES_CLI04: {sorted(resueltos)}"
    )
