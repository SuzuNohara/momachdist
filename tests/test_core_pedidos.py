"""Suite de entrada de mercancia (core.guardar_pedido / _detalle / confirmar_carga).

La fixture levanta el esquema real con `db.init_db(":memory:")`, de modo que el
CHECK de reparto, la tupla UNIQUE del detalle y la FK a `productos` son las de
produccion y se ejercitan de verdad -- no se simulan con dobles.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

import core
import db

TIPO_NORMAL: str = "Normal (con descuento)"
TIPO_SIN_DESCUENTO: str = "Sin descuento"

FOLIO_A: str = "C001264"
FOLIO_B: str = "C001265"


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
    ocurrencia: int = 1,
    solicitada: int = 3,
    surtida: int = 3,
    asociado: int = 0,
    casa: int = 3,
    local: int = 0,
    tipo: str = TIPO_NORMAL,
    nombre_asociado: str = "ETNAN GAMALIEL PEREZ",
    codigo_nota: str = "8043",
    distribuidora: str = "C0001 DISTRIBUIDORA CENTRO",
    archivo: str = "C001264_NOTA.pdf (pag. 1)",
    precio_catalogo: float = 249.0,
    precio_con_iva: float = 288.84,
    precio_pagas: float = 199.0,
    valor_total: float = 866.52,
) -> dict[str, Any]:
    """Fila con exactamente las claves que entrega `pdf_extractor.procesar_pdf`."""
    return {
        "Fecha registro": "2026-07-22 09:00",
        "Semana": "01-2026",
        "Folio de pedido": folio,
        "Codigo nota": codigo_nota,
        "Distribuidora": distribuidora,
        "Nombre asociado": nombre_asociado,
        "Archivo origen": archivo,
        "Codigo articulo": codigo,
        "Descripcion": descripcion,
        "Cantidad solicitada": solicitada,
        "Cantidad surtida": surtida,
        "Cantidad Asociado": asociado,
        "Cantidad Casa": casa,
        "Cantidad Local": local,
        "Precio catalogo": precio_catalogo,
        "Precio con IVA": precio_con_iva,
        "Precio que pagas": precio_pagas,
        "Valor total con IVA": valor_total,
        "Tipo": tipo,
        "Ocurrencia": ocurrencia,
    }


def contar(conn: sqlite3.Connection, tabla: str) -> int:
    """Numero de filas de una de las tablas conocidas de la suite."""
    permitidas = {"productos", "pedidos", "pedido_detalle"}
    assert tabla in permitidas
    sql = {
        "productos": "SELECT COUNT(*) AS n FROM productos",
        "pedidos": "SELECT COUNT(*) AS n FROM pedidos",
        "pedido_detalle": "SELECT COUNT(*) AS n FROM pedido_detalle",
    }[tabla]
    return int(conn.execute(sql).fetchone()["n"])


def sembrar_productos(conn: sqlite3.Connection, filas: list[dict[str, Any]]) -> None:
    """Deja el catalogo listo para poder insertar detalle aislado del orquestador."""
    core.upsert_productos(conn, filas)


# --------------------------------------------------------------------------
# T1 / R1, R5, R8 - cabecera del pedido
# --------------------------------------------------------------------------
def test_guardar_pedido_creates_one_row_per_folio(conn: sqlite3.Connection) -> None:
    """Un folio inedito crea una unica cabecera con todos sus campos mapeados."""
    fila = fila_pdf()

    pedido_id = core.guardar_pedido(conn, fila)

    assert contar(conn, "pedidos") == 1
    fila_db = conn.execute(
        "SELECT * FROM pedidos WHERE id = ?", (pedido_id,)
    ).fetchone()
    assert fila_db["folio_pedido"] == FOLIO_A
    assert fila_db["codigo_nota"] == "8043"
    assert fila_db["distribuidora"] == "C0001 DISTRIBUIDORA CENTRO"
    assert fila_db["nombre_asociado_pdf"] == "ETNAN GAMALIEL PEREZ"
    assert fila_db["archivo_origen"] == "C001264_NOTA.pdf (pag. 1)"


def test_guardar_pedido_deja_semana_id_nulo(conn: sqlite3.Connection) -> None:
    """R8: la vinculacion con la semana es de BW-01, aqui queda NULL sin fallar."""
    pedido_id = core.guardar_pedido(conn, fila_pdf())

    fila_db = conn.execute(
        "SELECT semana_id FROM pedidos WHERE id = ?", (pedido_id,)
    ).fetchone()
    assert fila_db["semana_id"] is None


def test_guardar_pedido_reload_same_folio_returns_same_id(
    conn: sqlite3.Connection,
) -> None:
    """R5: recargar el mismo folio reutiliza el id y no crea una segunda fila."""
    primero = core.guardar_pedido(conn, fila_pdf())

    segundo = core.guardar_pedido(conn, fila_pdf(archivo="reenvio.pdf (pag. 1)"))

    assert primero == segundo
    fila_db = conn.execute(
        "SELECT COUNT(*) AS n FROM pedidos WHERE folio_pedido = ?", (FOLIO_A,)
    ).fetchone()
    assert fila_db["n"] == 1


def test_guardar_pedido_sin_folio_levanta_carga_error(
    conn: sqlite3.Connection,
) -> None:
    """Una cabecera sin folio no puede persistirse: error de dominio, no de SQLite."""
    with pytest.raises(core.CargaError):
        core.guardar_pedido(conn, fila_pdf(folio="  "))


def test_carga_error_hereda_del_error_base_de_core() -> None:
    """La jerarquia de errores no se duplica: `CargaError` cuelga de `CoreError`."""
    assert issubclass(core.CargaError, core.CoreError)


# --------------------------------------------------------------------------
# T2 / R2, R4 - detalle del pedido
# --------------------------------------------------------------------------
def test_detalle_inserts_row_per_product_with_ocurrencia(
    conn: sqlite3.Connection,
) -> None:
    """R2: N filas producen N lineas, cada una con su propia `ocurrencia`."""
    filas = [
        fila_pdf(codigo="11111", ocurrencia=1),
        fila_pdf(codigo="11111", ocurrencia=2),
        fila_pdf(codigo="22222", descripcion="Vaso termico", ocurrencia=1),
    ]
    sembrar_productos(conn, filas)
    pedido_id = core.guardar_pedido(conn, filas[0])

    insertadas = core.guardar_pedido_detalle(conn, pedido_id, filas)

    assert insertadas == 3
    assert contar(conn, "pedido_detalle") == 3
    ocurrencias = [
        fila["ocurrencia"]
        for fila in conn.execute(
            "SELECT ocurrencia FROM pedido_detalle "
            "WHERE codigo_articulo = ? ORDER BY ocurrencia",
            ("11111",),
        ).fetchall()
    ]
    assert ocurrencias == [1, 2]


def test_detalle_reload_same_folio_no_duplicate(conn: sqlite3.Connection) -> None:
    """R4: reprocesar el mismo folio no duplica lineas ni levanta error."""
    filas = [fila_pdf(codigo="11111"), fila_pdf(codigo="22222", descripcion="Vaso")]
    sembrar_productos(conn, filas)
    pedido_id = core.guardar_pedido(conn, filas[0])
    core.guardar_pedido_detalle(conn, pedido_id, filas)

    reinsertadas = core.guardar_pedido_detalle(conn, pedido_id, filas)

    assert reinsertadas == 0
    assert contar(conn, "pedido_detalle") == 2


def test_detalle_distingue_tipo_dentro_de_la_tupla_unica(
    conn: sqlite3.Connection,
) -> None:
    """El mismo codigo con distinto `tipo` es otra linea, no un duplicado."""
    filas = [
        fila_pdf(codigo="11111", tipo=TIPO_NORMAL),
        fila_pdf(codigo="11111", tipo=TIPO_SIN_DESCUENTO),
    ]
    sembrar_productos(conn, filas)
    pedido_id = core.guardar_pedido(conn, filas[0])

    insertadas = core.guardar_pedido_detalle(conn, pedido_id, filas)

    assert insertadas == 2


def test_la_tupla_unica_del_detalle_la_impone_el_esquema(
    conn: sqlite3.Connection,
) -> None:
    """El skip de R4 se apoya en un UNIQUE real: sin `ON CONFLICT` la BD aborta."""
    fila = fila_pdf()
    sembrar_productos(conn, [fila])
    pedido_id = core.guardar_pedido(conn, fila)
    core.guardar_pedido_detalle(conn, pedido_id, [fila])
    sql_crudo = (
        "INSERT INTO pedido_detalle (pedido_id, codigo_articulo, ocurrencia, "
        "cantidad_surtida, cantidad_casa, tipo) VALUES (?, ?, ?, ?, ?, ?)"
    )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(sql_crudo, (pedido_id, "11111", 1, 3, 3, TIPO_NORMAL))


def test_la_fk_a_productos_la_impone_el_esquema(conn: sqlite3.Connection) -> None:
    """La necesidad de R6 es real: un codigo ausente del catalogo aborta el insert."""
    fila = fila_pdf(codigo="99999")
    pedido_id = core.guardar_pedido(conn, fila)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        core.guardar_pedido_detalle(conn, pedido_id, [fila])


# --------------------------------------------------------------------------
# T3 / R3 - CHECK de reparto
# --------------------------------------------------------------------------
def test_detalle_reparto_desbalanceado_aborta_en_la_base(
    conn: sqlite3.Connection,
) -> None:
    """El CHECK del esquema se alcanza de verdad: el skip de R4 no lo silencia."""
    fila = fila_pdf(surtida=3, asociado=1, casa=1, local=0)
    sembrar_productos(conn, [fila])
    pedido_id = core.guardar_pedido(conn, fila)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        core.guardar_pedido_detalle(conn, pedido_id, [fila])


def test_reparto_mismatch_rejected_and_nothing_persisted(
    conn: sqlite3.Connection,
) -> None:
    """R3: el desbalance rechaza la carga completa y no deja ninguna fila."""
    filas = [
        fila_pdf(codigo="11111", surtida=3, asociado=0, casa=3, local=0),
        fila_pdf(codigo="22222", descripcion="Vaso", surtida=4, asociado=1, casa=1),
    ]

    with pytest.raises(core.CargaError):
        core.confirmar_carga(conn, filas)

    assert contar(conn, "pedido_detalle") == 0
    assert contar(conn, "pedidos") == 0
    assert contar(conn, "productos") == 0


@pytest.mark.parametrize(
    ("surtida", "asociado", "casa", "local", "acepta"),
    [
        (3, 0, 3, 0, True),
        (3, 3, 0, 0, True),
        (3, 1, 1, 1, True),
        (0, 0, 0, 0, True),
        (3, 0, 2, 0, False),
        (3, 1, 3, 0, False),
    ],
)
def test_confirmar_carga_acepta_solo_repartos_cuadrados(
    conn: sqlite3.Connection,
    surtida: int,
    asociado: int,
    casa: int,
    local: int,
    acepta: bool,
) -> None:
    """Frontera de R3: la suma del reparto debe igualar exactamente lo surtido."""
    filas = [
        fila_pdf(surtida=surtida, asociado=asociado, casa=casa, local=local)
    ]

    if acepta:
        resumen = core.confirmar_carga(conn, filas)
        assert resumen["detalle"] == 1
    else:
        with pytest.raises(core.CargaError):
            core.confirmar_carga(conn, filas)
        assert contar(conn, "pedido_detalle") == 0


# --------------------------------------------------------------------------
# T4 / R6, R7, R9 - orquestador
# --------------------------------------------------------------------------
def test_confirmar_carga_upserts_products_before_detalle(
    conn: sqlite3.Connection,
) -> None:
    """R6: los productos se dan de alta primero, asi la FK del detalle nunca falla."""
    filas = [
        fila_pdf(codigo="11111", descripcion="Sarten"),
        fila_pdf(codigo="22222", descripcion="Vaso termico"),
    ]

    resumen = core.confirmar_carga(conn, filas)

    assert resumen["detalle"] == 2
    codigos_catalogo = {
        producto["codigo_articulo"] for producto in core.obtener_catalogo(conn)
    }
    assert codigos_catalogo == {"11111", "22222"}
    codigos_detalle = {
        fila["codigo_articulo"]
        for fila in conn.execute(
            "SELECT codigo_articulo FROM pedido_detalle"
        ).fetchall()
    }
    assert codigos_detalle == codigos_catalogo


def test_confirmar_carga_atomic_rollback_on_failure(
    conn: sqlite3.Connection,
) -> None:
    """R7: un fallo en el segundo folio revierte tambien lo escrito por el primero."""
    filas = [
        fila_pdf(folio=FOLIO_A, codigo="11111"),
        fila_pdf(folio=FOLIO_A, codigo="22222", descripcion="Vaso"),
        fila_pdf(folio=FOLIO_B, codigo="33333", descripcion="Olla", surtida=5, casa=2),
    ]

    with pytest.raises(core.CargaError):
        core.confirmar_carga(conn, filas)

    assert contar(conn, "pedidos") == 0
    assert contar(conn, "pedido_detalle") == 0
    assert contar(conn, "productos") == 0


def test_confirmar_carga_no_arrastra_una_carga_previa_ya_confirmada(
    conn: sqlite3.Connection,
) -> None:
    """El rollback alcanza al lote en curso, nunca a lo ya confirmado antes."""
    core.confirmar_carga(conn, [fila_pdf(folio=FOLIO_A, codigo="11111")])

    with pytest.raises(core.CargaError):
        core.confirmar_carga(
            conn, [fila_pdf(folio=FOLIO_B, codigo="22222", surtida=9, casa=1)]
        )

    assert contar(conn, "pedidos") == 1
    assert contar(conn, "pedido_detalle") == 1


def test_confirmar_carga_returns_summary_no_excel(
    conn: sqlite3.Connection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9: solo SQL, resumen con las tres claves y ni un `.xlsx` en disco."""
    monkeypatch.chdir(tmp_path)
    filas = [
        fila_pdf(codigo="11111"),
        fila_pdf(codigo="22222", descripcion="Vaso", ocurrencia=1),
    ]

    resumen = core.confirmar_carga(conn, filas)

    assert set(resumen) == {"pedidos", "detalle", "folios"}
    assert resumen == {"pedidos": 1, "detalle": 2, "folios": [FOLIO_A]}
    assert list(tmp_path.rglob("*.xls*")) == []


def test_core_ya_no_conoce_la_ruta_de_excel() -> None:
    """R9: el guardado por Excel queda sustituido, no conviviendo con el SQL.

    Se inspecciona el AST y no el texto plano para que la prosa de los
    docstrings (que si menciona el Excel que se reemplaza) no de falsos
    positivos: lo que importa es que no quede codigo de hoja de calculo.
    """
    arbol = ast.parse(inspect.getsource(core))
    importados: set[str] = set()
    atributos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            importados.add(nodo.module.split(".")[0])
        elif isinstance(nodo, ast.Attribute):
            atributos.add(nodo.attr)

    assert importados.isdisjoint({"pandas", "openpyxl", "xlsxwriter"})
    assert atributos.isdisjoint({"to_excel", "read_excel", "ExcelWriter"})
    assert not hasattr(core, "actualizar_excel_maestro")
    assert not hasattr(core, "_guardar_excel_completo")


def test_confirmar_carga_es_idempotente_al_reprocesar_el_mismo_pdf(
    conn: sqlite3.Connection,
) -> None:
    """R4 + R5 vistos desde el orquestador: recargar el PDF no duplica nada."""
    filas = [fila_pdf(codigo="11111"), fila_pdf(codigo="22222", descripcion="Vaso")]
    core.confirmar_carga(conn, filas)

    resumen = core.confirmar_carga(conn, filas)

    assert resumen == {"pedidos": 0, "detalle": 0, "folios": [FOLIO_A]}
    assert contar(conn, "pedidos") == 1
    assert contar(conn, "pedido_detalle") == 2


def test_confirmar_carga_con_lote_vacio_no_escribe_nada(
    conn: sqlite3.Connection,
) -> None:
    """Un lote sin filas devuelve el resumen en cero sin tocar la base."""
    resumen = core.confirmar_carga(conn, [])

    assert resumen == {"pedidos": 0, "detalle": 0, "folios": []}
    assert contar(conn, "pedidos") == 0


def test_confirmar_carga_rechaza_fila_sin_folio(conn: sqlite3.Connection) -> None:
    """Sin folio no hay cabecera posible: se corta antes de abrir transaccion."""
    with pytest.raises(core.CargaError):
        core.confirmar_carga(conn, [fila_pdf(folio="")])

    assert contar(conn, "productos") == 0


# --------------------------------------------------------------------------
# T5 / R1, R2 - mapeo completo y lote multi-nota
# --------------------------------------------------------------------------
def test_multinote_batch_one_pedido_per_folio(conn: sqlite3.Connection) -> None:
    """Un PDF con dos notas produce dos cabeceras y su detalle por separado."""
    filas = [
        fila_pdf(folio=FOLIO_A, codigo="11111", nombre_asociado="ETNAN GAMALIEL"),
        fila_pdf(folio=FOLIO_A, codigo="22222", descripcion="Vaso"),
        fila_pdf(
            folio=FOLIO_B,
            codigo="33333",
            descripcion="Olla express",
            nombre_asociado="AURA JANNET",
            archivo="C001264_NOTA.pdf (pag. 2)",
        ),
    ]

    resumen = core.confirmar_carga(conn, filas)

    assert resumen["pedidos"] == 2
    assert resumen["detalle"] == 3
    assert resumen["folios"] == [FOLIO_A, FOLIO_B]
    conteos = {
        fila["folio_pedido"]: fila["n"]
        for fila in conn.execute(
            "SELECT p.folio_pedido AS folio_pedido, COUNT(d.id) AS n "
            "FROM pedidos p JOIN pedido_detalle d ON d.pedido_id = p.id "
            "GROUP BY p.folio_pedido"
        ).fetchall()
    }
    assert conteos == {FOLIO_A: 2, FOLIO_B: 1}


def test_multinote_batch_conserva_la_cabecera_de_cada_nota(
    conn: sqlite3.Connection,
) -> None:
    """Cada folio guarda su propio asociado y archivo de origen, sin mezclarse."""
    filas = [
        fila_pdf(folio=FOLIO_A, nombre_asociado="ETNAN GAMALIEL"),
        fila_pdf(
            folio=FOLIO_B,
            codigo="33333",
            descripcion="Olla",
            nombre_asociado="AURA JANNET",
            archivo="C001264_NOTA.pdf (pag. 2)",
        ),
    ]

    core.confirmar_carga(conn, filas)

    nombres = {
        fila["folio_pedido"]: fila["nombre_asociado_pdf"]
        for fila in conn.execute(
            "SELECT folio_pedido, nombre_asociado_pdf FROM pedidos"
        ).fetchall()
    }
    assert nombres == {FOLIO_A: "ETNAN GAMALIEL", FOLIO_B: "AURA JANNET"}


def test_detalle_mapea_todas_las_columnas_de_la_fila(
    conn: sqlite3.Connection,
) -> None:
    """R2: cada clave del dict del extractor aterriza en su columna 1:1."""
    fila = fila_pdf(
        codigo="44444",
        descripcion="Set de cuchillos",
        ocurrencia=2,
        solicitada=7,
        surtida=5,
        asociado=2,
        casa=1,
        local=2,
        tipo=TIPO_SIN_DESCUENTO,
        precio_catalogo=310.5,
        precio_con_iva=360.18,
        precio_pagas=248.4,
        valor_total=1800.9,
    )

    core.confirmar_carga(conn, [fila])

    linea = conn.execute("SELECT * FROM pedido_detalle").fetchone()
    esperado = {
        "codigo_articulo": "44444",
        "ocurrencia": 2,
        "cantidad_solicitada": 7,
        "cantidad_surtida": 5,
        "cantidad_asociado": 2,
        "cantidad_casa": 1,
        "cantidad_local": 2,
        "precio_catalogo": 310.5,
        "precio_con_iva": 360.18,
        "precio_que_pagas": 248.4,
        "valor_total_con_iva": 1800.9,
        "tipo": TIPO_SIN_DESCUENTO,
    }
    assert {columna: linea[columna] for columna in esperado} == esperado
    # `asociado_id` dejo de ser NULL en MERC-02: ahora apunta al asociado de la
    # nota. Su resolucion se prueba en `tests/test_core_asociados.py`; aqui solo
    # se comprueba que la columna quedo poblada y no rompe el mapeo 1:1.
    asociado = conn.execute(
        "SELECT id, nombre FROM asociados WHERE id = ?", (linea["asociado_id"],)
    ).fetchone()
    assert asociado["nombre"] == "ETNAN GAMALIEL PEREZ"


def test_detalle_tolera_cantidades_editadas_como_texto(
    conn: sqlite3.Connection,
) -> None:
    """La vista previa editable devuelve texto: debe convertirse, no romper."""
    fila = fila_pdf(surtida="4", asociado="1", casa="3", local="0", solicitada="")
    fila["Precio con IVA"] = "$1,288.84"

    resumen = core.confirmar_carga(conn, [fila])

    assert resumen["detalle"] == 1
    linea = conn.execute("SELECT * FROM pedido_detalle").fetchone()
    assert linea["cantidad_surtida"] == 4
    assert linea["cantidad_asociado"] == 1
    assert linea["cantidad_solicitada"] == 0
    assert linea["precio_con_iva"] == pytest.approx(1288.84)


def test_detalle_ocurrencia_ausente_cae_en_uno(conn: sqlite3.Connection) -> None:
    """Sin `Ocurrencia` la linea usa 1, el mismo default que el esquema."""
    fila = fila_pdf()
    del fila["Ocurrencia"]

    core.confirmar_carga(conn, [fila])

    linea = conn.execute("SELECT ocurrencia FROM pedido_detalle").fetchone()
    assert linea["ocurrencia"] == 1
