"""Guards del empaquetado: iconos, entrada de menu y script de instalacion.

`instalar.sh` copia una **lista explicita** de modulos de produccion. Esa lista
es exactamente el tipo de cosa que se queda atras en silencio: el dia que
aparezca un `core_*.py` nuevo, la suite entera seguira en verde y el programa
instalado reventara con `ModuleNotFoundError` en la maquina de la usuaria --
nunca en la del desarrollo, donde el archivo si existe. Es el mismo patron que
ya mordio cuatro veces con `core.X` inexistente (ver `test_gui_cableado.py`),
sólo que desplazado al artefacto instalado.

Por eso los modulos se **descubren por glob** y se comparan contra la lista del
script: agregar un modulo de dominio y olvidarse del instalador rompe la suite
en el acto.

Lo mismo vale para el icono. `gui_inventario.ICONOS_VENTANA` nombra archivos por
cadena; si uno se renombra o se deja de generar, `_aplicar_icono` no falla --
esta escrito para no abortar el arranque -- y la ventana se queda con el icono
generico sin que nadie se entere. Aqui se comprueba que cada nombre declarado
exista de verdad.

Las medidas de los PNG se leen del encabezado IHDR con `struct`, sin depender de
Pillow: es una dependencia transitiva de `pdfplumber`, no una que el proyecto
declare para los tests.
"""

from __future__ import annotations

import os
import re
import stat
import struct
import subprocess
from pathlib import Path
from typing import Final

import pytest

import gui_inventario

RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
ICONOS_DIR: Final[Path] = RAIZ_PROYECTO / "assets" / "icons"
INSTALADOR: Final[Path] = RAIZ_PROYECTO / "instalar.sh"
DESKTOP_IN: Final[Path] = RAIZ_PROYECTO / "packaging" / "momachdist.desktop.in"

#: Archivos `.py` de la raiz que NO son modulos de produccion y por lo tanto no
#: deben viajar en la instalacion.
NO_PRODUCCION: Final[frozenset[str]] = frozenset({"conftest.py", "test_db.py"})

#: Claves que el escritorio necesita para mostrar la aplicacion en el menu.
CLAVES_DESKTOP_OBLIGATORIAS: Final[tuple[str, ...]] = (
    "Type",
    "Name",
    "Exec",
    "Icon",
    "Terminal",
    "Categories",
    "StartupWMClass",
)

_PNG_FIRMA: Final[bytes] = b"\x89PNG\r\n\x1a\n"


def medidas_png(ruta: Path) -> tuple[int, int]:
    """Ancho y alto de un PNG, leidos del IHDR sin decodificar la imagen.

    Args:
        ruta: archivo PNG a medir.

    Returns:
        Tupla `(ancho, alto)` en pixeles.

    Raises:
        AssertionError: si el archivo no es un PNG valido.

    Time: O(1) | Space: O(1)
    """
    cabecera = ruta.read_bytes()[:24]
    assert cabecera[:8] == _PNG_FIRMA, f"{ruta.name} no es un PNG"
    assert cabecera[12:16] == b"IHDR", f"{ruta.name} no tiene un IHDR valido"
    ancho, alto = struct.unpack(">II", cabecera[16:24])
    return ancho, alto


def modulos_declarados_en_instalador() -> tuple[str, ...]:
    """Nombres del arreglo `MODULOS=( ... )` de `instalar.sh`.

    Time: O(n) sobre el tamano del script | Space: O(m) sobre los modulos
    """
    texto = INSTALADOR.read_text(encoding="utf-8")
    bloque = re.search(r"^MODULOS=\((.*?)^\)", texto, re.MULTILINE | re.DOTALL)
    assert bloque is not None, "no se encontro el arreglo MODULOS en instalar.sh"
    sin_comentarios = re.sub(r"#[^\n]*", "", bloque.group(1))
    return tuple(sorted(sin_comentarios.split()))


def modulos_de_produccion() -> tuple[str, ...]:
    """Modulos `.py` de la raiz que forman el programa, descubiertos por glob.

    Time: O(n) sobre los archivos de la raiz | Space: O(n)
    """
    return tuple(
        sorted(
            p.name
            for p in RAIZ_PROYECTO.glob("*.py")
            if p.name not in NO_PRODUCCION
        )
    )


# ----------------------------------------------------------------------
# Iconos
# ----------------------------------------------------------------------

def test_existen_los_iconos_que_la_gui_declara() -> None:
    """Cada nombre de `ICONOS_VENTANA` corresponde a un archivo real."""
    faltantes = [
        nombre
        for nombre in gui_inventario.ICONOS_VENTANA
        if not (ICONOS_DIR / nombre).is_file()
    ]

    assert not faltantes, (
        f"gui_inventario.ICONOS_VENTANA nombra iconos que no existen en "
        f"{ICONOS_DIR}: {faltantes}. La ventana caeria al icono generico sin "
        f"fallar, porque _aplicar_icono no aborta el arranque a proposito."
    )


def test_la_gui_apunta_a_la_carpeta_de_iconos_instalada() -> None:
    """`ICONOS_DIR` de la GUI resuelve dentro del arbol del programa."""
    esperado = os.path.join(str(RAIZ_PROYECTO), "assets", "icons")

    assert gui_inventario.ICONOS_DIR == esperado


@pytest.mark.parametrize("tamano", (16, 24, 32, 48, 64, 128, 256))
def test_cada_icono_mide_exactamente_su_tamano_nominal(tamano: int) -> None:
    """`momachdist-48.png` mide 48x48. Un tema de iconos lo exige."""
    ruta = ICONOS_DIR / f"momachdist-{tamano}.png"
    assert ruta.is_file(), f"falta {ruta.name}"

    assert medidas_png(ruta) == (tamano, tamano)


def test_el_icono_maestro_es_cuadrado() -> None:
    """El maestro sin sufijo alimenta a `iconphoto` y debe ser cuadrado.

    El arte de origen (`assets/momachdist_logo.png`) tiene margenes
    transparentes y no es cuadrado; el maestro se genera recortando al contenido
    visible y encuadrando. Si alguien lo regenera mal, el escritorio deforma o
    encaja mal el icono.
    """
    ancho, alto = medidas_png(ICONOS_DIR / "momachdist.png")

    assert ancho == alto, f"el icono maestro mide {ancho}x{alto}, no es cuadrado"


# ----------------------------------------------------------------------
# Entrada de menu
# ----------------------------------------------------------------------

def test_el_desktop_declara_las_claves_obligatorias() -> None:
    """La plantilla `.desktop` trae todo lo que el menu necesita."""
    texto = DESKTOP_IN.read_text(encoding="utf-8")
    claves = {
        linea.split("=", 1)[0]
        for linea in texto.splitlines()
        if "=" in linea and not linea.startswith("[")
    }

    faltantes = [c for c in CLAVES_DESKTOP_OBLIGATORIAS if c not in claves]
    assert not faltantes, f"claves ausentes en el .desktop: {faltantes}"
    assert texto.startswith("[Desktop Entry]")


def test_el_wm_class_del_desktop_coincide_con_el_de_la_ventana() -> None:
    """`StartupWMClass` tiene que ser identico al `className` de la ventana.

    Si divergen, el escritorio no asocia la ventana abierta con su lanzador: la
    muestra como una entrada aparte y con icono generico. Nada falla, sólo se ve
    mal -- por eso hace falta un test.
    """
    texto = DESKTOP_IN.read_text(encoding="utf-8")
    declarado = re.search(r"^StartupWMClass=(.+)$", texto, re.MULTILINE)
    assert declarado is not None

    assert declarado.group(1).strip() == gui_inventario.WM_CLASS


def test_el_desktop_referencia_el_icono_por_nombre_de_tema() -> None:
    """`Icon=` usa el nombre del tema, no una ruta absoluta.

    El instalador copia los PNG a `hicolor/<n>x<n>/apps/momachdist.png`; el
    nombre sin extension es lo que el escritorio resuelve al tamano que
    necesite. Una ruta fija dejaria un solo tamano disponible.
    """
    texto = DESKTOP_IN.read_text(encoding="utf-8")
    icono = re.search(r"^Icon=(.+)$", texto, re.MULTILINE)
    assert icono is not None

    valor = icono.group(1).strip()
    assert valor == "momachdist", f"Icon={valor!r} deberia ser el nombre de tema"
    assert not valor.endswith(".png")


def test_el_exec_del_desktop_queda_como_marcador() -> None:
    """`Exec` y `TryExec` los resuelve el instalador con la ruta real."""
    texto = DESKTOP_IN.read_text(encoding="utf-8")

    assert "Exec=@EXEC@" in texto
    assert "TryExec=@EXEC@" in texto


# ----------------------------------------------------------------------
# Script de instalacion
# ----------------------------------------------------------------------

def test_el_instalador_es_ejecutable() -> None:
    """Sin bit de ejecucion, `./instalar.sh` no corre."""
    assert INSTALADOR.is_file()

    assert INSTALADOR.stat().st_mode & stat.S_IXUSR, "falta el bit de ejecucion"


def test_el_instalador_tiene_sintaxis_valida() -> None:
    """`bash -n` sobre el script: un error de sintaxis se ve al instalar."""
    resultado = subprocess.run(
        ["bash", "-n", str(INSTALADOR)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert resultado.returncode == 0, resultado.stderr


def test_el_instalador_copia_todos_los_modulos_de_produccion() -> None:
    """El arreglo `MODULOS` cubre exactamente los modulos de la raiz.

    Este es el guard central del empaquetado: un modulo de dominio nuevo que no
    se agregue aqui produce un `ModuleNotFoundError` sólo en la maquina donde se
    instalo, nunca en la de desarrollo.
    """
    declarados = modulos_declarados_en_instalador()
    reales = modulos_de_produccion()

    sin_copiar = sorted(set(reales) - set(declarados))
    inexistentes = sorted(set(declarados) - set(reales))

    assert not sin_copiar, (
        f"instalar.sh no copia estos modulos de produccion: {sin_copiar}. "
        f"Agregalos al arreglo MODULOS."
    )
    assert not inexistentes, (
        f"instalar.sh dice copiar modulos que ya no existen: {inexistentes}."
    )


def test_el_instalador_no_copia_tests_ni_referencia() -> None:
    """Los tests y el codigo viejo de Excel no son parte del programa."""
    declarados = modulos_declarados_en_instalador()

    for excluido in NO_PRODUCCION:
        assert excluido not in declarados


def test_el_instalador_instala_los_iconos_que_existen() -> None:
    """Cada tamano de `TAMANOS_ICONO` tiene su PNG generado."""
    texto = INSTALADOR.read_text(encoding="utf-8")
    bloque = re.search(r"^TAMANOS_ICONO=\(([^)]*)\)", texto, re.MULTILINE)
    assert bloque is not None, "no se encontro TAMANOS_ICONO en instalar.sh"

    faltantes = [
        n for n in bloque.group(1).split()
        if not (ICONOS_DIR / f"momachdist-{n}.png").is_file()
    ]

    assert not faltantes, f"instalar.sh instalaria iconos inexistentes: {faltantes}"


def test_el_instalador_fija_el_interprete_del_sistema() -> None:
    """No debe usar el `python3` del PATH.

    Con Anaconda o conda en el PATH, `python3` resuelve a un interprete cuyo Tk
    esta compilado sin Xft: la aplicacion arranca, pero las letras se ven
    pixeleadas porque Tk cae a fuentes X core sin antialiasing. Fijar
    `/usr/bin/python3` es la unica forma de evitarlo desde el instalador.
    """
    texto = INSTALADOR.read_text(encoding="utf-8")

    assert 'PY_SISTEMA="/usr/bin/python3"' in texto


def test_el_instalador_no_borra_datos_al_desinstalar() -> None:
    """La desinstalacion nunca debe llevarse `inventario.db` por delante.

    Un `rm -rf` del directorio de instalacion borraria la base con todo el
    negocio. El script sólo puede quitar lanzador, `.desktop` e iconos; el
    borrado de datos se le indica a la usuaria, no se ejecuta.
    """
    texto = INSTALADOR.read_text(encoding="utf-8")
    bloque = re.search(
        r'if \[\[ "\$ACCION" == "desinstalar" \]\]; then(.*?)^fi',
        texto,
        re.MULTILINE | re.DOTALL,
    )
    assert bloque is not None, "no se encontro la rama de desinstalacion"

    ejecutados = re.findall(r"^\s*(?:rm|mv)\s.*$", bloque.group(1), re.MULTILINE)
    for linea in ejecutados:
        assert "-rf" not in linea, f"borrado recursivo en la desinstalacion: {linea!r}"
        assert "inventario.db" not in linea, f"la desinstalacion toca la base: {linea!r}"
