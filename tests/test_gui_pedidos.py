"""Pestana de Pedidos cableada a SQLite (MERC-06 T3, T4).

El resto de la suite de GUI verifica el cableado por AST porque construir
widgets en CI es fragil. Aqui no basta: lo que hay que probar es que el
Treeview **se llena de verdad** con lo que devuelve `core.obtener_movimientos`
y que los cuatro filtros siguen combinando con AND sobre las claves que la
consulta preserva. Asi que se instancian widgets reales contra un `Tk()`
oculto, con una app falsa que solo aporta la conexion en memoria.

No se instancia `App`: abriria la base de produccion, correria el backup de
arranque y construiria siete pestanas. La fixture se salta el modulo entero si
no hay servidor X disponible.
"""

from __future__ import annotations

import ast
import sqlite3
import tkinter as tk
import types
from collections.abc import Iterator
from pathlib import Path
from tkinter import ttk
from typing import Any, Final

import pytest

import core
import db
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

FOLIO_A: Final[str] = "C001264"
FOLIO_B: Final[str] = "C001265"

NOMBRE_A: Final[str] = "AURA JANNET RAMIREZ"
NOMBRE_B: Final[str] = "ETNAN GAMALIEL PEREZ"

SEMANA_A: Final[str] = "27 - 2026"
SEMANA_B: Final[str] = "28 - 2026"

#: Indices de columna del Treeview de la pestana, en el orden de `columnas`.
COL_SEMANA: Final[int] = 0
COL_FOLIO: Final[int] = 1
COL_CODIGO: Final[int] = 2
COL_ASOCIADO: Final[int] = 4


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


@pytest.fixture()
def raiz() -> Iterator[tk.Tk]:
    """Raiz de Tk oculta; se destruye siempre, aunque el test falle."""
    try:
        ventana = tk.Tk()
    except tk.TclError:  # pragma: no cover - depende del entorno, no del codigo
        pytest.skip("No hay servidor X disponible para instanciar widgets")
    ventana.withdraw()
    try:
        yield ventana
    finally:
        ventana.destroy()


@pytest.fixture()
def tab(raiz: tk.Tk, conn: sqlite3.Connection) -> gui_inventario.TabPedidos:
    """Pestana de Pedidos montada sobre una app falsa con la conexion."""
    notebook = ttk.Notebook(raiz)
    app_falsa = types.SimpleNamespace(conn=conn, refrescar_todo=lambda: None)
    return gui_inventario.TabPedidos(notebook, app_falsa)


def fila_pdf(
    *,
    folio: str = FOLIO_A,
    codigo: str = "11111",
    descripcion: str = "Sarten antiadherente 24cm",
    nombre_asociado: str = NOMBRE_A,
    surtida: int = 3,
    casa: int = 3,
) -> dict[str, Any]:
    """Fila con las claves que entrega `pdf_extractor.procesar_pdf`."""
    return {
        "Fecha registro": "2026-07-22 09:00",
        "Semana": "01-2026",
        "Folio de pedido": folio,
        "Codigo nota": "8043",
        "Distribuidora": "C0001 DISTRIBUIDORA CENTRO",
        "Nombre asociado": nombre_asociado,
        "Archivo origen": f"{folio}_NOTA.pdf (pag. 1)",
        "Codigo articulo": codigo,
        "Descripcion": descripcion,
        "Cantidad solicitada": surtida,
        "Cantidad surtida": surtida,
        "Cantidad Asociado": 0,
        "Cantidad Casa": casa,
        "Cantidad Local": surtida - casa,
        "Precio catalogo": 249.0,
        "Precio con IVA": 288.84,
        "Precio que pagas": 199.0,
        "Valor total con IVA": 866.52,
        "Tipo": "Normal (con descuento)",
        "Ocurrencia": 1,
    }


def vincular_semana(
    conexion: sqlite3.Connection, folio: str, semana_texto: str
) -> None:
    """Ata un folio ya cargado a una semana del catalogo (lo hara BW-01)."""
    conexion.execute(
        "INSERT INTO semanas_catalogo (semana_texto) VALUES (?)", (semana_texto,)
    )
    conexion.execute(
        "UPDATE pedidos SET semana_id = "
        "(SELECT id FROM semanas_catalogo WHERE semana_texto = ?) "
        "WHERE folio_pedido = ?",
        (semana_texto, folio),
    )
    conexion.commit()


def valores(tab_pedidos: gui_inventario.TabPedidos, columna: int) -> list[str]:
    """Columna `columna` de las filas visibles del Treeview, en orden."""
    arbol = tab_pedidos.tree
    return [str(arbol.item(iid, "values")[columna]) for iid in arbol.get_children()]


def metodo_ast(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


def cuerpo_ejecutable(clase: str, nombre: str) -> str:
    """Codigo del metodo sin su docstring, para afirmar sobre lo que se ejecuta.

    Un docstring que *menciona* `EXCEL_PATH` no es una referencia al Excel; sin
    esta poda la asercion mediria la prosa en vez del cableado.
    """
    nodo = metodo_ast(clase, nombre)
    sentencias = nodo.body
    if ast.get_docstring(nodo) is not None:
        sentencias = sentencias[1:]
    return "\n".join(ast.unparse(s) for s in sentencias)


def test_refrescar_carga_desde_conexion(
    tab: gui_inventario.TabPedidos, conn: sqlite3.Connection
) -> None:
    """T3: el historial sale de `core.obtener_movimientos(self.app.conn)`."""
    core.confirmar_carga(conn, [fila_pdf()])
    vincular_semana(conn, FOLIO_A, SEMANA_A)

    tab.refrescar()

    assert len(tab.datos_completos) == 1
    assert tab.datos_completos[0]["Folio de pedido"] == FOLIO_A
    assert valores(tab, COL_FOLIO) == [FOLIO_A]
    assert valores(tab, COL_SEMANA) == [SEMANA_A]
    assert tuple(tab.combo_semana["values"]) == ("Todas", SEMANA_A)


def test_refrescar_con_base_vacia_deja_la_tabla_sin_filas(
    tab: gui_inventario.TabPedidos,
) -> None:
    """T3: sin movimientos no hay guarda de Excel que valga, solo `[]`."""
    tab.refrescar()

    assert tab.datos_completos == []
    assert tab.tree.get_children() == ()
    assert tuple(tab.combo_semana["values"]) == ("Todas",)


def test_refrescar_no_referencia_el_maestro_de_excel() -> None:
    """T3: la rama `os.path.exists(EXCEL_PATH)` desaparecio del metodo."""
    fuente = cuerpo_ejecutable("TabPedidos", "refrescar")

    assert "core.obtener_movimientos(self.app.conn)" in fuente
    assert "EXCEL_PATH" not in fuente
    assert "os.path.exists" not in fuente


def test_filtros_combinan_and_sobre_claves() -> None:
    """T4: los cuatro filtros se aplican con AND sobre las claves preservadas.

    Se monta la pestana dentro del propio test (y no con la fixture) porque
    cada asercion necesita el mismo arbol tras cambiar un filtro distinto: son
    cuatro escenarios sobre un unico estado sembrado.
    """
    conexion = db.init_db(":memory:")
    try:
        ventana = tk.Tk()
    except tk.TclError:  # pragma: no cover - depende del entorno, no del codigo
        conexion.close()
        pytest.skip("No hay servidor X disponible para instanciar widgets")
    ventana.withdraw()

    try:
        core.confirmar_carga(
            conexion,
            [
                fila_pdf(folio=FOLIO_A, codigo="11111", nombre_asociado=NOMBRE_A),
                fila_pdf(
                    folio=FOLIO_B,
                    codigo="22222",
                    descripcion="Juego de sabanas",
                    nombre_asociado=NOMBRE_B,
                ),
            ],
        )
        vincular_semana(conexion, FOLIO_A, SEMANA_A)
        vincular_semana(conexion, FOLIO_B, SEMANA_B)
        app_falsa = types.SimpleNamespace(conn=conexion, refrescar_todo=lambda: None)
        tab_pedidos = gui_inventario.TabPedidos(ttk.Notebook(ventana), app_falsa)
        tab_pedidos.refrescar()

        assert tuple(tab_pedidos.combo_semana["values"]) == ("Todas", SEMANA_A, SEMANA_B)
        assert sorted(valores(tab_pedidos, COL_CODIGO)) == ["11111", "22222"]

        tab_pedidos.filtro_producto.set("sabanas")
        assert valores(tab_pedidos, COL_CODIGO) == ["22222"]

        tab_pedidos.filtro_asociado.set("aura")
        assert valores(tab_pedidos, COL_CODIGO) == []

        tab_pedidos.filtro_producto.set("")
        assert valores(tab_pedidos, COL_ASOCIADO) == [NOMBRE_A]

        tab_pedidos.filtro_semana.set(SEMANA_B)
        tab_pedidos._aplicar_filtro()
        assert valores(tab_pedidos, COL_CODIGO) == []

        tab_pedidos.filtro_semana.set(SEMANA_A)
        tab_pedidos._aplicar_filtro()
        assert valores(tab_pedidos, COL_FOLIO) == [FOLIO_A]

        tab_pedidos.filtro_folio.set(FOLIO_B)
        assert valores(tab_pedidos, COL_FOLIO) == []

        tab_pedidos.filtro_asociado.set("")
        tab_pedidos.filtro_semana.set("Todas")
        tab_pedidos._aplicar_filtro()
        assert valores(tab_pedidos, COL_FOLIO) == [FOLIO_B]
    finally:
        ventana.destroy()
        conexion.close()
