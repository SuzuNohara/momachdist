"""Directorio de asociados: resolucion por nombre a partir de la remision.

Cada nota del PDF trae el nombre del asociado como texto libre, sin codigo ni
identificador. Este modulo lo convierte en una fila estable de `asociados` para
que el detalle del pedido pueda referenciarla por FK.

Vive al mismo nivel que `core_productos` en el grafo de imports: solo depende de
`core_comun`, nunca de `core_pedidos` ni de la fachada `core`, de modo que las
dependencias siguen apuntando hacia abajo y no hay ciclos.

* `_normalizar_nombre`        -- recorte + colapso de espacios internos.
* `obtener_o_crear_asociado`  -- match sin distinguir mayusculas, o alta.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Final

from core_comun import _texto

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
