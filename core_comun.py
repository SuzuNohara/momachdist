"""Base compartida de la capa core: error de dominio y coercion de valores.

Este modulo es la raiz del grafo de imports de la capa de servicios: no importa
nada de `core_productos`, `core_pedidos` ni `core`, de modo que ambos dominios
pueden apoyarse en el sin riesgo de ciclos.

Contenido:

* `CoreError` -- error de dominio base del que heredan los demas.
* `_texto` / `_es_cero` / `_entero` / `_real` -- normalizacion de los valores
  crudos que llegan del extractor de PDF (y de la vista previa editable).
"""

from __future__ import annotations

from typing import Any


class CoreError(Exception):
    """Error base de dominio de la capa de servicios."""


def _texto(valor: Any) -> str:
    """Normaliza `valor` a texto sin espacios en los extremos (R4).

    Time: O(n) sobre la longitud del texto | Space: O(n)
    """
    if valor is None:
        return ""
    return str(valor).strip()


def _es_cero(valor: Any) -> bool:
    """Indica si `valor` representa exactamente el numero cero.

    Valores ausentes, booleanos o no numericos no cuentan como cero: solo un
    0 real (entero, flotante o su texto) satisface la condicion de R5.

    Time: O(1) | Space: O(1)
    """
    if valor is None or isinstance(valor, bool):
        return False
    if isinstance(valor, (int, float)):
        return float(valor) == 0.0
    try:
        return float(str(valor).strip()) == 0.0
    except ValueError:
        return False


def _entero(valor: Any, defecto: int = 0) -> int:
    """Convierte `valor` a entero replicando la tolerancia de `_clean_int`.

    Devuelve `defecto` cuando el valor esta vacio o no es numerico, para que la
    vista previa editable de la GUI no rompa la carga con una celda en blanco.

    Time: O(n) sobre la longitud del texto | Space: O(1)
    """
    texto = _texto(valor)
    if not texto:
        return defecto
    try:
        return int(float(texto))
    except ValueError:
        return defecto


def _real(valor: Any) -> float:
    """Convierte `valor` a flotante replicando la tolerancia de `_clean_money`.

    Time: O(n) sobre la longitud del texto | Space: O(n)
    """
    texto = _texto(valor).replace("$", "").replace(",", "")
    try:
        return float(texto)
    except ValueError:
        return 0.0
