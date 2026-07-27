"""Pestana de Asociados y su formulario, cableados a SQLite (MERC-07 T5).

Se instancian widgets reales sobre un `Tk()` oculto, no `App`: montar la app
completa abriria la base de produccion y correria el backup de arranque. La app
falsa es la propia raiz de Tk con dos atributos extra (`conn` y
`refrescar_todo`), porque `VentanaAsociadoForm` es un `Toplevel` y necesita un
master de verdad.

Los dialogos (`messagebox`, `simpledialog`) y el navegador son I/O externo: se
parchean con `mock.patch` como context manager (`.langs/python.md` 7, 8.9). La
capa core no se parchea nunca -- corre contra una base en memoria real, que es
lo unico que prueba que el `iid` del Treeview es el `id` verdadero.
"""

from __future__ import annotations

import ast
import sqlite3
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from tkinter import ttk
from typing import Any, Final
from unittest import mock

import pytest

import core
import db
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

NOMBRE_A: Final[str] = "Aura Jannet Ramirez"
NOMBRE_B: Final[str] = "Etnan Gamaliel Perez"
TELEFONO_A: Final[str] = "5512345678"

COLUMNAS_ESPERADAS: Final[tuple[str, ...]] = (
    "nombre",
    "telefono",
    "status",
    "saldo",
    "notas",
)

COL_NOMBRE: Final[int] = 0
COL_STATUS: Final[int] = 2
COL_SALDO: Final[int] = 3


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
def tab(app_falsa: tk.Tk) -> gui_inventario.TabAsociados:
    """Pestana de Asociados montada sobre la app falsa."""
    return gui_inventario.TabAsociados(ttk.Notebook(app_falsa), app_falsa)


def fila_pdf(*, folio: str = "C001264", nombre_asociado: str) -> dict[str, Any]:
    """Fila del extractor: sirve para dejar detalle ligado a un asociado."""
    return {
        "Fecha registro": "2026-07-22 09:00",
        "Semana": "01-2026",
        "Folio de pedido": folio,
        "Codigo nota": "8043",
        "Distribuidora": "C0001 DISTRIBUIDORA CENTRO",
        "Nombre asociado": nombre_asociado,
        "Archivo origen": f"{folio}_NOTA.pdf (pag. 1)",
        "Codigo articulo": "11111",
        "Descripcion": "Sarten antiadherente 24cm",
        "Cantidad solicitada": 3,
        "Cantidad surtida": 3,
        "Cantidad Asociado": 0,
        "Cantidad Casa": 3,
        "Cantidad Local": 0,
        "Precio catalogo": 249.0,
        "Precio con IVA": 288.84,
        "Precio que pagas": 199.0,
        "Valor total con IVA": 866.52,
        "Tipo": "Normal (con descuento)",
        "Ocurrencia": 1,
    }


def fuente_de(clase: str) -> str:
    """Codigo de la clase `clase` sin docstrings, para aserciones de cableado."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            return "\n".join(
                ast.unparse(hijo)
                for hijo in ast.walk(nodo)
                if isinstance(hijo, ast.Call | ast.Attribute | ast.Name)
            )
    raise AssertionError(f"No se encontro la clase {clase}")


def test_tabasociados_expone_estado_y_saldo_pendiente(
    tab: gui_inventario.TabAsociados,
) -> None:
    """R8: la tabla gana las columnas Estado y Saldo pendiente."""
    columnas = tuple(str(c) for c in tab.tree["columns"])

    assert columnas == COLUMNAS_ESPERADAS
    assert tab.tree.heading("status")["text"] == "Estado"
    assert tab.tree.heading("saldo")["text"] == "Saldo pendiente"


def test_tabasociados_refrescar_usa_el_id_real_como_iid(
    tab: gui_inventario.TabAsociados, conn: sqlite3.Connection
) -> None:
    """R8: el `iid` de cada fila es `asociados.id`, no el indice de fila."""
    id_a = core.crear_asociado(conn, NOMBRE_A, TELEFONO_A, "sin notas")
    id_b = core.crear_asociado(conn, NOMBRE_B, "", "", "Inactivo")

    tab.refrescar()

    assert tab.tree.get_children() == (str(id_a), str(id_b))
    assert tab.tree.item(str(id_a), "values")[COL_NOMBRE] == NOMBRE_A
    assert tab.tree.item(str(id_b), "values")[COL_STATUS] == "Inactivo"
    assert tab.tree.item(str(id_a), "values")[COL_SALDO] == "$0.00"


def test_tabasociados_eliminar_muestra_el_mensaje_del_asociadoerror(
    tab: gui_inventario.TabAsociados, conn: sqlite3.Connection
) -> None:
    """R9, DEUDA-02: la FK del detalle bloquea la baja y la excepcion no escapa."""
    core.confirmar_carga(conn, [fila_pdf(nombre_asociado=NOMBRE_A)])
    tab.refrescar()
    tab.tree.selection_set(tab.tree.get_children()[0])

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=True), \
            mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        tab._eliminar()

    assert showerror.call_count == 1
    assert "entregas ligadas" in showerror.call_args.args[1]
    assert core.listar_asociados(conn) != []


def test_tabasociados_eliminar_borra_al_asociado_sin_movimientos(
    tab: gui_inventario.TabAsociados, conn: sqlite3.Connection, app_falsa: tk.Tk
) -> None:
    """R9: sin FKs que lo protejan la baja pasa y se refresca la app."""
    id_a = core.crear_asociado(conn, NOMBRE_A)
    tab.refrescar()
    tab.tree.selection_set(str(id_a))

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=True):
        tab._eliminar()

    assert core.listar_asociados(conn) == []
    assert app_falsa.refrescos == [1]


def test_tabasociados_whatsapp_sin_telefono_no_abre_el_navegador(
    tab: gui_inventario.TabAsociados, conn: sqlite3.Connection
) -> None:
    """R9: `link_whatsapp` devuelve `None` y la GUI solo avisa."""
    id_a = core.crear_asociado(conn, NOMBRE_A, telefono="")
    tab.refrescar()
    tab.tree.selection_set(str(id_a))

    with mock.patch.object(gui_inventario.webbrowser, "open") as abrir, \
            mock.patch.object(gui_inventario.messagebox, "showwarning") as aviso:
        tab._enviar_whatsapp()

    assert abrir.call_count == 0
    assert aviso.call_count == 1


def test_tabasociados_whatsapp_con_telefono_abre_el_link_de_pdf_extractor(
    tab: gui_inventario.TabAsociados, conn: sqlite3.Connection
) -> None:
    """R9: el link lo construye `pdf_extractor.link_whatsapp`, no la GUI."""
    id_a = core.crear_asociado(conn, NOMBRE_A, telefono=TELEFONO_A)
    tab.refrescar()
    tab.tree.selection_set(str(id_a))

    with mock.patch.object(gui_inventario.simpledialog, "askstring", return_value="Hola"), \
            mock.patch.object(gui_inventario.webbrowser, "open") as abrir:
        tab._enviar_whatsapp()

    assert abrir.call_args.args[0] == gui_inventario.pdf_extractor.link_whatsapp(
        TELEFONO_A, "Hola"
    )


def test_formulario_agregar_persiste_con_el_estado_elegido(
    app_falsa: tk.Tk, conn: sqlite3.Connection
) -> None:
    """R9: el combobox Estado viaja hasta `core.crear_asociado`."""
    form = gui_inventario.VentanaAsociadoForm(app_falsa, modo="agregar")
    form.nombre_var.set(NOMBRE_A)
    form.telefono_var.set(TELEFONO_A)
    form.status_var.set("Inactivo")

    form._guardar()

    asociados = core.listar_asociados(conn)
    assert [f["nombre"] for f in asociados] == [NOMBRE_A]
    assert asociados[0]["status"] == "Inactivo"
    assert asociados[0]["telefono"] == TELEFONO_A


def test_formulario_editar_precarga_y_actualiza_por_id(
    app_falsa: tk.Tk, conn: sqlite3.Connection
) -> None:
    """R9: el formulario se precarga con el dict de `listar_asociados`."""
    id_a = core.crear_asociado(conn, NOMBRE_A, TELEFONO_A, "nota vieja")
    datos = core.listar_asociados(conn)[0]

    form = gui_inventario.VentanaAsociadoForm(
        app_falsa, modo="editar", asociado_id=id_a, datos=datos
    )
    assert form.nombre_var.get() == NOMBRE_A
    assert form.telefono_var.get() == TELEFONO_A
    assert form.status_var.get() == "Activo"
    assert form.notas_text.get("1.0", tk.END).strip() == "nota vieja"

    form.nombre_var.set(NOMBRE_B)
    form.status_var.set("Inactivo")
    form._guardar()

    actualizado = core.listar_asociados(conn)[0]
    assert actualizado["id"] == id_a
    assert actualizado["nombre"] == NOMBRE_B
    assert actualizado["status"] == "Inactivo"


def test_formulario_con_nombre_en_blanco_no_persiste(
    app_falsa: tk.Tk, conn: sqlite3.Connection
) -> None:
    """R3: nombre vacio -> aviso y ninguna escritura."""
    form = gui_inventario.VentanaAsociadoForm(app_falsa, modo="agregar")
    form.nombre_var.set("   ")

    with mock.patch.object(gui_inventario.messagebox, "showwarning") as aviso:
        form._guardar()

    assert aviso.call_count == 1
    assert core.listar_asociados(conn) == []
    form.destroy()


def test_asociados_no_referencian_el_maestro_de_excel() -> None:
    """R8: ni la pestana ni el formulario tocan ya `EXCEL_PATH` ni pandas."""
    fuente = fuente_de("TabAsociados") + fuente_de("VentanaAsociadoForm")

    assert "EXCEL_PATH" not in fuente
    assert "leer_directorio_asociados" not in fuente
    assert "core.listar_asociados(self.app.conn)" in fuente
