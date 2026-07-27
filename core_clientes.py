"""Directorio de clientes finales: CRUD sobre la tabla `clientes`.

Los clientes son los compradores propios (ventas de mostrador y encargos), y no
tienen nada que ver con el directorio de `asociados`, que vive en
`core_asociados`. Este modulo es hoja del grafo de imports de la capa core: solo
depende de `core_comun` y de la stdlib, de modo que la fachada `core` puede
re-exportarlo sin riesgo de ciclos.

Contenido:

* `ClienteError`     -- error de dominio del directorio de clientes.
* `_validar_nombre`  -- guarda de nombre obligatorio (R3).
* `listar_clientes`  -- lectura completa ordenada por nombre (R1).
* `crear_cliente`    -- alta y devolucion del id nuevo (R2).
* `editar_cliente`   -- actualizacion de los cuatro campos editables (R4).
* `eliminar_cliente` -- baja protegida por las FKs de ventas/encargos (R5, R6).

La conexion siempre llega inyectada: este modulo nunca abre bases de datos. El
`PRAGMA foreign_keys = ON` lo pone `db.get_conn`, y es lo que hace que borrar un
cliente con movimientos ligados levante `sqlite3.IntegrityError` (R6).
"""

from __future__ import annotations

import sqlite3
from typing import Final

from core_comun import CoreError, _texto

#: Columnas expuestas por `listar_clientes`, en el orden del contrato (R1).
CAMPOS_CLIENTE: Final[tuple[str, ...]] = (
    "id",
    "nombre",
    "telefono",
    "direccion",
    "notas",
    "fecha_alta",
)

_SQL_LISTAR: Final[str] = (
    "SELECT id, nombre, telefono, direccion, notas, fecha_alta "
    "FROM clientes ORDER BY nombre"
)

_SQL_CREAR: Final[str] = (
    "INSERT INTO clientes (nombre, telefono, direccion, notas) VALUES (?, ?, ?, ?)"
)

_SQL_EDITAR: Final[str] = (
    "UPDATE clientes SET nombre = ?, telefono = ?, direccion = ?, notas = ? WHERE id = ?"
)

_SQL_ELIMINAR: Final[str] = "DELETE FROM clientes WHERE id = ?"

_MSG_NOMBRE_VACIO: Final[str] = "El nombre del cliente es obligatorio."

_MSG_CLIENTE_CON_MOVIMIENTOS: Final[str] = (
    "No se puede eliminar: el cliente tiene ventas o encargos ligados."
)

_MSG_CLIENTE_INEXISTENTE: Final[str] = "El cliente indicado ya no existe."


class ClienteError(CoreError):
    """Error de dominio del directorio de clientes."""


def _validar_nombre(nombre: str) -> str:
    """Devuelve `nombre` recortado y rechaza los nombres en blanco (R3).

    El unico campo obligatorio de un cliente es el nombre: la tabla lo declara
    `NOT NULL`, pero un texto con solo espacios pasaria esa restriccion, asi que
    la guarda vive aqui, en el borde del dominio.

    Args:
        nombre: nombre crudo tal como llega de la GUI.

    Returns:
        El nombre sin espacios en los extremos.

    Raises:
        ClienteError: si el nombre esta vacio o es solo espacios.

    Time: O(n) sobre la longitud del nombre | Space: O(n)
    """
    limpio = _texto(nombre)
    if not limpio:
        raise ClienteError(_MSG_NOMBRE_VACIO)
    return limpio


def _opcional(valor: str | None) -> str | None:
    """Normaliza un campo opcional: recorta y convierte el vacio en `None`.

    Guardar `NULL` en vez de cadena vacia mantiene una sola representacion del
    dato ausente para toda la capa de lectura.

    Time: O(n) sobre la longitud del texto | Space: O(n)
    """
    if valor is None:
        return None
    limpio = _texto(valor)
    return limpio or None


def listar_clientes(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Devuelve todos los clientes ordenados por nombre (R1).

    Una sola consulta trae todas las columnas del contrato: no hay lecturas
    adicionales por fila (sin N+1).

    Args:
        conn: conexion inyectada, con `row_factory = sqlite3.Row`.

    Returns:
        Lista de diccionarios con las claves de `CAMPOS_CLIENTE`.

    Time: O(n log n) por el ORDER BY | Space: O(n)
    """
    filas = conn.execute(_SQL_LISTAR).fetchall()
    return [{campo: fila[campo] for campo in CAMPOS_CLIENTE} for fila in filas]


def crear_cliente(
    conn: sqlite3.Connection,
    nombre: str,
    telefono: str | None = None,
    direccion: str | None = None,
    notas: str | None = None,
) -> int:
    """Da de alta un cliente y devuelve su id nuevo (R2, R3).

    `fecha_alta` la pone el DEFAULT del esquema; el SQL es parametrizado.

    Args:
        conn: conexion inyectada.
        nombre: nombre del cliente (obligatorio).
        telefono: telefono de contacto, opcional.
        direccion: direccion de entrega, opcional.
        notas: observaciones libres, opcionales.

    Returns:
        El `id` autogenerado del cliente recien creado.

    Raises:
        ClienteError: si el nombre esta en blanco.

    Time: O(1) | Space: O(1)
    """
    limpio = _validar_nombre(nombre)
    with conn:
        cursor = conn.execute(
            _SQL_CREAR,
            (limpio, _opcional(telefono), _opcional(direccion), _opcional(notas)),
        )
    return int(cursor.lastrowid or 0)


def editar_cliente(
    conn: sqlite3.Connection,
    cliente_id: int,
    *,
    nombre: str,
    telefono: str | None = None,
    direccion: str | None = None,
    notas: str | None = None,
) -> None:
    """Actualiza los cuatro campos editables de un cliente (R4, R3).

    Los campos van siempre completos: la GUI envia el formulario entero, asi que
    un campo omitido significa borrarlo, no conservarlo.

    Args:
        conn: conexion inyectada.
        cliente_id: id del cliente a modificar.
        nombre: nuevo nombre (obligatorio).
        telefono: nuevo telefono, opcional.
        direccion: nueva direccion, opcional.
        notas: nuevas observaciones, opcionales.

    Raises:
        ClienteError: si el nombre esta en blanco.

    Time: O(1) | Space: O(1)
    """
    limpio = _validar_nombre(nombre)
    with conn:
        cursor = conn.execute(
            _SQL_EDITAR,
            (
                limpio,
                _opcional(telefono),
                _opcional(direccion),
                _opcional(notas),
                cliente_id,
            ),
        )
    if cursor.rowcount == 0:
        raise ClienteError(_MSG_CLIENTE_INEXISTENTE)


def eliminar_cliente(conn: sqlite3.Connection, cliente_id: int) -> None:
    """Borra un cliente que no tenga movimientos ligados (R5, R6).

    `ventas.cliente_id` y `encargos.cliente_id` protegen el historial: si el
    cliente esta referenciado, SQLite levanta `sqlite3.IntegrityError` y aqui se
    envuelve en `ClienteError` para que la GUI muestre un solo mensaje claro.
    Una venta de mostrador (`cliente_id` en `NULL`) no apunta a nadie, asi que
    nunca bloquea la baja.

    Args:
        conn: conexion inyectada, con `PRAGMA foreign_keys = ON`.
        cliente_id: id del cliente a borrar.

    Raises:
        ClienteError: si el cliente tiene ventas o encargos ligados.

    Time: O(1) | Space: O(1)
    """
    try:
        with conn:
            cursor = conn.execute(_SQL_ELIMINAR, (cliente_id,))
    except sqlite3.IntegrityError as exc:
        raise ClienteError(_MSG_CLIENTE_CON_MOVIMIENTOS) from exc
    if cursor.rowcount == 0:
        raise ClienteError(_MSG_CLIENTE_INEXISTENTE)
