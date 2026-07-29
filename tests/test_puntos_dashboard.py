"""Suite de las lecturas de puntos Betterware para el dashboard (BW-03, capa core).

Cubre las dos proyecciones que `core_semanas` expone al dashboard:

* `obtener_puntos_por_semana` -- la serie completa, en orden cronologico
  ascendente, con los puntos ya degradados a entero (R1, R2).
* `resumen_puntos` -- la semana mas reciente y sus puntos, en una sola consulta
  (R3).

Ambas deben sobrevivir a la base vacia sin lanzar (R4).

**El eje que justifica media suite:** BW-01 R6 admite semanas cuyo texto no se
pudo parsear, y esas filas tienen `numero_semana`/`anio` en `NULL`. SQLite pone
los `NULL` **primero** con `ORDER BY col ASC`, justo lo contrario de lo que pide
R1, y el fallo es silencioso: la serie sale ordenada, solo que con una barra sin
fecha abriendo la grafica. Por eso las semanas ilegibles se siembran siempre
*entre* semanas normales, nunca al principio ni al final de la siembra: una
ilegible sembrada de ultima pasaria el test aunque el `ORDER BY` estuviera mal.

La fixture levanta el esquema real con `db.init_db(":memory:")`, igual que
`tests/test_puntos_bw.py`, de donde tambien se reusa el helper de siembra
`semana_nueva` en vez de duplicarlo.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Final

import pytest

import core_semanas
import db
from tests.test_puntos_bw import semana_nueva

#: Texto que `_parsear_semana` no puede descomponer: la fila se persiste igual
#: (R6 de BW-01) pero con `numero_semana` y `anio` en `NULL`.
SEMANA_ILEGIBLE: Final[str] = "SEMANA ILEGIBLE"

#: Resumen que corresponde a un catalogo sin ninguna semana (R4).
RESUMEN_VACIO: Final[dict[str, object]] = {"ultima_semana": "", "puntos_ultima": 0}


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """Conexion en memoria con el esquema canonico aplicado."""
    conexion = db.init_db(":memory:")
    try:
        yield conexion
    finally:
        conexion.close()


def sembrar(
    conn: sqlite3.Connection, semana_texto: str, puntos: int | None = None
) -> int:
    """Da de alta la semana y le fija `puntos` si se indica; devuelve su id.

    Los puntos se escriben con `manual=True` para saltarse la semantica de
    maximo de BW-02: aqui interesa el valor exacto sembrado, no la regla de
    acumulacion.
    """
    semana_id = semana_nueva(conn, semana_texto)
    if puntos is not None:
        core_semanas.actualizar_puntos_semana(conn, semana_id, puntos, manual=True)
    return semana_id


def anular_puntos(conn: sqlite3.Connection, semana_id: int) -> None:
    """Deja `puntos_bw_acumulados` en `NULL`, estado que el `DEFAULT 0` no da."""
    with conn:
        conn.execute(
            "UPDATE semanas_catalogo SET puntos_bw_acumulados = NULL WHERE id = ?",
            (semana_id,),
        )


# --------------------------------------------------------------------------
# R1 - obtener_puntos_por_semana: orden cronologico ascendente
# --------------------------------------------------------------------------
def test_obtener_puntos_por_semana_ordena_ascendente_por_anio_y_numero(
    conn: sqlite3.Connection,
) -> None:
    """R1: la serie sale de la semana mas antigua a la mas reciente.

    `"9 - 2026"` y `"30 - 2026"` estan elegidas a proposito: por texto la 30 va
    antes que la 9, asi que el orden solo puede salir bien si se apoya en las
    columnas numericas y no en `semana_texto`.
    """
    # Arrange
    sembrar(conn, "30 - 2026", 900)
    sembrar(conn, "9 - 2026", 100)
    sembrar(conn, "51 - 2025", 50)

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)

    # Assert
    assert [fila["semana_texto"] for fila in serie] == [
        "51 - 2025",
        "9 - 2026",
        "30 - 2026",
    ]


def test_obtener_puntos_por_semana_coloca_la_semana_ilegible_al_final(
    conn: sqlite3.Connection,
) -> None:
    """R1: los `NULL` van al final, no al principio como haria SQLite por defecto.

    La ilegible se siembra **entre** dos semanas fechadas: si el `ORDER BY` no
    trata el `NULL` de forma explicita, esta fila abre la serie y la grafica del
    dashboard arranca con una barra sin fecha.
    """
    # Arrange
    sembrar(conn, "29 - 2026", 100)
    sembrar(conn, SEMANA_ILEGIBLE, 7)
    sembrar(conn, "30 - 2026", 200)

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)

    # Assert
    assert [fila["semana_texto"] for fila in serie] == [
        "29 - 2026",
        "30 - 2026",
        SEMANA_ILEGIBLE,
    ]
    assert serie[-1]["numero_semana"] is None
    assert serie[-1]["anio"] is None


def test_obtener_puntos_por_semana_agrupa_todas_las_ilegibles_al_final(
    conn: sqlite3.Connection,
) -> None:
    """R1: con varias semanas sin fecha, ninguna se cuela entre las fechadas."""
    # Arrange
    sembrar(conn, "SEMANA ROTA A", 1)
    sembrar(conn, "40 - 2026", 400)
    sembrar(conn, "SEMANA ROTA B", 2)
    sembrar(conn, "41 - 2026", 410)

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)

    # Assert
    assert [fila["semana_texto"] for fila in serie][:2] == ["40 - 2026", "41 - 2026"]
    assert {fila["semana_texto"] for fila in serie[2:]} == {
        "SEMANA ROTA A",
        "SEMANA ROTA B",
    }


def test_obtener_puntos_por_semana_expone_las_claves_del_contrato(
    conn: sqlite3.Connection,
) -> None:
    """R1: cada fila trae exactamente `CAMPOS_PUNTOS`, con `puntos` como clave.

    El `id` queda fuera a proposito: el dashboard solo dibuja, y quien necesita
    el id para escribir es `listar_semanas` (D11).
    """
    # Arrange
    sembrar(conn, "30 - 2026", 900)

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)

    # Assert
    assert set(serie[0]) == set(core_semanas.CAMPOS_PUNTOS)
    assert serie[0] == {
        "semana_texto": "30 - 2026",
        "numero_semana": 30,
        "anio": 2026,
        "puntos": 900,
    }


# --------------------------------------------------------------------------
# R2 - los puntos nunca llegan como None
# --------------------------------------------------------------------------
def test_obtener_puntos_por_semana_degrada_puntos_nulos_a_cero(
    conn: sqlite3.Connection,
) -> None:
    """R2: `NULL` en la columna sale como `0`, nunca como `None`."""
    # Arrange
    semana_id = sembrar(conn, "30 - 2026")
    anular_puntos(conn, semana_id)

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)

    # Assert
    assert serie[0]["puntos"] == 0
    assert isinstance(serie[0]["puntos"], int)


def test_obtener_puntos_por_semana_respeta_el_cero_del_esquema(
    conn: sqlite3.Connection,
) -> None:
    """R2: una semana recien creada arranca con el `DEFAULT 0` del esquema."""
    # Arrange
    sembrar(conn, "30 - 2026")

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)

    # Assert
    assert serie[0]["puntos"] == 0


# --------------------------------------------------------------------------
# R4 - base sin filas
# --------------------------------------------------------------------------
def test_obtener_puntos_por_semana_devuelve_lista_vacia_sin_filas(
    conn: sqlite3.Connection,
) -> None:
    """R4: catalogo vacio -> lista vacia, sin lanzar."""
    # Act / Assert
    assert core_semanas.obtener_puntos_por_semana(conn) == []


def test_resumen_puntos_devuelve_resumen_neutro_sin_filas(
    conn: sqlite3.Connection,
) -> None:
    """R4: catalogo vacio -> `{"ultima_semana": "", "puntos_ultima": 0}`."""
    # Act / Assert
    assert core_semanas.resumen_puntos(conn) == RESUMEN_VACIO


# --------------------------------------------------------------------------
# R3 - resumen_puntos: la semana mas reciente
# --------------------------------------------------------------------------
def test_resumen_puntos_devuelve_la_semana_mas_reciente(
    conn: sqlite3.Connection,
) -> None:
    """R3: gana el numero de semana mas alto dentro del mismo anio."""
    # Arrange
    sembrar(conn, "9 - 2026", 100)
    sembrar(conn, "30 - 2026", 900)
    sembrar(conn, "29 - 2026", 800)

    # Act
    resumen = core_semanas.resumen_puntos(conn)

    # Assert
    assert resumen == {"ultima_semana": "30 - 2026", "puntos_ultima": 900}


def test_resumen_puntos_prioriza_el_anio_sobre_el_numero_de_semana(
    conn: sqlite3.Connection,
) -> None:
    """R3: la semana 2 de 2026 es mas reciente que la 51 de 2025."""
    # Arrange
    sembrar(conn, "51 - 2025", 5100)
    sembrar(conn, "2 - 2026", 200)

    # Act
    resumen = core_semanas.resumen_puntos(conn)

    # Assert
    assert resumen == {"ultima_semana": "2 - 2026", "puntos_ultima": 200}


def test_resumen_puntos_no_deja_ganar_a_la_semana_ilegible(
    conn: sqlite3.Connection,
) -> None:
    """R3: una fila con `anio`/`numero_semana` en `NULL` no es "la mas reciente".

    Es la contraparte de R1 en la cabecera del dashboard: si el `NULL` no se
    trata explicito en el orden descendente, la semana ilegible se cuela como
    ultima y la cabecera reporta puntos que no son los de la semana en curso.
    """
    # Arrange
    sembrar(conn, "29 - 2026", 100)
    sembrar(conn, SEMANA_ILEGIBLE, 999999)
    sembrar(conn, "30 - 2026", 200)

    # Act
    resumen = core_semanas.resumen_puntos(conn)

    # Assert
    assert resumen == {"ultima_semana": "30 - 2026", "puntos_ultima": 200}


def test_resumen_puntos_usa_la_ilegible_cuando_es_la_unica_semana(
    conn: sqlite3.Connection,
) -> None:
    """R3, R4: sin ninguna semana fechada, la ilegible es lo unico que hay.

    Documenta el borde: el resumen neutro esta reservado al catalogo vacio; una
    semana sin fecha sigue siendo una semana y su texto es el dato crudo del PDF.
    """
    # Arrange
    sembrar(conn, SEMANA_ILEGIBLE, 42)

    # Act
    resumen = core_semanas.resumen_puntos(conn)

    # Assert
    assert resumen == {"ultima_semana": SEMANA_ILEGIBLE, "puntos_ultima": 42}


def test_resumen_puntos_coincide_con_el_ultimo_elemento_de_la_serie(
    conn: sqlite3.Connection,
) -> None:
    """R1, R3: cabecera y grafica no pueden discrepar sobre cual es la ultima.

    Las dos funciones tienen su propia sentencia (no hay N+1), asi que nada
    garantiza por construccion que compartan criterio: hay que comprobarlo. La
    siembra incluye una ilegible justo porque es el caso donde ambos ordenes
    podrian divergir.
    """
    # Arrange
    sembrar(conn, "51 - 2025", 50)
    sembrar(conn, SEMANA_ILEGIBLE, 7)
    sembrar(conn, "30 - 2026", 900)
    sembrar(conn, "9 - 2026", 100)

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)
    resumen = core_semanas.resumen_puntos(conn)

    # Assert
    fechadas = [fila for fila in serie if fila["anio"] is not None]
    assert resumen["ultima_semana"] == fechadas[-1]["semana_texto"]
    assert resumen["puntos_ultima"] == fechadas[-1]["puntos"]


def test_resumen_puntos_degrada_puntos_nulos_a_cero_entero(
    conn: sqlite3.Connection,
) -> None:
    """R2, R3: `puntos_ultima` es un `int`, tambien cuando la columna es `NULL`."""
    # Arrange
    semana_id = sembrar(conn, "30 - 2026")
    anular_puntos(conn, semana_id)

    # Act
    resumen = core_semanas.resumen_puntos(conn)

    # Assert
    assert resumen == {"ultima_semana": "30 - 2026", "puntos_ultima": 0}
    assert isinstance(resumen["puntos_ultima"], int)


def test_resumen_puntos_resuelve_con_una_sola_consulta(
    conn: sqlite3.Connection,
) -> None:
    """`.langs/python.md` §4: nada de N+1 ni de traer la serie entera para una fila."""
    # Arrange
    for numero in range(1, 11):
        sembrar(conn, f"{numero} - 2026", numero * 10)
    sentencias: list[str] = []
    conn.set_trace_callback(sentencias.append)

    # Act
    resumen = core_semanas.resumen_puntos(conn)
    conn.set_trace_callback(None)

    # Assert
    assert len(sentencias) == 1
    assert resumen == {"ultima_semana": "10 - 2026", "puntos_ultima": 100}


# --------------------------------------------------------------------------
# D11 - convivencia con listar_semanas
# --------------------------------------------------------------------------
def test_obtener_puntos_por_semana_y_listar_semanas_tienen_contratos_distintos(
    conn: sqlite3.Connection,
) -> None:
    """D11: ambas se mantienen porque no son la misma lectura.

    `listar_semanas` ordena DESC, incluye `id` y llama `puntos_bw_acumulados` a
    la columna (dialogo de correccion manual, W4); esta ordena ASC, omite el
    `id` y la llama `puntos` (dashboard). Si alguien "simplifica" haciendo que
    una llame a la otra, este test lo delata.
    """
    # Arrange
    sembrar(conn, "29 - 2026", 100)
    sembrar(conn, "30 - 2026", 200)

    # Act
    serie = core_semanas.obtener_puntos_por_semana(conn)
    semanas = core_semanas.listar_semanas(conn)

    # Assert
    assert [fila["semana_texto"] for fila in serie] == list(
        reversed([fila["semana_texto"] for fila in semanas])
    )
    assert set(core_semanas.CAMPOS_PUNTOS) != set(core_semanas.CAMPOS_SEMANA)
    assert "id" in semanas[0] and "id" not in serie[0]
