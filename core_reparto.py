"""Reparto de la mercancia recibida entre Asociado, Casa y Local.

El PDF no dice donde quedo cada pieza: solo cuantas llegaron. El reparto es una
decision del negocio que se toma en la vista previa, antes de confirmar la
carga. Este modulo concentra esa regla para que la GUI no la reimplemente y
para que `pdf_extractor` siga siendo una copia verbatim del parser original
(ADR-4): el extractor sigue entregando el default historico "todo a Casa" y la
inversion (O2 / ADR-2) se aplica aqui, como paso posterior a la extraccion.

Regla de negocio: lo normal es que la nota completa sea del asociado que la
firma, y que la usuaria mueva a Casa/Local solo lo excepcional. Por eso el
default arranca en Asociado y no en Casa.

Invariante que **no** cambia: `Asociado + Casa + Local == Cantidad surtida`.
Todas las funciones de este modulo lo preservan (incluido el caso de regalo,
`surtida == 0`, que deja las tres ubicaciones en 0). El CHECK homonimo de
`pedido_detalle` sigue siendo la ultima linea de defensa.

Vive al mismo nivel que `core_productos` en el grafo de imports: solo depende
de `core_comun` y de `core_asociados`, nunca de `core_pedidos` ni de la fachada
`core`, de modo que las dependencias siguen apuntando hacia abajo.

* `aplicar_reparto_default_asociado` -- voltea una fila al default de Asociado.
* `aplicar_default_post_extraccion`  -- aplica ese default a un lote extraido.
* `estampar_asociado_id`             -- sella el asociado resuelto en las filas.
* `normalizar_reparto_carga`         -- default por fila, con fallback a Casa.
"""

from __future__ import annotations

from typing import Any, Final

from core_asociados import CLAVE_NOMBRE_ASOCIADO, _normalizar_nombre
from core_comun import _entero, _texto

#: Identificador del asociado de la nota, tal y como lo resuelve MERC-02. Viaja
#: en la propia fila para que la vista previa y la capa de persistencia hablen
#: del mismo asociado sin volver a consultarlo.
CLAVE_ASOCIADO_ID: Final[str] = "asociado_id"

CLAVE_SURTIDA: Final[str] = "Cantidad surtida"
CLAVE_CANTIDAD_ASOCIADO: Final[str] = "Cantidad Asociado"
CLAVE_CANTIDAD_CASA: Final[str] = "Cantidad Casa"
CLAVE_CANTIDAD_LOCAL: Final[str] = "Cantidad Local"

#: Marca interna (no es columna de la base) con la que la vista previa resalta
#: las filas cuya nota no trae asociado resoluble. Evita que se confirme una
#: entrega a un asociado fantasma.
MARCA_REVISAR: Final[str] = "_revisar_asociado"


def aplicar_reparto_default_asociado(fila: dict[str, Any]) -> dict[str, Any]:
    """Asigna toda la cantidad surtida al asociado de la nota (R1).

    Invierte el default historico del parser ("todo a Casa"). Muta la fila y la
    devuelve, de modo que la lista de la vista previa conserva la identidad de
    sus dicts y lo editado por la usuaria no se pierde en una copia.

    Un producto de regalo (`Cantidad surtida == 0`) queda con las tres
    ubicaciones en 0, que tambien satisface `suma == surtida`.

    Args:
        fila: registro del extractor (o de la vista previa) a repartir.

    Returns:
        La misma fila, ya repartida.

    Time: O(1) | Space: O(1)
    """
    surtida = _entero(fila.get(CLAVE_SURTIDA))
    fila[CLAVE_CANTIDAD_ASOCIADO] = surtida
    fila[CLAVE_CANTIDAD_CASA] = 0
    fila[CLAVE_CANTIDAD_LOCAL] = 0
    return fila


def _aplicar_reparto_default_casa(fila: dict[str, Any]) -> dict[str, Any]:
    """Deja toda la cantidad surtida en Casa (fallback de R7).

    Es el default historico del parser, que aqui solo se usa cuando la nota no
    tiene asociado resoluble: sin asociado no puede haber entrega, asi que la
    mercancia se contabiliza como stock propio hasta que la usuaria decida.

    Time: O(1) | Space: O(1)
    """
    surtida = _entero(fila.get(CLAVE_SURTIDA))
    fila[CLAVE_CANTIDAD_ASOCIADO] = 0
    fila[CLAVE_CANTIDAD_CASA] = surtida
    fila[CLAVE_CANTIDAD_LOCAL] = 0
    return fila


def aplicar_default_post_extraccion(
    filas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Voltea al asociado el default de un lote recien extraido (R1, R8).

    Paso posterior a `pdf_extractor`, que no se toca por ser copia verbatim del
    parser original: sus dos ramas (tabla de 8 y de 9 columnas) inicializan
    `Cantidad Casa = surtida`, y aqui esa inicializacion pasa a `Asociado`.

    Args:
        filas: registros tal y como los entrega el extractor.

    Returns:
        La misma lista (mismos dicts), ya repartida al asociado.

    Time: O(n) | Space: O(1) adicional
    """
    for fila in filas:
        aplicar_reparto_default_asociado(fila)
    return filas


def estampar_asociado_id(
    filas: list[dict[str, Any]], asociado_id: int | None
) -> list[dict[str, Any]]:
    """Sella en cada fila el asociado ya resuelto de su nota (R2).

    `obtener_o_crear_asociado` resuelve el asociado una sola vez por nota
    (MERC-02); este helper reparte ese id entre todas las lineas del folio para
    que viaje con los datos hasta la vista previa y la persistencia, sin volver
    a consultar la base.

    Args:
        filas: lineas de una misma nota.
        asociado_id: id resuelto de la nota; `None` si la nota no trae nombre.

    Returns:
        La misma lista, con `asociado_id` en cada fila.

    Time: O(n) | Space: O(1) adicional
    """
    for fila in filas:
        fila[CLAVE_ASOCIADO_ID] = asociado_id
    return filas


def _tiene_asociado(fila: dict[str, Any]) -> bool:
    """Indica si la fila tiene un asociado al que asignarle la mercancia.

    Sirve el id sellado por `estampar_asociado_id` y, cuando todavia no existe
    (la vista previa corre antes de tocar la base), el nombre de la nota: un
    asociado nuevo es resoluble aunque aun no tenga fila en `asociados`.

    Time: O(n) sobre la longitud del nombre | Space: O(1)
    """
    identificador = fila.get(CLAVE_ASOCIADO_ID)
    if identificador is not None and _texto(identificador):
        return True
    return bool(_normalizar_nombre(fila.get(CLAVE_NOMBRE_ASOCIADO)))


def normalizar_reparto_carga(
    filas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aplica el default de reparto fila por fila, con fallback a Casa (R7).

    Con asociado resoluble: todo al asociado. Sin el: todo a Casa y la fila
    queda marcada con `_revisar_asociado` para que la vista previa la resalte y
    la usuaria revise antes de confirmar (una entrega sin asociado seria un
    fantasma en el modulo de entregas). La marca se retira cuando la fila
    vuelve a tener asociado, de modo que la funcion es idempotente.

    Args:
        filas: registros de la vista previa (una o varias notas mezcladas).

    Returns:
        La misma lista (mismos dicts), normalizada.

    Time: O(n) | Space: O(1) adicional
    """
    for fila in filas:
        if _tiene_asociado(fila):
            aplicar_reparto_default_asociado(fila)
            fila.pop(MARCA_REVISAR, None)
        else:
            _aplicar_reparto_default_casa(fila)
            fila[MARCA_REVISAR] = True
    return filas
