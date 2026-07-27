"""Suite del reparto en la vista previa (default al asociado + mover a Casa).

Cobertura por requisito:

* R1 -> `test_aplicar_reparto_default_asociado_*`
* R8 -> `test_default_post_extraccion_*` (sobre la salida real del extractor)
* R2 -> `test_estampar_asociado_id_*` y `test_confirmar_carga_asociado_id_*`
* R7 -> `test_normalizar_reparto_carga_*`
* R5 -> `test_confirmar_carga_rechaza_*` / `test_confirmar_carga_persiste_*`
* R3/R4/R6 -> aserciones estaticas sobre `VentanaPrevisualizacion`

Las pruebas de dominio levantan el esquema real con `db.init_db(":memory:")`,
de modo que el CHECK `asociado + casa + local = surtida` de `pedido_detalle` se
ejercita de verdad: hay un caso que lo hace estallar y otro que lo satisface
con el reparto que produce esta actividad.

Las aserciones sobre `gui_inventario.py` son **estaticas** (`ast.parse`): el
modulo importa `inventario_core`, que aun no existe fuera de `reference/`, y
construir un `tkinter.Tk` no es viable en el runner (mismo criterio que
`tests/test_backup.py`).
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

import core
import db
from pdf_extractor import extraer_productos_de_tabla

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
GUI_PATH: Final[Path] = RAIZ_PROYECTO / "gui_inventario.py"

TIPO_NORMAL: Final[str] = "Normal (con descuento)"
FOLIO_A: Final[str] = "C001264"
FOLIO_B: Final[str] = "C001265"
ASOCIADA_A: Final[str] = "ETNAN GAMALIEL PEREZ"
ASOCIADA_B: Final[str] = "MARIA DEL CARMEN SOLIS"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def fila_pdf(
    *,
    folio: str = FOLIO_A,
    codigo: str = "11111",
    descripcion: str = "Sarten antiadherente 24cm",
    surtida: int = 5,
    asociado: int = 0,
    casa: int = 5,
    local: int = 0,
    nombre_asociado: str = ASOCIADA_A,
) -> dict[str, Any]:
    """Fila con las claves que entrega `pdf_extractor.procesar_pdf`."""
    return {
        "Fecha registro": "2026-07-23 09:00",
        "Semana": "01-2026",
        "Folio de pedido": folio,
        "Codigo nota": "8043",
        "Distribuidora": "C0001 DISTRIBUIDORA CENTRO",
        "Nombre asociado": nombre_asociado,
        "Archivo origen": "C001264_NOTA.pdf (pag. 1)",
        "Codigo articulo": codigo,
        "Descripcion": descripcion,
        "Cantidad solicitada": surtida,
        "Cantidad surtida": surtida,
        "Cantidad Asociado": asociado,
        "Cantidad Casa": casa,
        "Cantidad Local": local,
        "Precio catalogo": 249.0,
        "Precio con IVA": 288.84,
        "Precio que pagas": 199.0,
        "Valor total con IVA": 1444.2,
        "Tipo": TIPO_NORMAL,
        "Ocurrencia": 1,
    }


def tabla_de_8_columnas(surtida: int = 4) -> list[list[str]]:
    """Tabla con la forma de la rama de 8 columnas del extractor."""
    return [
        ["Artículo", "Descripción", "Pag", "Sol", "Sur", "S/IVA", "C/IVA", "Total"],
        ["11111", "Sarten 24cm", "12", "4", str(surtida), "249.00", "288.84", "1155.36"],
    ]


def tabla_de_9_columnas(surtida: int = 3) -> list[list[str]]:
    """Tabla con la forma de la rama de 9 columnas del extractor."""
    return [
        ["Artículo", "Desc", "Sol", "Sur", "Cat", "Pagas", "Ganas", "C/IVA", "Total"],
        ["22222", "Juego de vasos", "3", str(surtida), "199.00", "149.00", "50.00",
         "230.84", "692.52"],
    ]


# ----------------------------------------------------------------------
# R1 -- default de reparto al asociado (T1)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("surtida", [1, 5, 42])
def test_aplicar_reparto_default_asociado_asigna_todo_al_asociado(
    surtida: int,
) -> None:
    """Toda la cantidad surtida arranca con el asociado de la nota (R1)."""
    fila = fila_pdf(surtida=surtida, casa=surtida)

    resultado = core.aplicar_reparto_default_asociado(fila)

    assert resultado["Cantidad Asociado"] == surtida
    assert resultado["Cantidad Casa"] == 0
    assert resultado["Cantidad Local"] == 0


def test_aplicar_reparto_default_asociado_deja_todo_en_cero_si_es_regalo() -> None:
    """Un producto de regalo (`surtida == 0`) queda 0/0/0 y sigue cuadrando."""
    fila = fila_pdf(surtida=0, casa=0)

    resultado = core.aplicar_reparto_default_asociado(fila)

    reparto = (
        resultado["Cantidad Asociado"],
        resultado["Cantidad Casa"],
        resultado["Cantidad Local"],
    )
    assert reparto == (0, 0, 0)
    assert sum(reparto) == resultado["Cantidad surtida"]


def test_aplicar_reparto_default_asociado_muta_la_misma_fila() -> None:
    """Devuelve el mismo dict: la vista previa conserva sus referencias."""
    fila = fila_pdf()

    resultado = core.aplicar_reparto_default_asociado(fila)

    assert resultado is fila


def test_aplicar_reparto_default_asociado_tolera_la_surtida_como_texto() -> None:
    """La celda editada en la GUI llega como texto y se coacciona igual."""
    fila = fila_pdf()
    fila["Cantidad surtida"] = "7"

    resultado = core.aplicar_reparto_default_asociado(fila)

    assert resultado["Cantidad Asociado"] == 7
    assert resultado["Cantidad Casa"] == 0


# ----------------------------------------------------------------------
# R8 -- la inversion es un paso POSTERIOR al extractor verbatim (T2)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tabla", "surtida"),
    [
        (tabla_de_8_columnas(4), 4),
        (tabla_de_9_columnas(3), 3),
    ],
    ids=["rama_8_columnas", "rama_9_columnas"],
)
def test_default_post_extraccion_invierte_el_default_de_casa(
    tabla: list[list[str]], surtida: int
) -> None:
    """El extractor sigue entregando "todo a Casa"; core lo voltea (R8)."""
    filas = extraer_productos_de_tabla(tabla)
    assert filas and filas[0]["Cantidad Casa"] == surtida  # default historico

    resultado = core.aplicar_default_post_extraccion(filas)

    assert resultado[0]["Cantidad Asociado"] == surtida
    assert resultado[0]["Cantidad Casa"] == 0
    assert resultado[0]["Cantidad Local"] == 0


def test_default_post_extraccion_deja_el_regalo_en_cero() -> None:
    """Un renglon surtido en 0 sale 0/0/0 por las dos ramas del extractor."""
    filas = extraer_productos_de_tabla(tabla_de_8_columnas(0))
    filas += extraer_productos_de_tabla(tabla_de_9_columnas(0))

    resultado = core.aplicar_default_post_extraccion(filas)

    assert len(resultado) == 2
    for fila in resultado:
        assert (
            fila["Cantidad Asociado"],
            fila["Cantidad Casa"],
            fila["Cantidad Local"],
        ) == (0, 0, 0)


def test_default_post_extraccion_no_altera_precios_ni_descripcion() -> None:
    """Solo toca el reparto: el resto de la fila extraida queda intacto."""
    filas = extraer_productos_de_tabla(tabla_de_8_columnas(4))
    antes = {k: v for k, v in filas[0].items() if not k.startswith("Cantidad ")}

    core.aplicar_default_post_extraccion(filas)

    despues = {k: v for k, v in filas[0].items() if not k.startswith("Cantidad ")}
    assert despues == antes


# ----------------------------------------------------------------------
# R2 -- el asociado resuelto se sella en cada fila (T3)
# ----------------------------------------------------------------------


def test_estampar_asociado_id_sella_todas_las_filas_de_la_nota() -> None:
    """El id se resuelve una vez por nota y se reparte entre sus lineas."""
    filas = [fila_pdf(codigo="11111"), fila_pdf(codigo="22222")]

    core.estampar_asociado_id(filas, 7)

    assert [f["asociado_id"] for f in filas] == [7, 7]


def test_estampar_asociado_id_sella_none_cuando_la_nota_no_trae_asociado() -> None:
    """Sin asociado resoluble la llave existe, pero vacia (no se inventa)."""
    filas = [fila_pdf(nombre_asociado="")]

    core.estampar_asociado_id(filas, None)

    assert filas[0]["asociado_id"] is None


def test_confirmar_carga_asociado_id_queda_en_cada_fila_de_la_nota(
    conn: sqlite3.Connection,
) -> None:
    """Tras confirmar, cada fila conserva el asociado_id de SU nota (R2)."""
    filas = [
        fila_pdf(folio=FOLIO_A, codigo="11111", nombre_asociado=ASOCIADA_A),
        fila_pdf(folio=FOLIO_A, codigo="22222", nombre_asociado=ASOCIADA_A),
        fila_pdf(folio=FOLIO_B, codigo="33333", nombre_asociado=ASOCIADA_B),
    ]

    core.confirmar_carga(conn, filas)

    ids_nota_a = {filas[0]["asociado_id"], filas[1]["asociado_id"]}
    assert len(ids_nota_a) == 1
    assert filas[2]["asociado_id"] not in ids_nota_a
    esperado = conn.execute(
        "SELECT id FROM asociados WHERE nombre = ?", (ASOCIADA_A,)
    ).fetchone()["id"]
    assert ids_nota_a == {esperado}


def test_confirmar_carga_asociado_id_es_none_si_la_nota_no_trae_nombre(
    conn: sqlite3.Connection,
) -> None:
    """Una nota sin nombre no crea asociado y sella `None` (R4 de MERC-02)."""
    filas = [fila_pdf(nombre_asociado="   ")]

    core.confirmar_carga(conn, filas)

    assert filas[0]["asociado_id"] is None
    assert conn.execute("SELECT COUNT(*) AS n FROM asociados").fetchone()["n"] == 0


# ----------------------------------------------------------------------
# R7 -- normalizacion por fila, con fallback a Casa (T4)
# ----------------------------------------------------------------------


def test_normalizar_reparto_carga_asigna_al_asociado_cuando_hay_id() -> None:
    """Con asociado_id sellado, la fila arranca completa con el asociado."""
    fila = fila_pdf(surtida=6, casa=6)
    fila["asociado_id"] = 3

    (resultado,) = core.normalizar_reparto_carga([fila])

    assert resultado["Cantidad Asociado"] == 6
    assert resultado["Cantidad Casa"] == 0
    assert "_revisar_asociado" not in resultado


def test_normalizar_reparto_carga_marca_la_fila_sin_asociado() -> None:
    """Sin asociado: todo a Casa y marca de revision (R7)."""
    fila = fila_pdf(surtida=6, casa=6, nombre_asociado="")
    fila["asociado_id"] = None

    (resultado,) = core.normalizar_reparto_carga([fila])

    assert resultado["Cantidad Casa"] == 6
    assert resultado["Cantidad Asociado"] == 0
    assert resultado["Cantidad Local"] == 0
    assert resultado["_revisar_asociado"] is True


@pytest.mark.parametrize("vacio", ["", "   ", None])
def test_normalizar_reparto_carga_marca_cuando_el_nombre_viene_en_blanco(
    vacio: str | None,
) -> None:
    """Un nombre en blanco no es asociado resoluble, en ninguna de sus formas."""
    fila = fila_pdf(surtida=2, casa=2, nombre_asociado="")
    fila["Nombre asociado"] = vacio

    (resultado,) = core.normalizar_reparto_carga([fila])

    assert resultado["_revisar_asociado"] is True
    assert resultado["Cantidad Casa"] == 2


def test_normalizar_reparto_carga_usa_el_nombre_cuando_aun_no_hay_id() -> None:
    """En la vista previa todavia no hay id: basta el nombre de la nota."""
    fila = fila_pdf(surtida=4, casa=4, nombre_asociado=ASOCIADA_A)

    (resultado,) = core.normalizar_reparto_carga([fila])

    assert "asociado_id" not in resultado
    assert resultado["Cantidad Asociado"] == 4
    assert "_revisar_asociado" not in resultado


def test_normalizar_reparto_carga_retira_la_marca_al_recuperar_asociado() -> None:
    """Es idempotente: la marca desaparece cuando la fila ya tiene asociado."""
    fila = fila_pdf(surtida=3, casa=3, nombre_asociado="")
    core.normalizar_reparto_carga([fila])
    fila["Nombre asociado"] = ASOCIADA_B

    (resultado,) = core.normalizar_reparto_carga([fila])

    assert "_revisar_asociado" not in resultado
    assert resultado["Cantidad Asociado"] == 3


def test_normalizar_reparto_carga_preserva_las_llaves_ajenas() -> None:
    """Solo reescribe el reparto: precios, folio y asociado_id sobreviven."""
    fila = fila_pdf(surtida=5, casa=5)
    fila["asociado_id"] = 9
    antes = {k: v for k, v in fila.items() if not k.startswith("Cantidad ")}

    (resultado,) = core.normalizar_reparto_carga([fila])

    assert {
        k: v for k, v in resultado.items() if not k.startswith("Cantidad ")
    } == antes


def test_normalizar_reparto_carga_mantiene_la_suma_en_cada_fila() -> None:
    """Invariante `asociado + casa + local == surtida` para notas mezcladas."""
    filas = [
        fila_pdf(surtida=5, casa=5, nombre_asociado=ASOCIADA_A),
        fila_pdf(surtida=0, casa=0, nombre_asociado=ASOCIADA_A),
        fila_pdf(surtida=2, casa=2, nombre_asociado=""),
    ]

    resultado = core.normalizar_reparto_carga(filas)

    for fila in resultado:
        suma = (
            fila["Cantidad Asociado"] + fila["Cantidad Casa"] + fila["Cantidad Local"]
        )
        assert suma == fila["Cantidad surtida"]


# ----------------------------------------------------------------------
# R5 -- el reparto llega a la base y el CHECK real sigue vivo
# ----------------------------------------------------------------------


def test_confirmar_carga_persiste_el_reparto_por_default_al_asociado(
    conn: sqlite3.Connection,
) -> None:
    """El default normalizado satisface el CHECK y queda en `pedido_detalle`."""
    filas = core.normalizar_reparto_carga([fila_pdf(surtida=5, casa=5)])

    core.confirmar_carga(conn, filas)

    fila_db = conn.execute(
        "SELECT cantidad_asociado, cantidad_casa, cantidad_local, asociado_id"
        " FROM pedido_detalle"
    ).fetchone()
    assert fila_db["cantidad_asociado"] == 5
    assert fila_db["cantidad_casa"] == 0
    assert fila_db["cantidad_local"] == 0
    assert fila_db["asociado_id"] is not None


def test_confirmar_carga_persiste_las_piezas_movidas_a_casa(
    conn: sqlite3.Connection,
) -> None:
    """Mover parte de la linea a Casa (lo que hace la usuaria) se persiste."""
    (fila,) = core.normalizar_reparto_carga([fila_pdf(surtida=5, casa=5)])
    fila["Cantidad Asociado"] = 3
    fila["Cantidad Casa"] = 2

    core.confirmar_carga(conn, [fila])

    fila_db = conn.execute(
        "SELECT cantidad_asociado, cantidad_casa FROM pedido_detalle"
    ).fetchone()
    assert (fila_db["cantidad_asociado"], fila_db["cantidad_casa"]) == (3, 2)


def test_confirmar_carga_rechaza_un_reparto_que_no_suma_la_surtida(
    conn: sqlite3.Connection,
) -> None:
    """El CHECK del esquema es alcanzable: un reparto malo aborta el lote."""
    (fila,) = core.normalizar_reparto_carga([fila_pdf(surtida=5, casa=5)])
    fila["Cantidad Casa"] = 2  # 5 + 2 != 5

    with pytest.raises(core.CargaError):
        core.confirmar_carga(conn, [fila])

    assert conn.execute("SELECT COUNT(*) AS n FROM pedido_detalle").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM pedidos").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM asociados").fetchone()["n"] == 0


def test_core_reexporta_la_api_de_reparto() -> None:
    """La fachada `core` expone las funciones nuevas (import graph intacto)."""
    publicas = [
        "aplicar_reparto_default_asociado",
        "aplicar_default_post_extraccion",
        "estampar_asociado_id",
        "normalizar_reparto_carga",
    ]
    for nombre in publicas:
        assert nombre in core.__all__
        assert callable(getattr(core, nombre))


# ----------------------------------------------------------------------
# Aserciones estaticas sobre la vista previa (R3, R4, R6, R7)
# ----------------------------------------------------------------------


def _arbol_gui() -> ast.Module:
    """Arbol sintactico de `gui_inventario.py`."""
    return ast.parse(GUI_PATH.read_text(encoding="utf-8"))


def _buscar_clase(nombre: str) -> ast.ClassDef:
    """Localiza una clase de la GUI por nombre."""
    for nodo in ast.walk(_arbol_gui()):
        if isinstance(nodo, ast.ClassDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"No se encontro la clase {nombre}")


def _metodo(clase: ast.ClassDef, nombre: str) -> ast.FunctionDef:
    """Localiza un metodo dentro de una clase ya resuelta."""
    for nodo in ast.walk(clase):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"No se encontro el metodo {nombre}")


def _atributo_literal(clase: ast.ClassDef, nombre: str) -> Any:
    """Evalua un atributo de clase declarado como literal."""
    for sentencia in clase.body:
        if isinstance(sentencia, ast.Assign):
            for destino in sentencia.targets:
                if isinstance(destino, ast.Name) and destino.id == nombre:
                    return ast.literal_eval(sentencia.value)
    raise AssertionError(f"No se encontro el atributo {nombre}")


PREVIEW: Final[ast.ClassDef] = _buscar_clase("VentanaPrevisualizacion")


def test_previsualizacion_muestra_el_nombre_del_asociado_solo_lectura() -> None:
    """R3: columna "Nombre asociado" presente y NO editable."""
    columnas = _atributo_literal(PREVIEW, "COLUMNAS")

    entradas = [c for c in columnas if c[0] == "Nombre asociado"]
    assert len(entradas) == 1
    col_id, titulo, ancho, editable = entradas[0]
    assert (titulo, ancho, editable) == ("Asociado", 130, False)


def test_previsualizacion_pinta_la_columna_nueva_desde_la_fila() -> None:
    """`_llenar_tabla` recorre COLUMNAS con `fila.get(col_id, "")`."""
    fuente = ast.unparse(_metodo(PREVIEW, "_llenar_tabla"))

    assert "for col_id, *_ in self.COLUMNAS" in fuente
    assert "fila.get(col_id, '')" in fuente


def test_previsualizacion_normaliza_el_reparto_al_abrir() -> None:
    """T6: `__init__` pasa las filas por `core.normalizar_reparto_carga`."""
    init = _metodo(PREVIEW, "__init__")

    llamadas = [
        ast.unparse(nodo)
        for nodo in ast.walk(init)
        if isinstance(nodo, ast.Call) and "normalizar_reparto_carga" in ast.unparse(nodo)
    ]
    assert llamadas, "el preview no normaliza el reparto al abrirse"
    assert any(llamada.startswith("core.normalizar_reparto_carga(") for llamada in llamadas)


def test_previsualizacion_marca_con_tag_las_filas_a_revisar() -> None:
    """R7: la marca de core se traduce en un tag visual del Treeview."""
    init = ast.unparse(_metodo(PREVIEW, "__init__"))
    llenar = ast.unparse(_metodo(PREVIEW, "_llenar_tabla"))

    assert "tag_configure('revisar'" in init
    assert core.MARCA_REVISAR in llenar
    assert "tags=" in llenar


def test_editar_celda_mover_piezas_respeta_el_maximo_de_la_surtida() -> None:
    """R4: Asociado es editable y el guard por celda cubre el trasvase."""
    columnas = _atributo_literal(PREVIEW, "COLUMNAS")
    ubicaciones = _atributo_literal(PREVIEW, "COLUMNAS_UBICACION")
    editables = {c[0] for c in columnas if c[3]}
    fuente = ast.unparse(_metodo(PREVIEW, "_editar_celda"))

    assert {"Cantidad Asociado", "Cantidad Casa", "Cantidad Local"} == set(ubicaciones)
    assert ubicaciones <= editables
    assert "otras_columnas = self.COLUMNAS_UBICACION - {col_nombre}" in fuente
    assert "if suma_otras + nuevo_valor_num > total:" in fuente
    assert "if nuevo_valor_num < 0:" in fuente


def test_confirmar_bloquea_suma_distinta_de_la_cantidad_surtida() -> None:
    """R5: `_confirmar` valida las tres ubicaciones y corta antes de guardar."""
    confirmar = _metodo(PREVIEW, "_confirmar")
    fuente = ast.unparse(confirmar)

    assert "suma = asociado + casa + local" in fuente
    assert "if suma != total:" in fuente

    indices_guarda = [
        i
        for i, sentencia in enumerate(confirmar.body)
        if isinstance(sentencia, ast.If)
        and "errores_reparto" in ast.unparse(sentencia.test)
        and any(isinstance(s, ast.Return) for s in sentencia.body)
    ]
    indices_llamada = [
        i
        for i, sentencia in enumerate(confirmar.body)
        if "al_confirmar_carga" in ast.unparse(sentencia)
    ]
    assert indices_guarda and indices_llamada
    assert max(indices_guarda) < min(indices_llamada)


def test_confirmar_arrastra_asociado_id_hacia_la_capa_core() -> None:
    """R6: las filas viajan completas (sin recorte de llaves) a la app."""
    init = ast.unparse(_metodo(PREVIEW, "__init__"))
    confirmar = ast.unparse(_metodo(PREVIEW, "_confirmar"))

    assert "[dict(f) for f in filas]" in init
    assert "self.app.al_confirmar_carga(self.filas)" in confirmar


def test_confirmar_arrastra_asociado_id_en_los_datos_normalizados() -> None:
    """La copia + normalizacion del preview conserva `asociado_id` intacto."""
    original = fila_pdf(surtida=5, casa=5)
    original["asociado_id"] = 11

    copia = core.normalizar_reparto_carga([dict(original)])

    assert copia[0]["asociado_id"] == 11
    assert copia[0]["Nombre asociado"] == ASOCIADA_A
