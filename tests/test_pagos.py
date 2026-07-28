"""Suite del componente de pagos agnostico de tabla (`core_pagos`, CLI-03).

La fixture levanta el esquema real con `db.init_db(":memory:")`, de modo que los
CHECK de `forma_pago` y `monto`, las FK hacia `ventas`/`encargos` y la columna
`fecha_pago` armonizada por `db._harmonize_venta_pagos` son los de produccion.

Dos cosas se prueban con especial insistencia porque son las que rompen si
alguien "simplifica" el modulo mas tarde:

* **La inyeccion por el argumento `tabla`** (T13): el nombre de la tabla y el de
  la columna FK deben salir siempre de `PAGO_TABLAS`, y un nombre no mapeado
  tiene que rebotar *antes* de tocar la base. Se comprueba pasando una conexion
  centinela que revienta si alguien intenta ejecutar SQL.
* **La equivalencia con el historial de ventas** (contrato de CLI-05): el
  `total_pagado` y el `saldo_pendiente` de este modulo deben coincidir al centavo
  con los que ya calcula `core_ventas.obtener_ventas_historial`, o la misma venta
  se veria con dos cifras distintas segun la pantalla.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterator
from typing import Any, cast

import pytest

import core_pagos
import core_ventas
import db
from core_comun import CoreError

FORMA_A: str = "Efectivo"
FORMA_B: str = "Transferencia"

CODIGO_A: str = "ART-001"
DESCRIPCION_A: str = "Organizador multiusos"

#: Nombres que no estan en `PAGO_TABLAS`: inyeccion clasica, tablas reales del
#: esquema, variaciones de mayusculas/espacios y tipos que ni siquiera son texto.
TABLAS_INVALIDAS: tuple[Any, ...] = (
    "venta_pagos; DROP TABLE ventas",
    "venta_pagos--",
    "ventas",
    "asociados",
    "VENTA_PAGOS",
    "venta_pagos ",
    " venta_pagos",
    "",
    None,
    123,
    ["venta_pagos"],
)

MONTOS_INVALIDOS: tuple[Any, ...] = (
    0,
    0.0,
    -5,
    -0.01,
    "abc",
    "",
    None,
    True,
    float("nan"),
    float("inf"),
)


class ConexionCentinela:
    """Conexion falsa: cualquier intento de usarla es un fallo de la prueba.

    Sirve para demostrar que la validacion de `tabla` ocurre *antes* de tocar la
    base: si el codigo llegara a ejecutar SQL, la prueba muere aqui en vez de
    pasar silenciosamente.
    """

    def execute(self, *args: object, **kwargs: object) -> object:
        """Nadie deberia llegar a ejecutar SQL con una tabla invalida."""
        raise AssertionError("Se ejecuto SQL antes de validar la tabla.")

    def __enter__(self) -> ConexionCentinela:
        """Nadie deberia llegar a abrir la transaccion con una tabla invalida."""
        raise AssertionError("Se abrio la transaccion antes de validar la tabla.")

    def __exit__(self, *args: object) -> bool:
        """Contrapartida de `__enter__`; inalcanzable en una prueba que pasa."""
        return False


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def centinela() -> sqlite3.Connection:
    """Conexion centinela con el tipo que esperan las funciones del modulo."""
    return cast(sqlite3.Connection, ConexionCentinela())


def alta_venta(conexion: sqlite3.Connection) -> int:
    """Crea una venta de mostrador y devuelve su id."""
    cursor = conexion.execute("INSERT INTO ventas (cliente_id) VALUES (NULL)")
    conexion.commit()
    return int(cursor.lastrowid or 0)


def alta_encargo(conexion: sqlite3.Connection) -> int:
    """Crea un cliente y su encargo, y devuelve el id del encargo."""
    cliente = conexion.execute(
        "INSERT INTO clientes (nombre) VALUES (?)", ("Ana Lucia Torres",)
    )
    cursor = conexion.execute(
        "INSERT INTO encargos (cliente_id) VALUES (?)", (int(cliente.lastrowid or 0),)
    )
    conexion.commit()
    return int(cursor.lastrowid or 0)


def alta_linea_venta(conexion: sqlite3.Connection, venta_id: int, total: float) -> None:
    """Anade una linea a la venta para que el historial tenga `total_venta`."""
    conexion.execute(
        "INSERT INTO productos (codigo_articulo, descripcion) VALUES (?, ?)",
        (CODIGO_A, DESCRIPCION_A),
    )
    conexion.execute(
        "INSERT INTO venta_detalle (venta_id, codigo_articulo, cantidad, "
        "precio_costo, precio_publico, total, ganancia) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (venta_id, CODIGO_A, 1, 0.0, total, total, total),
    )
    conexion.commit()


def contar_pagos(conexion: sqlite3.Connection) -> int:
    """Filas actuales en `venta_pagos`."""
    return int(conexion.execute("SELECT COUNT(*) AS n FROM venta_pagos").fetchone()["n"])


# --------------------------------------------------------------------------
# T1 -- whitelist, formas permitidas y jerarquia de errores (R4, R10)
# --------------------------------------------------------------------------


def test_pago_tablas_whitelist_shape() -> None:
    """R4: la whitelist mapea exactamente las tres tablas con su columna FK."""
    # Arrange / Act
    whitelist = core_pagos.PAGO_TABLAS

    # Assert
    assert whitelist == {
        "venta_pagos": "venta_id",
        "entrega_pagos": "entrega_id",
        "encargo_pagos": "encargo_id",
    }


def test_pago_tablas_es_la_unica_fuente_de_nombres() -> None:
    """R10: el SQL preparado cubre la whitelist y solo la whitelist."""
    # Arrange / Act
    preparados = (core_pagos._SQL_INSERT, core_pagos._SQL_LISTAR, core_pagos._SQL_TOTAL)

    # Assert
    for sentencias in preparados:
        assert set(sentencias) == set(core_pagos.PAGO_TABLAS)
        for tabla, sql in sentencias.items():
            assert tabla in sql
            assert core_pagos.PAGO_TABLAS[tabla] in sql


def test_sql_nunca_escribe_el_saldo_del_asociado() -> None:
    """ADR-3 / RT-3: el saldo lo ajusta el trigger, nunca este modulo."""
    # Arrange
    todas = (
        *core_pagos._SQL_INSERT.values(),
        *core_pagos._SQL_LISTAR.values(),
        *core_pagos._SQL_TOTAL.values(),
    )

    # Act / Assert
    for sql in todas:
        assert "saldo_pendiente" not in sql
        assert "asociados" not in sql
        assert "UPDATE" not in sql.upper()


def test_formas_pago_validas_espeja_el_check() -> None:
    """R3: las formas admitidas son las cuatro del CHECK del esquema."""
    # Arrange / Act
    formas = core_pagos.FORMAS_PAGO_VALIDAS

    # Assert
    assert isinstance(formas, frozenset)
    assert formas == {"Efectivo", "Transferencia", "Tarjeta", "Otro"}


@pytest.mark.parametrize(
    "clase",
    [
        core_pagos.TablaPagoInvalidaError,
        core_pagos.FormaPagoInvalidaError,
        core_pagos.MontoInvalidoError,
    ],
)
def test_errores_de_pago_cuelgan_de_coreerror(clase: type[Exception]) -> None:
    """La GUI captura `CoreError`: los tres errores deben caer ahi."""
    # Arrange / Act
    error = clase("fallo")

    # Assert
    assert issubclass(clase, core_pagos.PagoError)
    assert issubclass(clase, CoreError)
    assert isinstance(error, CoreError)
    assert str(error) == "fallo"


def test_pagoerror_hereda_de_coreerror() -> None:
    """D3: la base real de la capa core es `CoreError`, no `DomainError`."""
    # Arrange / Act / Assert
    assert issubclass(core_pagos.PagoError, CoreError)


# --------------------------------------------------------------------------
# T2 -- guarda central de validacion (R2, R3, R4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tabla", TABLAS_INVALIDAS)
def test_validar_pago_rejects_bad_inputs_tabla(tabla: Any) -> None:
    """R4: una tabla fuera de la whitelist no pasa la guarda."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.TablaPagoInvalidaError):
        core_pagos._validar_pago(tabla, FORMA_A, 100.0)


@pytest.mark.parametrize("forma", ["Bitcoin", "", "efectivo", "EFECTIVO", None, 7])
def test_validar_pago_rejects_bad_inputs_forma(forma: Any) -> None:
    """R3: la forma de pago se compara exacta contra el CHECK del esquema."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.FormaPagoInvalidaError):
        core_pagos._validar_pago("venta_pagos", forma, 100.0)


@pytest.mark.parametrize("monto", MONTOS_INVALIDOS)
def test_validar_pago_rejects_bad_inputs_monto(monto: Any) -> None:
    """R2: solo un numero finito estrictamente mayor que cero es un monto."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.MontoInvalidoError):
        core_pagos._validar_pago("venta_pagos", FORMA_A, monto)


@pytest.mark.parametrize("tabla", ["venta_pagos", "entrega_pagos", "encargo_pagos"])
@pytest.mark.parametrize("monto", [0.01, 1, 150.75, "250.5"])
def test_validar_pago_accepts_valid(tabla: str, monto: Any) -> None:
    """La guarda deja pasar cualquier combinacion legitima, sin tocar la base."""
    # Arrange / Act
    resultado = core_pagos._validar_pago(tabla, FORMA_B, monto)

    # Assert
    assert resultado is None


def test_validar_pago_revisa_la_tabla_primero() -> None:
    """La tabla se valida antes que forma y monto: es la guarda de seguridad."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.TablaPagoInvalidaError):
        core_pagos._validar_pago("ventas", "Bitcoin", -5)


# --------------------------------------------------------------------------
# T3 -- alta de pago (R1, R8, R10)
# --------------------------------------------------------------------------


def test_agregar_pago_inserts_and_returns_id(conn: sqlite3.Connection) -> None:
    """R1: el abono persiste con sus cuatro campos y devuelve el id generado."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act
    pago_id = core_pagos.agregar_pago(
        conn, "venta_pagos", venta_id, FORMA_A, 150.5, fecha="2026-07-20"
    )

    # Assert
    assert isinstance(pago_id, int)
    assert pago_id > 0
    fila = conn.execute("SELECT * FROM venta_pagos WHERE id = ?", (pago_id,)).fetchone()
    assert fila is not None
    assert fila["venta_id"] == venta_id
    assert fila["forma_pago"] == FORMA_A
    assert fila["monto"] == pytest.approx(150.5)
    assert fila["fecha_pago"] == "2026-07-20"


def test_agregar_pago_devuelve_ids_distintos(conn: sqlite3.Connection) -> None:
    """R1: cada abono es una fila nueva, aunque repita forma y monto."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act
    primero = core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 50.0)
    segundo = core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 50.0)

    # Assert
    assert primero != segundo
    assert contar_pagos(conn) == 2


def test_agregar_pago_acepta_monto_en_texto(conn: sqlite3.Connection) -> None:
    """R2: un monto tecleado en la GUI llega como texto y se coacciona."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act
    pago_id = core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, "99.9")

    # Assert
    fila = conn.execute("SELECT * FROM venta_pagos WHERE id = ?", (pago_id,)).fetchone()
    assert fila["monto"] == pytest.approx(99.9)


def test_agregar_pago_parent_inexistente_raises_pagoerror(
    conn: sqlite3.Connection,
) -> None:
    """R10: la FK rota se traduce a `PagoError`, no a `IntegrityError` crudo."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.PagoError) as excinfo:
        core_pagos.agregar_pago(conn, "venta_pagos", 9999, FORMA_A, 10.0)
    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert contar_pagos(conn) == 0


def test_agregar_pago_funciona_igual_en_encargo_pagos(
    conn: sqlite3.Connection,
) -> None:
    """ADR-6: el mismo codigo sirve a otra tabla sin tocar una linea (ENC-02)."""
    # Arrange
    encargo_id = alta_encargo(conn)

    # Act
    pago_id = core_pagos.agregar_pago(conn, "encargo_pagos", encargo_id, FORMA_B, 300.0)

    # Assert
    fila = conn.execute(
        "SELECT * FROM encargo_pagos WHERE id = ?", (pago_id,)
    ).fetchone()
    assert fila is not None
    assert fila["encargo_id"] == encargo_id
    assert core_pagos.total_pagado(conn, "encargo_pagos", encargo_id) == 300.0
    assert contar_pagos(conn) == 0


# --------------------------------------------------------------------------
# T4 -- lectura de pagos (R5, R10)
# --------------------------------------------------------------------------


def test_listar_pagos_returns_ordered_dicts(conn: sqlite3.Connection) -> None:
    """R5: los abonos salen del mas antiguo al mas reciente, como dicts."""
    # Arrange
    venta_id = alta_venta(conn)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 100.0, "2026-07-01")
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_B, 50.25, "2026-07-02")

    # Act
    pagos = core_pagos.listar_pagos(conn, "venta_pagos", venta_id)

    # Assert
    assert [pago["id"] for pago in pagos] == sorted(pago["id"] for pago in pagos)
    assert set(pagos[0]) == set(core_pagos.CAMPOS_PAGO)
    assert pagos[0]["forma_pago"] == FORMA_A
    assert pagos[0]["monto"] == pytest.approx(100.0)
    assert pagos[0]["fecha"] == "2026-07-01"
    assert pagos[1]["forma_pago"] == FORMA_B
    assert pagos[1]["fecha"] == "2026-07-02"


def test_listar_pagos_empty(conn: sqlite3.Connection) -> None:
    """R5: una venta sin abonos devuelve lista vacia, no `None`."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act
    pagos = core_pagos.listar_pagos(conn, "venta_pagos", venta_id)

    # Assert
    assert pagos == []


def test_listar_pagos_aisla_por_padre(conn: sqlite3.Connection) -> None:
    """R5: el WHERE parametrizado no mezcla los abonos de dos ventas."""
    # Arrange
    primera = alta_venta(conn)
    segunda = alta_venta(conn)
    core_pagos.agregar_pago(conn, "venta_pagos", primera, FORMA_A, 100.0)
    core_pagos.agregar_pago(conn, "venta_pagos", segunda, FORMA_A, 200.0)

    # Act
    pagos = core_pagos.listar_pagos(conn, "venta_pagos", primera)

    # Assert
    assert len(pagos) == 1
    assert pagos[0]["monto"] == pytest.approx(100.0)


# --------------------------------------------------------------------------
# T5 -- total abonado (R6, R10)
# --------------------------------------------------------------------------


def test_total_pagado_sums_and_zero_default(conn: sqlite3.Connection) -> None:
    """R6: sin abonos el total es `0.0`; con abonos, su suma redondeada."""
    # Arrange
    venta_id = alta_venta(conn)
    vacio = core_pagos.total_pagado(conn, "venta_pagos", venta_id)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 100.1)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_B, 50.2)

    # Act
    total = core_pagos.total_pagado(conn, "venta_pagos", venta_id)

    # Assert
    assert vacio == 0.0
    assert isinstance(vacio, float)
    assert total == 150.3


def test_total_pagado_ignora_otras_ventas(conn: sqlite3.Connection) -> None:
    """R6: el agregado se restringe al padre pedido."""
    # Arrange
    primera = alta_venta(conn)
    segunda = alta_venta(conn)
    core_pagos.agregar_pago(conn, "venta_pagos", primera, FORMA_A, 100.0)
    core_pagos.agregar_pago(conn, "venta_pagos", segunda, FORMA_A, 999.0)

    # Act
    total = core_pagos.total_pagado(conn, "venta_pagos", primera)

    # Assert
    assert total == 100.0


# --------------------------------------------------------------------------
# T6 -- saldo pendiente (R7)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "abonos", "esperado"),
    [
        (100.0, (), 100.0),
        (100.0, (40.0,), 60.0),
        (100.0, (40.0, 60.0), 0.0),
        (100.0, (150.0,), -50.0),
        (0.1, (0.03, 0.04), 0.03),
        (99.99, (33.33, 33.33), 33.33),
    ],
)
def test_saldo_pendiente_rounds(
    conn: sqlite3.Connection,
    total: float,
    abonos: tuple[float, ...],
    esperado: float,
) -> None:
    """R7: el saldo es `round(total - total_pagado, 2)`, sin arrastre binario."""
    # Arrange
    venta_id = alta_venta(conn)
    for abono in abonos:
        core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, abono)

    # Act
    saldo = core_pagos.saldo_pendiente(conn, "venta_pagos", venta_id, total)

    # Assert
    assert saldo == esperado


# --------------------------------------------------------------------------
# T10 -- tres abonos sobre una venta: suma y saldo (R1, R5, R6, R7)
# --------------------------------------------------------------------------


def test_agregar_n_pagos_venta_suma_y_saldo(conn: sqlite3.Connection) -> None:
    """R1/R5/R6/R7: tres abonos parciales se listan, se suman y dejan saldo."""
    # Arrange
    venta_id = alta_venta(conn)
    total_venta = 500.0

    # Act
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 200.0)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_B, 150.5)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, "Tarjeta", 49.5)

    # Assert
    pagos = core_pagos.listar_pagos(conn, "venta_pagos", venta_id)
    assert len(pagos) == 3
    assert [pago["forma_pago"] for pago in pagos] == [FORMA_A, FORMA_B, "Tarjeta"]
    assert core_pagos.total_pagado(conn, "venta_pagos", venta_id) == 400.0
    assert core_pagos.saldo_pendiente(conn, "venta_pagos", venta_id, total_venta) == 100.0


def test_pagos_coinciden_con_el_historial_de_ventas(conn: sqlite3.Connection) -> None:
    """Contrato CLI-05: la misma venta no puede tener dos cifras distintas."""
    # Arrange
    venta_id = alta_venta(conn)
    alta_linea_venta(conn, venta_id, 333.33)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 111.11)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_B, 100.05)

    # Act
    fila = core_ventas.obtener_ventas_historial(conn)[0]

    # Assert
    assert core_pagos.total_pagado(conn, "venta_pagos", venta_id) == fila["total_pagado"]
    assert (
        core_pagos.saldo_pendiente(conn, "venta_pagos", venta_id, fila["total_venta"])
        == fila["saldo_pendiente"]
    )


# --------------------------------------------------------------------------
# T11 -- monto invalido (R2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("monto", [0, -5])
def test_agregar_pago_monto_invalido_no_inserta(
    conn: sqlite3.Connection, monto: float
) -> None:
    """R2: un monto de 0 o negativo se rechaza y no deja fila fantasma."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act / Assert
    with pytest.raises(core_pagos.MontoInvalidoError):
        core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, monto)
    assert contar_pagos(conn) == 0
    assert core_pagos.total_pagado(conn, "venta_pagos", venta_id) == 0.0


@pytest.mark.parametrize("monto", MONTOS_INVALIDOS)
def test_agregar_pago_monto_no_numerico_no_inserta(
    conn: sqlite3.Connection, monto: Any
) -> None:
    """R2: ningun monto no numerico o no finito llega al INSERT."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act / Assert
    with pytest.raises(core_pagos.MontoInvalidoError):
        core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, monto)
    assert contar_pagos(conn) == 0


# --------------------------------------------------------------------------
# T12 -- forma de pago invalida (R3)
# --------------------------------------------------------------------------


def test_agregar_pago_forma_invalida_no_inserta(conn: sqlite3.Connection) -> None:
    """R3: 'Bitcoin' no esta en el CHECK del esquema: se rechaza en Python."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act / Assert
    with pytest.raises(core_pagos.FormaPagoInvalidaError):
        core_pagos.agregar_pago(conn, "venta_pagos", venta_id, "Bitcoin", 100.0)
    assert contar_pagos(conn) == 0


@pytest.mark.parametrize("forma", ["efectivo", "EFECTIVO", "Efectivo!", "", None])
def test_agregar_pago_forma_casi_valida_no_inserta(
    conn: sqlite3.Connection, forma: Any
) -> None:
    """R3: la comparacion contra el CHECK es exacta, no laxa."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act / Assert
    with pytest.raises(core_pagos.FormaPagoInvalidaError):
        core_pagos.agregar_pago(conn, "venta_pagos", venta_id, forma, 100.0)
    assert contar_pagos(conn) == 0


def test_agregar_pago_forma_con_espacios_se_recorta(conn: sqlite3.Connection) -> None:
    """R3: los espacios de un campo de texto no invalidan una forma legitima."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act
    pago_id = core_pagos.agregar_pago(conn, "venta_pagos", venta_id, "  Efectivo ", 10.0)

    # Assert
    fila = conn.execute("SELECT * FROM venta_pagos WHERE id = ?", (pago_id,)).fetchone()
    assert fila["forma_pago"] == "Efectivo"


# --------------------------------------------------------------------------
# T13 -- inyeccion por el argumento `tabla` (R4, R10)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tabla", TABLAS_INVALIDAS)
def test_agregar_pago_tabla_invalida_no_toca_la_base(tabla: Any) -> None:
    """R4: la guarda dispara antes de que exista siquiera una transaccion."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.TablaPagoInvalidaError):
        core_pagos.agregar_pago(centinela(), tabla, 1, FORMA_A, 100.0)


@pytest.mark.parametrize("tabla", TABLAS_INVALIDAS)
def test_listar_pagos_tabla_invalida_no_toca_la_base(tabla: Any) -> None:
    """R4: la misma guarda protege la lectura."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.TablaPagoInvalidaError):
        core_pagos.listar_pagos(centinela(), tabla, 1)


@pytest.mark.parametrize("tabla", TABLAS_INVALIDAS)
def test_total_pagado_tabla_invalida_no_toca_la_base(tabla: Any) -> None:
    """R4: y tambien el agregado."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.TablaPagoInvalidaError):
        core_pagos.total_pagado(centinela(), tabla, 1)


@pytest.mark.parametrize("tabla", TABLAS_INVALIDAS)
def test_saldo_pendiente_tabla_invalida_no_toca_la_base(tabla: Any) -> None:
    """R4: `saldo_pendiente` delega en `total_pagado` y hereda la guarda."""
    # Arrange / Act / Assert
    with pytest.raises(core_pagos.TablaPagoInvalidaError):
        core_pagos.saldo_pendiente(centinela(), tabla, 1, 100.0)


def test_inyeccion_no_borra_la_tabla_ventas(conn: sqlite3.Connection) -> None:
    """R4: el payload clasico rebota y el esquema queda intacto."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act / Assert
    with pytest.raises(core_pagos.TablaPagoInvalidaError):
        core_pagos.agregar_pago(
            conn, "venta_pagos; DROP TABLE ventas", venta_id, FORMA_A, 100.0
        )
    filas = conn.execute("SELECT COUNT(*) AS n FROM ventas").fetchone()
    assert int(filas["n"]) == 1
    assert contar_pagos(conn) == 0


# --------------------------------------------------------------------------
# T14 -- fecha por defecto (R8)
# --------------------------------------------------------------------------


def test_agregar_pago_fecha_none_usa_hoy(conn: sqlite3.Connection) -> None:
    """R8: sin fecha explicita se persiste la fecha local de hoy."""
    # Arrange
    venta_id = alta_venta(conn)
    hoy = datetime.date.today().isoformat()

    # Act
    pago_id = core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 100.0)

    # Assert
    fila = conn.execute("SELECT * FROM venta_pagos WHERE id = ?", (pago_id,)).fetchone()
    assert fila["fecha_pago"] == hoy
    assert core_pagos.listar_pagos(conn, "venta_pagos", venta_id)[0]["fecha"] == hoy


def test_agregar_pago_fecha_en_blanco_usa_hoy(conn: sqlite3.Connection) -> None:
    """R8: un campo de fecha vacio en la GUI equivale a no indicar fecha."""
    # Arrange
    venta_id = alta_venta(conn)
    hoy = datetime.date.today().isoformat()

    # Act
    pago_id = core_pagos.agregar_pago(
        conn, "venta_pagos", venta_id, FORMA_A, 100.0, fecha="   "
    )

    # Assert
    fila = conn.execute("SELECT * FROM venta_pagos WHERE id = ?", (pago_id,)).fetchone()
    assert fila["fecha_pago"] == hoy


def test_agregar_pago_fecha_nunca_queda_nula(conn: sqlite3.Connection) -> None:
    """`venta_pagos.fecha_pago` es nullable: el modulo no debe dejarla en NULL."""
    # Arrange
    venta_id = alta_venta(conn)

    # Act
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_A, 10.0)
    core_pagos.agregar_pago(conn, "venta_pagos", venta_id, FORMA_B, 20.0, "2026-01-05")

    # Assert
    nulas = conn.execute(
        "SELECT COUNT(*) AS n FROM venta_pagos WHERE fecha_pago IS NULL"
    ).fetchone()
    assert int(nulas["n"]) == 0
