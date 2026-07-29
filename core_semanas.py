"""Catalogo de semanas Betterware: resolucion de la semana de cada remision.

Cada nota del PDF trae la semana como texto libre (`"30 - 2026"`), sin id ni
codigo. Este modulo lo convierte en una fila estable de `semanas_catalogo` para
que la cabecera del pedido pueda referenciarla por FK, y deja preparado el
soporte de los puntos Betterware (`puntos_bw_acumulados`), que mantiene BW-02.

Vive al mismo nivel que `core_asociados` en el grafo de imports: solo depende de
`core_comun`, nunca de `core_pedidos` ni de la fachada `core`, de modo que las
dependencias siguen apuntando hacia abajo y no hay ciclos.

* `_parsear_semana`          -- extrae numero y anio del texto, sin tocar la base.
* `obtener_o_crear_semana`   -- upsert idempotente sobre `UNIQUE(semana_texto)`.
* `actualizar_puntos_semana` -- fija los puntos BW de una semana (BW-02).
* `procesar_puntos_bw`       -- cablea la extraccion del PDF con la semana (BW-02).
* `obtener_puntos_por_semana` / `resumen_puntos` -- lecturas del dashboard (BW-03).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Final

from core_comun import CoreError, _texto

#: Clave con la que el extractor de PDF entrega la semana de catalogo.
CLAVE_SEMANA: Final[str] = "Semana"

#: `"30 - 2026"`, `"30-2026"` y `"30  -  2026"` son la misma semana: el guion
#: admite espacios a ambos lados. El anio se fija a cuatro digitos para no
#: confundirlo con el numero de semana cuando el texto trae ruido alrededor.
_PATRON_SEMANA: Final[re.Pattern[str]] = re.compile(r"(\d+)\s*-\s*(\d{4})")

#: `INSERT OR IGNORE` (y no `ON CONFLICT ... DO NOTHING` con target explicito)
#: porque aqui el unico conflicto posible es el de `UNIQUE(semana_texto)`: la
#: tabla no tiene CHECK ni FK que convenga silenciar por accidente.
INSERT_SEMANA_SQL: Final[str] = """
INSERT OR IGNORE INTO semanas_catalogo (semana_texto, numero_semana, anio)
VALUES (?, ?, ?)
"""

SELECT_SEMANA_ID_SQL: Final[str] = (
    "SELECT id FROM semanas_catalogo WHERE semana_texto = ?"
)

SELECT_PUNTOS_SQL: Final[str] = (
    "SELECT puntos_bw_acumulados FROM semanas_catalogo WHERE id = ?"
)

UPDATE_PUNTOS_SQL: Final[str] = (
    "UPDATE semanas_catalogo SET puntos_bw_acumulados = ? WHERE id = ?"
)

#: Orden cronologico inverso: lo mas reciente primero, que es lo que la usuaria
#: consulta y corrige. `anio`/`numero_semana` son NULL cuando el texto no se
#: pudo parsear (R6 de BW-01), asi que esas filas caen al final sin romper el
#: orden de las demas.
SELECT_SEMANAS_SQL: Final[str] = """
SELECT id, semana_texto, numero_semana, anio,
       COALESCE(puntos_bw_acumulados, 0) AS puntos_bw_acumulados
FROM semanas_catalogo
ORDER BY anio DESC, numero_semana DESC, semana_texto DESC
"""

#: Orden cronologico ascendente, el que lee la grafica del dashboard (BW-03 R1).
#: Con `ASC` SQLite pone los `NULL` **primero**, al reves de lo que pide R1, asi
#: que cada `col IS NULL` (0 antes que 1) manda al final las semanas no
#: parseables (R6 de BW-01). `NULLS LAST` ataria el build a SQLite >= 3.30.
SELECT_PUNTOS_POR_SEMANA_SQL: Final[str] = """
SELECT semana_texto, numero_semana, anio,
       COALESCE(puntos_bw_acumulados, 0) AS puntos
FROM semanas_catalogo
ORDER BY anio IS NULL, anio ASC, numero_semana IS NULL, numero_semana ASC, semana_texto ASC
"""

#: El mismo criterio invertido, para la cabecera (BW-03 R3). Los `IS NULL` siguen
#: en ASC: una semana sin fecha no debe ganar como "la mas reciente" por ser NULL.
SELECT_ULTIMA_SEMANA_SQL: Final[str] = """
SELECT semana_texto, COALESCE(puntos_bw_acumulados, 0) AS puntos
FROM semanas_catalogo
ORDER BY anio IS NULL, anio DESC, numero_semana IS NULL, numero_semana DESC, semana_texto DESC
LIMIT 1
"""

#: Claves que expone `listar_semanas`, contrato de la GUI.
CAMPOS_SEMANA: Final[tuple[str, ...]] = (
    "id",
    "semana_texto",
    "numero_semana",
    "anio",
    "puntos_bw_acumulados",
)

#: Claves que expone `obtener_puntos_por_semana`, contrato del dashboard.
CAMPOS_PUNTOS: Final[tuple[str, ...]] = ("semana_texto", "numero_semana", "anio", "puntos")


def listar_semanas(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Devuelve las semanas de catalogo, de la mas reciente a la mas antigua.

    Existe porque la GUI nunca ejecuta SQL (ADR-2): la afordancia de correccion
    manual de puntos necesita listar las semanas y su valor actual, y esa
    lectura tiene que salir de la capa core.

    `puntos_bw_acumulados` llega siempre como entero: la columna admite NULL
    (semana recien creada por `obtener_o_crear_semana`, que no fija puntos) y
    aqui se degrada a `0` para que el formateo de la GUI no reciba `None`.

    Args:
        conn: conexion inyectada por el call-site.

    Returns:
        Lista de diccionarios con las claves de `CAMPOS_SEMANA`.

    Time: O(n log n) por el ORDER BY | Space: O(n)
    """
    filas = conn.execute(SELECT_SEMANAS_SQL).fetchall()
    return [{campo: fila[campo] for campo in CAMPOS_SEMANA} for fila in filas]


def obtener_puntos_por_semana(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Serie de puntos BW por semana, en orden cronologico ascendente (R1, R2).

    Alimenta la grafica del dashboard, que se lee de izquierda a derecha. Las
    semanas sin fecha (texto no parseable, R6 de BW-01) van todas al final.

    **No sustituye a `listar_semanas`, la funcion de aqui arriba** (D11): aquella
    ordena DESC, incluye `id` y llama `puntos_bw_acumulados` a la columna; la usa
    el dialogo de correccion manual (W4), que necesita el `id` para escribir. Esta
    ordena ASC, omite el `id` y la llama `puntos`; la usa el dashboard, que dibuja.

    `puntos` llega siempre como entero: `COALESCE` degrada el `NULL` a `0` (R2).

    Args:
        conn: conexion inyectada por el call-site (ADR-2).

    Returns:
        Un diccionario con las claves de `CAMPOS_PUNTOS` por fila del catalogo;
        lista vacia si no hay ninguna (R4).

    Time: O(n log n) por el ORDER BY | Space: O(n)
    """
    filas = conn.execute(SELECT_PUNTOS_POR_SEMANA_SQL).fetchall()
    return [{campo: fila[campo] for campo in CAMPOS_PUNTOS} for fila in filas]


def resumen_puntos(conn: sqlite3.Connection) -> dict[str, object]:
    """Semana mas reciente del catalogo y sus puntos (R3, R4).

    Cabecera del dashboard. Invierte el criterio de orden de R1 para que cabecera
    y grafica no discrepen; una semana sin fecha solo gana si es la unica fila.

    Una sola sentencia con `LIMIT 1`: reusar `obtener_puntos_por_semana` y quedarse
    con el ultimo elemento traeria el catalogo entero para leer una fila (§4).

    Args:
        conn: conexion inyectada por el call-site (ADR-2).

    Returns:
        `{"ultima_semana": str, "puntos_ultima": int}`; con el catalogo vacio,
        `{"ultima_semana": "", "puntos_ultima": 0}` (R4).

    Time: O(n) sobre las filas del catalogo | Space: O(1)
    """
    fila = conn.execute(SELECT_ULTIMA_SEMANA_SQL).fetchone()
    if fila is None:
        return {"ultima_semana": "", "puntos_ultima": 0}
    return {"ultima_semana": fila["semana_texto"], "puntos_ultima": int(fila["puntos"])}


def _parsear_semana(texto: str | None) -> tuple[int | None, int | None]:
    """Extrae `(numero_semana, anio)` del texto de la semana (R1).

    Funcion pura y total: no toca la base y **nunca lanza**. Un texto ausente,
    en blanco o que no case con el patron devuelve `(None, None)`, que es
    exactamente lo que las columnas nullables del catalogo esperan (R6).

    Args:
        texto: semana tal y como la entrega `pdf_extractor.extraer_metadata`,
            p. ej. `"30 - 2026"`; puede venir vacia o `None`.

    Returns:
        `(numero_semana, anio)` como enteros, o `(None, None)` si no hay un
        par reconocible en el texto.

    Time: O(n) sobre la longitud del texto | Space: O(1)
    """
    normalizado = _texto(texto)
    if not normalizado:
        return (None, None)

    coincidencia = _PATRON_SEMANA.search(normalizado)
    if coincidencia is None:
        return (None, None)
    return (int(coincidencia.group(1)), int(coincidencia.group(2)))


def obtener_o_crear_semana(
    conn: sqlite3.Connection, semana_texto: str | None
) -> int | None:
    """Devuelve el id de la semana, dandola de alta si falta (R2, R3, R4, R6).

    Resuelve R2 (alta de la semana nueva), R3 (idempotencia sobre
    `UNIQUE(semana_texto)`: el mismo texto siempre devuelve el mismo id y una
    sola fila) y R4 (texto en blanco -> sin semana). La guarda de R4 corta antes
    de tocar la base: una semana vacia no crea ninguna fila.

    Un texto que no case con el patron igual se persiste (R6): la fila queda con
    `numero_semana` y `anio` en `NULL` y la carga no se interrumpe, porque el
    dato crudo del PDF es la fuente de verdad y perderlo seria peor que no
    poder ordenarlo.

    No abre transaccion ni hace commit (R7): cuando la llama `confirmar_carga`
    corre dentro de su unico `with conn:`, de modo que un fallo posterior del
    lote tambien revierte el alta de la semana. Por el mismo motivo no envuelve
    `sqlite3.Error` en un error de dominio: dejarlo propagar permite que el
    `except sqlite3.Error` del orquestador lo reporte como `CargaError`.

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        semana_texto: semana tal y como viene de la nota.

    Returns:
        El `semanas_catalogo.id` existente o recien creado; `None` si el texto
        es blanco, ausente o solo espacios.

    Raises:
        sqlite3.Error: si SQLite rechaza el alta o la lectura.

    Time: O(log m) sobre el indice de `semana_texto` | Space: O(1)
    """
    normalizado = _texto(semana_texto)
    if not normalizado:
        return None

    numero_semana, anio = _parsear_semana(normalizado)
    conn.execute(INSERT_SEMANA_SQL, (normalizado, numero_semana, anio))

    fila = conn.execute(SELECT_SEMANA_ID_SQL, (normalizado,)).fetchone()
    if fila is None:
        raise sqlite3.DatabaseError(
            f"No se pudo recuperar la semana {normalizado!r} tras el upsert"
        )
    return int(fila["id"])


def actualizar_puntos_semana(
    conn: sqlite3.Connection, semana_id: int, puntos: int, *, manual: bool = False
) -> bool:
    """Fija los puntos Betterware de una semana (R4, R6).

    **R6 -- decision de negocio del 2026-07-27** (reemplaza la guarda de
    no-clobber del plan original):

    * `manual=True`  -> escribe **siempre** el valor exacto. La correccion de la
      usuaria conserva prioridad absoluta, incluso a la baja.
    * `manual=False` -> escribe **solo si `puntos` supera al valor almacenado**
      (semantica de maximo). `NULL` cuenta como "sin valor" y siempre se escribe.

    El motivo: `Total PB acumulados` es un corrido de temporada y una misma nota
    puede traerlo repetido con cortes distintos (en `C001264_NOTA.pdf` las cuatro
    paginas con puntos del cierre 29 dicen 20003, 6428, 22272 y 8777). El valor
    real de la semana es el mayor; los menores son cortes anteriores. Quedarse
    con el maximo hace ademas que el resultado **no dependa del orden** en que se
    recorran las paginas, cosa que la guarda original no garantizaba.

    Riesgo residual aceptado: el esquema no marca el origen del valor, asi que un
    extract automatico posterior con un numero mayor si puede pisar una
    correccion manual hacia abajo. Distinguirlo exigiria una columna nueva.

    Escritura propia -> delimita su transaccion con `with conn:` (nunca un
    `conn.commit()` suelto). La lectura de la guarda va dentro del mismo bloque
    para que comparar y escribir sean atomicos.

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        semana_id: id de `semanas_catalogo` destino.
        puntos: valor a fijar en `puntos_bw_acumulados`.
        manual: `True` si lo corrige la usuaria; `False` si viene de un extract.

    Returns:
        `True` si la fila se escribio; `False` si la guarda de maximo la freno o
        si `semana_id` no existe.

    Raises:
        sqlite3.Error: si SQLite rechaza la lectura o la escritura.

    Time: O(log m) sobre la PK de `semanas_catalogo` | Space: O(1)
    """
    with conn:
        if not manual and not _supera_puntos_almacenados(conn, semana_id, puntos):
            return False

        cursor = conn.execute(UPDATE_PUNTOS_SQL, (puntos, semana_id))
        return cursor.rowcount > 0


def _supera_puntos_almacenados(
    conn: sqlite3.Connection, semana_id: int, puntos: int
) -> bool:
    """Indica si `puntos` debe pisar al valor guardado en modo automatico (R6).

    `NULL` y una semana inexistente se tratan como "sin valor": en el primer caso
    hay que escribir, en el segundo el `UPDATE` posterior no afectara ninguna
    fila y `actualizar_puntos_semana` devolvera `False` por si mismo.

    Time: O(log m) | Space: O(1)
    """
    fila = conn.execute(SELECT_PUNTOS_SQL, (semana_id,)).fetchone()
    if fila is None:
        return True

    almacenado = fila["puntos_bw_acumulados"]
    return almacenado is None or puntos > int(almacenado)


def _leer_paginas_con_puntos(
    extractor: Any, ruta_pdf: str
) -> list[tuple[int, int | None]]:
    """Lee las paginas con puntos envolviendo los fallos del lector de PDF.

    `pdfplumber` levanta excepciones que heredan de `Exception` a secas
    (`PdfminerException`, `MalformedPDFException`), asi que un PDF corrupto se
    escapaba de todo manejo por tipo y llegaba crudo hasta la GUI. Envolverlas
    aqui, en el borde del dominio, es lo que manda `.langs/python.md` §6 y evita
    que la capa de presentacion tenga que nombrar tipos de `pdfplumber`.

    Los tipos se alcanzan a traves del propio `extractor` (que es quien posee la
    dependencia) para no importar `pdfplumber` en este modulo.

    Time: O(p) sobre las paginas del PDF | Space: O(p)
    """
    fallos_lector = (
        extractor.pdfplumber.utils.exceptions.PdfminerException,
        extractor.pdfplumber.utils.exceptions.MalformedPDFException,
    )
    try:
        return extractor.extraer_puntos_de_paginas(ruta_pdf)
    except fallos_lector as exc:
        raise CoreError(f"No se pudo leer el PDF {ruta_pdf}: {exc}") from exc
    except OSError as exc:
        raise CoreError(f"No se pudo abrir el PDF {ruta_pdf}: {exc}") from exc


def procesar_puntos_bw(
    conn: sqlite3.Connection, ruta_pdf: str, semana_id_pedido: int, anio: int
) -> None:
    """Extrae los puntos del PDF y los fija en la semana que les corresponde (R5, R7).

    **Regla de asociacion de semana (R5).** El valor `Total PB acumulados` es un
    acumulado que Betterware reporta *al cierre de la semana N*: **esa N es la
    semana a la que pertenecen los puntos**, no la `Semana` del pedido (que es
    cuando se levanto la orden). En `C001264_NOTA.pdf` los pedidos son de la
    semana 30 y los puntos dicen `al cierre de semana 29`, de modo que los puntos
    van a la `29 - 2026`. La semana destino se resuelve con
    `obtener_o_crear_semana(conn, f"{N} - {anio}")` -- ese f-string construye un
    **valor**, no SQL, asi que no viola `.langs/python.md` §5.

    **Fallback.** Sin referencia de cierre los puntos se quedan en la semana
    propia del pedido (`semana_id_pedido`).

    **Limitacion aceptada.** No se maneja el rollover de anio (semana 52 -> 1):
    el anio se hereda tal cual del pedido.

    Las paginas sin puntos no llegan aqui -- `extraer_puntos_de_paginas` ya las
    descarta -- de modo que no disparan ningun `UPDATE` (R7). Cada pagina se
    escribe con `manual=False`, es decir bajo la semantica de maximo de R6: sobre
    una nota con varios cortes del mismo cierre gana el mayor, sin importar el
    orden de lectura.

    `pdf_extractor` se importa **dentro** de la funcion a proposito: el grafo de
    imports exige que `core_semanas` solo dependa de `core_comun` a nivel de
    modulo, de forma que importar la capa de dominio no arrastre `pdfplumber` ni
    abra la puerta a un ciclo. El import local tambien deja un punto de inyeccion
    limpio para las pruebas.

    Args:
        conn: conexion inyectada por el call-site (ADR-2).
        ruta_pdf: PDF de remisiones recien cargado.
        semana_id_pedido: semana del pedido, usada como fallback.
        anio: anio heredado del pedido para construir la clave de semana.

    Raises:
        CoreError: si el PDF no se puede leer (ruta ilegible o archivo corrupto).
        sqlite3.Error: si SQLite rechaza alguna de las escrituras.

    Time: O(p * (n + log m)) | Space: O(p)  (p = paginas con puntos)
    """
    import pdf_extractor

    for puntos, semana_cierre in _leer_paginas_con_puntos(pdf_extractor, ruta_pdf):
        semana_destino = semana_id_pedido
        if semana_cierre is not None:
            semana_destino = obtener_o_crear_semana(conn, f"{semana_cierre} - {anio}")

        if semana_destino is None:
            continue
        actualizar_puntos_semana(conn, semana_destino, puntos, manual=False)
