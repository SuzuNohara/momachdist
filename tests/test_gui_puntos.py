"""Correccion manual de puntos Betterware: `VentanaPuntosSemana` (BW-02, T6).

**Desviacion D9.** El plan pedia la afordancia "en la vista de semanas /
Betterware", que no existe todavia -- la crea BW-03, de W5 --, asi que T6 se
resolvio con un dialogo minimo autocontenido en vez de con una pestana completa
que BW-03 tendria que rehacer. Estas pruebas fijan su contrato: listar desde
`core.listar_semanas`, editar y guardar con
`core.actualizar_puntos_semana(..., manual=True)`.

Lo que la suite protege por encima de todo es el `manual=True`: sin el, la
correccion de la usuaria caeria bajo la semantica de maximo de R6 y una
correccion a la baja se descartaria en silencio, que es exactamente el caso de
uso que motiva R8.

Se instancian widgets reales contra un `Tk()` oculto que hace de `app`; nunca la
clase `App`, que abriria la base de produccion y correria el backup de arranque.
"""

from __future__ import annotations

import ast
import sqlite3
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from typing import Final
from unittest import mock

import pytest

import core
import db
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

SEMANA_29: Final[str] = "29 - 2026"
SEMANA_30: Final[str] = "30 - 2026"
SEMANA_RARA: Final[str] = "sin formato"

COL_SEMANA: Final[int] = 0
COL_NUMERO: Final[int] = 1
COL_ANIO: Final[int] = 2
COL_PUNTOS: Final[int] = 3


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
    ventana.refrescos = []
    ventana.refrescar_todo = lambda: ventana.refrescos.append(1)
    try:
        yield ventana
    finally:
        ventana.destroy()


@pytest.fixture()
def semanas(conn: sqlite3.Connection) -> dict[str, int]:
    """Dos semanas de catalogo, la 29 con puntos ya extraidos del PDF."""
    ids = {
        texto: core.obtener_o_crear_semana(conn, texto)
        for texto in (SEMANA_29, SEMANA_30)
    }
    conn.commit()
    core.actualizar_puntos_semana(conn, ids[SEMANA_29], 20003, manual=False)
    return ids


@pytest.fixture()
def ventana(app_falsa: tk.Tk) -> Iterator[gui_inventario.VentanaPuntosSemana]:
    """Dialogo de puntos montado sobre la app falsa."""
    dialogo = gui_inventario.VentanaPuntosSemana(app_falsa)
    try:
        yield dialogo
    finally:
        dialogo.destroy()


def _puntos_de(conexion: sqlite3.Connection, semana_id: int) -> int:
    """Lee los puntos persistidos de una semana."""
    fila = conexion.execute(
        "SELECT COALESCE(puntos_bw_acumulados, 0) AS p FROM semanas_catalogo WHERE id = ?",
        (semana_id,),
    ).fetchone()
    assert fila is not None
    return int(fila["p"])


def _metodo_ast(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


# --------------------------------------------------------------------------
# T6 -- listado de semanas
# --------------------------------------------------------------------------


def test_ventana_puntos_lista_las_semanas_desde_core(
    ventana: gui_inventario.VentanaPuntosSemana, semanas: dict[str, int]
) -> None:
    """T6: el arbol se llena con `core.listar_semanas`, `iid` = id de la semana."""
    ventana._refrescar()

    assert set(ventana.tree.get_children()) == {str(i) for i in semanas.values()}
    valores = ventana.tree.item(str(semanas[SEMANA_29]), "values")
    assert valores[COL_SEMANA] == SEMANA_29
    assert valores[COL_NUMERO] == "29"
    assert valores[COL_ANIO] == "2026"
    assert valores[COL_PUNTOS] == "20003"


def test_ventana_puntos_semana_sin_parsear_deja_numero_y_anio_en_blanco(
    ventana: gui_inventario.VentanaPuntosSemana, conn: sqlite3.Connection
) -> None:
    """T6: un texto que no case con el patron se lista igual, sin 'None'."""
    semana_id = core.obtener_o_crear_semana(conn, SEMANA_RARA)
    conn.commit()

    ventana._refrescar()

    valores = ventana.tree.item(str(semana_id), "values")
    assert valores[COL_SEMANA] == SEMANA_RARA
    assert valores[COL_NUMERO] == ""
    assert valores[COL_ANIO] == ""
    assert valores[COL_PUNTOS] == "0"


def test_ventana_puntos_base_vacia_no_pinta_filas(
    ventana: gui_inventario.VentanaPuntosSemana,
) -> None:
    """T6: sin semanas de catalogo el dialogo abre vacio, sin fallar."""
    assert ventana.tree.get_children() == ()


def test_ventana_puntos_seleccionar_precarga_el_campo(
    ventana: gui_inventario.VentanaPuntosSemana, semanas: dict[str, int]
) -> None:
    """T6: al elegir una semana su valor actual entra en el campo editable."""
    ventana._refrescar()

    ventana.tree.selection_set(str(semanas[SEMANA_29]))
    ventana._al_seleccionar()

    assert ventana.puntos_var.get() == "20003"


# --------------------------------------------------------------------------
# T6 -- guardado manual
# --------------------------------------------------------------------------


def test_guardar_puntos_manual_llama_core(
    ventana: gui_inventario.VentanaPuntosSemana,
    conn: sqlite3.Connection,
    semanas: dict[str, int],
) -> None:
    """T6, R8: la correccion persiste con `manual=True` y la lista se refresca."""
    ventana._refrescar()
    ventana.tree.selection_set(str(semanas[SEMANA_30]))
    ventana.puntos_var.set("1500")

    ventana._guardar()

    assert _puntos_de(conn, semanas[SEMANA_30]) == 1500
    assert ventana.tree.item(str(semanas[SEMANA_30]), "values")[COL_PUNTOS] == "1500"


def test_guardar_puntos_pasa_manual_true_a_la_capa_core(
    ventana: gui_inventario.VentanaPuntosSemana, semanas: dict[str, int]
) -> None:
    """T6, R6/R8: sin `manual=True` una correccion a la baja se perderia."""
    ventana._refrescar()
    ventana.tree.selection_set(str(semanas[SEMANA_29]))
    ventana.puntos_var.set("777")

    with mock.patch.object(gui_inventario.core, "actualizar_puntos_semana") as guardar:
        ventana._guardar()

    assert guardar.call_count == 1
    assert guardar.call_args.kwargs == {"manual": True}
    assert guardar.call_args.args[1:] == (semanas[SEMANA_29], 777)


def test_guardar_puntos_permite_corregir_a_la_baja(
    ventana: gui_inventario.VentanaPuntosSemana,
    conn: sqlite3.Connection,
    semanas: dict[str, int],
) -> None:
    """T6, R8: la usuaria gana sobre el extract, incluso bajando el valor."""
    ventana._refrescar()
    ventana.tree.selection_set(str(semanas[SEMANA_29]))
    ventana.puntos_var.set("10")

    ventana._guardar()

    assert _puntos_de(conn, semanas[SEMANA_29]) == 10


def test_guardar_puntos_sin_seleccion_avisa_y_no_escribe(
    ventana: gui_inventario.VentanaPuntosSemana,
    conn: sqlite3.Connection,
    semanas: dict[str, int],
) -> None:
    """T6: sin semana seleccionada se avisa inline y la base no se toca."""
    ventana._refrescar()
    ventana.puntos_var.set("999")

    ventana._guardar()

    assert "selecciona una semana" in ventana.status_label.cget("text").lower()
    assert _puntos_de(conn, semanas[SEMANA_29]) == 20003


def test_guardar_puntos_no_numericos_avisa_y_no_escribe(
    ventana: gui_inventario.VentanaPuntosSemana,
    conn: sqlite3.Connection,
    semanas: dict[str, int],
) -> None:
    """T6: un valor que no es entero avisa inline y no persiste nada."""
    ventana._refrescar()
    ventana.tree.selection_set(str(semanas[SEMANA_29]))
    ventana.puntos_var.set("muchos")

    ventana._guardar()

    assert "entero" in ventana.status_label.cget("text").lower()
    assert _puntos_de(conn, semanas[SEMANA_29]) == 20003


def test_guardar_puntos_negativos_avisa_y_no_escribe(
    ventana: gui_inventario.VentanaPuntosSemana,
    conn: sqlite3.Connection,
    semanas: dict[str, int],
) -> None:
    """T6: los puntos son un acumulado; un negativo no es corregible."""
    ventana._refrescar()
    ventana.tree.selection_set(str(semanas[SEMANA_29]))
    ventana.puntos_var.set("-5")

    ventana._guardar()

    assert "negativos" in ventana.status_label.cget("text").lower()
    assert _puntos_de(conn, semanas[SEMANA_29]) == 20003


def test_guardar_puntos_error_de_base_sale_por_dialogo_sin_crash(
    ventana: gui_inventario.VentanaPuntosSemana, semanas: dict[str, int]
) -> None:
    """T6: un fallo de SQLite se muestra por `messagebox`, sin escapar."""
    ventana._refrescar()
    ventana.tree.selection_set(str(semanas[SEMANA_29]))
    ventana.puntos_var.set("42")

    with mock.patch.object(
        gui_inventario.core,
        "actualizar_puntos_semana",
        side_effect=sqlite3.OperationalError("base bloqueada"),
    ), mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        ventana._guardar()

    assert showerror.call_count == 1


# --------------------------------------------------------------------------
# T6 -- restricciones de capa y punto de entrada
# --------------------------------------------------------------------------


def test_ventana_puntos_no_ejecuta_sql() -> None:
    """ADR-2: el dialogo no escribe SQL; lee y escribe siempre por `core`."""
    fuente = ast.unparse(_metodo_ast("VentanaPuntosSemana", "_refrescar"))
    fuente += ast.unparse(_metodo_ast("VentanaPuntosSemana", "_guardar"))

    for palabra in ("SELECT", "INSERT INTO", "UPDATE ", "execute(", "get_conn"):
        assert palabra not in fuente, f"El dialogo ejecuta SQL: aparece {palabra!r}"


def test_barra_superior_abre_la_ventana_de_puntos() -> None:
    """T6, D9: hay un punto de entrada real desde la barra superior."""
    fuente = ast.unparse(_metodo_ast("App", "_construir_barra_superior"))
    apertura = ast.unparse(_metodo_ast("App", "abrir_ventana_puntos"))

    assert "command=self.abrir_ventana_puntos" in fuente
    assert "VentanaPuntosSemana(self)" in apertura
