"""Guard transversal: ninguna sentencia SQL de la capa de datos se interpola.

`.langs/python.md` §5 prohibe construir SQL con f-strings o con `%`. Ese guard
ya existia, pero cableado a un solo modulo (`test_core_ventas.py::
test_ventas_sql_parametrizado`), de modo que reescribir con f-string un UPDATE
de `core_entregas` o de `core_semanas` dejaba la suite en verde. Aqui se aplica
a **todos** los modulos de datos, descubiertos por glob: un modulo nuevo queda
cubierto el dia que se crea, sin que nadie tenga que acordarse de anadirlo.

Que se prohibe y por que:

* `ast.JoinedStr`  -- f-string: `execute(f"UPDATE t SET c={v}")`.
* `ast.BinOp(Mod)` -- interpolacion con `%`: `execute("... %s" % v)`.

Que se permite como argumento SQL:

* `ast.Constant`  -- literal escrito a mano.
* `ast.Name`      -- constante de modulo, o un local ya armado a partir de
  constantes (el patron de `core_ventas`, que concatena marcadores `?`).
* `ast.Subscript` -- indexado de un diccionario de sentencias preconstruidas
  (el patron de `core_pagos`, que resuelve la tabla por whitelist).

Un `ast.BinOp(Add)` directo en el call-site tampoco pasa: concatenar en el
propio `execute` es la via mas comun de colar un dato en la sentencia.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent

#: Modulos que hablan SQL. `core.py` es fachada y no ejecuta nada, pero se
#: incluye igual: si alguna vez le apareciera un `execute`, seria justo el tipo
#: de regresion que este guard debe delatar.
MODULOS_DATOS: Final[tuple[Path, ...]] = tuple(
    sorted(RAIZ_PROYECTO.glob("core*.py")) + [RAIZ_PROYECTO / "db.py"]
)

_METODOS_SQL: Final[frozenset[str]] = frozenset(
    {"execute", "executemany", "executescript"}
)

#: Nodos admisibles como primer argumento de un `execute`.
_NODOS_SEGUROS: Final[tuple[type[ast.expr], ...]] = (
    ast.Constant,
    ast.Name,
    ast.Subscript,
)


def _llamadas_sql(arbol: ast.Module) -> list[ast.Call]:
    """Toda llamada a `execute`/`executemany`/`executescript` del modulo."""
    return [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in _METODOS_SQL
        and nodo.args
    ]


def test_hay_modulos_de_datos_que_auditar() -> None:
    """Si el glob dejara de encontrar modulos, el guard seria vacuo."""
    # Assert
    assert len(MODULOS_DATOS) >= 8
    assert all(ruta.exists() for ruta in MODULOS_DATOS)


@pytest.mark.parametrize("ruta", MODULOS_DATOS, ids=lambda p: p.name)
def test_sql_de_la_capa_de_datos_nunca_se_interpola(ruta: Path) -> None:
    """Ningun `execute` del modulo recibe SQL armado con f-string o `%`."""
    # Arrange
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    # Act
    llamadas = _llamadas_sql(arbol)

    # Assert
    for llamada in llamadas:
        sql = llamada.args[0]
        assert not isinstance(sql, ast.JoinedStr), (
            f"{ruta.name}:{llamada.lineno} arma SQL con f-string"
        )
        assert not (isinstance(sql, ast.BinOp) and isinstance(sql.op, ast.Mod)), (
            f"{ruta.name}:{llamada.lineno} arma SQL con '%'"
        )
        assert isinstance(sql, _NODOS_SEGUROS), (
            f"{ruta.name}:{llamada.lineno} pasa un SQL de tipo "
            f"{type(sql).__name__}, que no es auditable de forma estatica"
        )
