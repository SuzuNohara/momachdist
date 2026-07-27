"""Pestana de Clientes y su formulario (CLI-01, T8 a T13).

La pestana es nueva, asi que la suite cubre las cuatro piezas: las columnas del
Treeview (T8), el poblado con `iid` = `clientes.id` (T9), la precarga del
formulario (T10), el guardado con nombre obligatorio (T11), el borrado que
traduce `ClienteError` a `messagebox` (T12) y el registro de la pestana en el
notebook (T13).

Se instancian widgets reales contra un `Tk()` oculto que hace de `app`; nunca la
clase `App`, que abriria la base de produccion y correria el backup de arranque.
T13 es la excepcion: se verifica por AST sobre `App.__init__`, sin construir
nada. Solo se parchean los dialogos (`messagebox`), que son I/O externo -- la
capa core corre de verdad contra una base en memoria.
"""

from __future__ import annotations

import ast
import sqlite3
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from tkinter import ttk
from typing import Final
from unittest import mock

import pytest

import core
import db
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

NOMBRE_A: Final[str] = "Ana Lucia Torres"
NOMBRE_B: Final[str] = "Beatriz Mendoza"
TELEFONO_A: Final[str] = "5512345678"
DIRECCION_A: Final[str] = "Av. Reforma 100"
NOTAS_A: Final[str] = "Prefiere entregas por la tarde"

COLUMNAS_ESPERADAS: Final[tuple[str, ...]] = (
    "nombre",
    "telefono",
    "direccion",
    "notas",
)

COL_NOMBRE: Final[int] = 0
COL_TELEFONO: Final[int] = 1
COL_DIRECCION: Final[int] = 2
COL_NOTAS: Final[int] = 3


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
def tab(app_falsa: tk.Tk) -> gui_inventario.TabClientes:
    """Pestana de Clientes montada sobre la app falsa."""
    return gui_inventario.TabClientes(ttk.Notebook(app_falsa), app_falsa)


def alta_venta(conexion: sqlite3.Connection, cliente_id: int) -> None:
    """Registra una venta ligada al cliente, para bloquear su baja (R6)."""
    conexion.execute("INSERT INTO ventas (cliente_id) VALUES (?)", (cliente_id,))
    conexion.commit()


def metodo_ast(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


def test_tabclientes_has_expected_columns(tab: gui_inventario.TabClientes) -> None:
    """T8: barra + Treeview con las cuatro columnas del contrato."""
    columnas = tuple(str(c) for c in tab.tree["columns"])

    assert columnas == COLUMNAS_ESPERADAS
    assert tab.tree.heading("nombre")["text"] == "Nombre"
    assert tab.tree.heading("direccion")["text"] == "Dirección"
    assert isinstance(tab, ttk.Frame)


def test_tabclientes_refrescar_lists_rows(
    tab: gui_inventario.TabClientes, conn: sqlite3.Connection
) -> None:
    """T9: el arbol se llena desde `core.listar_clientes`, con `iid` = `id`."""
    id_b = core.crear_cliente(conn, NOMBRE_B)
    id_a = core.crear_cliente(conn, NOMBRE_A, TELEFONO_A, DIRECCION_A, NOTAS_A)

    tab.refrescar()

    assert tab.tree.get_children() == (str(id_a), str(id_b))
    valores = tab.tree.item(str(id_a), "values")
    assert valores[COL_NOMBRE] == NOMBRE_A
    assert valores[COL_TELEFONO] == TELEFONO_A
    assert valores[COL_DIRECCION] == DIRECCION_A
    assert valores[COL_NOTAS] == NOTAS_A


def test_tabclientes_refrescar_deja_vacios_los_campos_opcionales_nulos(
    tab: gui_inventario.TabClientes, conn: sqlite3.Connection
) -> None:
    """T9: los `NULL` del CRM se pintan como cadena vacia, no como 'None'."""
    id_a = core.crear_cliente(conn, NOMBRE_A)

    tab.refrescar()

    assert tab.tree.item(str(id_a), "values")[COL_TELEFONO] == ""
    assert tab.tree.item(str(id_a), "values")[COL_NOTAS] == ""


def test_tabclientes_id_seleccionado_devuelve_el_id_o_none(
    tab: gui_inventario.TabClientes, conn: sqlite3.Connection
) -> None:
    """T9: `_id_seleccionado` traduce la seleccion a la clave primaria."""
    id_a = core.crear_cliente(conn, NOMBRE_A)
    tab.refrescar()

    with mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        sin_seleccion = tab._id_seleccionado()
    tab.tree.selection_set(str(id_a))
    con_seleccion = tab._id_seleccionado()

    assert sin_seleccion is None
    assert aviso.call_count == 1
    assert con_seleccion == id_a


def test_ventanaclienteform_prefills_edit(
    app_falsa: tk.Tk, conn: sqlite3.Connection
) -> None:
    """T10: en modo editar los cuatro campos llegan precargados."""
    cliente_id = core.crear_cliente(conn, NOMBRE_A, TELEFONO_A, DIRECCION_A, NOTAS_A)
    datos = core.listar_clientes(conn)[0]

    form = gui_inventario.VentanaClienteForm(
        app_falsa, modo="editar", cliente_id=cliente_id, datos=datos
    )

    assert form.cliente_id == cliente_id
    assert form.nombre_var.get() == NOMBRE_A
    assert form.telefono_var.get() == TELEFONO_A
    assert form.direccion_var.get() == DIRECCION_A
    assert form.notas_text.get("1.0", tk.END).strip() == NOTAS_A
    form.destroy()


def test_ventanaclienteform_agregar_nace_vacio(app_falsa: tk.Tk) -> None:
    """T10: en modo agregar no hay precarga de ningun campo."""
    form = gui_inventario.VentanaClienteForm(app_falsa, modo="agregar")

    assert form.cliente_id is None
    assert form.nombre_var.get() == ""
    assert form.direccion_var.get() == ""
    assert form.notas_text.get("1.0", tk.END).strip() == ""
    form.destroy()


def test_form_blank_name_no_persist(
    app_falsa: tk.Tk, conn: sqlite3.Connection
) -> None:
    """T11, R3: nombre en blanco -> aviso y ninguna escritura."""
    form = gui_inventario.VentanaClienteForm(app_falsa, modo="agregar")
    form.nombre_var.set("   ")
    form.telefono_var.set(TELEFONO_A)

    with mock.patch.object(gui_inventario.messagebox, "showwarning") as aviso:
        form._guardar()

    assert aviso.call_count == 1
    assert core.listar_clientes(conn) == []
    assert app_falsa.refrescos == []
    form.destroy()


def test_form_valid_name_calls_core(
    app_falsa: tk.Tk, conn: sqlite3.Connection
) -> None:
    """T11: con nombre valido se persiste via `crear_cliente` y se refresca."""
    form = gui_inventario.VentanaClienteForm(app_falsa, modo="agregar")
    form.nombre_var.set(NOMBRE_A)
    form.telefono_var.set(TELEFONO_A)
    form.direccion_var.set(DIRECCION_A)

    form._guardar()

    clientes = core.listar_clientes(conn)
    assert [c["nombre"] for c in clientes] == [NOMBRE_A]
    assert clientes[0]["telefono"] == TELEFONO_A
    assert clientes[0]["direccion"] == DIRECCION_A
    assert app_falsa.refrescos == [1]


def test_form_edit_calls_editar_cliente_por_id(
    app_falsa: tk.Tk, conn: sqlite3.Connection
) -> None:
    """T11: en modo editar el guardado actualiza la fila por clave primaria."""
    cliente_id = core.crear_cliente(conn, NOMBRE_A, TELEFONO_A)
    form = gui_inventario.VentanaClienteForm(
        app_falsa, modo="editar", cliente_id=cliente_id,
        datos=core.listar_clientes(conn)[0],
    )
    form.nombre_var.set(NOMBRE_B)

    form._guardar()

    clientes = core.listar_clientes(conn)
    assert len(clientes) == 1
    assert clientes[0]["id"] == cliente_id
    assert clientes[0]["nombre"] == NOMBRE_B


def test_eliminar_ui_surfaces_clienteerror_message(
    tab: gui_inventario.TabClientes, conn: sqlite3.Connection
) -> None:
    """T12, R10: la FK de ventas bloquea la baja y el mensaje sale por dialogo."""
    cliente_id = core.crear_cliente(conn, NOMBRE_A)
    alta_venta(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(cliente_id))

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=True), \
            mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        tab._eliminar()

    assert showerror.call_count == 1
    assert "ventas o encargos ligados" in showerror.call_args.args[1]
    assert len(core.listar_clientes(conn)) == 1


def test_eliminar_borra_al_cliente_sin_movimientos(
    tab: gui_inventario.TabClientes, conn: sqlite3.Connection, app_falsa: tk.Tk
) -> None:
    """T12: sin FKs que lo protejan la baja pasa y la app se refresca."""
    cliente_id = core.crear_cliente(conn, NOMBRE_A)
    tab.refrescar()
    tab.tree.selection_set(str(cliente_id))

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=True):
        tab._eliminar()

    assert core.listar_clientes(conn) == []
    assert app_falsa.refrescos == [1]


def test_eliminar_cancelado_no_borra_nada(
    tab: gui_inventario.TabClientes, conn: sqlite3.Connection
) -> None:
    """T12: si el usuario dice que no en la confirmacion, no se toca la base."""
    cliente_id = core.crear_cliente(conn, NOMBRE_A)
    tab.refrescar()
    tab.tree.selection_set(str(cliente_id))

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=False):
        tab._eliminar()

    assert len(core.listar_clientes(conn)) == 1


def test_notebook_includes_clientes_tab() -> None:
    """T13: `App.__init__` construye y registra la pestana de Clientes."""
    fuente = ast.unparse(metodo_ast("App", "__init__"))

    assert "self.tab_clientes = TabClientes(self.notebook, self)" in fuente
    assert "self.notebook.add(self.tab_clientes" in fuente


def test_refrescar_todo_incluye_la_pestana_de_clientes() -> None:
    """T13: sin esta llamada la pestana nunca se poblaria tras una carga."""
    fuente = ast.unparse(metodo_ast("App", "refrescar_todo"))

    assert "self.tab_clientes.refrescar()" in fuente
