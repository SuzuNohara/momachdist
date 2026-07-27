"""Directorio de asociados: resolucion por nombre y mantenimiento manual.

Cada nota del PDF trae el nombre del asociado como texto libre, sin codigo ni
identificador. Este modulo lo convierte en una fila estable de `asociados` para
que el detalle del pedido pueda referenciarla por FK, y ademas expone el CRUD
que la pestana de asociados de la GUI necesita para mantener el directorio a
mano (MERC-07).

Vive al mismo nivel que `core_productos` en el grafo de imports: solo depende de
`core_comun`, nunca de `core_pedidos` ni de la fachada `core`, de modo que las
dependencias siguen apuntando hacia abajo y no hay ciclos.

* `AsociadoError`             -- error de dominio del directorio.
* `_normalizar_nombre`        -- recorte + colapso de espacios internos.
* `obtener_o_crear_asociado`  -- match sin distinguir mayusculas, o alta.
* `listar_asociados`          -- directorio completo con su saldo pendiente.
* `crear_asociado`            -- alta manual desde la GUI.
* `editar_asociado`           -- actualizacion parcial campo a campo.
* `eliminar_asociado`         -- baja protegida por las FKs del esquema.

`asociados.saldo_pendiente` lo mantienen los triggers `trg_entrega_insert`,
`trg_pago_insert` y `trg_pago_delete` (ADR-3): ninguna funcion de aqui la
escribe, solo la lee.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Final

from core_comun import CoreError, _texto

#: Clave con la que el extractor de PDF entrega el nombre del asociado.
CLAVE_NOMBRE_ASOCIADO: Final[str] = "Nombre asociado"

#: Cualquier racha de espacio en blanco (incluidos saltos de linea y tabuladores)
#: colapsa a un unico espacio simple.
_ESPACIOS: Final[re.Pattern[str]] = re.compile(r"\s+")

#: `COLLATE NOCASE` hace el match insensible a mayusculas. Como el nombre se
#: guarda ya normalizado, la misma comparacion cubre tambien las diferencias de
#: espaciado sin necesitar una columna derivada ni un indice funcional.
SELECT_ASOCIADO_ID_SQL: Final[str] = (
    "SELECT id FROM asociados WHERE nombre = ? COLLATE NOCASE"
)

INSERT_ASOCIADO_SQL: Final[str] = "INSERT INTO asociados (nombre) VALUES (?)"

#: El listado alimenta la tabla de la GUI: solo las columnas que se pintan.
#: `saldo_pendiente` se lee de la propia fila (ADR-3), no se recalcula con
#: `vw_saldo_asociados`, que queda como fuente de reconciliacion/auditoria.
LISTAR_ASOCIADOS_SQL: Final[str] = (
    "SELECT id, nombre, telefono, notas, status, saldo_pendiente "
    "FROM asociados ORDER BY nombre"
)

CREAR_ASOCIADO_SQL: Final[str] = (
    "INSERT INTO asociados (nombre, telefono, notas, status) VALUES (?, ?, ?, ?)"
)

DELETE_ASOCIADO_SQL: Final[str] = "DELETE FROM asociados WHERE id = ?"

#: Fragmentos de `SET` precompilados. El `UPDATE` parcial se arma concatenando
#: solo estas constantes -- ningun dato de la GUI entra jamas en el texto del
#: SQL, los valores viajan siempre como parametros (`.langs/python.md` 5).
_SET_POR_CAMPO: Final[dict[str, str]] = {
    "nombre": "nombre = ?",
    "telefono": "telefono = ?",
    "notas": "notas = ?",
    "status": "status = ?",
}

_UPDATE_PREFIJO: Final[str] = "UPDATE asociados SET "
_UPDATE_SUFIJO: Final[str] = " WHERE id = ?"

STATUS_ACTIVO: Final[str] = "Activo"

_MSG_NOMBRE_VACIO: Final[str] = "El nombre del asociado no puede estar vacio."
_MSG_TIENE_ENTREGAS: Final[str] = (
    "No se puede eliminar: el asociado tiene entregas ligadas."
)


class AsociadoError(CoreError):
    """Error de dominio del directorio de asociados (MERC-07).

    Traduce al lenguaje de la GUI tanto las validaciones de negocio (nombre en
    blanco, id inexistente) como los rechazos crudos de SQLite (CHECK de
    `status`, FKs que protegen el borrado), de modo que la capa de presentacion
    nunca ve un `sqlite3.Error`.
    """


def _no_existe(asociado_id: int) -> AsociadoError:
    """Construye el error de id inexistente (R5, R7).

    Time: O(1) | Space: O(1)
    """
    return AsociadoError(f"No existe el asociado con id {asociado_id}.")


def _status_invalido(status: str) -> AsociadoError:
    """Construye el error del CHECK de `status` (R4).

    Time: O(1) | Space: O(1)
    """
    return AsociadoError(
        f"Status invalido: {status!r}. Debe ser 'Activo' o 'Inactivo'."
    )


def _normalizar_nombre(nombre: str | None) -> str:
    """Normaliza el nombre del asociado tal y como se persiste (R3).

    Recorta los extremos y colapsa cualquier racha de espacio interno, pero
    conserva la capitalizacion original del PDF: quien compara es SQLite con
    `COLLATE NOCASE`, asi que no hace falta destruir informacion.

    Args:
        nombre: texto crudo de la metadata de la nota (puede venir vacio o None).

    Returns:
        El nombre normalizado, o `""` si no habia nombre util.

    Time: O(n) sobre la longitud del nombre | Space: O(n)
    """
    texto = _texto(nombre)
    if not texto:
        return ""
    return _ESPACIOS.sub(" ", texto)


def obtener_o_crear_asociado(
    conn: sqlite3.Connection, nombre: str | None
) -> int | None:
    """Devuelve el id del asociado con ese nombre, dandolo de alta si falta.

    Resuelve R1 (match del existente), R2 (alta del nuevo), R3 (el match ignora
    mayusculas y espaciado) y R4 (nombre en blanco -> sin asociado). La guarda de
    R4 corta antes de tocar la base: un nombre vacio no crea ninguna fila.

    No abre transaccion ni hace commit: cuando la llama `confirmar_carga` corre
    dentro de su unico `with conn:`, de modo que un fallo posterior tambien
    revierte el alta del asociado. Por el mismo motivo no envuelve
    `sqlite3.Error` en un error de dominio: dejarlo propagar permite que el
    `except sqlite3.Error` del orquestador lo reporte como `CargaError` y no
    rompe el contrato de errores de MERC-01.

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        nombre: nombre del asociado tal y como viene de la nota.

    Returns:
        El `asociados.id` existente o recien creado; `None` si el nombre es
        blanco, ausente o solo espacios.

    Raises:
        sqlite3.Error: si SQLite rechaza la lectura o el alta.

    Time: O(m) sobre las filas de `asociados` (comparacion NOCASE) | Space: O(1)
    """
    normalizado = _normalizar_nombre(nombre)
    if not normalizado:
        return None

    fila = conn.execute(SELECT_ASOCIADO_ID_SQL, (normalizado,)).fetchone()
    if fila is not None:
        return int(fila["id"])

    cursor = conn.execute(INSERT_ASOCIADO_SQL, (normalizado,))
    nuevo_id = cursor.lastrowid
    if nuevo_id is None:
        raise sqlite3.DatabaseError(
            f"El alta del asociado {normalizado!r} no devolvio id"
        )
    return int(nuevo_id)


def listar_asociados(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Devuelve el directorio completo ordenado por nombre (R1).

    Cada fila incluye `saldo_pendiente` tal y como lo dejaron los triggers de
    entregas y pagos (ADR-3), asi que el listado es una sola lectura plana: no
    hay consulta por asociado ni agregacion en bucle (anti N+1).

    Args:
        conn: conexion inyectada por el call-site (ADR-2).

    Returns:
        Una lista de dicts con las claves `id`, `nombre`, `telefono`, `notas`,
        `status` y `saldo_pendiente`. Lista vacia si no hay asociados.

    Raises:
        AsociadoError: si SQLite rechaza la lectura.

    Time: O(n log n) por el ORDER BY sobre n asociados | Space: O(n)
    """
    try:
        filas = conn.execute(LISTAR_ASOCIADOS_SQL).fetchall()
    except sqlite3.Error as exc:
        raise AsociadoError(
            f"No se pudo leer el directorio de asociados: {exc}"
        ) from exc
    return [dict(fila) for fila in filas]


def crear_asociado(
    conn: sqlite3.Connection,
    nombre: str,
    telefono: str = "",
    notas: str = "",
    status: str = STATUS_ACTIVO,
) -> int:
    """Da de alta un asociado desde la GUI y devuelve su id (R2, R3, R4).

    El nombre se guarda normalizado con la misma regla que usa la carga de
    remisiones (`_normalizar_nombre`), de modo que un alta manual y un alta
    automatica del PDF resuelven a la misma fila via `COLLATE NOCASE`.

    La validez de `status` la impone el CHECK del esquema; aqui solo se traduce
    el `sqlite3.IntegrityError` resultante a `AsociadoError` en el borde (6).
    `saldo_pendiente` no se toca: nace en 0 y lo mueven los triggers (ADR-3).

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        nombre: nombre del asociado; en blanco es un error de negocio.
        telefono: telefono de contacto, opcional.
        notas: texto libre, opcional.
        status: `'Activo'` o `'Inactivo'`.

    Returns:
        El `asociados.id` recien creado.

    Raises:
        AsociadoError: si el nombre queda vacio tras normalizar, si el status
            no pasa el CHECK o si el alta no devuelve id.

    Time: O(n) sobre la longitud del nombre | Space: O(1)
    """
    normalizado = _normalizar_nombre(nombre)
    if not normalizado:
        raise AsociadoError(_MSG_NOMBRE_VACIO)

    parametros = (normalizado, _texto(telefono), _texto(notas), _texto(status))
    with conn:
        try:
            cursor = conn.execute(CREAR_ASOCIADO_SQL, parametros)
        except sqlite3.IntegrityError as exc:
            raise _status_invalido(status) from exc

    nuevo_id = cursor.lastrowid
    if nuevo_id is None:
        raise AsociadoError(f"El alta de {normalizado!r} no devolvio id.")
    return int(nuevo_id)


def _cambios_de_edicion(
    nombre: str | None,
    telefono: str | None,
    notas: str | None,
    status: str | None,
) -> dict[str, str]:
    """Reduce los argumentos de `editar_asociado` a los campos realmente dados.

    `None` significa "no tocar"; cadena vacia es un valor legitimo para
    `telefono` y `notas` (borrar el dato) pero no para `nombre`, que la fila
    declara `NOT NULL` y el negocio exige no vacio (R3).

    Args:
        nombre: nuevo nombre, o `None` para dejarlo como esta.
        telefono: nuevo telefono, o `None`.
        notas: nuevas notas, o `None`.
        status: nuevo status, o `None`.

    Returns:
        Mapa columna -> valor ya normalizado, en el orden en que se recibio.

    Raises:
        AsociadoError: si se provee un nombre que queda vacio al normalizar.

    Time: O(n) sobre la longitud de los textos | Space: O(1)
    """
    cambios: dict[str, str] = {}
    if nombre is not None:
        normalizado = _normalizar_nombre(nombre)
        if not normalizado:
            raise AsociadoError(_MSG_NOMBRE_VACIO)
        cambios["nombre"] = normalizado
    if telefono is not None:
        cambios["telefono"] = _texto(telefono)
    if notas is not None:
        cambios["notas"] = _texto(notas)
    if status is not None:
        cambios["status"] = _texto(status)
    return cambios


def editar_asociado(
    conn: sqlite3.Connection,
    asociado_id: int,
    nombre: str | None = None,
    telefono: str | None = None,
    notas: str | None = None,
    status: str | None = None,
) -> None:
    """Actualiza solo los campos provistos de un asociado (R4, R5).

    El `UPDATE` se arma concatenando exclusivamente fragmentos constantes de
    `_SET_POR_CAMPO`; los valores viajan como parametros, nunca interpolados.
    Sin campos provistos la llamada es un no-op deliberado (la GUI puede enviar
    un formulario sin cambios). `saldo_pendiente` queda fuera del mapa de
    columnas editables a proposito: lo mantienen los triggers (ADR-3).

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        asociado_id: id de la fila a modificar.
        nombre: nuevo nombre, o `None` para no tocarlo.
        telefono: nuevo telefono, o `None`.
        notas: nuevas notas, o `None`.
        status: nuevo status, o `None`.

    Raises:
        AsociadoError: si el nombre provisto queda vacio, si el status no pasa
            el CHECK o si `asociado_id` no existe.

    Time: O(1) sobre la busqueda por clave primaria | Space: O(1)
    """
    cambios = _cambios_de_edicion(nombre, telefono, notas, status)
    if not cambios:
        return

    asignaciones = ", ".join(_SET_POR_CAMPO[campo] for campo in cambios)
    sql = _UPDATE_PREFIJO + asignaciones + _UPDATE_SUFIJO
    parametros = (*cambios.values(), asociado_id)

    with conn:
        try:
            cursor = conn.execute(sql, parametros)
        except sqlite3.IntegrityError as exc:
            raise _status_invalido(_texto(status)) from exc
        if cursor.rowcount == 0:
            raise _no_existe(asociado_id)


def eliminar_asociado(conn: sqlite3.Connection, asociado_id: int) -> None:
    """Borra un asociado siempre que no tenga movimientos ligados (R6, R7).

    Dos claves foraneas protegen la fila: `entregas_asociado.asociado_id`
    (ON DELETE RESTRICT) y `pedido_detalle.asociado_id` (sin clausula, es decir
    NO ACTION). Ambas producen `sqlite3.IntegrityError` -- pero solo con
    `PRAGMA foreign_keys = ON`, que fija `db.get_conn` por conexion (FUND-02).

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        asociado_id: id de la fila a borrar.

    Raises:
        AsociadoError: si el asociado tiene entregas o detalle ligados, o si
            `asociado_id` no existe.

    Time: O(1) sobre la busqueda por clave primaria | Space: O(1)
    """
    with conn:
        try:
            cursor = conn.execute(DELETE_ASOCIADO_SQL, (asociado_id,))
        except sqlite3.IntegrityError as exc:
            raise AsociadoError(_MSG_TIENE_ENTREGAS) from exc
        if cursor.rowcount == 0:
            raise _no_existe(asociado_id)
