"""GUI de ventas: canasta multi-linea e historial SQLite (CLI-02 T12-T14, CLI-05 T7-T11).

Aqui no basta con verificar el cableado por AST: lo que hay que probar es que la
canasta **acumula lineas de verdad**, que la venta entera viaja en una sola
llamada a `core.registrar_venta` y que los filtros de la pestana recortan el
Treeview sobre las claves **nuevas** del historial (minusculas, sin forma de
pago). Asi que se instancian widgets reales contra un `Tk()` oculto que hace de
`app`, con una conexion en memoria; la capa core corre de verdad.

No se instancia `App`: abriria la base de produccion, correria el respaldo de
arranque y construiria siete pestanas. Solo se parchean los dialogos
(`messagebox`), que son I/O externo. Las fixtures se saltan el modulo entero si
no hay servidor X disponible.

Cobertura por tarea:

* CLI-02 T12 -> `test_ventana_venta_agrega_y_quita_lineas`, combo de cliente,
  ausencia de la forma de pago.
* CLI-02 T13 -> `test_ventana_venta_registra_canasta` + las dos rutas de fallo
  (canasta vacia y `VentaError` inline con la canasta intacta).
* CLI-02 T14 -> `test_tab_ventas_refrescar_usa_sqlite` (AST).
* CLI-05 T7  -> `test_tab_ventas_refrescar_carga_datos`.
* CLI-05 T8  -> `test_tab_ventas_filtro_producto`.
* CLI-05 T9  -> `test_tab_ventas_filtro_fecha_rango` + el dia completo del
  extremo `hasta`.
* CLI-05 T10 -> `test_tab_ventas_columna_saldo_condicional`.
* CLI-05 T11 -> `test_tab_ventas_muestra_mostrador`.
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

CODIGO_A: Final[str] = "11111"
CODIGO_B: Final[str] = "22222"
DESCRIPCION_A: Final[str] = "Sarten antiadherente 24cm"
DESCRIPCION_B: Final[str] = "Juego de sabanas matrimonial"

NOMBRE_CLIENTE: Final[str] = "Ana Lucia Torres"

PIEZAS_A: Final[int] = 10
PIEZAS_B: Final[int] = 4

#: Indices de columna del Treeview del historial, en el orden de `columnas`.
COL_FECHA: Final[int] = 0
COL_CLIENTE: Final[int] = 1
COL_CODIGO: Final[int] = 2
COL_DESCRIPCION: Final[int] = 3
COL_CANTIDAD: Final[int] = 4
COL_SALDO: Final[int] = 9

#: Indices de columna del Treeview de la canasta de `VentanaVenta`.
CANASTA_CODIGO: Final[int] = 0
CANASTA_CANTIDAD: Final[int] = 2

COLUMNAS_HISTORIAL: Final[tuple[str, ...]] = (
    "fecha", "cliente", "codigo", "descripcion", "cantidad",
    "costo", "publico", "total", "ganancia", "saldo",
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


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
    # `TabVentas` ata el boton de la barra a este metodo de la app real.
    ventana.abrir_ventana_venta = lambda *_, **__: None
    try:
        yield ventana
    finally:
        ventana.destroy()


@pytest.fixture()
def tab(app_falsa: tk.Tk) -> gui_inventario.TabVentas:
    """Pestana de Ventas montada sobre la app falsa."""
    return gui_inventario.TabVentas(ttk.Notebook(app_falsa), app_falsa)


@pytest.fixture()
def inventario(conn: sqlite3.Connection) -> None:
    """Dos productos con existencias, listos para vender."""
    sembrar_inventario(conn)


@pytest.fixture()
def ventana(app_falsa: tk.Tk, inventario: None) -> Iterator[gui_inventario.VentanaVenta]:
    """Ventana de venta abierta sobre un inventario ya sembrado."""
    dialogo = gui_inventario.VentanaVenta(app_falsa)
    try:
        yield dialogo
    finally:
        dialogo.destroy()


# ----------------------------------------------------------------------
# Utilidades de siembra y lectura
# ----------------------------------------------------------------------


def fila_pdf(
    *,
    folio: str = "C001264",
    codigo: str = CODIGO_A,
    descripcion: str = DESCRIPCION_A,
    piezas: int = PIEZAS_A,
    precio_que_pagas: float = 500.0,
) -> dict[str, Any]:
    """Fila con las claves que entrega `pdf_extractor.procesar_pdf`.

    Todo lo surtido se queda en casa, de modo que `vw_existencias` reporta
    `piezas` disponibles del articulo.
    """
    return {
        "Fecha registro": "2026-07-22 09:00",
        "Semana": "01-2026",
        "Folio de pedido": folio,
        "Codigo nota": "8043",
        "Distribuidora": "C0001 DISTRIBUIDORA CENTRO",
        "Nombre asociado": "AURA JANNET RAMIREZ",
        "Archivo origen": f"{folio}_NOTA.pdf (pag. 1)",
        "Codigo articulo": codigo,
        "Descripcion": descripcion,
        "Cantidad solicitada": piezas,
        "Cantidad surtida": piezas,
        "Cantidad Asociado": 0,
        "Cantidad Casa": piezas,
        "Cantidad Local": 0,
        "Precio catalogo": 249.0,
        "Precio con IVA": 288.84,
        "Precio que pagas": precio_que_pagas,
        "Valor total con IVA": 866.52,
        "Tipo": "Normal (con descuento)",
        "Ocurrencia": 1,
    }


def sembrar_inventario(conexion: sqlite3.Connection) -> None:
    """Carga los dos productos de prueba con sus existencias."""
    core.confirmar_carga(
        conexion,
        [
            fila_pdf(),
            fila_pdf(
                folio="C001265", codigo=CODIGO_B,
                descripcion=DESCRIPCION_B, piezas=PIEZAS_B,
            ),
        ],
    )


def linea(codigo: str, cantidad: int, precio: float) -> dict[str, Any]:
    """Linea de canasta con el contrato que consume `core.registrar_venta`."""
    return {"codigo": codigo, "cantidad": cantidad, "precio_publico": precio}


def fechar_venta(conexion: sqlite3.Connection, venta_id: int, dia: str) -> None:
    """Reescribe la fecha de una venta conservando la parte horaria.

    El default de la columna es `datetime('now','localtime')`, asi que para
    probar el rango hace falta sembrar dias concretos -- con hora, que es
    justo lo que el filtro debe ignorar.
    """
    conexion.execute(
        "UPDATE ventas SET fecha = ? WHERE id = ?", (f"{dia} 13:45:07", venta_id)
    )
    conexion.commit()


def pagar(conexion: sqlite3.Connection, venta_id: int, monto: float) -> None:
    """Registra un pago de la venta (el dominio de pagos es de CLI-03)."""
    conexion.execute(
        "INSERT INTO venta_pagos (venta_id, forma_pago, monto) VALUES (?, 'Efectivo', ?)",
        (venta_id, monto),
    )
    conexion.commit()


def valores(arbol: ttk.Treeview, columna: int) -> list[str]:
    """Columna `columna` de las filas visibles del arbol, en orden."""
    return [str(arbol.item(iid, "values")[columna]) for iid in arbol.get_children()]


def elegir_producto(dialogo: gui_inventario.VentanaVenta, codigo: str) -> None:
    """Selecciona un producto del catalogo como lo haria la usuaria.

    Filtrar por el codigo deja un unico resultado y la ventana lo autoselecciona.
    """
    dialogo.busqueda_var.set(codigo)


def agregar(
    dialogo: gui_inventario.VentanaVenta, codigo: str, cantidad: int, precio: float
) -> None:
    """Captura un producto y lo empuja a la canasta."""
    elegir_producto(dialogo, codigo)
    dialogo.cantidad_var.set(str(cantidad))
    dialogo.precio_var.set(str(precio))
    dialogo._agregar_linea()


def cuerpo_ejecutable(clase: str, nombre: str) -> str:
    """Codigo del metodo `clase.nombre` sin su docstring.

    Un docstring que *menciona* `EXCEL_PATH` no es una referencia al Excel; sin
    esta poda la asercion mediria la prosa en vez del cableado.
    """
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    sentencias = hijo.body
                    if ast.get_docstring(hijo) is not None:
                        sentencias = sentencias[1:]
                    return "\n".join(ast.unparse(s) for s in sentencias)
    raise AssertionError(f"No se encontro {clase}.{nombre}")


# ----------------------------------------------------------------------
# CLI-02 T12 -- la ventana de venta es una canasta
# ----------------------------------------------------------------------


def test_ventana_venta_agrega_y_quita_lineas(
    ventana: gui_inventario.VentanaVenta,
) -> None:
    """T12: dos productos entran a la canasta y quitar uno deja una linea."""
    agregar(ventana, CODIGO_A, 2, 150.0)
    agregar(ventana, CODIGO_B, 1, 300.0)

    assert valores(ventana.tree_canasta, CANASTA_CODIGO) == [CODIGO_A, CODIGO_B]
    assert ventana._canasta() == [
        linea(CODIGO_A, 2, 150.0),
        linea(CODIGO_B, 1, 300.0),
    ]

    ventana.tree_canasta.selection_set(ventana.tree_canasta.get_children()[0])
    ventana._quitar_linea()

    assert valores(ventana.tree_canasta, CANASTA_CODIGO) == [CODIGO_B]
    assert ventana._canasta() == [linea(CODIGO_B, 1, 300.0)]


def test_ventana_venta_acumula_dos_lineas_del_mismo_producto(
    ventana: gui_inventario.VentanaVenta,
) -> None:
    """T12: el mismo codigo puede ir dos veces; el core suma para el stock."""
    agregar(ventana, CODIGO_A, 2, 150.0)
    agregar(ventana, CODIGO_A, 3, 120.0)

    assert valores(ventana.tree_canasta, CANASTA_CANTIDAD) == ["2", "3"]
    assert len(ventana._canasta()) == 2


def test_ventana_venta_rechaza_captura_invalida_sin_tocar_la_canasta(
    ventana: gui_inventario.VentanaVenta,
) -> None:
    """T12: cantidad no entera, cero o precio no numerico no crean linea."""
    agregar(ventana, CODIGO_A, 0, 150.0)
    cantidad_cero = ventana.status_label.cget("text")

    elegir_producto(ventana, CODIGO_A)
    ventana.cantidad_var.set("dos")
    ventana.precio_var.set("150")
    ventana._agregar_linea()
    cantidad_texto = ventana.status_label.cget("text")

    ventana.cantidad_var.set("2")
    ventana.precio_var.set("gratis")
    ventana._agregar_linea()
    precio_texto = ventana.status_label.cget("text")

    assert "mayor que cero" in cantidad_cero
    assert "número entero" in cantidad_texto
    assert "número" in precio_texto
    assert ventana._canasta() == []


def test_ventana_venta_combo_cliente_ofrece_mostrador_y_el_directorio(
    app_falsa: tk.Tk, conn: sqlite3.Connection, inventario: None
) -> None:
    """T12: el combo se puebla con `core.listar_clientes` mas `Mostrador`."""
    cliente_id = core.crear_cliente(conn, NOMBRE_CLIENTE)

    dialogo = gui_inventario.VentanaVenta(app_falsa)

    try:
        etiquetas = list(dialogo.combo_cliente["values"])
        assert etiquetas[0] == core.CLIENTE_MOSTRADOR
        assert NOMBRE_CLIENTE in etiquetas
        assert dialogo.cliente_var.get() == core.CLIENTE_MOSTRADOR
        assert dialogo.clientes_por_etiqueta[core.CLIENTE_MOSTRADOR] is None
        assert dialogo.clientes_por_etiqueta[NOMBRE_CLIENTE] == cliente_id
    finally:
        dialogo.destroy()


def test_ventana_venta_ya_no_pide_forma_de_pago(
    ventana: gui_inventario.VentanaVenta,
) -> None:
    """T12: los pagos son de CLI-03; el combo salio de esta ventana."""
    assert not hasattr(ventana, "pago_var")
    assert "FORMA_PAGO_OPCIONES" not in cuerpo_ejecutable("VentanaVenta", "_construir_formulario")


# ----------------------------------------------------------------------
# CLI-02 T13 -- registro de la canasta contra la capa core
# ----------------------------------------------------------------------


def test_ventana_venta_registra_canasta(
    ventana: gui_inventario.VentanaVenta, conn: sqlite3.Connection, app_falsa: tk.Tk
) -> None:
    """T13: la canasta entera se registra en una sola venta y luego se vacia."""
    agregar(ventana, CODIGO_A, 2, 150.0)
    agregar(ventana, CODIGO_B, 1, 300.0)

    with mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        ventana._registrar()

    historial = core.obtener_ventas_historial(conn)
    assert len(historial) == 2
    assert {f["venta_id"] for f in historial} == {1}
    assert historial[0]["total_venta"] == 600.0
    assert aviso.call_count == 1
    assert "Total: $600.00" in aviso.call_args.args[1]
    assert app_falsa.refrescos == [1]
    assert ventana._canasta() == []
    assert ventana.tree_canasta.get_children() == ()
    assert ventana.status_label.cget("text") == ""


def test_ventana_venta_registra_con_el_cliente_elegido(
    app_falsa: tk.Tk, conn: sqlite3.Connection, inventario: None
) -> None:
    """T13: elegir un cliente ata la venta a su id; el historial lo muestra."""
    core.crear_cliente(conn, NOMBRE_CLIENTE)
    dialogo = gui_inventario.VentanaVenta(app_falsa)

    try:
        agregar(dialogo, CODIGO_A, 1, 150.0)
        dialogo.cliente_var.set(NOMBRE_CLIENTE)
        with mock.patch.object(gui_inventario.messagebox, "showinfo"):
            dialogo._registrar()
    finally:
        dialogo.destroy()

    assert [f["cliente"] for f in core.obtener_ventas_historial(conn)] == [NOMBRE_CLIENTE]


def test_ventana_venta_muestra_el_error_de_stock_inline_y_conserva_la_canasta(
    ventana: gui_inventario.VentanaVenta, conn: sqlite3.Connection, app_falsa: tk.Tk
) -> None:
    """T13: `VentaError` se ve inline, con el disponible real y sin perder nada."""
    agregar(ventana, CODIGO_A, PIEZAS_A + 5, 150.0)

    with mock.patch.object(gui_inventario.messagebox, "showerror") as dialogo_error:
        ventana._registrar()

    mensaje = ventana.status_label.cget("text")
    assert "Stock insuficiente" in mensaje
    assert f"hay {PIEZAS_A} disponibles" in mensaje
    assert dialogo_error.call_count == 0
    assert len(ventana._canasta()) == 1
    assert core.obtener_ventas_historial(conn) == []
    assert app_falsa.refrescos == []


def test_ventana_venta_avisa_inline_cuando_la_canasta_esta_vacia(
    ventana: gui_inventario.VentanaVenta, conn: sqlite3.Connection
) -> None:
    """T13: registrar sin lineas no llega al core ni abre ningun dialogo."""
    with mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        ventana._registrar()

    assert "canasta está vacía" in ventana.status_label.cget("text")
    assert aviso.call_count == 0
    assert core.obtener_ventas_historial(conn) == []


def test_ventana_venta_registrar_usa_la_conexion_de_la_sesion() -> None:
    """T13: la firma es `(self.app.conn, cliente_id, lineas, observaciones)`."""
    fuente = cuerpo_ejecutable("VentanaVenta", "_registrar")

    assert "core.registrar_venta(self.app.conn, cliente_id, lineas, observaciones)" in fuente
    assert "EXCEL_PATH" not in fuente
    assert "forma_pago" not in fuente


# ----------------------------------------------------------------------
# CLI-02 T14 / CLI-05 T7 -- la pestana lee de SQLite
# ----------------------------------------------------------------------


def test_tab_ventas_refrescar_usa_sqlite() -> None:
    """T14: `refrescar` lee de la conexion; la guarda de Excel desaparecio."""
    fuente = cuerpo_ejecutable("TabVentas", "refrescar")

    assert "self.datos_completos = core.obtener_ventas_historial(self.app.conn)" in fuente
    assert "self._aplicar_filtro()" in fuente
    assert "EXCEL_PATH" not in fuente
    assert "os.path.exists" not in fuente


def test_tab_ventas_refrescar_carga_datos(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """T7: `datos_completos` se puebla y el arbol se pinta con esas filas."""
    core.registrar_venta(conn, None, [linea(CODIGO_A, 2, 150.0), linea(CODIGO_B, 1, 300.0)])

    tab.refrescar()

    assert len(tab.datos_completos) == 2
    assert sorted(valores(tab.tree, COL_CODIGO)) == [CODIGO_A, CODIGO_B]
    assert valores(tab.tree, COL_DESCRIPCION) != []


def test_tab_ventas_refrescar_con_base_vacia_deja_la_tabla_sin_filas(
    tab: gui_inventario.TabVentas,
) -> None:
    """T7: sin ventas no hay guarda de Excel que valga, solo `[]`."""
    tab.refrescar()

    assert tab.datos_completos == []
    assert tab.tree.get_children() == ()


def test_tab_ventas_columnas_incluyen_cliente_y_saldo(
    tab: gui_inventario.TabVentas,
) -> None:
    """T10: la columna de forma de pago cedio su lugar a cliente y saldo."""
    columnas = tuple(str(c) for c in tab.tree["columns"])

    assert columnas == COLUMNAS_HISTORIAL
    assert "pago" not in columnas
    assert tab.tree.heading("cliente")["text"] == "Cliente"
    assert tab.tree.heading("saldo")["text"] == "Saldo pendiente"


# ----------------------------------------------------------------------
# CLI-05 T8, T9 -- filtros client-side
# ----------------------------------------------------------------------


def test_tab_ventas_filtro_producto(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """T8: substring case-insensitive sobre codigo o descripcion; vacio = todas."""
    core.registrar_venta(conn, None, [linea(CODIGO_A, 2, 150.0), linea(CODIGO_B, 1, 300.0)])
    tab.refrescar()

    tab.filtro_var.set("SABANAS")
    assert valores(tab.tree, COL_CODIGO) == [CODIGO_B]

    tab.filtro_var.set(CODIGO_A)
    assert valores(tab.tree, COL_CODIGO) == [CODIGO_A]

    tab.filtro_var.set("no existe")
    assert valores(tab.tree, COL_CODIGO) == []

    tab.filtro_var.set("")
    assert sorted(valores(tab.tree, COL_CODIGO)) == [CODIGO_A, CODIGO_B]


def test_tab_ventas_filtro_fecha_rango(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """T9: `desde`/`hasta` acotan inclusive y un extremo en blanco no acota."""
    vieja = core.registrar_venta(conn, None, [linea(CODIGO_A, 1, 150.0)])
    nueva = core.registrar_venta(conn, None, [linea(CODIGO_B, 1, 300.0)])
    fechar_venta(conn, vieja["venta_id"], "2026-07-01")
    fechar_venta(conn, nueva["venta_id"], "2026-07-20")
    tab.refrescar()

    tab.filtro_desde.set("2026-07-10")
    assert valores(tab.tree, COL_CODIGO) == [CODIGO_B]

    tab.filtro_desde.set("")
    tab.filtro_hasta.set("2026-07-10")
    assert valores(tab.tree, COL_CODIGO) == [CODIGO_A]

    tab.filtro_desde.set("2026-07-01")
    tab.filtro_hasta.set("2026-07-20")
    assert sorted(valores(tab.tree, COL_CODIGO)) == [CODIGO_A, CODIGO_B]

    tab.filtro_desde.set("2026-08-01")
    tab.filtro_hasta.set("")
    assert valores(tab.tree, COL_CODIGO) == []


def test_tab_ventas_filtro_fecha_incluye_el_dia_completo_de_los_extremos(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """T9: comparar contra la fecha con hora dejaria fuera el propio dia."""
    venta = core.registrar_venta(conn, None, [linea(CODIGO_A, 1, 150.0)])
    fechar_venta(conn, venta["venta_id"], "2026-07-15")
    tab.refrescar()

    tab.filtro_desde.set("2026-07-15")
    tab.filtro_hasta.set("2026-07-15")

    assert tab.datos_completos[0]["fecha"] == "2026-07-15 13:45:07"
    assert valores(tab.tree, COL_CODIGO) == [CODIGO_A]


def test_tab_ventas_filtros_combinan_con_and(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """T8 + T9: producto y fecha se aplican a la vez, no uno u otro."""
    vieja = core.registrar_venta(conn, None, [linea(CODIGO_A, 1, 150.0)])
    nueva = core.registrar_venta(conn, None, [linea(CODIGO_B, 1, 300.0)])
    fechar_venta(conn, vieja["venta_id"], "2026-07-01")
    fechar_venta(conn, nueva["venta_id"], "2026-07-20")
    tab.refrescar()

    tab.filtro_var.set("sarten")
    tab.filtro_desde.set("2026-07-10")

    assert valores(tab.tree, COL_CODIGO) == []


# ----------------------------------------------------------------------
# CLI-05 T10, T11 -- columnas cliente y saldo
# ----------------------------------------------------------------------


def test_tab_ventas_columna_saldo_condicional(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """T10: el saldo se pinta solo si es mayor que cero; saldada = celda vacia."""
    pendiente = core.registrar_venta(conn, None, [linea(CODIGO_A, 2, 150.0)])
    saldada = core.registrar_venta(conn, None, [linea(CODIGO_B, 1, 300.0)])
    pagar(conn, pendiente["venta_id"], 100.0)
    pagar(conn, saldada["venta_id"], 300.0)

    tab.refrescar()

    saldos = dict(zip(valores(tab.tree, COL_CODIGO), valores(tab.tree, COL_SALDO)))
    assert saldos[CODIGO_A] == "$200.00"
    assert saldos[CODIGO_B] == ""


def test_tab_ventas_muestra_mostrador(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """T11: sin cliente la columna dice `Mostrador`; con cliente, su nombre."""
    cliente_id = core.crear_cliente(conn, NOMBRE_CLIENTE)
    core.registrar_venta(conn, None, [linea(CODIGO_A, 1, 150.0)])
    core.registrar_venta(conn, cliente_id, [linea(CODIGO_B, 1, 300.0)])

    tab.refrescar()

    clientes = dict(zip(valores(tab.tree, COL_CODIGO), valores(tab.tree, COL_CLIENTE)))
    assert clientes[CODIGO_A] == core.CLIENTE_MOSTRADOR
    assert clientes[CODIGO_B] == NOMBRE_CLIENTE


def test_tab_ventas_pinta_la_fecha_completa_de_la_venta(
    tab: gui_inventario.TabVentas, conn: sqlite3.Connection, inventario: None
) -> None:
    """El filtro ignora la hora, pero la tabla si la muestra."""
    venta = core.registrar_venta(conn, None, [linea(CODIGO_A, 1, 150.0)])
    fechar_venta(conn, venta["venta_id"], "2026-07-15")

    tab.refrescar()

    assert valores(tab.tree, COL_FECHA) == ["2026-07-15 13:45:07"]
    assert valores(tab.tree, COL_CANTIDAD) == ["1"]


def test_render_solo_lee_claves_del_contrato_del_core() -> None:
    """Ninguna clave del render esta mal escrita respecto a `CAMPOS_HISTORIAL`.

    `_fila_visible_venta` lee con `.get(clave, 0)`, asi que un typo en una de
    las cuatro columnas de dinero pintaria `$0.00` en silencio y la suite
    seguiria verde: el core produce el dato correcto y el render lo descarta.
    Este guard compara por AST las claves literales que consume el render
    contra el contrato que publica el core, de modo que el typo falla aqui.
    """
    # Arrange
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    render = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_fila_visible_venta"
    )

    # Act
    leidas = {
        nodo.args[0].value
        for nodo in ast.walk(render)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "get"
        and nodo.args
        and isinstance(nodo.args[0], ast.Constant)
        and isinstance(nodo.args[0].value, str)
    }

    # Assert
    assert leidas, "No se detecto ninguna clave en el render"
    assert leidas <= set(core.CAMPOS_HISTORIAL), (
        f"El render lee claves que el core no publica: "
        f"{sorted(leidas - set(core.CAMPOS_HISTORIAL))}"
    )
