"""Pruebas de la capa de resiliencia (`backup.py`) y de su cableado en la GUI.

Cobertura por requisito:

* R1 -> `test_backup_db_*_copia_*`
* R2 -> `test_backup_db_devuelve_none_*`
* R3 -> `test_startup_*_cuando_el_respaldo_falla` y
        `test_startup_*_cuando_no_se_puede_configurar_la_bitacora`
* R4 -> `test_gui_inventario_registra_en_bitacora_*` (estatica, derivada)
* R5 -> `test_setup_logging_*`
* R6 -> `test_resolvers_apuntan_a_la_base_de_la_app_y_no_al_cwd` + estatica de
        la GUI, que consume esos mismos resolvers.

Las aserciones sobre `gui_inventario.py` son **estaticas** (`ast.parse`): el
modulo importa `inventario_core`, que aun no existe fuera de `reference/`, y
construir un `tkinter.Tk` no es viable en el runner. La cobertura de R4 se
**deriva** del arbol: se localiza cada `try` que envuelve una llamada al
dominio (`core.*`) y se exige bitacora en todos, de modo que un manejador
nuevo sin `logger.exception` rompe la suite automaticamente.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import pytest

import backup
from db import ruta_base

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"
PATRON_RESPALDO: Final[re.Pattern[str]] = re.compile(
    r"inventario-\d{8}-\d{6}\.db$"
)


@pytest.fixture
def logging_aislado() -> Iterator[None]:
    """Restaura los handlers y el nivel del logger raiz tras cada prueba."""
    raiz = logging.getLogger()
    handlers_previos = list(raiz.handlers)
    nivel_previo = raiz.level

    yield

    for handler in list(raiz.handlers):
        if handler not in handlers_previos:
            raiz.removeHandler(handler)
            handler.close()
    raiz.setLevel(nivel_previo)


@pytest.fixture
def carpeta_sin_escritura(tmp_path: Path) -> Iterator[Path]:
    """Carpeta de solo lectura; restaura permisos para que pytest pueda limpiar."""
    carpeta = tmp_path / "solo_lectura"
    carpeta.mkdir()
    carpeta.chmod(0o500)

    yield carpeta

    carpeta.chmod(0o700)


def _crear_db(tmp_path: Path, contenido: bytes = b"SQLite format 3\x00datos") -> Path:
    """Crea un `inventario.db` de prueba y devuelve su ruta."""
    db_path = tmp_path / backup.DB_FILENAME
    db_path.write_bytes(contenido)
    return db_path


def _handlers_de_archivo(destino: Path) -> list[logging.FileHandler]:
    """Devuelve los `FileHandler` del logger raiz que apuntan a `destino`."""
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler)
        and Path(h.baseFilename) == destino.resolve()
    ]


# ----------------------------------------------------------------------
# R6 -- resolucion de rutas bajo el directorio base compartido
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resolver", "nombre"),
    [
        (backup.ruta_db, backup.DB_FILENAME),
        (backup.ruta_log, backup.LOG_FILENAME),
    ],
)
def test_resolvers_apuntan_a_la_base_de_la_app_y_no_al_cwd(
    resolver: Callable[[], Path], nombre: str
) -> None:
    ruta = resolver()

    assert ruta == ruta_base() / nombre
    assert ruta.parent == RAIZ_PROYECTO


# ----------------------------------------------------------------------
# T1 / R5 -- setup_logging
# ----------------------------------------------------------------------


def test_setup_logging_registra_un_solo_handler_cuando_se_llama_dos_veces(
    tmp_path: Path, logging_aislado: None
) -> None:
    log_path = tmp_path / backup.LOG_FILENAME

    backup.setup_logging(log_path)
    backup.setup_logging(log_path)

    assert len(_handlers_de_archivo(log_path)) == 1


def test_setup_logging_escribe_el_error_en_el_archivo_cuando_esta_configurado(
    tmp_path: Path, logging_aislado: None
) -> None:
    log_path = tmp_path / backup.LOG_FILENAME
    backup.setup_logging(log_path)

    logging.getLogger().error("fallo controlado de prueba")

    assert "fallo controlado de prueba" in log_path.read_text(encoding="utf-8")


def test_setup_logging_crea_el_directorio_padre_cuando_no_existe(
    tmp_path: Path, logging_aislado: None
) -> None:
    log_path = tmp_path / "sub" / "nivel" / backup.LOG_FILENAME

    backup.setup_logging(log_path)

    assert log_path.parent.is_dir()


# ----------------------------------------------------------------------
# T2 / R1, R6 -- backup_db, camino feliz
# ----------------------------------------------------------------------


def test_backup_db_crea_copia_identica_cuando_la_base_existe(tmp_path: Path) -> None:
    db_path = _crear_db(tmp_path)

    destino = backup.backup_db(db_path)

    assert destino is not None
    copia = Path(destino)
    assert copia.is_file()
    assert copia.stat().st_size == db_path.stat().st_size
    assert copia.read_bytes() == db_path.read_bytes()


def test_backup_db_nombra_la_copia_con_marca_de_tiempo_cuando_respalda(
    tmp_path: Path,
) -> None:
    db_path = _crear_db(tmp_path)

    destino = backup.backup_db(db_path)

    assert destino is not None
    assert PATRON_RESPALDO.search(Path(destino).name) is not None


def test_backup_db_resuelve_backups_bajo_la_base_cuando_no_se_indica_carpeta(
    tmp_path: Path,
) -> None:
    db_path = _crear_db(tmp_path)

    destino = backup.backup_db(db_path)

    assert destino is not None
    assert Path(destino).parent == tmp_path / backup.BACKUPS_DIRNAME


def test_backup_db_usa_la_carpeta_indicada_cuando_se_pasa_backups_dir(
    tmp_path: Path,
) -> None:
    db_path = _crear_db(tmp_path)
    carpeta = tmp_path / "otra" / "ruta"

    destino = backup.backup_db(db_path, str(carpeta))

    assert destino is not None
    assert Path(destino).parent == carpeta


# ----------------------------------------------------------------------
# T3 / R2 -- backup_db, base ausente
# ----------------------------------------------------------------------


@pytest.mark.parametrize("nombre", ["inventario.db", "no_existe.db"])
def test_backup_db_devuelve_none_cuando_la_base_no_existe(
    tmp_path: Path, nombre: str
) -> None:
    db_path = tmp_path / nombre

    resultado = backup.backup_db(db_path)

    assert resultado is None
    assert not (tmp_path / backup.BACKUPS_DIRNAME).exists()
    assert list(tmp_path.iterdir()) == []


def test_backup_db_devuelve_none_cuando_la_ruta_es_un_directorio(
    tmp_path: Path,
) -> None:
    directorio = tmp_path / "soy_un_directorio.db"
    directorio.mkdir()

    resultado = backup.backup_db(directorio)

    assert resultado is None


# ----------------------------------------------------------------------
# T4 / R3 -- startup
# ----------------------------------------------------------------------


def test_startup_devuelve_la_ruta_cuando_el_respaldo_tiene_exito(
    tmp_path: Path, logging_aislado: None
) -> None:
    db_path = _crear_db(tmp_path)
    log_path = tmp_path / backup.LOG_FILENAME

    destino = backup.startup(db_path, log_path)

    assert destino is not None
    assert Path(destino).is_file()


def test_startup_devuelve_none_y_no_propaga_cuando_el_respaldo_falla(
    tmp_path: Path, logging_aislado: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / backup.LOG_FILENAME

    def _explota(*_args: object, **_kwargs: object) -> str | None:
        raise OSError("disco lleno simulado")

    monkeypatch.setattr(backup, "backup_db", _explota)

    resultado = backup.startup(tmp_path / backup.DB_FILENAME, log_path)

    assert resultado is None


def test_startup_registra_el_traceback_cuando_el_respaldo_falla(
    tmp_path: Path, logging_aislado: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / backup.LOG_FILENAME

    def _explota(*_args: object, **_kwargs: object) -> str | None:
        raise OSError("disco lleno simulado")

    monkeypatch.setattr(backup, "backup_db", _explota)

    backup.startup(tmp_path / backup.DB_FILENAME, log_path)

    contenido = log_path.read_text(encoding="utf-8")
    assert "Fallo el respaldo de arranque" in contenido
    assert "Traceback (most recent call last)" in contenido
    assert "disco lleno simulado" in contenido


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignora los permisos de la carpeta de solo lectura",
)
def test_startup_no_propaga_cuando_no_se_puede_configurar_la_bitacora(
    tmp_path: Path,
    carpeta_sin_escritura: Path,
    logging_aislado: None,
) -> None:
    db_path = _crear_db(tmp_path)
    log_path = carpeta_sin_escritura / "sub" / backup.LOG_FILENAME

    destino = backup.startup(db_path, log_path)

    assert not log_path.exists()
    assert destino is not None
    assert Path(destino).is_file()


def test_startup_registra_el_fallo_de_bitacora_cuando_la_ruta_es_invalida(
    tmp_path: Path,
    logging_aislado: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    obstaculo = tmp_path / "no_soy_una_carpeta"
    obstaculo.write_text("archivo regular", encoding="utf-8")
    db_path = _crear_db(tmp_path)

    with caplog.at_level(logging.ERROR, logger=backup.__name__):
        destino = backup.startup(db_path, obstaculo / backup.LOG_FILENAME)

    assert destino is not None
    registros = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert registros
    assert any("No se pudo configurar la bitacora" in r.getMessage() for r in registros)
    assert all(r.exc_info is not None for r in registros)


def test_startup_no_duplica_handlers_cuando_se_invoca_dos_veces(
    tmp_path: Path, logging_aislado: None
) -> None:
    db_path = _crear_db(tmp_path)
    log_path = tmp_path / backup.LOG_FILENAME

    backup.startup(db_path, log_path)
    backup.startup(db_path, log_path)

    assert len(_handlers_de_archivo(log_path)) == 1


# ----------------------------------------------------------------------
# T5 / T6 -- verificacion estatica del cableado en gui_inventario.py
# ----------------------------------------------------------------------


def _arbol_gui() -> ast.Module:
    """Parsea `gui_inventario.py` sin importarlo (depende de `inventario_core`)."""
    return ast.parse(GUI_PATH.read_text(encoding="utf-8"))


def _asignaciones_modulo(arbol: ast.Module) -> dict[str, ast.expr]:
    """Mapea nombre -> valor de las asignaciones a nivel de modulo."""
    asignaciones: dict[str, ast.expr] = {}
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign):
            for destino in nodo.targets:
                if isinstance(destino, ast.Name):
                    asignaciones[destino.id] = nodo.value
    return asignaciones


def _buscar_funcion(arbol: ast.AST, nombre: str) -> ast.FunctionDef:
    """Devuelve la primera `FunctionDef` llamada `nombre` en el arbol."""
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            return nodo
    msg = f"No se encontro la funcion {nombre}"
    raise AssertionError(msg)


def _buscar_clase(arbol: ast.Module, nombre: str) -> ast.ClassDef:
    """Devuelve la `ClassDef` llamada `nombre`."""
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == nombre:
            return nodo
    msg = f"No se encontro la clase {nombre}"
    raise AssertionError(msg)


def test_gui_inventario_sigue_siendo_python_valido_tras_el_cableado() -> None:
    fuente = GUI_PATH.read_text(encoding="utf-8")

    arbol = ast.parse(fuente)

    assert isinstance(arbol, ast.Module)


@pytest.mark.parametrize(
    ("constante", "resolver"),
    [("DB_PATH", "backup.ruta_db()"), ("LOG_PATH", "backup.ruta_log()")],
)
def test_gui_inventario_define_las_constantes_de_ruta_a_nivel_de_modulo(
    constante: str, resolver: str
) -> None:
    arbol = _arbol_gui()

    asignaciones = _asignaciones_modulo(arbol)

    assert constante in asignaciones
    assert resolver in ast.unparse(asignaciones[constante])


def test_gui_inventario_llama_a_backup_startup_justo_tras_super_init() -> None:
    arbol = _arbol_gui()
    init = _buscar_funcion(_buscar_clase(arbol, "App"), "__init__")

    primeras = [ast.unparse(nodo) for nodo in init.body[:2]]

    assert primeras[0] == "super().__init__()"
    assert primeras[1] == "backup.startup(DB_PATH, LOG_PATH)"


# ----------------------------------------------------------------------
# T6 / R4 -- cada fallo de parseo o guardado queda registrado en la bitacora
#
# La lista de manejadores NO esta escrita a mano: se deriva del arbol
# buscando cada `try` que envuelve una llamada a la capa de dominio
# (`core.*`), es decir cada operacion de parseo o guardado. Un manejador
# nuevo sin `logger.exception` aparece solo y rompe la suite.
# ----------------------------------------------------------------------


def _bloques_try(nodo: ast.AST, prefijo: str = "") -> Iterator[tuple[str, ast.Try]]:
    """Recorre `nodo` y produce `(nombre cualificado, bloque try)`.

    Time: O(n) sobre los nodos del arbol | Space: O(d) por la recursion
    """
    for hijo in ast.iter_child_nodes(nodo):
        if isinstance(hijo, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            yield from _bloques_try(hijo, f"{prefijo}{hijo.name}.")
            continue
        if isinstance(hijo, ast.Try):
            yield (prefijo.rstrip("."), hijo)
        yield from _bloques_try(hijo, prefijo)


#: Modulos que constituyen la capa de dominio desde la GUI. `pdf_extractor` se
#: sumo a `core` cuando la carga de remisiones paso a llamarlo directamente: el
#: parseo de un PDF es una operacion de dominio con las mismas exigencias de R4
#: (bitacora + aviso a la usuaria), y dejarlo fuera del detector habria hecho
#: que esa ruta de fallo saliera del radar sin que nadie la retirara a proposito.
MODULOS_DOMINIO: Final[frozenset[str]] = frozenset({"core", "pdf_extractor"})


def _invoca_al_dominio(bloque: ast.Try) -> bool:
    """Indica si el cuerpo protegido llama a la capa de dominio.

    Time: O(n) sobre los nodos del bloque | Space: O(1)
    """
    return any(
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id in MODULOS_DOMINIO
        for sentencia in bloque.body
        for nodo in ast.walk(sentencia)
    )


def _bloques_parseo_o_guardado() -> list[tuple[str, ast.Try]]:
    """Bloques `try` de `gui_inventario.py` que envuelven una operacion de dominio."""
    return [
        (nombre, bloque)
        for nombre, bloque in _bloques_try(_arbol_gui())
        if _invoca_al_dominio(bloque)
    ]


BLOQUES_R4: Final[list[tuple[str, ast.Try]]] = _bloques_parseo_o_guardado()

FUNCIONES_R4_CONOCIDAS: Final[frozenset[str]] = frozenset(
    {
        "App.abrir_flujo_carga_pdf",
        "App.al_confirmar_carga",
        "VentanaAsociadoForm._guardar",
        # `VentanaDetalleEntrega._guardar` vivio aqui hasta CLI-04. Ese dialogo
        # escribia la entrega en el Excel y salio del arbol al migrar la pestana
        # de Entregas a SQLite; su ruta de fallo la heredan `VentanaPagos._agregar`
        # (el registro del abono) y `TabEntregas._aplicar_status` (el ciclo de
        # estado), que son las dos mitades en que se partio.
        "VentanaPagos._agregar",
        "TabEntregas._aplicar_status",
        "VentanaVenta._registrar",
    }
)


def test_la_deteccion_de_rutas_de_fallo_cubre_las_conocidas_y_no_es_vacia() -> None:
    detectadas = {nombre for nombre, _ in BLOQUES_R4}

    assert detectadas >= FUNCIONES_R4_CONOCIDAS
    assert len(BLOQUES_R4) >= len(FUNCIONES_R4_CONOCIDAS)


@pytest.mark.parametrize(
    ("nombre", "bloque"),
    [
        pytest.param(nombre, bloque, id=f"{nombre}:L{bloque.lineno}")
        for nombre, bloque in BLOQUES_R4
    ],
)
def test_gui_inventario_registra_en_bitacora_cada_fallo_de_parseo_o_guardado(
    nombre: str, bloque: ast.Try
) -> None:
    cuerpos = [
        "\n".join(ast.unparse(s) for s in manejador.body)
        for manejador in bloque.handlers
    ]

    con_bitacora = [cuerpo for cuerpo in cuerpos if "logger.exception(" in cuerpo]

    assert cuerpos, f"{nombre}: el bloque try no tiene manejadores"
    assert con_bitacora, f"{nombre}: ningun manejador llama a logger.exception"


@pytest.mark.parametrize(
    ("nombre", "bloque"),
    [
        pytest.param(nombre, bloque, id=f"{nombre}:L{bloque.lineno}")
        for nombre, bloque in BLOQUES_R4
    ],
)
def test_gui_inventario_conserva_el_aviso_al_usuario_en_cada_ruta_de_fallo(
    nombre: str, bloque: ast.Try
) -> None:
    cuerpos = [
        "\n".join(ast.unparse(s) for s in manejador.body)
        for manejador in bloque.handlers
    ]

    con_aviso = [cuerpo for cuerpo in cuerpos if "messagebox." in cuerpo]

    assert con_aviso, f"{nombre}: se perdio el aviso al usuario"
