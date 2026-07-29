"""Boton "Exportar a Excel" de la barra superior (CLI-06, GUI).

`export_excel.exportar_a_excel` ya tiene su suite propia: aqui solo se prueba lo
que la GUI aporta, que son tres decisiones y ninguna linea de formato:

* pedir la ruta con `filedialog.asksaveasfilename` y **no hacer nada** si la
  usuaria cancela (el dialogo devuelve cadena vacia);
* llamar a la capa core con la conexion de la sesion, nunca con SQL propio;
* traducir el fallo de escritura a un `messagebox` en vez de propagarlo.

El metodo se ejercita **desbindado** sobre un doble de `App` --como en
`tests/test_gui_entregas.py`--: instanciar `App` abriria la base de produccion y
correria el backup de arranque. Asi corre el codigo real contra una base en
memoria y un `tmp_path` de verdad, sin ventana.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Final
from unittest import mock

import pytest

import db
import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

HOJAS_ESPERADAS: Final[tuple[str, ...]] = ("Existencias", "Ventas")


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


@pytest.fixture()
def app_doble(conn: sqlite3.Connection) -> SimpleNamespace:
    """Doble de `App` con lo unico que el exportador consume."""
    doble = SimpleNamespace(conn=conn, status=[])
    doble.mostrar_status = lambda texto, color="#008000": doble.status.append(texto)
    return doble


def exportar(doble: SimpleNamespace) -> None:
    """Ejecuta el `exportar_a_excel` real de `App` sobre el doble."""
    gui_inventario.App.exportar_a_excel(doble)


def metodo_ast(clase: str, nombre: str) -> ast.FunctionDef:
    """Nodo AST del metodo `nombre` de `clase` en `gui_inventario.py`."""
    arbol = ast.parse(GUI_PATH.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.FunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"No se encontro {clase}.{nombre}")


# --------------------------------------------------------------------------
# La ruta del dialogo manda
# --------------------------------------------------------------------------


def test_exportar_genera_el_libro_en_la_ruta_elegida(
    app_doble: SimpleNamespace, tmp_path: Path
) -> None:
    """La ruta del dialogo es la que recibe `core.exportar_a_excel`."""
    destino = tmp_path / "reporte.xlsx"

    with mock.patch.object(
        gui_inventario.filedialog, "asksaveasfilename", return_value=str(destino)
    ), mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        exportar(app_doble)

    assert destino.exists()
    assert aviso.call_count == 1
    assert str(destino) in aviso.call_args.args[1]


def test_exportar_pide_la_extension_xlsx(
    app_doble: SimpleNamespace, tmp_path: Path
) -> None:
    """El dialogo se abre con `.xlsx` por omision, no con el nombre pelado."""
    destino = tmp_path / "reporte.xlsx"

    with mock.patch.object(
        gui_inventario.filedialog, "asksaveasfilename", return_value=str(destino)
    ) as dialogo, mock.patch.object(gui_inventario.messagebox, "showinfo"):
        exportar(app_doble)

    assert dialogo.call_args.kwargs["defaultextension"] == ".xlsx"
    assert dialogo.call_args.kwargs["filetypes"] == [("Libro de Excel", "*.xlsx")]


def test_exportar_deja_las_dos_hojas_del_reporte(
    app_doble: SimpleNamespace, tmp_path: Path
) -> None:
    """El contenido lo arma la capa core: la GUI no lo reimplementa."""
    openpyxl = pytest.importorskip("openpyxl")
    destino = tmp_path / "reporte.xlsx"

    with mock.patch.object(
        gui_inventario.filedialog, "asksaveasfilename", return_value=str(destino)
    ), mock.patch.object(gui_inventario.messagebox, "showinfo"):
        exportar(app_doble)

    libro = openpyxl.load_workbook(destino)
    assert tuple(libro.sheetnames) == HOJAS_ESPERADAS


def test_exportar_actualiza_la_barra_de_status(
    app_doble: SimpleNamespace, tmp_path: Path
) -> None:
    """La ruta generada queda a la vista sin tener que leer el dialogo."""
    destino = tmp_path / "reporte.xlsx"

    with mock.patch.object(
        gui_inventario.filedialog, "asksaveasfilename", return_value=str(destino)
    ), mock.patch.object(gui_inventario.messagebox, "showinfo"):
        exportar(app_doble)

    assert app_doble.status == [f"Exportación generada en {destino}"]


# --------------------------------------------------------------------------
# Cancelar y fallar
# --------------------------------------------------------------------------


def test_cancelar_el_dialogo_no_exporta_nada(
    app_doble: SimpleNamespace, tmp_path: Path
) -> None:
    """Cancelar devuelve cadena vacia: no se escribe ni se avisa."""
    with mock.patch.object(
        gui_inventario.filedialog, "asksaveasfilename", return_value=""
    ), mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso, \
            mock.patch.object(gui_inventario.messagebox, "showerror") as error:
        exportar(app_doble)

    assert list(tmp_path.iterdir()) == []
    assert aviso.call_count == 0
    assert error.call_count == 0
    assert app_doble.status == []


def test_ruta_no_escribible_se_muestra_por_dialogo_sin_propagar(
    app_doble: SimpleNamespace, tmp_path: Path
) -> None:
    """El fallo de escritura llega envuelto en `ExportError` (un `CoreError`)."""
    destino = tmp_path / "carpeta_inexistente" / "reporte.xlsx"

    with mock.patch.object(
        gui_inventario.filedialog, "asksaveasfilename", return_value=str(destino)
    ), mock.patch.object(gui_inventario.messagebox, "showerror") as error, \
            mock.patch.object(gui_inventario.messagebox, "showinfo") as aviso:
        exportar(app_doble)

    assert error.call_count == 1
    assert aviso.call_count == 0
    assert not destino.exists()


def test_error_crudo_del_sistema_de_archivos_tampoco_propaga(
    app_doble: SimpleNamespace, tmp_path: Path
) -> None:
    """`OSError` sin envolver cae en el mismo manejador, no en `Exception`."""
    destino = tmp_path / "reporte.xlsx"

    with mock.patch.object(
        gui_inventario.filedialog, "asksaveasfilename", return_value=str(destino)
    ), mock.patch.object(
        gui_inventario.core, "exportar_a_excel", side_effect=OSError("disco lleno")
    ), mock.patch.object(gui_inventario.messagebox, "showerror") as error:
        exportar(app_doble)

    assert error.call_count == 1
    assert "disco lleno" in error.call_args.args[1]


# --------------------------------------------------------------------------
# Cableado y estandares
# --------------------------------------------------------------------------


def test_la_barra_superior_ofrece_el_boton_de_exportar() -> None:
    """El punto de entrada existe de verdad en la barra de la ventana."""
    barra = ast.unparse(metodo_ast("App", "_construir_barra_superior"))
    acciones = ast.unparse(metodo_ast("App", "_construir_acciones_archivo"))

    assert "self._construir_acciones_archivo(barra)" in barra
    assert "command=self.exportar_a_excel" in acciones
    assert "Exportar a Excel" in acciones


def test_exportar_llama_a_la_capa_core_con_la_conexion_de_sesion() -> None:
    """ADR-2: la GUI no arma el libro ni abre conexiones propias."""
    fuente = ast.unparse(metodo_ast("App", "exportar_a_excel"))

    assert "core.exportar_a_excel(self.conn, ruta)" in fuente
    for palabra in ("SELECT", "execute(", "get_conn", "Workbook"):
        assert palabra not in fuente, f"La GUI hace de mas: aparece {palabra!r}"


def test_exportar_no_captura_exception_generica() -> None:
    """`.langs/python.md` §6: se capturan los errores de dominio concretos."""
    metodo = metodo_ast("App", "exportar_a_excel")

    capturados = {
        ast.unparse(manejador.type)
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.Try)
        for manejador in nodo.handlers
        if manejador.type is not None
    }

    assert capturados == {"(core.CoreError, OSError)"}
