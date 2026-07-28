"""Dialogo de pagos reusable `VentanaPagos` (CLI-03, T7 a T9).

`VentanaPagos` es el **unico** lugar del sistema donde se capturan abonos: la
entrega a asociado (CLI-04) lo abre con `"entrega_pagos"` y el anticipo de
encargo (ENC-02) lo abrira con `"encargo_pagos"`. Por eso la suite no se limita
a ejercitarlo sobre un dominio: hay una prueba explicita de que el widget es
agnostico -- que su codigo no nombra ninguna tabla ni ningun dominio concreto --,
porque esa es justamente la propiedad que ENC-02 va a heredar.

Se instancian widgets reales contra un `Tk()` oculto que hace de `app`; nunca la
clase `App`, que abriria la base de produccion y correria el backup de arranque.
La capa core corre de verdad contra una base en memoria: solo se parchean los
dialogos (`messagebox`), que son I/O externo.
"""

from __future__ import annotations

import ast
import sqlite3
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest

import core
import db
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

TABLA: Final[str] = "entrega_pagos"
TIPO_NORMAL: Final[str] = "Normal (con descuento)"
ASOCIADA: Final[str] = "Ana Ruiz"
FORMA_A: Final[str] = "Efectivo"
FORMA_B: Final[str] = "Transferencia"
MONTO_QUE_DEBE: Final[float] = 300.0

COL_FECHA: Final[int] = 0
COL_FORMA: Final[int] = 1
COL_MONTO: Final[int] = 2

#: Nombres que un componente compartido no puede llevar cableados: las tres
#: tablas de pago y sus columnas FK. Salen de `core.PAGO_TABLAS` para que anadir
#: un cuarto dominio de pagos extienda la guarda sin tocar esta prueba. Si
#: alguno aparece en el cuerpo de la clase, el widget dejo de ser reusable.
NOMBRES_DE_DOMINIO: Final[tuple[str, ...]] = tuple(
    sorted(set(core.PAGO_TABLAS) | set(core.PAGO_TABLAS.values()))
)


def _fila(
    *,
    folio: str = "F1",
    codigo: str = "A1",
    surtida: int = 10,
    precio: float = MONTO_QUE_DEBE,
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
    }


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico: CHECK, FK y triggers."""
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
def entrega_id(conn: sqlite3.Connection) -> int:
    """Siembra una entrega por el flujo real y devuelve su id."""
    core.confirmar_carga(conn, [_fila()])
    assert core.generar_entregas(conn) == 1
    return int(conn.execute("SELECT id FROM entregas_asociado").fetchone()["id"])


@pytest.fixture()
def ventana(
    app_falsa: tk.Tk, entrega_id: int
) -> Iterator[gui_inventario.VentanaPagos]:
    """Dialogo de pagos montado sobre una entrega real."""
    dialogo = gui_inventario.VentanaPagos(
        app_falsa, TABLA, entrega_id, MONTO_QUE_DEBE, "Pagos de prueba"
    )
    try:
        yield dialogo
    finally:
        dialogo.destroy()


def _clase_ast(nombre: str) -> ast.ClassDef:
    """Nodo AST de la clase `nombre` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"No se encontro la clase {nombre}")


def _init_ast(clase: ast.ClassDef) -> ast.FunctionDef:
    """Nodo AST del `__init__` de `clase`."""
    for hijo in clase.body:
        if isinstance(hijo, ast.FunctionDef) and hijo.name == "__init__":
            return hijo
    raise AssertionError(f"{clase.name} no define __init__")


def _codigo_sin_docstrings(clase: ast.ClassDef) -> str:
    """Fuente ejecutable de `clase`, sin sus docstrings ni las de sus metodos.

    La asercion de agnosticismo es sobre el **codigo**: la documentacion si debe
    poder nombrar a los tres dominios que reusan el componente, porque explicar
    quien lo comparte es justamente lo que evita que alguien lo especialice.

    Time: O(n) sobre los nodos de la clase | Space: O(n)
    """
    copia = ast.parse(ast.unparse(clase)).body[0]
    for nodo in ast.walk(copia):
        cuerpo = getattr(nodo, "body", None)
        if not isinstance(cuerpo, list) or not cuerpo:
            continue
        primero = cuerpo[0]
        if isinstance(primero, ast.Expr) and isinstance(primero.value, ast.Constant):
            if isinstance(primero.value.value, str):
                cuerpo[0] = ast.Pass()
    return ast.unparse(copia)


# --------------------------------------------------------------------------
# T7 -- construccion: estado del padre, lista de pagos y panel de totales
# --------------------------------------------------------------------------


def test_ventana_pagos_constructs(
    app_falsa: tk.Tk, entrega_id: int
) -> None:
    """T7: el dialogo guarda tabla/padre/total y monta lista + totales."""
    dialogo = gui_inventario.VentanaPagos(
        app_falsa, TABLA, entrega_id, MONTO_QUE_DEBE, "Pagos de prueba"
    )

    assert dialogo.tabla == TABLA
    assert dialogo.parent_id == entrega_id
    assert dialogo.total == MONTO_QUE_DEBE
    assert tuple(str(c) for c in dialogo.tree["columns"]) == ("fecha", "forma", "monto")
    assert dialogo.lbl_pagado.cget("text") == "Pagado: $0.00"
    assert dialogo.lbl_total.cget("text") == f"Total: ${MONTO_QUE_DEBE:.2f}"
    assert dialogo.lbl_saldo.cget("text") == f"Saldo: ${MONTO_QUE_DEBE:.2f}"

    dialogo.destroy()


def test_ventana_pagos_lista_los_abonos_previos_al_abrirse(
    app_falsa: tk.Tk, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T7/T9: un abono anterior ya esta en la lista al construir el dialogo."""
    core.agregar_pago(conn, TABLA, entrega_id, FORMA_A, 100.0, "2026-07-20")

    dialogo = gui_inventario.VentanaPagos(
        app_falsa, TABLA, entrega_id, MONTO_QUE_DEBE, "Pagos de prueba"
    )

    filas = dialogo.tree.get_children()
    assert len(filas) == 1
    assert dialogo.tree.item(filas[0], "values")[COL_FORMA] == FORMA_A
    assert dialogo.tree.item(filas[0], "values")[COL_MONTO] == "$100.00"

    dialogo.destroy()


def test_ventana_pagos_es_agnostica_del_dominio_padre() -> None:
    """T7, R9: el componente no cablea ninguna tabla ni columna de un dominio.

    Es la propiedad que hace que ENC-02 pueda reusarlo tal cual: la tabla, el id
    del padre y el total entran por el constructor, exactamente como `core_pagos`
    hizo en la capa core (ADR-6).
    """
    clase = _clase_ast("VentanaPagos")
    fuente = _codigo_sin_docstrings(clase)

    presentes = [n for n in NOMBRES_DE_DOMINIO if n in fuente]
    assert presentes == [], (
        f"VentanaPagos cablea nombres de un dominio concreto: {presentes}"
    )
    firma = [arg.arg for arg in _init_ast(clase).args.args]
    assert firma == ["self", "app", "tabla", "parent_id", "total", "titulo"]


# --------------------------------------------------------------------------
# T8 -- alta de un abono: combo desde la constante, parseo y errores por dialogo
# --------------------------------------------------------------------------


def test_ventana_pagos_combo_se_puebla_desde_la_constante_de_core(
    ventana: gui_inventario.VentanaPagos,
) -> None:
    """T8: las formas de pago salen de `core.FORMAS_PAGO_VALIDAS`, no de la GUI."""
    assert set(core.FORMAS_PAGO_VALIDAS) == set(gui_inventario.core.FORMAS_PAGO_VALIDAS)
    assert ventana.forma_var.get() in core.FORMAS_PAGO_VALIDAS


def test_ventana_pagos_fecha_por_defecto_es_hoy(
    ventana: gui_inventario.VentanaPagos,
) -> None:
    """T8: la fecha nace con el dia de hoy en el formato del esquema."""
    import datetime

    assert ventana.fecha_var.get() == datetime.date.today().isoformat()


def test_ventana_pagos_agregar_calls_core(
    ventana: gui_inventario.VentanaPagos,
    conn: sqlite3.Connection,
    entrega_id: int,
    app_falsa: tk.Tk,
) -> None:
    """T8: el alta persiste por `core.agregar_pago` y refresca la app."""
    ventana.forma_var.set(FORMA_B)
    ventana.monto_var.set("120.50")
    ventana.fecha_var.set("2026-07-21")

    ventana._agregar()

    pagos = core.listar_pagos(conn, TABLA, entrega_id)
    assert [p["forma_pago"] for p in pagos] == [FORMA_B]
    assert pagos[0]["monto"] == 120.50
    assert pagos[0]["fecha"] == "2026-07-21"
    assert app_falsa.refrescos == [1]
    assert ventana.monto_var.get() == ""


def test_ventana_pagos_monto_no_numerico_no_persiste(
    ventana: gui_inventario.VentanaPagos, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T8: un monto que no es numero avisa inline y no toca la base."""
    ventana.monto_var.set("mil pesos")

    ventana._agregar()

    assert core.listar_pagos(conn, TABLA, entrega_id) == []
    assert "número" in ventana.status_label.cget("text")


def test_ventana_pagos_monto_invalido_se_muestra_por_dialogo(
    ventana: gui_inventario.VentanaPagos, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T8, R9: `MontoInvalidoError` sale por `messagebox`, sin escapar."""
    ventana.monto_var.set("0")

    with mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        ventana._agregar()

    assert showerror.call_count == 1
    assert core.listar_pagos(conn, TABLA, entrega_id) == []


def test_ventana_pagos_tabla_invalida_se_muestra_por_dialogo(
    app_falsa: tk.Tk, entrega_id: int
) -> None:
    """T8, R10: una tabla fuera de la whitelist no rompe el dialogo."""
    with mock.patch.object(gui_inventario.messagebox, "showerror") as showerror:
        dialogo = gui_inventario.VentanaPagos(
            app_falsa, "tabla_inventada", entrega_id, MONTO_QUE_DEBE, "Pagos"
        )

    assert showerror.call_count == 1
    assert dialogo.tree.get_children() == ()

    dialogo.destroy()


# --------------------------------------------------------------------------
# T9 -- refresco: lista y totales salen siempre de la capa core
# --------------------------------------------------------------------------


def test_ventana_pagos_refresca_totales(
    ventana: gui_inventario.VentanaPagos, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T9: pagado y saldo se recalculan con `core` tras cada abono."""
    ventana.forma_var.set(FORMA_A)
    ventana.monto_var.set("100")
    ventana._agregar()
    ventana.forma_var.set(FORMA_B)
    ventana.monto_var.set("50.25")
    ventana._agregar()

    ventana._refrescar()

    esperado_pagado = core.total_pagado(conn, TABLA, entrega_id)
    esperado_saldo = core.saldo_pendiente(conn, TABLA, entrega_id, MONTO_QUE_DEBE)
    assert esperado_pagado == 150.25
    assert ventana.lbl_pagado.cget("text") == f"Pagado: ${esperado_pagado:.2f}"
    assert ventana.lbl_saldo.cget("text") == f"Saldo: ${esperado_saldo:.2f}"
    assert len(ventana.tree.get_children()) == 2


def test_ventana_pagos_refrescar_muestra_sobrepago_como_saldo_negativo(
    ventana: gui_inventario.VentanaPagos,
) -> None:
    """T9: abonar de mas deja saldo negativo; no se recorta a cero."""
    ventana.forma_var.set(FORMA_A)
    ventana.monto_var.set("350")

    ventana._agregar()

    assert ventana.lbl_saldo.cget("text") == "Saldo: $-50.00"


def test_ventana_pagos_refrescar_usa_el_id_del_pago_como_iid(
    ventana: gui_inventario.VentanaPagos, conn: sqlite3.Connection, entrega_id: int
) -> None:
    """T9: el `iid` de cada fila es la clave primaria del pago."""
    pago_id = core.agregar_pago(conn, TABLA, entrega_id, FORMA_A, 25.0)

    ventana._refrescar()

    assert ventana.tree.get_children() == (str(pago_id),)


def test_ventana_pagos_no_escribe_el_saldo_del_asociado(
    ventana: gui_inventario.VentanaPagos, conn: sqlite3.Connection
) -> None:
    """R9, ADR-3: el saldo lo baja el trigger; la GUI no lo toca (riesgo RT-3)."""
    ventana.forma_var.set(FORMA_A)
    ventana.monto_var.set("100")

    ventana._agregar()

    fila = conn.execute(
        "SELECT saldo_pendiente FROM asociados WHERE nombre = ?", (ASOCIADA,)
    ).fetchone()
    assert round(float(fila["saldo_pendiente"]), 2) == MONTO_QUE_DEBE - 100.0
