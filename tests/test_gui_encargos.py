"""Pestana de Encargos y su formulario (ENC-02 R11-R15, ENC-04 R4-R10).

Las dos actividades comparten pantalla, asi que comparten suite: la conversion
(ENC-04) es una accion mas de la misma barra y su boton se habilita leyendo el
mismo listado que pinta ENC-02.

**El eje que justifica media suite:** el boton Surtir y la conversion usan el
mismo criterio, pero el boton se pinta en el refresco y la conversion ocurre en
el clic. Entre los dos momentos el stock puede haberse agotado --otra venta, otro
encargo surtido-- y entonces `surtir_encargo` levanta `VentaError` con un boton
que decia que si. Ese hueco se prueba de verdad
(`test_surtir_con_el_stock_agotado_...`): se agota el inventario **despues** de
refrescar y se pulsa igual. La validacion real es la del core; la GUI no replica
la logica de stock.

Se instancian widgets reales contra un `Tk()` oculto que hace de `app`; nunca la
clase `App`, que abriria la base de produccion y correria el backup de arranque.
Lo unico parcheado son los dialogos (`messagebox`) y `VentanaPagos` cuando se
verifica con que argumentos se abre: la capa core corre de verdad contra una
base en memoria con el esquema canonico.

Los helpers de siembra se reusan de `tests/test_conversion_encargo.py` en vez de
duplicarlos: son los mismos que la suite de la capa core, de modo que la GUI se
prueba contra el inventario que el core considera valido (la trampa de
`vw_existencias`, que solo cuenta `cantidad_casa + cantidad_local`, ya esta
resuelta alli).
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
from tests.test_conversion_encargo import _linea, _seed_cliente, _seed_stock

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

CODIGO: Final[str] = "ART-001"
DESCRIPCION: Final[str] = "Sarten antiadherente"
NOMBRE_CLIENTE: Final[str] = "Ana Lucia Torres"
PRECIO: Final[float] = 150.0
OBSERVACIONES: Final[str] = "Lo recoge el viernes"

COLUMNAS_ESPERADAS: Final[tuple[str, ...]] = (
    "cliente",
    "fecha",
    "status",
    "total_estimado",
    "total_anticipado",
    "saldo_estimado",
)

COL_CLIENTE: Final[int] = 0
COL_FECHA: Final[int] = 1
COL_STATUS: Final[int] = 2
COL_ESTIMADO: Final[int] = 3
COL_ANTICIPADO: Final[int] = 4
COL_SALDO: Final[int] = 5


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico y stock sembrado."""
    conexion = db.init_db(":memory:")
    _seed_stock(conexion, codigo=CODIGO, descripcion=DESCRIPCION, piezas=10, costo_total=800.0)
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
def tab(app_falsa: tk.Tk) -> gui_inventario.TabEncargos:
    """Pestana de Encargos montada sobre la app falsa."""
    return gui_inventario.TabEncargos(ttk.Notebook(app_falsa), app_falsa)


@pytest.fixture()
def cliente_id(conn: sqlite3.Connection) -> int:
    """Cliente del directorio: `encargos.cliente_id` es NOT NULL."""
    return _seed_cliente(conn, NOMBRE_CLIENTE)


def encargo_de(
    conexion: sqlite3.Connection, cliente: int, cantidad: int = 2, precio: float = PRECIO
) -> int:
    """Encargo `Pendiente` de una sola linea sobre el articulo sembrado."""
    return core.crear_encargo(
        conexion, cliente, [_linea(CODIGO, cantidad, precio)], OBSERVACIONES
    )


def status_de(conexion: sqlite3.Connection, encargo_id: int) -> str:
    """`encargos.status` leido directamente, sin pasar por la GUI."""
    fila = conexion.execute(
        "SELECT status FROM encargos WHERE id = ?", (encargo_id,)
    ).fetchone()
    return str(fila["status"])


def forzar_status(conexion: sqlite3.Connection, encargo_id: int, status: str) -> None:
    """Deja el encargo en `status` sin pasar por la GUI: simula el boton obsoleto."""
    with conexion:
        conexion.execute(
            "UPDATE encargos SET status = ? WHERE id = ?", (status, encargo_id)
        )


def agotar_stock(conexion: sqlite3.Connection) -> None:
    """Vende todo el inventario del articulo: deja el encargo sin stock (R7)."""
    disponibles = int(
        conexion.execute(
            "SELECT piezas_disponibles FROM vw_existencias WHERE codigo_articulo = ?",
            (CODIGO,),
        ).fetchone()["piezas_disponibles"]
    )
    core.registrar_venta(
        conexion, None, [{"codigo": CODIGO, "cantidad": disponibles, "precio_publico": 1.0}]
    )


def metodo_ast(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


def codigo_sin_docstrings(nodo: ast.AST) -> str:
    """Fuente ejecutable de `nodo`, sin sus docstrings ni los de sus hijos."""
    copia = ast.parse(ast.unparse(nodo)).body[0]
    for hijo in ast.walk(copia):
        cuerpo = getattr(hijo, "body", None)
        if not isinstance(cuerpo, list) or not cuerpo:
            continue
        primero = cuerpo[0]
        if isinstance(primero, ast.Expr) and isinstance(primero.value, ast.Constant):
            if isinstance(primero.value.value, str):
                cuerpo[0] = ast.Pass()
    return ast.unparse(copia)


def formulario(app: tk.Tk, **kwargs: Any) -> gui_inventario.VentanaEncargoForm:
    """Formulario de encargo montado sobre la app falsa."""
    return gui_inventario.VentanaEncargoForm(app, **kwargs)


def capturar_linea(
    form: gui_inventario.VentanaEncargoForm, cantidad: str, precio: str
) -> None:
    """Captura una linea por la via real: articulo de la lista + cantidad + precio."""
    form.listbox.selection_set(0)
    form._al_seleccionar(None)
    form.cantidad_var.set(cantidad)
    form.precio_var.set(precio)
    form._agregar_linea()


# --------------------------------------------------------------------------
# ENC-02 R14 -- la pestana: columnas, poblado y filtro
# --------------------------------------------------------------------------


def test_tabencargos_expone_las_columnas_del_contrato(
    tab: gui_inventario.TabEncargos,
) -> None:
    """R14 + ENC-04 R9: las cinco columnas del contrato mas el saldo estimado."""
    columnas = tuple(str(c) for c in tab.tree["columns"])

    assert columnas == COLUMNAS_ESPERADAS
    assert tab.tree.heading("total_anticipado")["text"] == "Anticipo"
    assert tab.tree.heading("saldo_estimado")["text"] == "Saldo estimado"
    assert isinstance(tab, ttk.Frame)


def test_tabencargos_refrescar_puebla_desde_listar_encargos(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R14: la fila sale de `core.listar_encargos`, con `iid` = `encargos.id`."""
    encargo_id = encargo_de(conn, cliente_id)

    tab.refrescar()

    assert tab.tree.get_children() == (str(encargo_id),)
    valores = tab.tree.item(str(encargo_id), "values")
    assert valores[COL_CLIENTE] == NOMBRE_CLIENTE
    assert valores[COL_STATUS] == "Pendiente"
    assert valores[COL_ESTIMADO] == "$300.00"
    assert len(valores[COL_FECHA]) == gui_inventario.LARGO_FECHA


def test_tabencargos_refrescar_muestra_anticipo_y_saldo_estimado(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """ENC-04 R9: el saldo es la resta de los dos totales que ya trae el listado."""
    encargo_id = encargo_de(conn, cliente_id)
    core.agregar_pago(conn, gui_inventario.TABLA_PAGOS_ENCARGO, encargo_id, "Efectivo", 120.0)

    tab.refrescar()

    valores = tab.tree.item(str(encargo_id), "values")
    assert valores[COL_ESTIMADO] == "$300.00"
    assert valores[COL_ANTICIPADO] == "$120.00"
    assert valores[COL_SALDO] == "$180.00"


def test_tabencargos_refrescar_con_la_base_vacia_no_pinta_filas(
    tab: gui_inventario.TabEncargos,
) -> None:
    """R14: sin encargos el arbol queda vacio y la cache tambien."""
    tab.refrescar()

    assert tab.tree.get_children() == ()
    assert tab.encargos == {}


def test_tabencargos_filtro_de_status_acota_el_listado(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R14: el combo traduce su etiqueta al filtro de `listar_encargos`."""
    pendiente = encargo_de(conn, cliente_id)
    cancelado = encargo_de(conn, cliente_id)
    core.cancelar_encargo(conn, cancelado)

    tab.filtro_var.set("Cancelado")
    tab.refrescar()

    assert tab.tree.get_children() == (str(cancelado),)
    tab.filtro_var.set(gui_inventario.FILTRO_TODOS)
    tab.refrescar()
    assert set(tab.tree.get_children()) == {str(pendiente), str(cancelado)}


def test_tabencargos_no_ejecuta_sql() -> None:
    """ADR-2: la pestana no escribe SQL; toda lectura pasa por `core`."""
    fuente = "".join(
        codigo_sin_docstrings(metodo_ast("TabEncargos", nombre))
        for nombre in ("refrescar", "_fila_visible", "_actualizar_acciones", "_surtir")
    )

    for palabra in ("SELECT", "JOIN", "INSERT INTO", "UPDATE ", "get_conn", "execute("):
        assert palabra not in fuente, f"La GUI ejecuta SQL: aparece {palabra!r}"


def test_notebook_incluye_la_pestana_de_encargos() -> None:
    """R14: `App.__init__` construye y registra la pestana."""
    fuente = ast.unparse(metodo_ast("App", "__init__"))

    assert "self.tab_encargos = TabEncargos(self.notebook, self)" in fuente
    assert "self.notebook.add(self.tab_encargos" in fuente


def test_refrescar_todo_incluye_la_pestana_de_encargos() -> None:
    """R14: sin esta llamada la pestana nunca se poblaria tras una carga."""
    fuente = ast.unparse(metodo_ast("App", "refrescar_todo"))

    assert "self.tab_encargos.refrescar()" in fuente


# --------------------------------------------------------------------------
# ENC-02 R11, R12 -- el formulario: cliente, canasta y validacion
# --------------------------------------------------------------------------


def test_formulario_ofrece_los_clientes_del_directorio(
    app_falsa: tk.Tk, cliente_id: int
) -> None:
    """R11: el combo sale de `core.listar_clientes`, con placeholder sin id."""
    form = formulario(app_falsa)

    etiquetas = list(form.clientes_por_etiqueta)
    assert etiquetas[0] == gui_inventario.SIN_CLIENTE
    assert form.clientes_por_etiqueta[gui_inventario.SIN_CLIENTE] is None
    assert form.clientes_por_etiqueta[NOMBRE_CLIENTE] == cliente_id
    assert core.CLIENTE_MOSTRADOR not in form.clientes_por_etiqueta
    form.destroy()


def test_formulario_agrega_y_quita_lineas_de_la_canasta(app_falsa: tk.Tk) -> None:
    """R12: la canasta acepta lineas del catalogo y suelta la seleccionada."""
    form = formulario(app_falsa)

    capturar_linea(form, "3", "150")
    capturar_linea(form, "1", "80")

    assert len(form.tree_canasta.get_children()) == 2
    primera = form.tree_canasta.get_children()[0]
    form.tree_canasta.selection_set(primera)
    form._quitar_linea()
    assert len(form.tree_canasta.get_children()) == 1
    assert form._canasta() == [_linea(CODIGO, 1, 80.0)]
    form.destroy()


def test_formulario_sin_cliente_avisa_y_no_persiste_nada(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R12: sin cliente elegido se avisa y la base no se toca."""
    form = formulario(app_falsa)
    capturar_linea(form, "2", "150")

    with mock.patch.object(gui_inventario.messagebox, "showwarning") as aviso:
        form._guardar()

    assert aviso.call_count == 1
    assert core.listar_encargos(conn) == []
    assert app_falsa.refrescos == []
    form.destroy()


def test_formulario_con_canasta_vacia_avisa_y_no_persiste_nada(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R12: con cliente pero sin articulos tampoco se persiste nada."""
    form = formulario(app_falsa)
    form.cliente_var.set(NOMBRE_CLIENTE)

    with mock.patch.object(gui_inventario.messagebox, "showwarning") as aviso:
        form._guardar()

    assert aviso.call_count == 1
    assert core.listar_encargos(conn) == []
    form.destroy()


def test_formulario_valido_crea_el_encargo_y_refresca(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R11: cliente + canasta se persisten en una sola llamada a `crear_encargo`."""
    form = formulario(app_falsa)
    capturar_linea(form, "2", "150")
    form.cliente_var.set(NOMBRE_CLIENTE)
    form.obs_text.insert("1.0", OBSERVACIONES)

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=False):
        form._guardar()

    encargos = core.listar_encargos(conn)
    assert len(encargos) == 1
    assert encargos[0]["cliente_id"] == cliente_id
    assert encargos[0]["total_estimado"] == 300.0
    assert encargos[0]["observaciones"] == OBSERVACIONES
    assert app_falsa.refrescos == [1]


def test_formulario_en_modo_editar_llega_precargado(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R11: en modo editar los tres bloques salen del encargo existente."""
    encargo_id = encargo_de(conn, cliente_id)
    datos = core.obtener_encargo(conn, encargo_id)

    form = formulario(app_falsa, modo="editar", encargo_id=encargo_id, datos=datos)

    assert form.cliente_var.get() == NOMBRE_CLIENTE
    assert form.obs_text.get("1.0", tk.END).strip() == OBSERVACIONES
    assert form._canasta() == [_linea(CODIGO, 2, PRECIO)]
    form.destroy()


def test_formulario_en_modo_editar_reemplaza_la_canasta(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R11: guardar en modo editar sustituye el detalle entero por el capturado."""
    encargo_id = encargo_de(conn, cliente_id)
    form = formulario(
        app_falsa, modo="editar", encargo_id=encargo_id,
        datos=core.obtener_encargo(conn, encargo_id),
    )
    form.tree_canasta.selection_set(form.tree_canasta.get_children()[0])
    form._quitar_linea()
    capturar_linea(form, "5", "20")

    form._guardar()

    encargo = core.obtener_encargo(conn, encargo_id)
    assert encargo["lineas"] == [_linea(CODIGO, 5, 20.0)]
    assert encargo["total_estimado"] == 100.0


def test_nuevo_abre_el_formulario_en_modo_agregar(
    tab: gui_inventario.TabEncargos,
) -> None:
    """R14: el control Nuevo es el punto de entrada del alta."""
    with mock.patch.object(gui_inventario, "VentanaEncargoForm") as ventana:
        tab._nuevo()

    assert ventana.call_args.kwargs["modo"] == "agregar"


def test_editar_abre_el_formulario_con_el_detalle_del_encargo(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R11: el detalle no esta en el listado, se pide con `obtener_encargo`."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))

    with mock.patch.object(gui_inventario, "VentanaEncargoForm") as ventana:
        tab._editar()

    assert ventana.call_args.kwargs["encargo_id"] == encargo_id
    assert ventana.call_args.kwargs["modo"] == "editar"
    assert ventana.call_args.kwargs["datos"]["lineas"] == [_linea(CODIGO, 2, PRECIO)]


def test_editar_un_encargo_que_ya_no_existe_avisa_y_no_abre_nada(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R15: un listado obsoleto llega como `EncargoError`, no como excepcion."""
    encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.encargos[999] = {
        "id": 999, "status": "Pendiente", "cliente_nombre": "", "total_estimado": 0,
    }
    tab.tree.insert("", "end", iid="999", values=tuple("-" for _ in COLUMNAS_ESPERADAS))
    tab.tree.selection_set("999")

    with mock.patch.object(gui_inventario, "VentanaEncargoForm") as ventana, \
            mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        tab._editar()

    assert showerror.call_count == 1
    assert ventana.call_count == 0


# --------------------------------------------------------------------------
# ENC-02 R13 -- el anticipo es opcional
# --------------------------------------------------------------------------


def test_formulario_ofrece_registrar_el_anticipo_tras_crear(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R13: aceptando la oferta se abre `VentanaPagos` sobre `encargo_pagos`."""
    form = formulario(app_falsa)
    capturar_linea(form, "2", "150")
    form.cliente_var.set(NOMBRE_CLIENTE)

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=True), \
            mock.patch.object(gui_inventario, "VentanaPagos") as ventana:
        form._guardar()

    encargo_id = core.listar_encargos(conn)[0]["id"]
    assert ventana.call_count == 1
    assert ventana.call_args.args[1] == gui_inventario.TABLA_PAGOS_ENCARGO
    assert ventana.call_args.args[2] == encargo_id
    assert ventana.call_args.args[3] == 300.0


def test_formulario_saltarse_el_anticipo_deja_el_encargo_valido_en_cero(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R13: el anticipo es opcional -- sin el, el encargo existe con cero."""
    form = formulario(app_falsa)
    capturar_linea(form, "2", "150")
    form.cliente_var.set(NOMBRE_CLIENTE)

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=False), \
            mock.patch.object(gui_inventario, "VentanaPagos") as ventana:
        form._guardar()

    encargo = core.listar_encargos(conn)[0]
    assert ventana.call_count == 0
    assert encargo["status"] == "Pendiente"
    assert encargo["total_anticipado"] == 0.0


def test_anticipo_desde_la_barra_abre_ventanapagos_del_encargo(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R14: el control Anticipo reusa el dialogo compartido de pagos."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))

    with mock.patch.object(gui_inventario, "VentanaPagos") as ventana:
        tab._anticipo()

    assert ventana.call_args.args[1] == gui_inventario.TABLA_PAGOS_ENCARGO
    assert ventana.call_args.args[2] == encargo_id
    assert ventana.call_args.args[3] == 300.0


# --------------------------------------------------------------------------
# ENC-02 R15 -- el error de dominio se muestra, no se propaga
# --------------------------------------------------------------------------


def test_editar_un_encargo_no_pendiente_muestra_el_mensaje_del_core(
    app_falsa: tk.Tk, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R15: `EncargoError` al guardar sale por dialogo y no escapa."""
    encargo_id = encargo_de(conn, cliente_id)
    form = formulario(
        app_falsa, modo="editar", encargo_id=encargo_id,
        datos=core.obtener_encargo(conn, encargo_id),
    )
    core.cancelar_encargo(conn, encargo_id)

    with mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        form._guardar()

    assert showerror.call_count == 1
    assert "Pendiente" in showerror.call_args.args[1]
    assert core.obtener_encargo(conn, encargo_id)["status"] == "Cancelado"
    form.destroy()


def test_cancelar_con_el_boton_obsoleto_muestra_el_mensaje_del_core(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R15: si el status cambio tras el refresco, el core rechaza y se avisa."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))
    forzar_status(conn, encargo_id, "Cancelado")

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=True), \
            mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        tab._cancelar()

    assert showerror.call_count == 1
    assert status_de(conn, encargo_id) == "Cancelado"


def test_cancelar_enruta_al_cancelar_encargo_del_core(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """ENC-04 R10: cancelar no se reimplementa, enruta al CRUD de ENC-02."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))

    with mock.patch.object(gui_inventario.messagebox, "askyesno", return_value=True):
        tab._cancelar()

    assert status_de(conn, encargo_id) == "Cancelado"
    assert tab.tree.item(str(encargo_id), "values")[COL_STATUS] == "Cancelado"


# --------------------------------------------------------------------------
# ENC-04 R4, R5, R10 -- el boton Surtir y la habilitacion de la barra
# --------------------------------------------------------------------------


def test_la_barra_conserva_los_controles_de_enc02_y_suma_surtir(
    tab: gui_inventario.TabEncargos,
) -> None:
    """R4: Surtir se agrega **junto a** los de ENC-02, sin quitarlos."""
    assert set(tab.botones) == {"nuevo", "editar", "cancelar", "anticipo", "surtir"}
    assert tab.botones["surtir"]["text"].endswith("Surtir")
    assert tab.tree.bind("<<TreeviewSelect>>")


def test_surtir_se_habilita_con_un_encargo_surtible(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R5: un solo encargo seleccionado y con stock deja el boton `normal`."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()

    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()

    assert str(tab.botones["surtir"]["state"]) == "normal"
    assert core.encargo_surtible(conn, encargo_id) is True


def test_surtir_esta_deshabilitado_sin_seleccion(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R5: sin seleccion no hay encargo que surtir."""
    encargo_de(conn, cliente_id)

    tab.refrescar()

    assert str(tab.botones["surtir"]["state"]) == "disabled"
    assert str(tab.botones["cancelar"]["state"]) == "disabled"


def test_surtir_esta_deshabilitado_con_seleccion_multiple(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R5: la conversion es de uno en uno; dos filas no habilitan nada."""
    primero = encargo_de(conn, cliente_id, cantidad=1)
    segundo = encargo_de(conn, cliente_id, cantidad=1)
    tab.refrescar()

    tab.tree.selection_set(str(primero), str(segundo))
    tab._actualizar_acciones()

    assert str(tab.botones["surtir"]["state"]) == "disabled"


def test_surtir_esta_deshabilitado_si_el_stock_no_alcanza(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R5: el criterio es el del core, que compara contra `vw_existencias`."""
    encargo_id = encargo_de(conn, cliente_id, cantidad=99)
    tab.refrescar()

    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()

    assert str(tab.botones["surtir"]["state"]) == "disabled"


@pytest.mark.parametrize("status", ["Surtido", "Entregado", "Cancelado"])
def test_surtir_esta_deshabilitado_para_un_status_cerrado(
    tab: gui_inventario.TabEncargos,
    conn: sqlite3.Connection,
    cliente_id: int,
    status: str,
) -> None:
    """R8: un encargo ya cerrado no es surtible, aunque sobre inventario."""
    encargo_id = encargo_de(conn, cliente_id)
    forzar_status(conn, encargo_id, status)
    tab.refrescar()

    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()

    assert str(tab.botones["surtir"]["state"]) == "disabled"


def test_cancelar_solo_se_habilita_para_un_encargo_pendiente(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R10: cancelar sigue alcanzable, pero solo sobre un `Pendiente`."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()
    pendiente = str(tab.botones["cancelar"]["state"])

    forzar_status(conn, encargo_id, "Entregado")
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()

    assert pendiente == "normal"
    assert str(tab.botones["cancelar"]["state"]) == "disabled"


# --------------------------------------------------------------------------
# ENC-04 R6, R7, R8 -- la conversion
# --------------------------------------------------------------------------


def test_surtir_convierte_el_encargo_en_venta_y_refresca(
    tab: gui_inventario.TabEncargos,
    conn: sqlite3.Connection,
    cliente_id: int,
    app_falsa: tk.Tk,
) -> None:
    """R6: en exito el encargo queda `Entregado` y la pestana se repinta."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))

    with mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        tab._surtir()

    assert aviso.call_count == 1
    assert status_de(conn, encargo_id) == "Entregado"
    assert tab.tree.item(str(encargo_id), "values")[COL_STATUS] == "Entregado"
    assert app_falsa.refrescos == [1]


def test_surtir_con_el_stock_agotado_muestra_el_ventaerror_sin_tocar_el_encargo(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R7: el boton quedo obsoleto; la validacion real es la del core.

    El stock se agota **despues** del refresco que habilito el boton, que es
    exactamente el hueco entre el chequeo y el clic. La GUI no lo predice: lo
    muestra cuando el core lo rechaza.
    """
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()
    habilitado = str(tab.botones["surtir"]["state"])
    agotar_stock(conn)

    with mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        tab._surtir()

    assert habilitado == "normal"
    assert showerror.call_count == 1
    assert status_de(conn, encargo_id) == "Pendiente"
    assert int(conn.execute("SELECT COUNT(*) AS n FROM ventas").fetchone()["n"]) == 1


def test_surtir_dos_veces_lo_rechaza_el_core_y_no_duplica_la_venta(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R8: una habilitacion obsoleta no puede surtir dos veces."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))
    with mock.patch.object(gui_inventario.messagebox, "showinfo"):
        tab._surtir()

    tab.tree.selection_set(str(encargo_id))
    with mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        tab._surtir()

    assert showerror.call_count == 1
    assert int(conn.execute("SELECT COUNT(*) AS n FROM ventas").fetchone()["n"]) == 1
    assert status_de(conn, encargo_id) == "Entregado"


def test_surtir_traspasa_el_anticipo_a_la_venta(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R6: la conversion entera es del core; la GUI solo enruta el resultado."""
    encargo_id = encargo_de(conn, cliente_id)
    core.agregar_pago(conn, gui_inventario.TABLA_PAGOS_ENCARGO, encargo_id, "Efectivo", 120.0)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))

    with mock.patch.object(gui_inventario.messagebox, "showinfo"):
        tab._surtir()

    venta_id = int(
        conn.execute("SELECT venta_id FROM encargos WHERE id = ?", (encargo_id,))
        .fetchone()["venta_id"]
    )
    assert core.total_pagado(conn, "venta_pagos", venta_id) == 120.0


def test_surtir_sin_seleccion_avisa_y_no_convierte_nada(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R5: sin seleccion el handler corta antes de llamar al core."""
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()

    with mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        tab._surtir()

    assert aviso.call_count == 1
    assert status_de(conn, encargo_id) == "Pendiente"


def test_anticipo_se_deshabilita_sobre_un_encargo_no_pendiente(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """Un abono contra un encargo cancelado se escribiria sin que nadie lo vete.

    `core_pagos.agregar_pago` valida tabla, forma, monto y FK, pero **no mira el
    status del encargo padre**, asi que aqui no vale el argumento que si vale
    para `editar` (dejar el boton vivo para que el core explique el rechazo):
    no habria rechazo. Mientras la capa de pagos no valide el estado del padre
    (DEUDA-09), este gating es la unica barrera.
    """
    # Arrange
    encargo_id = encargo_de(conn, cliente_id)
    core.cancelar_encargo(conn, encargo_id)
    tab.refrescar()

    # Act
    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()

    # Assert
    assert str(tab.botones["anticipo"]["state"]) == "disabled"
    assert str(tab.botones["cancelar"]["state"]) == "disabled"
    assert str(tab.botones["editar"]["state"]) == "normal"


def test_anticipo_se_habilita_sobre_un_encargo_pendiente(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """El caso normal sigue abierto: un encargo pendiente admite anticipos."""
    # Arrange
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()

    # Act
    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()

    # Assert
    assert str(tab.botones["anticipo"]["state"]) == "normal"


def test_refrescar_reevalua_el_estado_de_los_botones(
    tab: gui_inventario.TabEncargos, conn: sqlite3.Connection, cliente_id: int
) -> None:
    """R5 exige reevaluar al refrescar, no solo al cambiar la seleccion.

    Sin este test, borrar la llamada a `_actualizar_acciones()` del final de
    `refrescar` dejaria la suite verde y el boton Surtir se quedaria habilitado
    sobre un encargo que ya no lo admite.
    """
    # Arrange: encargo surtible y seleccionado
    encargo_id = encargo_de(conn, cliente_id)
    tab.refrescar()
    tab.tree.selection_set(str(encargo_id))
    tab._actualizar_acciones()
    assert str(tab.botones["surtir"]["state"]) == "normal"

    # Act: el encargo deja de ser surtible y solo se refresca
    core.cancelar_encargo(conn, encargo_id)
    tab.refrescar()

    # Assert
    assert str(tab.botones["surtir"]["state"]) == "disabled"
