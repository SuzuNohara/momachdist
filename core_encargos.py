"""Dominio de encargos: pedidos que el cliente aparta y aun no hay en stock (ENC-02).

Un **encargo** es la promesa de surtir uno o varios articulos a un cliente. Nace
en `Pendiente`, puede recibir anticipos, y mas tarde ENC-03 lo convierte en venta
o el usuario lo cancela. Aqui vive el CRUD: alta, consulta, edicion, cancelacion.

Decisiones que conviene no re-descubrir:

* **Los anticipos no se implementan aqui.** `encargo_pagos` ya esta en la
  whitelist `core_pagos.PAGO_TABLAS` (ADR-6): el abono se registra con
  `core_pagos.agregar_pago(conn, TABLA_PAGOS, encargo_id, forma, monto)` y el
  acumulado sale de `core_pagos.total_pagado`. Duplicar aqui la validacion del
  monto o el INSERT habria creado dos verdades para la misma cifra.
* **`listar_encargos` resuelve los dos agregados en la misma consulta** (dos
  subconsultas correlacionadas), como `core_ventas.obtener_ventas_historial`:
  una lectura por fila seria el N+1 que `.langs/python.md` §4 prohibe.
  `obtener_encargo` trabaja sobre **un** encargo, sin bucle que optimizar, y
  ahi si pide el acumulado a `core_pagos.total_pagado`.
* **El status se relee dentro de la transaccion** antes de editar o cancelar:
  guarda y escritura tienen que ser la misma operacion atomica.
* **La FK rota se traduce en el borde**: `cliente_id` o `codigo_articulo`
  inexistentes llegan como `sqlite3.IntegrityError` y salen como `EncargoError`.

Grafo de imports: `core_comun` (error base) y `core_pagos` (anticipos). No
importa `core` (ciclo) ni `core_ventas` (eso llega con ENC-03).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

import core_pagos
from core_comun import CoreError, _texto

#: Clave de este dominio dentro de `core_pagos.PAGO_TABLAS`. La GUI y ENC-03 la
#: pasan a `core_pagos` para los anticipos; vivir aqui evita teclear el literal.
TABLA_PAGOS: Final[str] = "encargo_pagos"

STATUS_PENDIENTE: Final[str] = "Pendiente"
STATUS_SURTIDO: Final[str] = "Surtido"
STATUS_ENTREGADO: Final[str] = "Entregado"
STATUS_CANCELADO: Final[str] = "Cancelado"

#: Espeja el `CHECK (status IN (...))` del DDL (ADR-5).
STATUS_VALIDOS: Final[frozenset[str]] = frozenset(
    {STATUS_PENDIENTE, STATUS_SURTIDO, STATUS_ENTREGADO, STATUS_CANCELADO}
)

#: Claves de cada fila de `listar_encargos`, en el orden del contrato de la GUI;
#: `obtener_encargo` devuelve estas mismas mas `lineas`.
CAMPOS_ENCARGO: Final[tuple[str, ...]] = (
    "id", "cliente_id", "cliente_nombre", "fecha_encargo", "status",
    "observaciones", "total_estimado", "total_anticipado",
)

#: Claves de cada linea de detalle, tanto de entrada como de salida.
CAMPOS_LINEA: Final[tuple[str, ...]] = (
    "codigo_articulo", "cantidad_solicitada", "precio_estimado",
)

_MSG_SIN_LINEAS: Final[str] = "El encargo no tiene lineas: agrega al menos un articulo."
_MSG_LINEA_INVALIDA: Final[str] = "Cada linea del encargo debe ser un diccionario."
_MSG_CODIGO_VACIO: Final[str] = "Cada linea del encargo necesita un codigo de articulo."
_MSG_CANTIDAD: Final[str] = "La cantidad de '{codigo}' debe ser un entero mayor que cero."
_MSG_PRECIO: Final[str] = "El precio estimado de '{codigo}' no puede ser negativo."
_MSG_CLIENTE_VACIO: Final[str] = "El encargo necesita un cliente del directorio."
_MSG_CLIENTE_INEXISTENTE: Final[str] = "El cliente indicado no existe."
_MSG_ARTICULO_INEXISTENTE: Final[str] = "Algun articulo del encargo no esta en el catalogo."
_MSG_NO_EXISTE: Final[str] = "El encargo {encargo_id} no existe."
_MSG_NO_PENDIENTE: Final[str] = (
    "El encargo {encargo_id} esta en estado '{status}': solo se puede {accion} "
    "mientras siga en 'Pendiente'."
)
_MSG_STATUS_INVALIDO: Final[str] = "Status de encargo no valido: {status!r}."

_SQL_INSERT_ENCARGO: Final[str] = "INSERT INTO encargos (cliente_id, observaciones) VALUES (?, ?)"
_SQL_INSERT_DETALLE: Final[str] = (
    "INSERT INTO encargo_detalle "
    "(encargo_id, codigo_articulo, cantidad_solicitada, precio_estimado) VALUES (?, ?, ?, ?)"
)
_SQL_UPDATE_ENCARGO: Final[str] = (
    "UPDATE encargos SET cliente_id = ?, observaciones = ? WHERE id = ?"
)
_SQL_BORRAR_DETALLE: Final[str] = "DELETE FROM encargo_detalle WHERE encargo_id = ?"
_SQL_UPDATE_STATUS: Final[str] = "UPDATE encargos SET status = ? WHERE id = ?"
_SQL_STATUS: Final[str] = "SELECT status FROM encargos WHERE id = ?"
_SQL_LINEAS: Final[str] = (
    "SELECT codigo_articulo, cantidad_solicitada, precio_estimado "
    "FROM encargo_detalle WHERE encargo_id = ? ORDER BY id ASC"
)

#: Cabecera + los dos agregados en **una sola consulta**, sin lecturas por fila.
#: Los dos filtros viajan como parametros nombrados y se anulan solos cuando
#: valen `NULL`: la misma sentencia sirve al listado, al listado filtrado y a la
#: lectura de un encargo suelto, de modo que las vistas no pueden divergir.
_SQL_LEER: Final[str] = """
SELECT e.id, e.cliente_id, e.fecha_encargo, e.status,
    COALESCE(c.nombre, '') AS cliente_nombre,
    COALESCE(e.observaciones, '') AS observaciones,
    (SELECT COALESCE(SUM(d.cantidad_solicitada * d.precio_estimado), 0)
       FROM encargo_detalle d WHERE d.encargo_id = e.id) AS total_estimado,
    (SELECT COALESCE(SUM(p.monto), 0)
       FROM encargo_pagos p WHERE p.encargo_id = e.id) AS total_anticipado
FROM encargos e
LEFT JOIN clientes c ON c.id = e.cliente_id
WHERE (:status IS NULL OR e.status = :status)
  AND (:encargo_id IS NULL OR e.id = :encargo_id)
ORDER BY e.fecha_encargo DESC, e.id DESC
"""


class EncargoError(CoreError):
    """Error de dominio del CRUD de encargos.

    Hereda de `CoreError` (`core_comun`), la base real de la capa core: la GUI
    captura una sola familia de errores para todos los dominios.
    """


def _numero(valor: Any) -> float | None:
    """Convierte `valor` a flotante, o `None` si no es un numero.

    Acepta entero, flotante y texto numerico (lo que llega de un campo de la
    GUI); el booleano no cuenta. Time: O(n) sobre el texto | Space: O(1)
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        return None
    try:
        return float(valor)
    except ValueError:
        return None


def _entero_positivo(valor: Any, mensaje: str) -> int:
    """Exige un entero > 0; si no lo es, `EncargoError(mensaje)` (R3, R4).

    Sirve a la cantidad solicitada --espejo del `CHECK (cantidad_solicitada > 0)`
    del DDL-- y al `cliente_id`, cuyo hueco previo (`None`, vacio o basura) no
    cubre la FK. Time: O(n) sobre la longitud del texto | Space: O(1)
    """
    numero = _numero(valor)
    if numero is None or numero <= 0 or numero != int(numero):
        raise EncargoError(mensaje)
    return int(numero)


def _precio_valido(valor: Any, codigo: str) -> float:
    """Precio estimado como numero >= 0; si no, `EncargoError` (R3).

    Cero es legitimo: al levantar el encargo puede no saberse aun el precio.
    Time: O(1) | Space: O(1)
    """
    numero = _numero(valor)
    if numero is None or numero < 0:
        raise EncargoError(_MSG_PRECIO.format(codigo=codigo))
    return numero


def _validar_lineas(lineas: Any) -> list[dict[str, Any]]:
    """Valida la peticion completa y normaliza sus lineas (R2, R3).

    No toca la base, asi que el rechazo es atomico por construccion: basta una
    linea mala --lista vacia, elemento que no es dict, codigo en blanco,
    cantidad no entera positiva o precio negativo-- para tumbar la peticion
    entera con `EncargoError`. Time: O(n) sobre las lineas | Space: O(n)
    """
    if not lineas:
        raise EncargoError(_MSG_SIN_LINEAS)
    normalizadas: list[dict[str, Any]] = []
    for linea in lineas:
        if not isinstance(linea, dict):
            raise EncargoError(_MSG_LINEA_INVALIDA)
        codigo = _texto(linea.get("codigo_articulo"))
        if not codigo:
            raise EncargoError(_MSG_CODIGO_VACIO)
        cantidad = _entero_positivo(
            linea.get("cantidad_solicitada"), _MSG_CANTIDAD.format(codigo=codigo)
        )
        normalizadas.append({
            "codigo_articulo": codigo,
            "cantidad_solicitada": cantidad,
            "precio_estimado": _precio_valido(linea.get("precio_estimado"), codigo),
        })
    return normalizadas


def _status_filtro(status: Any) -> str | None:
    """Normaliza el filtro de `listar_encargos`: `None` significa "todos" (R5).

    Un status desconocido se rechaza con `EncargoError` en vez de devolver lista
    vacia, que esconderia una errata tras un resultado plausible.
    Time: O(1) | Space: O(1)
    """
    if status is None:
        return None
    texto = _texto(status)
    if texto not in STATUS_VALIDOS:
        raise EncargoError(_MSG_STATUS_INVALIDO.format(status=status))
    return texto


def _exigir_pendiente(conn: sqlite3.Connection, encargo_id: int, accion: str) -> None:
    """Guarda de R7/R8: el encargo existe y sigue en `Pendiente`.

    Se llama **dentro** de la transaccion que va a escribir, para que leer el
    status y actuar sobre el sean una sola operacion atomica. Raises
    `EncargoError` si no existe o si ya no esta `Pendiente`.
    Time: O(log n) por el indice de clave primaria | Space: O(1)
    """
    fila = conn.execute(_SQL_STATUS, (encargo_id,)).fetchone()
    if fila is None:
        raise EncargoError(_MSG_NO_EXISTE.format(encargo_id=encargo_id))
    status = str(fila["status"])
    if status != STATUS_PENDIENTE:
        raise EncargoError(
            _MSG_NO_PENDIENTE.format(encargo_id=encargo_id, status=status, accion=accion)
        )


def _escribir_detalle(
    conn: sqlite3.Connection, encargo_id: int, lineas: list[dict[str, Any]]
) -> None:
    """Inserta todas las lineas dentro de la transaccion en curso (R1, R4).

    Un `codigo_articulo` ausente de `productos` viola la FK y llega como
    `sqlite3.IntegrityError`: se traduce a `EncargoError` aqui, en el borde.
    Time: O(n log m) | Space: O(n)
    """
    filas = [
        (encargo_id, linea["codigo_articulo"], linea["cantidad_solicitada"],
         linea["precio_estimado"])
        for linea in lineas
    ]
    try:
        conn.executemany(_SQL_INSERT_DETALLE, filas)
    except sqlite3.IntegrityError as exc:
        raise EncargoError(_MSG_ARTICULO_INEXISTENTE) from exc


def crear_encargo(
    conn: sqlite3.Connection, cliente_id: int, lineas: list[dict], observaciones: str = ""
) -> int:
    """Registra un encargo `Pendiente` con todas sus lineas, o ninguna (R1-R4, R10).

    Valida primero la peticion completa sin tocar la base, y despues escribe
    cabecera y detalle en un unico `with conn:`: si falla el insert de cualquier
    linea, el rollback se lleva tambien la cabecera, de modo que nunca queda un
    encargo huerfano sin articulos (R10).

    `lineas` es `[{codigo_articulo, cantidad_solicitada, precio_estimado}, ...]`
    y `conn` la conexion inyectada por el call-site (ADR-2, `foreign_keys ON`).
    Devuelve el `encargos.id` creado. Levanta `EncargoError` con la lista vacia
    o una linea invalida (R2, R3), cliente ausente o inexistente (R4), articulo
    fuera del catalogo, o fallo de escritura.
    Time: O(n log m) sobre las lineas | Space: O(n)
    """
    normalizadas = _validar_lineas(lineas)
    cliente = _entero_positivo(cliente_id, _MSG_CLIENTE_VACIO)
    texto = _texto(observaciones)
    try:
        with conn:
            try:
                cursor = conn.execute(_SQL_INSERT_ENCARGO, (cliente, texto))
            except sqlite3.IntegrityError as exc:
                raise EncargoError(_MSG_CLIENTE_INEXISTENTE) from exc
            encargo_id = int(cursor.lastrowid or 0)
            _escribir_detalle(conn, encargo_id, normalizadas)
    except sqlite3.Error as exc:
        raise EncargoError(f"No se pudo registrar el encargo: {exc}") from exc
    return encargo_id


def _fila_encargo(fila: sqlite3.Row, total_anticipado: float) -> dict[str, Any]:
    """Mapea una fila cruda al contrato de `CAMPOS_ENCARGO`.

    Los dos totales se redondean a dos decimales con la misma semantica que
    `core_pagos.total_pagado`, para que ninguna pantalla muestre otra cifra.
    Time: O(1) | Space: O(1)
    """
    return {
        "id": int(fila["id"]),
        "cliente_id": int(fila["cliente_id"]),
        "cliente_nombre": fila["cliente_nombre"],
        "fecha_encargo": fila["fecha_encargo"],
        "status": fila["status"],
        "observaciones": fila["observaciones"],
        "total_estimado": round(float(fila["total_estimado"]), 2),
        "total_anticipado": round(total_anticipado, 2),
    }


def listar_encargos(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    """Encargos con su total estimado y su total anticipado (R5, R9).

    Todo sale de **una sola consulta**: el nombre del cliente por `LEFT JOIN` y
    los dos agregados por subconsultas correlacionadas. Consultar los pagos
    encargo por encargo seria N+1, prohibido por `.langs/python.md` §4.

    `status` filtra por uno de `STATUS_VALIDOS`; `None` devuelve todos. Salen
    diccionarios con las claves de `CAMPOS_ENCARGO`, ordenados por
    `fecha_encargo` descendente (id descendente para desempatar); sin encargos,
    `[]`. Levanta `EncargoError` con un status desconocido o si la consulta
    falla. Time: O(n log n) por el ORDER BY | Space: O(n)
    """
    filtro = _status_filtro(status)
    try:
        filas = conn.execute(_SQL_LEER, {"status": filtro, "encargo_id": None}).fetchall()
    except sqlite3.Error as exc:
        raise EncargoError(f"No se pudieron leer los encargos: {exc}") from exc
    return [_fila_encargo(fila, float(fila["total_anticipado"])) for fila in filas]


def obtener_encargo(conn: sqlite3.Connection, encargo_id: int) -> dict:
    """Un encargo con su detalle completo y su anticipo acumulado (R6, R9).

    Reusa la misma sentencia que el listado (anulando el filtro de status) para
    que ambas vistas no puedan divergir. Sobre un solo encargo no hay bucle que
    optimizar, asi que el acumulado se pide a `core_pagos.total_pagado`: el
    reuso explicito del componente de CLI-03 vale mas que ahorrar una consulta.

    Devuelve las claves de `CAMPOS_ENCARGO` mas `lineas` --diccionarios con las
    de `CAMPOS_LINEA`, en el orden capturado--, y levanta `EncargoError` si el
    encargo no existe o si la consulta falla.
    Time: O(k log n) sobre las lineas del encargo | Space: O(k)
    """
    try:
        fila = conn.execute(_SQL_LEER, {"status": None, "encargo_id": encargo_id}).fetchone()
        lineas = conn.execute(_SQL_LINEAS, (encargo_id,)).fetchall()
    except sqlite3.Error as exc:
        raise EncargoError(f"No se pudo leer el encargo: {exc}") from exc
    if fila is None:
        raise EncargoError(_MSG_NO_EXISTE.format(encargo_id=encargo_id))
    encargo = _fila_encargo(fila, core_pagos.total_pagado(conn, TABLA_PAGOS, encargo_id))
    encargo["lineas"] = [
        {"codigo_articulo": linea["codigo_articulo"],
         "cantidad_solicitada": int(linea["cantidad_solicitada"]),
         "precio_estimado": float(linea["precio_estimado"])}
        for linea in lineas
    ]
    return encargo


def editar_encargo(
    conn: sqlite3.Connection,
    encargo_id: int,
    cliente_id: int,
    lineas: list[dict],
    observaciones: str = "",
) -> None:
    """Reemplaza cabecera y detalle de un encargo `Pendiente` (R7).

    El detalle se sustituye entero (borrar + reinsertar) en vez de conciliarse
    linea a linea: la GUI manda siempre la canasta completa, y reemplazar es la
    unica forma de que quitar una linea sea representable. Todo ocurre en una
    sola transaccion, y la guarda de status se relee dentro de ella; un encargo
    que ya no esta `Pendiente` se rechaza sin modificar absolutamente nada.

    Levanta `EncargoError` si el encargo no existe o no esta `Pendiente`, si la
    canasta viene vacia o invalida, si el cliente o algun articulo no existen, o
    si falla la escritura. Time: O(n log m) sobre las lineas | Space: O(n)
    """
    normalizadas = _validar_lineas(lineas)
    cliente = _entero_positivo(cliente_id, _MSG_CLIENTE_VACIO)
    texto = _texto(observaciones)
    try:
        with conn:
            _exigir_pendiente(conn, encargo_id, "editar")
            try:
                conn.execute(_SQL_UPDATE_ENCARGO, (cliente, texto, encargo_id))
            except sqlite3.IntegrityError as exc:
                raise EncargoError(_MSG_CLIENTE_INEXISTENTE) from exc
            conn.execute(_SQL_BORRAR_DETALLE, (encargo_id,))
            _escribir_detalle(conn, encargo_id, normalizadas)
    except sqlite3.Error as exc:
        raise EncargoError(f"No se pudo editar el encargo: {exc}") from exc


def cancelar_encargo(conn: sqlite3.Connection, encargo_id: int) -> None:
    """Pasa un encargo `Pendiente` a `Cancelado` (R8).

    Cancelar no mueve dinero: los anticipos ya registrados en `encargo_pagos`
    quedan intactos, porque devolverlos es una decision de negocio que este
    ciclo no modela. El detalle tampoco se borra: es la historia de lo pedido.
    Levanta `EncargoError` si el encargo no existe, si su status ya no es
    `Pendiente` (cancelar dos veces falla) o si el UPDATE falla.
    Time: O(log n) por el indice de clave primaria | Space: O(1)
    """
    try:
        with conn:
            _exigir_pendiente(conn, encargo_id, "cancelar")
            conn.execute(_SQL_UPDATE_STATUS, (STATUS_CANCELADO, encargo_id))
    except sqlite3.Error as exc:
        raise EncargoError(f"No se pudo cancelar el encargo: {exc}") from exc
