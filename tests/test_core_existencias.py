"""Suite de existencias y resumen del Dashboard (core_existencias).

La fixture levanta el esquema real con `db.init_db(":memory:")`, de modo que las
vistas `vw_existencias`/`vw_saldo_asociados`, sus `COALESCE`/guardas de division
y los CHECK del detalle se ejercitan de verdad -- no se simulan con dobles. Las
pruebas de la GUI son estaticas (AST) porque `gui_inventario.py` todavia hace
`import inventario_core as core` y no es importable en este punto de la ola.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

import core_existencias
import db

PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
GUI_PATH: pathlib.Path = PROJECT_ROOT / "gui_inventario.py"

TIPO_NORMAL: str = "Normal (con descuento)"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def _seed_producto(conn: sqlite3.Connection, codigo: str, descripcion: str) -> None:
    conn.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        (codigo, descripcion),
    )


def _seed_pedido(conn: sqlite3.Connection, folio: str) -> int:
    cur = conn.execute("INSERT INTO pedidos (folio_pedido) VALUES (?)", (folio,))
    return int(cur.lastrowid)


def _seed_detalle(
    conn: sqlite3.Connection,
    *,
    pedido_id: int,
    codigo: str,
    surtida: int,
    casa: int,
    local: int,
    asociado: int,
    precio_que_pagas: float,
    valor_total: float,
    ocurrencia: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO pedido_detalle (
            pedido_id, codigo_articulo, ocurrencia, cantidad_solicitada,
            cantidad_surtida, cantidad_asociado, cantidad_casa, cantidad_local,
            precio_que_pagas, valor_total_con_iva, tipo
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pedido_id, codigo, ocurrencia, surtida, surtida, asociado, casa,
            local, precio_que_pagas, valor_total, TIPO_NORMAL,
        ),
    )
    return int(cur.lastrowid)


def _seed_venta(conn: sqlite3.Connection, *, codigo: str, cantidad: int,
                precio_costo: float, precio_publico: float) -> None:
    cur = conn.execute("INSERT INTO ventas DEFAULT VALUES")
    venta_id = int(cur.lastrowid)
    total = cantidad * precio_publico
    ganancia = total - cantidad * precio_costo
    conn.execute(
        """
        INSERT INTO venta_detalle (
            venta_id, codigo_articulo, cantidad, precio_costo,
            precio_publico, total, ganancia
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (venta_id, codigo, cantidad, precio_costo, precio_publico, total, ganancia),
    )


def _seed_entrega(conn: sqlite3.Connection, *, detalle_id: int, asociado_id: int,
                  cantidad: int, monto: float, status: str, pagado: float = 0.0) -> None:
    cur = conn.execute(
        """
        INSERT INTO entregas_asociado (
            pedido_detalle_id, asociado_id, cantidad_entregada, monto_que_debe, status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (detalle_id, asociado_id, cantidad, monto, status),
    )
    entrega_id = int(cur.lastrowid)
    if pagado > 0:
        conn.execute(
            "INSERT INTO entrega_pagos (entrega_id, forma_pago, monto) VALUES (?, ?, ?)",
            (entrega_id, "Efectivo", pagado),
        )


# ---------------------------------------------------------------------------
# R8 -- T1
# ---------------------------------------------------------------------------


def test_stock_bajo_umbral_es_3() -> None:
    # Arrange / Act / Assert
    assert core_existencias.STOCK_BAJO_UMBRAL == 3


# ---------------------------------------------------------------------------
# R1, R3 -- T2
# ---------------------------------------------------------------------------


def test_obtener_existencias_mapea_claves_y_valores(conn: sqlite3.Connection) -> None:
    # Arrange: 10 piezas recibidas (casa) a costo 200 total, 3 vendidas
    _seed_producto(conn, "11111", "Sarten 24cm")
    pedido_id = _seed_pedido(conn, "C001264")
    _seed_detalle(
        conn, pedido_id=pedido_id, codigo="11111", surtida=10, casa=10,
        local=0, asociado=0, precio_que_pagas=2000.0, valor_total=3000.0,
    )
    _seed_venta(conn, codigo="11111", cantidad=3, precio_costo=200.0, precio_publico=350.0)

    # Act
    filas = core_existencias.obtener_existencias(conn)

    # Assert
    assert len(filas) == 1
    fila = filas[0]
    assert set(fila.keys()) == {
        "Codigo articulo", "Descripcion", "Piezas recibidas", "Piezas vendidas",
        "Piezas disponibles", "Precio unitario costo", "Total pagado real",
        "Valor catalogo total",
    }
    assert fila["Codigo articulo"] == "11111"
    assert fila["Descripcion"] == "Sarten 24cm"
    assert fila["Piezas recibidas"] == 10
    assert fila["Piezas vendidas"] == 3
    assert fila["Piezas disponibles"] == 7
    assert fila["Precio unitario costo"] == pytest.approx(200.0)  # 2000 / 10
    assert fila["Total pagado real"] == pytest.approx(2000.0)
    assert fila["Valor catalogo total"] == pytest.approx(3000.0)


def test_obtener_existencias_ordena_por_codigo(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_producto(conn, "22222", "B")
    _seed_producto(conn, "11111", "A")
    pedido_id = _seed_pedido(conn, "C001")
    _seed_detalle(conn, pedido_id=pedido_id, codigo="22222", surtida=1, casa=1,
                  local=0, asociado=0, precio_que_pagas=10.0, valor_total=15.0)
    _seed_detalle(conn, pedido_id=pedido_id, codigo="11111", surtida=1, casa=1,
                  local=0, asociado=0, precio_que_pagas=10.0, valor_total=15.0)

    # Act
    codigos = [f["Codigo articulo"] for f in core_existencias.obtener_existencias(conn)]

    # Assert
    assert codigos == ["11111", "22222"]


# ---------------------------------------------------------------------------
# R3 -- costo 0 sin recibidas
# ---------------------------------------------------------------------------


def test_obtener_existencias_costo_cero_cuando_todo_al_asociado(conn: sqlite3.Connection) -> None:
    # Arrange: todo surtido al asociado -> recibidas (casa+local) = 0
    _seed_producto(conn, "33333", "Regalo")
    pedido_id = _seed_pedido(conn, "C002")
    _seed_detalle(conn, pedido_id=pedido_id, codigo="33333", surtida=5, casa=0,
                  local=0, asociado=5, precio_que_pagas=500.0, valor_total=750.0)

    # Act
    fila = core_existencias.obtener_existencias(conn)[0]

    # Assert: sin division por cero, costo unitario 0
    assert fila["Piezas recibidas"] == 0
    assert fila["Piezas disponibles"] == 0
    assert fila["Precio unitario costo"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# R2 -- T3
# ---------------------------------------------------------------------------


def test_obtener_existencias_bd_vacia_retorna_lista_vacia(conn: sqlite3.Connection) -> None:
    # Arrange: conn recien inicializada, sin pedido_detalle
    # Act
    resultado = core_existencias.obtener_existencias(conn)

    # Assert
    assert resultado == []


# ---------------------------------------------------------------------------
# R4, R5 -- T4
# ---------------------------------------------------------------------------


def test_obtener_resumen_dashboard_totales_correctos(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_producto(conn, "11111", "Sarten")
    conn.execute("INSERT INTO asociados (id, nombre) VALUES (1, 'Ana')")
    pedido_id = _seed_pedido(conn, "C001264")
    detalle_id = _seed_detalle(
        conn, pedido_id=pedido_id, codigo="11111", surtida=10, casa=6,
        local=0, asociado=4, precio_que_pagas=1000.0, valor_total=1500.0,
    )
    # recibidas = casa 6 ; costo total proporcional = 1000 * 6/10 = 600 ; costo unit = 100
    _seed_venta(conn, codigo="11111", cantidad=2, precio_costo=100.0, precio_publico=180.0)
    # disponibles = 6 - 2 = 4 ; valor inventario = 4 * 100 = 400
    _seed_entrega(conn, detalle_id=detalle_id, asociado_id=1, cantidad=4,
                  monto=800.0, status="Pendiente de recoger", pagado=300.0)

    # Act
    resumen = core_existencias.obtener_resumen_dashboard(conn)

    # Assert: 10 claves presentes
    assert set(resumen.keys()) == {
        "productos_distintos", "piezas_disponibles", "valor_inventario_costo",
        "productos_bajo_stock", "num_ventas", "total_vendido", "ganancia_total",
        "num_pedidos_distintos", "entregas_pendientes_cobro", "monto_pendiente_asociados",
    }
    assert resumen["productos_distintos"] == 1
    assert resumen["piezas_disponibles"] == 4
    assert resumen["valor_inventario_costo"] == pytest.approx(400.0)
    assert resumen["num_ventas"] == 1
    assert resumen["total_vendido"] == pytest.approx(360.0)   # 2 * 180
    assert resumen["ganancia_total"] == pytest.approx(160.0)  # 360 - 2*100
    assert resumen["num_pedidos_distintos"] == 1
    assert resumen["entregas_pendientes_cobro"] == 1          # status != 'Pagado'
    assert resumen["monto_pendiente_asociados"] == pytest.approx(500.0)  # 800 - 300


def test_dashboard_entregas_pagadas_no_cuentan_como_pendientes(conn: sqlite3.Connection) -> None:
    # Arrange
    _seed_producto(conn, "11111", "Sarten")
    conn.execute("INSERT INTO asociados (id, nombre) VALUES (1, 'Ana')")
    pedido_id = _seed_pedido(conn, "C001")
    detalle_id = _seed_detalle(conn, pedido_id=pedido_id, codigo="11111", surtida=4,
                               casa=0, local=0, asociado=4, precio_que_pagas=400.0,
                               valor_total=600.0)
    _seed_entrega(conn, detalle_id=detalle_id, asociado_id=1, cantidad=4,
                  monto=600.0, status="Pagado", pagado=600.0)

    # Act
    resumen = core_existencias.obtener_resumen_dashboard(conn)

    # Assert: totalmente pagada -> 0 pendientes de cobro y saldo 0
    assert resumen["entregas_pendientes_cobro"] == 0
    assert resumen["monto_pendiente_asociados"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# R6 -- T5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "disponibles, esperado_en_lista",
    [(2, True), (3, True), (4, False)],
)
def test_dashboard_marca_bajo_stock_en_umbral(
    conn: sqlite3.Connection, disponibles: int, esperado_en_lista: bool
) -> None:
    # Arrange: recibidas = disponibles (sin ventas)
    _seed_producto(conn, "44444", "Producto borde")
    pedido_id = _seed_pedido(conn, "C009")
    _seed_detalle(conn, pedido_id=pedido_id, codigo="44444", surtida=disponibles,
                  casa=disponibles, local=0, asociado=0, precio_que_pagas=10.0,
                  valor_total=15.0)

    # Act
    bajo = core_existencias.obtener_resumen_dashboard(conn)["productos_bajo_stock"]

    # Assert
    codigos = [p["Codigo articulo"] for p in bajo]
    assert ("44444" in codigos) is esperado_en_lista
    if esperado_en_lista:
        item = next(p for p in bajo if p["Codigo articulo"] == "44444")
        assert set(item.keys()) == {"Codigo articulo", "Descripcion", "Piezas disponibles"}
        assert item["Piezas disponibles"] == disponibles


# ---------------------------------------------------------------------------
# R7 -- T6
# ---------------------------------------------------------------------------


def test_obtener_resumen_dashboard_bd_vacia_todo_en_cero(conn: sqlite3.Connection) -> None:
    # Arrange: conn vacia
    # Act
    resumen = core_existencias.obtener_resumen_dashboard(conn)

    # Assert
    assert resumen["productos_distintos"] == 0
    assert resumen["piezas_disponibles"] == 0
    assert resumen["valor_inventario_costo"] == 0
    assert resumen["num_ventas"] == 0
    assert resumen["total_vendido"] == 0
    assert resumen["ganancia_total"] == 0
    assert resumen["num_pedidos_distintos"] == 0
    assert resumen["entregas_pendientes_cobro"] == 0
    assert resumen["monto_pendiente_asociados"] == 0
    assert resumen["productos_bajo_stock"] == []


# ---------------------------------------------------------------------------
# Utilidades AST para verificar la GUI estaticamente
# ---------------------------------------------------------------------------


def _gui_tree() -> ast.Module:
    return ast.parse(GUI_PATH.read_text(encoding="utf-8"))


def _find_class(tree: ast.Module, nombre: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == nombre:
            return node
    raise AssertionError(f"clase {nombre} no encontrada en gui_inventario.py")


def _find_method(clase: ast.ClassDef, nombre: str) -> ast.FunctionDef:
    for node in clase.body:
        if isinstance(node, ast.FunctionDef) and node.name == nombre:
            return node
    raise AssertionError(f"metodo {nombre} no encontrado en {clase.name}")


def _llamadas_core(nodo: ast.AST) -> list[str]:
    """Nombres de atributo de toda llamada `core.<algo>(...)` dentro de `nodo`."""
    nombres: list[str] = []
    for sub in ast.walk(nodo):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            valor = sub.func.value
            if isinstance(valor, ast.Name) and valor.id == "core":
                nombres.append(sub.func.attr)
    return nombres


# ---------------------------------------------------------------------------
# R9 -- T7 (verificacion estatica)
# ---------------------------------------------------------------------------


def test_tab_inventario_refrescar_usa_obtener_existencias() -> None:
    # Arrange
    tree = _gui_tree()
    metodo = _find_method(_find_class(tree, "TabInventario"), "refrescar")

    # Act
    llamadas = _llamadas_core(metodo)

    # Assert: la fuente es obtener_existencias, ya no el catalogo Excel
    assert "obtener_existencias" in llamadas
    assert "obtener_catalogo" not in llamadas


def test_tab_inventario_resaltado_usa_umbral_del_core() -> None:
    # Arrange
    tree = _gui_tree()
    metodo = _find_method(_find_class(tree, "TabInventario"), "_aplicar_filtro")
    fuente = ast.get_source_segment(GUI_PATH.read_text(encoding="utf-8"), metodo) or ""

    # Assert: el tag de bajo stock sigue leyendo el umbral unico del core
    assert "bajo_stock" in fuente
    assert "core.STOCK_BAJO_UMBRAL" in fuente


# ---------------------------------------------------------------------------
# R10 -- T8 (verificacion estatica)
# ---------------------------------------------------------------------------


def test_tab_dashboard_refrescar_usa_resumen() -> None:
    # Arrange
    tree = _gui_tree()
    metodo = _find_method(_find_class(tree, "TabDashboard"), "refrescar")

    # Act
    llamadas = _llamadas_core(metodo)
    fuente = ast.get_source_segment(GUI_PATH.read_text(encoding="utf-8"), metodo) or ""

    # Assert
    assert "obtener_resumen_dashboard" in llamadas
    assert "self.app.conn" in fuente
    assert "productos_bajo_stock" in fuente


# ---------------------------------------------------------------------------
# R3, R4 -- T9 (retiro de funciones Excel del camino diario)
# ---------------------------------------------------------------------------


def test_no_referencias_a_funciones_excel_existencias() -> None:
    # Arrange
    fuente = GUI_PATH.read_text(encoding="utf-8")

    # Assert: ninguna referencia a las funciones Excel retiradas
    assert "obtener_catalogo" not in fuente
    assert "construir_existencias" not in fuente
