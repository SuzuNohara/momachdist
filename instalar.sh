#!/usr/bin/env bash
# Instala Inventario Betterware como un programa del sistema.
#
#   ./instalar.sh                  instala (o actualiza) para el usuario actual
#   ./instalar.sh --desinstalar    quita el programa, CONSERVANDO tus datos
#   ./instalar.sh --prefix RUTA    cambia el destino (por omision ~/.local)
#
# Instalacion por usuario: no pide sudo y no toca nada fuera de tu carpeta
# personal. Tras instalar, "Inventario Betterware" aparece en el menu de
# aplicaciones y tambien se puede lanzar desde la terminal con: momachdist
#
# Lo que se instala, con PREFIX = ~/.local:
#
#   ~/.local/share/momachdist/                  programa + su entorno virtual
#   ~/.local/share/momachdist/inventario.db     TUS DATOS (nunca se sobreescriben)
#   ~/.local/bin/momachdist                     lanzador de terminal
#   ~/.local/share/applications/                entrada del menu (.desktop)
#   ~/.local/share/icons/hicolor/*/apps/        icono en todos sus tamanos

set -euo pipefail

APP_ID="momachdist"
APP_NOMBRE="Inventario Betterware"
PREFIX="${HOME}/.local"
ACCION="instalar"

# Modulos de produccion. Lista explicita a proposito: los tests, los spikes y
# el codigo de referencia no forman parte del programa instalado.
MODULOS=(
    gui_inventario.py
    core.py core_comun.py core_productos.py core_asociados.py core_reparto.py
    core_pedidos.py core_existencias.py core_entregas.py core_clientes.py
    core_ventas.py core_historial.py core_pagos.py core_encargos.py
    core_semanas.py core_conversion.py
    db.py pdf_extractor.py backup.py export_excel.py
)

# Tamanos de icono que se instalan en el tema hicolor.
TAMANOS_ICONO=(16 24 32 48 64 128 256)

TAB=$'\t'

info()  { printf '  %s\n' "$*"; }
paso()  { printf '\n== %s\n' "$*"; }
fatal() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------
# Argumentos
# ----------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --desinstalar) ACCION="desinstalar"; shift ;;
        --prefix)
            [[ $# -ge 2 ]] || fatal "--prefix necesita una ruta."
            PREFIX="$2"; shift 2 ;;
        -h|--help)
            # Imprime el bloque de comentarios de la cabecera y se detiene en la
            # primera linea que ya no lo sea -- asi la ayuda no se desincroniza
            # con el archivo al editarlo.
            awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
            exit 0 ;;
        *) fatal "Opcion desconocida: $1 (usa --help)" ;;
    esac
done

ORIGEN="$(dirname "$(readlink -f "$0")")"

# Si el destino no es el de omision, cualquier instruccion que se le imprima a
# la usuaria tiene que repetir el --prefix, o apuntaria a otra instalacion.
SUFIJO_PREFIX=""
[[ "$PREFIX" != "${HOME}/.local" ]] && SUFIJO_PREFIX=" --prefix ${PREFIX}"

DESTINO="${PREFIX}/share/${APP_ID}"
LANZADOR="${PREFIX}/bin/${APP_ID}"
DESKTOP="${PREFIX}/share/applications/${APP_ID}.desktop"
ICONOS_BASE="${PREFIX}/share/icons/hicolor"

# ----------------------------------------------------------------------
# Desinstalacion
# ----------------------------------------------------------------------
if [[ "$ACCION" == "desinstalar" ]]; then
    paso "Desinstalando ${APP_NOMBRE}"

    for f in "$LANZADOR" "$DESKTOP"; do
        if [[ -e "$f" ]]; then rm "$f"; info "quitado $f"; fi
    done

    for n in "${TAMANOS_ICONO[@]}"; do
        icono="${ICONOS_BASE}/${n}x${n}/apps/${APP_ID}.png"
        if [[ -e "$icono" ]]; then rm "$icono"; info "quitado $icono"; fi
    done

    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "${PREFIX}/share/applications" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 &&
        gtk-update-icon-cache -f -t "$ICONOS_BASE" 2>/dev/null || true

    paso "Listo"
    if [[ -e "${DESTINO}/inventario.db" ]]; then
        cat <<FIN

  TUS DATOS NO SE BORRARON. Siguen en:

      ${DESTINO}/inventario.db
      ${DESTINO}/backups/

  Si de verdad quieres borrarlos -- y con ellos todo tu inventario,
  ventas e historial -- copialos primero a un lugar seguro y luego:

      rm -rf ${DESTINO}

FIN
    else
        info "No habia datos guardados en ${DESTINO}."
    fi
    exit 0
fi

# ----------------------------------------------------------------------
# Comprobaciones previas
# ----------------------------------------------------------------------
paso "Comprobando el sistema"

PY_SISTEMA="/usr/bin/python3"
[[ -x "$PY_SISTEMA" ]] || fatal "No existe ${PY_SISTEMA}. Instala Python 3: sudo apt install python3"

# El interprete se fija a /usr/bin/python3 a proposito, NO al 'python3' del
# PATH: si hay un Anaconda/conda en el PATH, su Tk viene compilado sin Xft y
# las letras de la aplicacion se ven pixeleadas. Ver README seccion 1.2.
info "interprete: ${PY_SISTEMA} ($("$PY_SISTEMA" -V 2>&1))"

"$PY_SISTEMA" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' ||
    fatal "Se necesita Python 3.12 o mayor."

"$PY_SISTEMA" -c 'import tkinter' 2>/dev/null ||
    fatal "Falta tkinter, que es un paquete del sistema (no de pip):
       sudo apt install python3-tk"

"$PY_SISTEMA" -c 'import venv' 2>/dev/null ||
    fatal "Falta el modulo venv:
       sudo apt install python3-venv"

if ! "$PY_SISTEMA" - <<'PY' 2>/dev/null
import subprocess, _tkinter, sys
salida = subprocess.run(["ldd", _tkinter.__file__], capture_output=True, text=True).stdout
sys.exit(0 if "libXft" in salida else 1)
PY
then
    info "AVISO: el Tk de ${PY_SISTEMA} no enlaza libXft; las letras podrian"
    info "       verse pixeleadas. La instalacion continua."
fi

for m in "${MODULOS[@]}"; do
    [[ -f "${ORIGEN}/${m}" ]] || fatal "Falta ${m} en ${ORIGEN}. Ejecuta este script desde la carpeta del proyecto."
done
[[ -f "${ORIGEN}/reference/db_schema.sql" ]] ||
    fatal "Falta reference/db_schema.sql, que el programa lee en cada arranque."
[[ -f "${ORIGEN}/packaging/${APP_ID}.desktop.in" ]] ||
    fatal "Falta packaging/${APP_ID}.desktop.in"
info "archivos del programa: completos"

# ----------------------------------------------------------------------
# Copia del programa
# ----------------------------------------------------------------------
if [[ -e "${DESTINO}/inventario.db" ]]; then
    paso "Actualizando la instalacion existente"
    info "se detectaron datos previos: inventario.db NO se toca"
else
    paso "Instalando en ${DESTINO}"
fi

mkdir -p "${DESTINO}/assets/icons" "${DESTINO}/reference" "${PREFIX}/bin" \
         "${PREFIX}/share/applications"

for m in "${MODULOS[@]}"; do
    install -m 644 "${ORIGEN}/${m}" "${DESTINO}/${m}"
done
info "${#MODULOS[@]} modulos copiados"

install -m 644 "${ORIGEN}/reference/db_schema.sql" "${DESTINO}/reference/db_schema.sql"
install -m 644 "${ORIGEN}/requirements.txt" "${DESTINO}/requirements.txt"
[[ -f "${ORIGEN}/LEEME_INSTRUCCIONES.txt" ]] &&
    install -m 644 "${ORIGEN}/LEEME_INSTRUCCIONES.txt" "${DESTINO}/LEEME_INSTRUCCIONES.txt"

# Los iconos van al programa (para la ventana) y al tema (para el menu).
for f in "${ORIGEN}"/assets/icons/*.png; do
    install -m 644 "$f" "${DESTINO}/assets/icons/$(basename "$f")"
done
info "iconos copiados al programa"

# ----------------------------------------------------------------------
# Entorno virtual y dependencias
# ----------------------------------------------------------------------
paso "Preparando el entorno virtual"
VENV="${DESTINO}/.venv"

if [[ -x "${VENV}/bin/python" ]] &&
   "${VENV}/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
    info "reusando ${VENV}"
else
    [[ -e "$VENV" ]] && mv "$VENV" "${VENV}-viejo-$(date +%Y%m%d-%H%M%S)"
    "$PY_SISTEMA" -m venv "$VENV"
    info "creado ${VENV}"
fi

info "instalando dependencias (necesita internet la primera vez)..."
if ! "${VENV}/bin/python" -m pip install --quiet --upgrade pip; then
    info "AVISO: no se pudo actualizar pip; se continua."
fi
"${VENV}/bin/python" -m pip install --quiet -r "${DESTINO}/requirements.txt" ||
    fatal "No se pudieron instalar las dependencias. Revisa tu conexion a internet."

"${VENV}/bin/python" -c 'import pdfplumber, openpyxl, tkinter' ||
    fatal "El entorno virtual quedo incompleto."
info "dependencias verificadas"

# ----------------------------------------------------------------------
# Lanzador de terminal
# ----------------------------------------------------------------------
paso "Instalando el lanzador"
cat > "$LANZADOR" <<FIN
#!/usr/bin/env bash
# Lanzador de ${APP_NOMBRE}. Generado por instalar.sh -- no editar a mano.
set -euo pipefail

DESTINO="${DESTINO}"

if [[ -z "\${DISPLAY:-}" && -z "\${WAYLAND_DISPLAY:-}" ]]; then
    echo "ERROR: ${APP_NOMBRE} necesita entorno grafico." >&2
    exit 1
fi

exec "\${DESTINO}/.venv/bin/python" "\${DESTINO}/gui_inventario.py" "\$@"
FIN
chmod 755 "$LANZADOR"
info "$LANZADOR"

if [[ ":${PATH}:" != *":${PREFIX}/bin:"* ]]; then
    info "AVISO: ${PREFIX}/bin no esta en tu PATH."
    info "       Agregalo con:  echo 'export PATH=\"${PREFIX}/bin:\$PATH\"' >> ~/.bashrc"
fi

# ----------------------------------------------------------------------
# Entrada del menu e iconos del tema
# ----------------------------------------------------------------------
paso "Registrando el programa en el escritorio"

sed "s|@EXEC@|${LANZADOR}|g" "${ORIGEN}/packaging/${APP_ID}.desktop.in" > "$DESKTOP"
chmod 644 "$DESKTOP"
info "$DESKTOP"

for n in "${TAMANOS_ICONO[@]}"; do
    origen_icono="${ORIGEN}/assets/icons/${APP_ID}-${n}.png"
    [[ -f "$origen_icono" ]] || continue
    mkdir -p "${ICONOS_BASE}/${n}x${n}/apps"
    install -m 644 "$origen_icono" "${ICONOS_BASE}/${n}x${n}/apps/${APP_ID}.png"
done
info "iconos instalados en el tema hicolor"

command -v update-desktop-database >/dev/null 2>&1 &&
    update-desktop-database "${PREFIX}/share/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 &&
    gtk-update-icon-cache -f -t "$ICONOS_BASE" 2>/dev/null || true

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$DESKTOP" && info "el .desktop pasa la validacion" ||
        info "AVISO: desktop-file-validate reporto observaciones (ver arriba)."
fi

# ----------------------------------------------------------------------
# Cierre
# ----------------------------------------------------------------------
paso "${APP_NOMBRE} quedo instalado"
cat <<FIN

  Para abrirlo:

    - Buscalo como "${APP_NOMBRE}" en tu menu de aplicaciones
      (si no aparece de inmediato, cierra y abre la sesion).
    - O desde la terminal:  ${APP_ID}

  Tus datos viven en:

    ${DESTINO}/inventario.db      la base con todo tu negocio
    ${DESTINO}/backups/           copias automaticas, una por arranque

  Respalda ese .db de vez en cuando a una USB o a tu Drive.

  Para desinstalar (sin borrar tus datos):

    ${ORIGEN}/instalar.sh --desinstalar${SUFIJO_PREFIX}

FIN
