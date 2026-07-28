"""Pestana de Entregas sobre SQLite y cableado del flujo de carga (CLI-04).

Cubre las tres piezas de la reescritura -- las columnas nuevas (T3), el doble
clic que abre `VentanaPagos` (T4) y el control de status (T5) -- mas el cableado
que las hace utiles: hasta esta ola `generar_entregas` y `procesar_puntos_bw` no
tenian **ningun** call-site, asi que la pestana habria estado siempre vacia y la
semana nunca habria recibido sus puntos (DEUDA-03 y BW-02 R7).

El cableado se prueba de verdad, no por AST: se llama a `App.al_confirmar_carga`
sin instanciar `App` -- que abriria la base de produccion y correria el backup de
arranque -- usando la funcion desbindada sobre un doble que aporta `conn`,
`mostrar_status` y `refrescar_todo`. Asi se ejercita el codigo real del metodo
contra una base en memoria con el esquema canonico.
"""

from __future__ import annotations

import ast
import sqlite3
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from typing import Any, Final
from unittest import mock

import pytest

import core
import db
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"
PDF_MUESTRA: Final[Path] = RAIZ_PROYECTO / "reference" / "C001264_NOTA.pdf"

TABLA: Final[str] = "entrega_pagos"
TIPO_NORMAL: Final[str] = "Normal (con descuento)"
ASOCIADA: Final[str] = "Ana Ruiz"
FORMA_A: Final[str] = "Efectivo"
MONTO_QUE_DEBE: Final[float] = 300.0
SEMANA_PEDIDO: Final[str] = "30 - 2026"

COLUMNAS_ESPERADAS: Final[tuple[str, ...]] = (
    "fecha",
    "folio",
    "codigo",
    "descripcion",
    "cantidad",
    "monto",
    "pagado",
    "saldo",
    "status",
)

#: Columnas de la version Excel que la migracion retira: los abonos ya no caben
#: en dos parejas fijas de (forma, monto), viven en `entrega_pagos`.
COLUMNAS_RETIRADAS: Final[tuple[str, ...]] = ("pago1", "monto1", "pago2", "monto2")

COL_MONTO: Final[int] = 5
COL_PAGADO: Final[int] = 6
COL_SALDO: Final[int] = 7
COL_STATUS: Final[int] = 8


def _fila(
    *,
    folio: str = "F1",
    codigo: str = "A1",
    surtida: int = 10,
    precio: float = MONTO_QUE_DEBE,
    semana: str = SEMANA_PEDIDO,
    archivo: str = "C001264_NOTA.pdf (pag. 1)",
) -> dict[str, Any]:
    """Fila del extractor lista para `confirmar_carga`, toda al asociado.

    Time: O(1) | Space: O(1)
    """
    return {
        "Folio de pedido": folio,
        "Nombre asociado": ASOCIADA,
        "Codigo articulo": codigo,
        "Descripcion": "Producto de prueba",
        "Cantidad solicitada": surtida,
        "Cantidad surtida": surtida,
        "Cantidad Asociado": surtida,
        "Cantidad Casa": 0,
        "Cantidad Local": 0,
        "Precio catalogo": precio,
        "Precio con IVA": precio,
        "Precio que pagas": precio,
        "Valor total con IVA": precio,
        "Tipo": TIPO_NORMAL,
        "Semana": semana,
        "Archivo origen": archivo,
    }


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico: CHECK, FK, vistas y triggers."""
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
def tab(app_falsa: tk.Tk) -> gui_inventario.TabEntregas:
    """Pestana de Entregas montada sobre la app falsa."""
    return gui_inventario.TabEntregas(ttk.Notebook(app_falsa), app_falsa)


@pytest.fixture()
def entrega_id(conn: sqlite3.Connection) -> int:
    """Siembra una entrega por el flujo real y devuelve su id."""
    core.confirmar_carga(conn, [_fila()])
    assert core.generar_entregas(conn) == 1
    return int(conn.execute("SELECT id FROM entregas_asociado").fetchone()["id"])


def _metodo_ast(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


def _codigo_sin_docstrings(nodo: ast.AST) -> str:
    """Fuente ejecutable de `nodo`, sin sus docstrings ni los de sus hijos.

    Time: O(n) sobre los nodos | Space: O(n)
    """
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


def _confirmar_con_doble(
    conexion: sqlite3.Connection, filas: list[dict], rutas_pdf: list[str] | None = None
) -> SimpleNamespace:
    """Ejecuta el `al_confirmar_carga` real sobre un doble de `App`.

    Time: O(n) sobre las filas | Space: O(n)
    """
    doble = SimpleNamespace(
        conn=conexion,
        rutas_pdf_carga=list(rutas_pdf or ()),
        mostrar_status=lambda texto, color="#008000": None,
        refrescar_todo=lambda: None,
    )
    for nombre in (
        "_generar_entregas_de_la_carga",
        "_procesar_puntos_de_la_carga",
        "_procesar_puntos_de_pdf",
    ):
        setattr(doble, nombre, getattr(gui_inventario.App, nombre).__get__(doble))

    with mock.patch.object(gui_inventario.messagebox, "showinfo"):
        gui_inventario.App.al_confirmar_carga(doble, filas)
    return doble


# --------------------------------------------------------------------------
# T3 -- columnas nuevas y poblado desde SQLite
# --------------------------------------------------------------------------


def test_tab_entregas_columns_drop_forma_pago(
    tab: gui_inventario.TabEntregas,
) -> None:
    """T3: las cuatro columnas de pago fijo salen; entran `pagado` y `saldo`."""
    columnas = tuple(str(c) for c in tab.tree["columns"])

    assert columnas == COLUMNAS_ESPERADAS
    assert not set(COLUMNAS_RETIRADAS) & set(columnas)
    assert tab.tree.heading("pagado")["text"] == "Pagado"
    assert tab.tree.heading("saldo")["text"] == "Saldo"


def test_tab_entregas_refrescar_lee_de_sqlite(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T3: la fila sale de `core.listar_entregas`, con `iid` = id de la entrega."""
    tab.refrescar()

    assert tab.tree.get_children() == (str(entrega_id),)
    valores = tab.tree.item(str(entrega_id), "values")
    assert valores[COL_MONTO] == f"${MONTO_QUE_DEBE:.2f}"
    assert valores[COL_PAGADO] == "$0.00"
    assert valores[COL_SALDO] == f"${MONTO_QUE_DEBE:.2f}"
    assert valores[COL_STATUS] == core.ENTREGA_STATUS_VALIDOS[0]


def test_tab_entregas_refrescar_calcula_pagado_y_saldo_por_fila(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T3: los agregados vienen de `core_pagos`, no del JOIN de entregas."""
    core.agregar_pago(conn, TABLA, entrega_id, FORMA_A, 120.0)

    tab.refrescar()

    valores = tab.tree.item(str(entrega_id), "values")
    assert valores[COL_PAGADO] == "$120.00"
    assert valores[COL_SALDO] == "$180.00"


def test_tab_entregas_refrescar_base_vacia_no_pinta_filas(
    tab: gui_inventario.TabEntregas,
) -> None:
    """T3: sin entregas el arbol queda vacio y la cache tambien."""
    tab.refrescar()

    assert tab.tree.get_children() == ()
    assert tab.entregas == {}


def test_tab_entregas_refrescar_conserva_el_color_por_status(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T3: el tag de la fila sigue el status, como en la version Excel."""
    core.actualizar_status_entrega(conn, entrega_id, "Pagado")

    tab.refrescar()

    assert tab.tree.item(str(entrega_id), "tags") == ("pagado",)


def test_tab_entregas_muestra_pagado_con_saldo_pendiente(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T3: status "Pagado" y saldo > 0 conviven, y ambos quedan a la vista.

    Marcar "Pagado" no registra abono: status y dinero son ejes independientes
    en la capa core. Mostrar las dos columnas juntas hace visible la
    discrepancia en vez de esconderla.
    """
    core.actualizar_status_entrega(conn, entrega_id, "Pagado")

    tab.refrescar()

    valores = tab.tree.item(str(entrega_id), "values")
    assert valores[COL_STATUS] == "Pagado"
    assert valores[COL_SALDO] == f"${MONTO_QUE_DEBE:.2f}"


def test_tab_entregas_no_ejecuta_sql() -> None:
    """ADR-2: la pestana no escribe SQL; toda lectura pasa por `core`.

    Se mira el codigo ejecutable, no los docstrings: la documentacion si puede
    -- y debe -- explicar que el JOIN vive en `core.listar_entregas`.
    """
    fuente = _codigo_sin_docstrings(_metodo_ast("TabEntregas", "refrescar"))
    fuente += _codigo_sin_docstrings(_metodo_ast("TabEntregas", "_fila_visible"))

    for palabra in ("SELECT", "JOIN", "INSERT INTO", "UPDATE ", "get_conn", "execute("):
        assert palabra not in fuente, f"La GUI ejecuta SQL: aparece {palabra!r}"


# --------------------------------------------------------------------------
# T4 -- doble clic: `VentanaPagos`, no `VentanaDetalleEntrega`
# --------------------------------------------------------------------------


def test_tab_entregas_doubleclick_opens_ventana_pagos(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T4: el doble clic abre el dialogo compartido sobre `entrega_pagos`."""
    tab.refrescar()
    tab.tree.selection_set(str(entrega_id))

    with mock.patch.object(gui_inventario, "VentanaPagos") as ventana:
        tab._abrir_pagos(None)

    assert ventana.call_count == 1
    argumentos = ventana.call_args.args
    assert argumentos[1] == TABLA
    assert argumentos[2] == entrega_id
    assert argumentos[3] == MONTO_QUE_DEBE


def test_tab_entregas_doubleclick_sin_seleccion_no_abre_nada(
    tab: gui_inventario.TabEntregas, entrega_id: int
) -> None:
    """T4: sin fila seleccionada el doble clic no construye ningun dialogo."""
    tab.refrescar()

    with mock.patch.object(gui_inventario, "VentanaPagos") as ventana:
        tab._abrir_pagos(None)

    assert ventana.call_count == 0


def test_ventana_detalle_entrega_sale_del_arbol() -> None:
    """T4: `VentanaDetalleEntrega` queda retirada del flujo de entregas."""
    fuente = GUI_PATH.read_text(encoding="utf-8")

    assert not hasattr(gui_inventario, "VentanaDetalleEntrega")
    assert "VentanaDetalleEntrega" not in fuente


# --------------------------------------------------------------------------
# T5 -- control de status
# --------------------------------------------------------------------------


def test_tab_entregas_status_calls_core(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T5: el combo enruta por `core.actualizar_status_entrega` y refresca."""
    tab.refrescar()
    tab.tree.selection_set(str(entrega_id))
    tab.status_var.set("Recogido - no pagado")

    tab._aplicar_status()

    fila = conn.execute(
        "SELECT status FROM entregas_asociado WHERE id = ?", (entrega_id,)
    ).fetchone()
    assert fila["status"] == "Recogido - no pagado"
    assert tab.tree.item(str(entrega_id), "values")[COL_STATUS] == "Recogido - no pagado"


def test_tab_entregas_status_se_puebla_desde_la_constante_de_core(
    tab: gui_inventario.TabEntregas,
) -> None:
    """T5: las opciones son `core.ENTREGA_STATUS_VALIDOS`, sin lista paralela."""
    valores = tuple(str(v) for v in tab.combo_status["values"])

    assert valores == core.ENTREGA_STATUS_VALIDOS


def test_tab_entregas_status_sin_seleccion_avisa_y_no_escribe(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T5: sin fila seleccionada se avisa y la base no se toca."""
    tab.refrescar()

    with mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        tab._aplicar_status()

    assert aviso.call_count == 1
    fila = conn.execute(
        "SELECT status FROM entregas_asociado WHERE id = ?", (entrega_id,)
    ).fetchone()
    assert fila["status"] == core.ENTREGA_STATUS_VALIDOS[0]


def test_tab_entregas_status_invalido_sale_por_dialogo_sin_crash(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T5: `StatusEntregaInvalidoError` se muestra y no escapa."""
    tab.refrescar()
    tab.tree.selection_set(str(entrega_id))
    tab.status_var.set("Status inventado")

    with mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        tab._aplicar_status()

    assert showerror.call_count == 1
    fila = conn.execute(
        "SELECT status FROM entregas_asociado WHERE id = ?", (entrega_id,)
    ).fetchone()
    assert fila["status"] == core.ENTREGA_STATUS_VALIDOS[0]


def test_tab_entregas_status_no_mueve_el_saldo_del_asociado(
    tab: gui_inventario.TabEntregas, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T5, ADR-3: marcar "Pagado" es un cambio de estado, no un abono."""
    tab.refrescar()
    tab.tree.selection_set(str(entrega_id))
    tab.status_var.set("Pagado")

    tab._aplicar_status()

    fila = conn.execute(
        "SELECT saldo_pendiente FROM asociados WHERE nombre = ?", (ASOCIADA,)
    ).fetchone()
    assert round(float(fila["saldo_pendiente"]), 2) == MONTO_QUE_DEBE


# --------------------------------------------------------------------------
# T-W1 -- cableado del flujo de carga (DEUDA-03 + BW-02 R7)
# --------------------------------------------------------------------------


def test_al_confirmar_carga_genera_entregas_y_puntos(
    conn: sqlite3.Connection,
) -> None:
    """T-W1: confirmar la carga crea las entregas y fija los puntos del PDF.

    Es el hueco que cerro esta ola: `generar_entregas` y `procesar_puntos_bw`
    existian desde MERC-04 y BW-01 pero nadie las llamaba, asi que ni se creaba
    una sola entrega ni se guardaba un solo punto.
    """
    if not PDF_MUESTRA.exists():  # pragma: no cover - depende del arbol de datos
        pytest.skip("No esta el PDF de muestra en reference/")

    _confirmar_con_doble(conn, [_fila()], [str(PDF_MUESTRA)])

    entregas = core.listar_entregas(conn)
    assert len(entregas) == 1
    assert entregas[0]["asociado"] == ASOCIADA
    assert entregas[0]["monto_que_debe"] == MONTO_QUE_DEBE

    puntos = {
        s["semana_texto"]: s["puntos_bw_acumulados"] for s in core.listar_semanas(conn)
    }
    assert max(puntos.values()) > 0


def test_al_confirmar_carga_atribuye_los_puntos_a_la_semana_de_cierre(
    conn: sqlite3.Connection,
) -> None:
    """T-W1, BW-02 R5: los puntos van a la semana de cierre, no a la del pedido.

    El PDF de muestra levanta pedidos de la semana 30 y reporta el acumulado
    "al cierre de semana 29": los puntos pertenecen a la 29.
    """
    if not PDF_MUESTRA.exists():  # pragma: no cover - depende del arbol de datos
        pytest.skip("No esta el PDF de muestra en reference/")

    _confirmar_con_doble(conn, [_fila()], [str(PDF_MUESTRA)])

    puntos = {
        s["semana_texto"]: s["puntos_bw_acumulados"] for s in core.listar_semanas(conn)
    }
    assert puntos["29 - 2026"] > 0
    assert puntos[SEMANA_PEDIDO] == 0


def test_al_confirmar_carga_sin_rutas_pdf_igual_genera_entregas(
    conn: sqlite3.Connection,
) -> None:
    """T-W1: los pasos son independientes; sin PDF abrible las entregas salen."""
    doble = _confirmar_con_doble(conn, [_fila()], [])

    assert len(core.listar_entregas(conn)) == 1
    assert doble.rutas_pdf_carga == []


def test_al_confirmar_carga_pdf_ilegible_no_pierde_la_carga(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """T-W1: un fallo al procesar puntos avisa pero no revierte lo commiteado."""
    roto = tmp_path / "roto.pdf"
    roto.write_text("esto no es un PDF", encoding="utf-8")
    fila = _fila(archivo=f"{roto.name} (pag. 1)")

    with mock.patch.object(gui_inventario.messagebox, "showwarning") as aviso:
        _confirmar_con_doble(conn, [fila], [str(roto)])

    assert aviso.call_count == 1
    assert len(core.listar_entregas(conn)) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM pedidos").fetchone()["n"] == 1


def test_al_confirmar_carga_es_reejecutable_sin_duplicar_entregas(
    conn: sqlite3.Connection,
) -> None:
    """T-W1: `generar_entregas` es idempotente; confirmar dos veces no duplica."""
    _confirmar_con_doble(conn, [_fila()], [])
    _confirmar_con_doble(conn, [_fila()], [])

    assert len(core.listar_entregas(conn)) == 1


def test_al_confirmar_carga_no_captura_exception() -> None:
    """T-W1, `.langs/python.md` 6: los manejadores nombran errores de dominio."""
    fuente = ast.unparse(_metodo_ast("App", "al_confirmar_carga"))

    assert "except Exception" not in fuente
    assert "except core.CargaError" in fuente


def test_al_confirmar_carga_encadena_entregas_y_puntos() -> None:
    """T-W1: los dos pasos nuevos cuelgan del guardado, en ese orden."""
    fuente = ast.unparse(_metodo_ast("App", "al_confirmar_carga"))

    assert fuente.index("core.confirmar_carga") < fuente.index(
        "_generar_entregas_de_la_carga"
    )
    assert fuente.index("_generar_entregas_de_la_carga") < fuente.index(
        "_procesar_puntos_de_la_carga"
    )


def test_semana_por_archivo_indexa_por_nombre_de_archivo() -> None:
    """T-W1: `"Archivo origen"` trae nombre + pagina, nunca una ruta abrible."""
    filas = [
        _fila(archivo="A.pdf (pag. 1)", semana="30 - 2026"),
        _fila(archivo="A.pdf (pag. 2)", semana="30 - 2026"),
        _fila(archivo="B.pdf (pag. 1)", semana="31 - 2026"),
    ]

    mapa = gui_inventario._semana_por_archivo(filas)

    assert mapa == {"A.pdf": "30 - 2026", "B.pdf": "31 - 2026"}


def test_semana_por_archivo_ignora_las_filas_sin_semana() -> None:
    """T-W1: sin semana no hay a que atribuir los puntos; la fila se descarta."""
    filas = [_fila(archivo="A.pdf (pag. 1)", semana="")]

    mapa = gui_inventario._semana_por_archivo(filas)

    assert mapa == {}
