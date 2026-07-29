"""Seccion de puntos Betterware del dashboard (BW-03 R5, R6).

La capa core ya entrega las dos lecturas que esta seccion necesita; lo que aqui
se prueba es el unico juicio que la GUI toma sobre ellas: **el orden**.

`core.obtener_puntos_por_semana` devuelve la serie **ascendente** porque su
contrato se penso para graficar de izquierda a derecha (BW-03 R1). La tabla del
dashboard se lee al reves --la semana en curso arriba--, asi que la inversion
ocurre en la GUI y no se le pide otro orden al core: una sola fuente, dos
presentaciones. Por eso la asercion central no compara contra una lista escrita a
mano sino contra `reversed(...)` de lo que el core devuelve: si algun dia el core
cambiara de criterio, la prueba seguiria describiendo la relacion correcta en vez
de fosilizar un orden concreto.

Se instancian widgets reales contra un `Tk()` oculto que hace de `app`; nunca la
clase `App`, que abriria la base de produccion. El helper de siembra se reusa de
`tests/test_puntos_dashboard.py`, la suite de la capa core de esta misma lectura.
"""

from __future__ import annotations

import ast
import sqlite3
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from tkinter import ttk
from typing import Final

import pytest

import core
import db
import gui_inventario
from tests.test_puntos_dashboard import sembrar

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

SEMANA_VIEJA: Final[str] = "29 - 2026"
SEMANA_MEDIA: Final[str] = "30 - 2026"
SEMANA_NUEVA: Final[str] = "31 - 2026"
SEMANA_ILEGIBLE: Final[str] = "SEMANA ILEGIBLE"

COLUMNAS_ESPERADAS: Final[tuple[str, ...]] = ("semana", "puntos")

COL_SEMANA: Final[int] = 0
COL_PUNTOS: Final[int] = 1


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


@pytest.fixture()
def app_falsa(conn: sqlite3.Connection) -> Iterator[tk.Tk]:
    """Raiz de Tk oculta que hace de `app`: aporta `conn` y `refrescar_todo`."""
    try:
        ventana = tk.Tk()
    except tk.TclError:  # pragma: no cover - depende del entorno, no del codigo
        pytest.skip("No hay servidor X disponible para instanciar widgets")
    ventana.withdraw()
    ventana.conn = conn
    ventana.refrescar_todo = lambda: None
    try:
        yield ventana
    finally:
        ventana.destroy()


@pytest.fixture()
def tab(app_falsa: tk.Tk) -> gui_inventario.TabDashboard:
    """Dashboard montado sobre la app falsa."""
    return gui_inventario.TabDashboard(ttk.Notebook(app_falsa), app_falsa)


def metodo_ast(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


def semanas_pintadas(tab: gui_inventario.TabDashboard) -> list[str]:
    """Textos de semana del arbol de puntos, de arriba a abajo."""
    return [
        str(tab.tree_puntos.item(iid, "values")[COL_SEMANA])
        for iid in tab.tree_puntos.get_children()
    ]


# --------------------------------------------------------------------------
# R5 -- la tabla: columnas, poblado y orden invertido
# --------------------------------------------------------------------------


def test_dashboard_tiene_la_seccion_de_puntos_por_semana(
    tab: gui_inventario.TabDashboard,
) -> None:
    """R5: un Treeview propio con las columnas `semana` y `puntos`."""
    columnas = tuple(str(c) for c in tab.tree_puntos["columns"])

    assert columnas == COLUMNAS_ESPERADAS
    assert tab.tree_puntos.heading("semana")["text"] == "Semana"
    assert tab.tree_puntos.heading("puntos")["text"] == "Puntos acumulados"


def test_puntos_se_pintan_con_la_semana_mas_reciente_arriba(
    tab: gui_inventario.TabDashboard, conn: sqlite3.Connection
) -> None:
    """R5: la vista invierte el orden ascendente que entrega el core."""
    sembrar(conn, SEMANA_VIEJA, 100)
    sembrar(conn, SEMANA_NUEVA, 300)
    sembrar(conn, SEMANA_MEDIA, 200)

    tab.refrescar()

    assert semanas_pintadas(tab) == [SEMANA_NUEVA, SEMANA_MEDIA, SEMANA_VIEJA]


def test_el_orden_pintado_es_exactamente_el_inverso_del_core(
    tab: gui_inventario.TabDashboard, conn: sqlite3.Connection
) -> None:
    """R5: la inversion vive en la GUI; al core no se le pide otro orden.

    Incluye una semana ilegible (BW-01 R6), que el core manda al final de la
    serie ascendente: la relacion "vista == inverso del core" tiene que
    sostenerse tambien con ella, sin ningun caso especial en la GUI.
    """
    sembrar(conn, SEMANA_VIEJA, 100)
    sembrar(conn, SEMANA_ILEGIBLE, 50)
    sembrar(conn, SEMANA_NUEVA, 300)

    tab.refrescar()

    del_core = [str(f["semana_texto"]) for f in core.obtener_puntos_por_semana(conn)]
    assert semanas_pintadas(tab) == list(reversed(del_core))


def test_puntos_se_pintan_con_el_valor_de_cada_semana(
    tab: gui_inventario.TabDashboard, conn: sqlite3.Connection
) -> None:
    """R5: cada fila lleva los puntos acumulados de su semana, ya enteros."""
    sembrar(conn, SEMANA_MEDIA, 1250)

    tab.refrescar()

    valores = tab.tree_puntos.item(tab.tree_puntos.get_children()[0], "values")
    assert valores[COL_SEMANA] == SEMANA_MEDIA
    assert valores[COL_PUNTOS] == "1,250"


def test_refrescar_repuebla_la_tabla_sin_duplicar_filas(
    tab: gui_inventario.TabDashboard, conn: sqlite3.Connection
) -> None:
    """R5: dos refrescos seguidos no acumulan la serie dos veces."""
    sembrar(conn, SEMANA_MEDIA, 200)

    tab.refrescar()
    tab.refrescar()

    assert semanas_pintadas(tab) == [SEMANA_MEDIA]


def test_la_tabla_de_puntos_queda_vacia_sin_semanas(
    tab: gui_inventario.TabDashboard,
) -> None:
    """R5, R4 del core: con el catalogo vacio no se pinta nada y no se lanza."""
    tab.refrescar()

    assert tab.tree_puntos.get_children() == ()


# --------------------------------------------------------------------------
# R6 -- cabecera: ultima semana y sus puntos acumulados
# --------------------------------------------------------------------------


def test_cabecera_muestra_la_ultima_semana_y_sus_puntos(
    tab: gui_inventario.TabDashboard, conn: sqlite3.Connection
) -> None:
    """R6: la cabecera sale de `core.resumen_puntos`, no del ultimo elemento."""
    sembrar(conn, SEMANA_VIEJA, 100)
    sembrar(conn, SEMANA_NUEVA, 340)

    tab.refrescar()

    texto = tab.lbl_puntos_ultima["text"]
    assert SEMANA_NUEVA in texto
    assert "340" in texto


def test_cabecera_avisa_cuando_no_hay_semanas(
    tab: gui_inventario.TabDashboard,
) -> None:
    """R6: el resumen neutro de la base vacia no se pinta como '0 puntos de ''."""
    tab.refrescar()

    assert tab.lbl_puntos_ultima["text"] == gui_inventario.SIN_SEMANAS


def test_cabecera_coincide_con_la_primera_fila_de_la_tabla(
    tab: gui_inventario.TabDashboard, conn: sqlite3.Connection
) -> None:
    """R5 + R6: cabecera y tabla no pueden discrepar sobre la semana en curso."""
    sembrar(conn, SEMANA_VIEJA, 100)
    sembrar(conn, SEMANA_MEDIA, 200)
    sembrar(conn, SEMANA_NUEVA, 300)

    tab.refrescar()

    resumen = core.resumen_puntos(conn)
    assert semanas_pintadas(tab)[0] == resumen["ultima_semana"]


# --------------------------------------------------------------------------
# Cableado y ADR-2
# --------------------------------------------------------------------------


def test_refrescar_del_dashboard_incluye_la_seccion_de_puntos() -> None:
    """R5: sin esta llamada la seccion nunca se poblaria."""
    fuente = ast.unparse(metodo_ast("TabDashboard", "refrescar"))

    assert "self._refrescar_puntos()" in fuente


def test_la_seccion_de_puntos_lee_por_la_capa_core() -> None:
    """R5, R6: las dos lecturas son las de la fachada, con la conexion de sesion."""
    fuente = ast.unparse(metodo_ast("TabDashboard", "_refrescar_puntos"))

    assert "core.obtener_puntos_por_semana(self.app.conn)" in fuente
    assert "core.resumen_puntos(self.app.conn)" in fuente


def test_la_seccion_de_puntos_no_ejecuta_sql() -> None:
    """ADR-2: el dashboard no escribe SQL; toda lectura pasa por `core`."""
    fuente = ast.unparse(metodo_ast("TabDashboard", "_refrescar_puntos"))

    for palabra in ("SELECT", "ORDER BY", "execute(", "get_conn"):
        assert palabra not in fuente, f"La GUI ejecuta SQL: aparece {palabra!r}"
