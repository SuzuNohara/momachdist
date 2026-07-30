#!/usr/bin/env bash
# Lanzador de Inventario Betterware en Ubuntu.
#
# Uso:  ./ejecutar.sh
#
# Falla temprano y con un mensaje accionable si falta cualquiera de las tres
# piezas que la app necesita: el venv, tkinter (paquete del sistema) o las
# dependencias de runtime. Ver LEEME_INSTRUCCIONES.txt, Paso 1.

set -euo pipefail

# La app resuelve sus rutas contra el directorio del modulo (db.ruta_base()),
# pero el cwd sigue importando para rutas relativas: nos plantamos en la raiz
# del repo sin importar desde donde se invoque el script.
cd "$(dirname "$(readlink -f "$0")")"

PY="./.venv/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "ERROR: no existe el entorno virtual ($PY)." >&2
    echo >&2
    echo "Crealo con (nota el /usr/bin/ explicito, ver README 1.2):" >&2
    echo "    /usr/bin/python3 -m venv .venv" >&2
    echo "    ./.venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
fi

if ! "$PY" -c "import tkinter" 2>/dev/null; then
    echo "ERROR: falta tkinter, que es un paquete del sistema (no de pip)." >&2
    echo >&2
    echo "Instalalo con:" >&2
    echo "    sudo apt install python3-tk" >&2
    exit 1
fi

# Un Tk sin Xft no ve las fuentes de fontconfig y cae a fuentes X core sin
# antialiasing: la aplicacion arranca, pero se ve pixeleada. Le pasa al Python
# de Anaconda, que es facil de heredar del PATH al crear el venv. Se avisa y se
# continua: es un defecto de presentacion, no un impedimento.
if ! "$PY" - <<'PY' 2>/dev/null
import subprocess, _tkinter, sys
salida = subprocess.run(["ldd", _tkinter.__file__], capture_output=True, text=True).stdout
sys.exit(0 if "libXft" in salida else 1)
PY
then
    echo "AVISO: el Tk de este venv no enlaza libXft; las letras se veran" >&2
    echo "       pixeleadas. Recrea el venv con /usr/bin/python3 (README 1.2)." >&2
fi

if ! "$PY" -c "import pdfplumber, openpyxl" 2>/dev/null; then
    echo "ERROR: faltan dependencias de runtime." >&2
    echo >&2
    echo "Instalalas con:" >&2
    echo "    $PY -m pip install -r requirements.txt" >&2
    exit 1
fi

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    echo "ERROR: no hay entorno grafico (DISPLAY vacio)." >&2
    echo >&2
    echo "La app es tkinter y necesita pantalla. Abrela desde la computadora" >&2
    echo "donde vas a trabajar, o usa 'ssh -X', o un display virtual:" >&2
    echo "    xvfb-run -a $PY gui_inventario.py" >&2
    exit 1
fi

exec "$PY" gui_inventario.py
