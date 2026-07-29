"""Componente de pagos **agnostico de tabla** compartido por los tres dominios.

Venta (CLI-03), entrega a asociado (CLI-04) y anticipo de encargo (ENC-02)
registran abonos con la misma forma: un padre, una forma de pago, un monto y una
fecha. En vez de triplicar el codigo, ADR-6 armonizo las tres tablas
(`venta_pagos`, `entrega_pagos`, `encargo_pagos`) para que una sola
implementacion las sirva a las tres.

Decisiones que conviene no re-descubrir:

* **La superficie de inyeccion es que `tabla` es un argumento del llamador.**
  `PAGO_TABLAS` es la unica fuente de nombres de tabla y de columna FK que entran
  en el SQL; el argumento se usa exclusivamente como *clave de busqueda* en ese
  diccionario. Un nombre no mapeado se rechaza con `TablaPagoInvalidaError`
  **antes de tocar la base**. Las sentencias se arman una sola vez al importar el
  modulo, a partir de la whitelist, de modo que ningun dato de ejecucion puede
  llegar nunca al texto de la consulta. Los valores siempre viajan ligados.
* **`total_pagado` y `saldo_pendiente` replican la semantica que CLI-05 ya fijo**
  en `core_ventas.obtener_ventas_historial`: `COALESCE(SUM(monto), 0)` redondeado
  a dos decimales, y saldo `round(total - total_pagado, 2)`. Si las dos formulas
  divergieran, el historial y el dialogo de pagos mostrarian cifras distintas
  para la misma venta.
* **La fecha se manda siempre desde Python.** `venta_pagos.fecha_pago` es
  nullable (SQLite no admite `ALTER TABLE ... ADD COLUMN NOT NULL` con DEFAULT no
  constante, ver `db._harmonize_venta_pagos`), mientras que las otras dos tablas
  la declaran `NOT NULL DEFAULT (date('now','localtime'))`. Pasar siempre un
  valor explicito hace irrelevante esa diferencia.
* **Nunca se escribe `asociados.saldo_pendiente`** (ADR-3, riesgo RT-3): los
  triggers `trg_pago_insert` / `trg_pago_delete` son la unica fuente que ajusta
  esa columna al insertar o borrar en `entrega_pagos`. Escribirla desde la
  aplicacion seria doble contabilidad.

Grafo de imports: solo `core_comun` y la stdlib, para que la fachada `core`
pueda re-exportar este modulo sin ciclos.
"""

from __future__ import annotations

import datetime
import math
import sqlite3
from typing import Any, Final

from core_comun import CoreError, _texto

#: Unica fuente de verdad de los nombres que entran en el SQL: tabla de pagos ->
#: columna que apunta al padre. Anadir un dominio de pagos es anadir una entrada
#: aqui (mas su DDL); ninguna otra parte del modulo nombra tablas ni columnas.
PAGO_TABLAS: Final[dict[str, str]] = {
    "venta_pagos": "venta_id",
    "entrega_pagos": "entrega_id",
    "encargo_pagos": "encargo_id",
}

#: Formas de pago admitidas. Espeja el `CHECK (forma_pago IN (...))` que las tres
#: tablas declaran en el esquema, para rechazar en Python lo que rechazaria
#: SQLite y dar un mensaje de dominio en vez de un `IntegrityError`.
FORMAS_PAGO_VALIDAS: Final[frozenset[str]] = frozenset(
    {"Efectivo", "Transferencia", "Tarjeta", "Otro"}
)

#: Claves de cada fila devuelta por `listar_pagos`, en el orden del contrato que
#: lee la GUI. `fecha` es el alias de la columna `fecha_pago`.
CAMPOS_PAGO: Final[tuple[str, ...]] = ("id", "forma_pago", "monto", "fecha")

_MSG_TABLA: Final[str] = (
    "Tabla de pagos no permitida: {tabla!r}. Las permitidas son: {permitidas}."
)
_MSG_FORMA: Final[str] = (
    "Forma de pago no valida: {forma!r}. Las permitidas son: {permitidas}."
)
_MSG_MONTO: Final[str] = "El monto del pago debe ser un numero mayor que cero."

# Plantillas del SQL. El unico hueco que se rellena son identificadores tomados
# de `PAGO_TABLAS` (ver `_sql_por_tabla`): los valores viajan como parametros
# ligados `?`, nunca interpolados.
_PLANTILLA_INSERT: Final[str] = (
    "INSERT INTO {tabla} ({columna}, forma_pago, monto, fecha_pago) "
    "VALUES (?, ?, ?, ?)"
)
_PLANTILLA_LISTAR: Final[str] = (
    "SELECT id, forma_pago, monto, fecha_pago AS fecha "
    "FROM {tabla} WHERE {columna} = ? ORDER BY id ASC"
)
_PLANTILLA_TOTAL: Final[str] = (
    "SELECT COALESCE(SUM(monto), 0) AS total FROM {tabla} WHERE {columna} = ?"
)


def _sql_por_tabla(plantilla: str) -> dict[str, str]:
    """Instancia `plantilla` una vez por tabla de la whitelist.

    Se ejecuta al importar el modulo, con los identificadores literales de
    `PAGO_TABLAS`: en ejecucion no se construye SQL, solo se busca la sentencia
    ya armada por su clave. Ningun dato del llamador puede alcanzar el texto de
    la consulta.

    Time: O(t) sobre las tablas de la whitelist | Space: O(t)
    """
    return {
        tabla: plantilla.format(tabla=tabla, columna=columna)
        for tabla, columna in PAGO_TABLAS.items()
    }


_SQL_INSERT: Final[dict[str, str]] = _sql_por_tabla(_PLANTILLA_INSERT)
_SQL_LISTAR: Final[dict[str, str]] = _sql_por_tabla(_PLANTILLA_LISTAR)
_SQL_TOTAL: Final[dict[str, str]] = _sql_por_tabla(_PLANTILLA_TOTAL)


class PagoError(CoreError):
    """Error de dominio del registro y la consulta de pagos.

    Hereda de `CoreError`, la base real de la capa core (`core_comun`), en vez de
    abrir una jerarquia paralela: la GUI captura `CoreError` de forma uniforme.
    """


class TablaPagoInvalidaError(PagoError):
    """La tabla pedida no esta en `PAGO_TABLAS` (R4, R10)."""


class FormaPagoInvalidaError(PagoError):
    """La forma de pago no esta en `FORMAS_PAGO_VALIDAS` (R3)."""


class MontoInvalidoError(PagoError):
    """El monto no es un numero estrictamente mayor que cero (R2)."""


def _columna_padre(tabla: Any) -> str:
    """Resuelve la columna FK de `tabla` **desde la whitelist** (R4, R10).

    Es la unica puerta por la que un nombre de tabla llega al SQL: el argumento
    se usa solo como clave de busqueda, y cualquier cosa que no sea una clave
    exacta se rechaza antes de tocar la base.

    Raises:
        TablaPagoInvalidaError: si `tabla` no esta mapeada en `PAGO_TABLAS`.

    Time: O(1) | Space: O(1)
    """
    columna = PAGO_TABLAS.get(tabla) if isinstance(tabla, str) else None
    if columna is None:
        raise TablaPagoInvalidaError(
            _MSG_TABLA.format(tabla=tabla, permitidas=", ".join(sorted(PAGO_TABLAS)))
        )
    return columna


def _forma_valida(forma_pago: Any) -> str:
    """Normaliza y valida la forma de pago contra el CHECK del esquema (R3).

    Raises:
        FormaPagoInvalidaError: si no es una de `FORMAS_PAGO_VALIDAS`.

    Time: O(n) sobre la longitud del texto | Space: O(n)
    """
    forma = _texto(forma_pago)
    if forma not in FORMAS_PAGO_VALIDAS:
        raise FormaPagoInvalidaError(
            _MSG_FORMA.format(
                forma=forma_pago, permitidas=", ".join(sorted(FORMAS_PAGO_VALIDAS))
            )
        )
    return forma


def _monto_valido(monto: Any) -> float:
    """Coacciona `monto` a flotante y exige que sea mayor que cero (R2).

    Acepta entero, flotante o texto numerico (lo que puede llegar de un campo de
    la GUI); el booleano no cuenta como numero, y `nan`/`inf` se rechazan por no
    ser cantidades de dinero representables.

    Raises:
        MontoInvalidoError: si no es numerico, o no es finito, o es <= 0.

    Time: O(n) sobre la longitud del texto | Space: O(1)
    """
    if isinstance(monto, bool) or not isinstance(monto, (int, float, str)):
        raise MontoInvalidoError(_MSG_MONTO)
    try:
        numero = float(monto)
    except ValueError as exc:
        raise MontoInvalidoError(_MSG_MONTO) from exc
    if not math.isfinite(numero) or numero <= 0:
        raise MontoInvalidoError(_MSG_MONTO)
    return numero


def _validar_pago(tabla: Any, forma_pago: Any, monto: Any) -> None:
    """Guarda central de las tres validaciones de un pago (R2, R3, R4).

    Concentrarlas aqui garantiza que todo punto de entrada valide identico: el
    dia que se anada otro (un `editar_pago`, por ejemplo) hereda las mismas
    reglas sin copiarlas. No toca la base.

    Raises:
        TablaPagoInvalidaError: tabla fuera de la whitelist.
        FormaPagoInvalidaError: forma de pago fuera del CHECK del esquema.
        MontoInvalidoError: monto no numerico o <= 0.

    Time: O(n) sobre la longitud de los textos | Space: O(1)
    """
    _columna_padre(tabla)
    _forma_valida(forma_pago)
    _monto_valido(monto)


def _fecha_de(fecha: Any) -> str:
    """Devuelve la fecha del pago, o la fecha local de hoy si falta (R8).

    El formato `YYYY-MM-DD` es el mismo que produce el DEFAULT
    `date('now','localtime')` del esquema, de modo que las filas escritas por
    este modulo y las escritas por el motor son indistinguibles.

    Time: O(1) | Space: O(1)
    """
    texto = _texto(fecha)
    return texto if texto else datetime.date.today().isoformat()


def agregar_pago(
    conn: sqlite3.Connection,
    tabla: str,
    parent_id: int,
    forma_pago: str,
    monto: float,
    fecha: str | None = None,
) -> int:
    """Registra un abono en la tabla de pagos indicada (R1, R8, R10).

    Valida primero (tabla, forma y monto), resuelve la columna FK **desde la
    whitelist** y ejecuta un INSERT parametrizado dentro de su propia
    transaccion (`with conn:` -> commit al salir, rollback ante error).

    Args:
        conn: conexion inyectada por el call-site (ADR-2), con `foreign_keys ON`.
        tabla: clave de `PAGO_TABLAS`; cualquier otro valor se rechaza.
        parent_id: id de la venta, entrega o encargo que recibe el abono.
        forma_pago: una de `FORMAS_PAGO_VALIDAS`.
        monto: cantidad abonada, estrictamente mayor que cero.
        fecha: `YYYY-MM-DD`; `None` o vacio -> fecha local de hoy.

    Returns:
        El id autogenerado del pago insertado.

    Raises:
        TablaPagoInvalidaError, FormaPagoInvalidaError, MontoInvalidoError: si
            la validacion falla, **antes** de tocar la base.
        PagoError: si SQLite rechaza la insercion (por ejemplo un `parent_id`
            inexistente, que viola la FK).

    Time: O(log n) por el indice | Space: O(1)
    """
    # La validacion va ANTES de abrir la transaccion: R4 exige rechazar una
    # tabla fuera de la whitelist sin tocar la base, y `with conn:` ya es
    # tocarla. El guard de inyeccion lo comprueba con una conexion centinela
    # que lanza en `__enter__`.
    _validar_pago(tabla, forma_pago, monto)
    try:
        with conn:
            return agregar_pago_en_transaccion(
                conn, tabla, parent_id, forma_pago, monto, fecha
            )
    except sqlite3.Error as exc:
        raise PagoError(f"No se pudo registrar el pago: {exc}") from exc


def agregar_pago_en_transaccion(
    conn: sqlite3.Connection,
    tabla: str,
    parent_id: int,
    forma_pago: str,
    monto: float,
    fecha: str | None = None,
) -> int:
    """Registra un pago **sin** abrir transaccion propia (R1, R8, R10).

    Variante componible de `agregar_pago`: el limite transaccional lo gobierna el
    llamador. La usa ENC-03 para traspasar los anticipos de un encargo a la venta
    dentro de la misma transaccion que crea esa venta, de modo que un fallo a
    mitad del traspaso revierta tambien la venta.

    Existe por el hallazgo H1 del spike ENC-01: `agregar_pago` cierra su propia
    transaccion, asi que anidarla dejaba pagos ya committeados si algo fallaba
    despues -- dinero registrado contra una venta que no llego a existir.

    Las tres validaciones (whitelist de tabla, forma y monto) corren igual y
    **antes** de tocar la base: la variante componible no relaja ninguna guarda.

    Args:
        conn: conexion inyectada; el llamador ya abrio la transaccion.
        tabla: tabla de pago, resuelta contra la whitelist `PAGO_TABLAS`.
        parent_id: id de la venta/entrega/encargo dueña del pago.
        forma_pago: una de `FORMAS_PAGO_VALIDAS`.
        monto: importe positivo.
        fecha: fecha del pago; `None` usa la fecha local de hoy.

    Returns:
        El id autogenerado del pago insertado.

    Raises:
        TablaPagoInvalidaError, FormaPagoInvalidaError, MontoInvalidoError: si
            la validacion falla, **antes** de tocar la base.

    Time: O(log n) por el indice | Space: O(1)
    """
    _validar_pago(tabla, forma_pago, monto)
    sql = _SQL_INSERT[tabla]
    valores = (parent_id, _forma_valida(forma_pago), _monto_valido(monto), _fecha_de(fecha))
    cursor = conn.execute(sql, valores)
    return int(cursor.lastrowid or 0)


def listar_pagos(conn: sqlite3.Connection, tabla: str, parent_id: int) -> list[dict]:
    """Abonos de un padre, del mas antiguo al mas reciente (R5, R10).

    Una sola consulta parametrizada; sin pagos devuelve `[]`, nunca `None`.

    Args:
        conn: conexion inyectada por el call-site.
        tabla: clave de `PAGO_TABLAS`.
        parent_id: id de la venta, entrega o encargo.

    Returns:
        Lista de diccionarios con las claves de `CAMPOS_PAGO`, ordenada
        ascendentemente por `id`.

    Raises:
        TablaPagoInvalidaError: tabla fuera de la whitelist.
        PagoError: si la consulta falla.

    Time: O(k log n) sobre los pagos del padre | Space: O(k)
    """
    _columna_padre(tabla)
    try:
        filas = conn.execute(_SQL_LISTAR[tabla], (parent_id,)).fetchall()
    except sqlite3.Error as exc:
        raise PagoError(f"No se pudieron leer los pagos: {exc}") from exc
    return [
        {
            "id": int(fila["id"]),
            "forma_pago": fila["forma_pago"],
            "monto": float(fila["monto"]),
            "fecha": fila["fecha"],
        }
        for fila in filas
    ]


def total_pagado(conn: sqlite3.Connection, tabla: str, parent_id: int) -> float:
    """Suma de los abonos de un padre, `0.0` si no hay ninguno (R6, R10).

    Agrega en SQL con `COALESCE(SUM(monto), 0)` y redondea a dos decimales:
    exactamente la misma semantica que el `total_pagado` del historial de ventas
    (`core_ventas.obtener_ventas_historial`, CLI-05), para que ambas pantallas
    muestren la misma cifra.

    Raises:
        TablaPagoInvalidaError: tabla fuera de la whitelist.
        PagoError: si la consulta falla.

    Time: O(k log n) sobre los pagos del padre | Space: O(1)
    """
    _columna_padre(tabla)
    try:
        fila = conn.execute(_SQL_TOTAL[tabla], (parent_id,)).fetchone()
    except sqlite3.Error as exc:
        raise PagoError(f"No se pudo calcular el total pagado: {exc}") from exc
    return round(float(fila["total"]), 2)


def saldo_pendiente(
    conn: sqlite3.Connection, tabla: str, parent_id: int, total: float
) -> float:
    """Lo que falta por cubrir de `total` tras los abonos registrados (R7).

    `round(total - total_pagado, 2)`, la formula que CLI-05 ya dejo en produccion
    para el historial de ventas. Puede salir negativo si se abono de mas: eso es
    un sobrepago real y se muestra tal cual, no se recorta a cero.

    Raises:
        TablaPagoInvalidaError: tabla fuera de la whitelist.
        PagoError: si la consulta falla.

    Time: O(k log n) sobre los pagos del padre | Space: O(1)
    """
    return round(float(total) - total_pagado(conn, tabla, parent_id), 2)
