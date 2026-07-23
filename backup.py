"""Capa de resiliencia: respaldo de `inventario.db` y bitacora de errores.

Mitiga el riesgo RT-5 ("perdida o corrupcion del `.db`"). Solo biblioteca
estandar (`shutil`, `logging`, `datetime`, `pathlib`).

Expone:

* `setup_logging()` -- configura un unico `FileHandler` sobre `inventario.log`.
* `backup_db()`     -- copia timestamped de la base dentro de `backups/`.
* `startup()`       -- punto de entrada unico del arranque: nunca propaga fallos.

La resolucion del directorio base se delega en `db.ruta_base()` (unica fuente
de verdad, soporta PyInstaller); este modulo nunca usa el directorio actual.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from db import ruta_base

logger: Final[logging.Logger] = logging.getLogger(__name__)

DB_FILENAME: Final[str] = "inventario.db"
LOG_FILENAME: Final[str] = "inventario.log"
BACKUPS_DIRNAME: Final[str] = "backups"
BACKUP_PREFIX: Final[str] = "inventario-"
BACKUP_SUFFIX: Final[str] = ".db"
TIMESTAMP_FMT: Final[str] = "%Y%m%d-%H%M%S"
LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def ruta_db() -> Path:
    """Ruta canonica de `inventario.db` bajo el directorio base (R6).

    Time: O(1) | Space: O(1)
    """
    return ruta_base() / DB_FILENAME


def ruta_log() -> Path:
    """Ruta canonica de `inventario.log` bajo el directorio base (R6).

    Time: O(1) | Space: O(1)
    """
    return ruta_base() / LOG_FILENAME


def setup_logging(log_path: str | os.PathLike[str]) -> None:
    """Configura el logger raiz con un unico `FileHandler` sobre `log_path`.

    Es idempotente (R5): si ya hay un `FileHandler` apuntando al mismo archivo
    resuelto, no agrega otro. Crea el directorio padre si falta.

    Args:
        log_path: destino del archivo de bitacora.

    Time: O(h) sobre el numero de handlers del logger raiz | Space: O(1)
    """
    destino = Path(log_path).resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)

    raiz = logging.getLogger()
    for handler in raiz.handlers:
        if isinstance(handler, logging.FileHandler) and (
            Path(handler.baseFilename) == destino
        ):
            return

    archivo = logging.FileHandler(destino, encoding="utf-8")
    archivo.setLevel(logging.INFO)
    archivo.setFormatter(logging.Formatter(LOG_FORMAT))
    raiz.addHandler(archivo)
    raiz.setLevel(logging.INFO)


def backup_db(
    db_path: str | os.PathLike[str],
    backups_dir: str | None = None,
) -> str | None:
    """Copia `db_path` a `backups/inventario-<YYYYMMDD-HHMMSS>.db`.

    Si `backups_dir` es `None` se resuelve como `backups/` junto al propio
    `db_path` -- nunca el directorio actual (R6). Si la base no existe devuelve
    `None` sin crear nada ni lanzar excepcion (R2).

    Args:
        db_path: ruta del archivo `.db` a respaldar.
        backups_dir: carpeta destino; `None` para la ubicacion por defecto.

    Returns:
        Ruta del respaldo creado, o `None` si la base no existia.

    Raises:
        OSError: si la copia o la creacion del directorio fallan.

    Time: O(n) sobre el tamano de la base | Space: O(1)
    """
    origen = Path(db_path)
    if not origen.is_file():
        logger.info("No existe %s; se omite el respaldo.", origen)
        return None

    carpeta = (
        Path(backups_dir)
        if backups_dir is not None
        else origen.parent / BACKUPS_DIRNAME
    )
    carpeta.mkdir(parents=True, exist_ok=True)

    marca = datetime.now(tz=UTC).astimezone().strftime(TIMESTAMP_FMT)
    destino = carpeta / f"{BACKUP_PREFIX}{marca}{BACKUP_SUFFIX}"
    shutil.copy2(origen, destino)
    logger.info("Respaldo creado en %s", destino)
    return str(destino)


def startup(
    db_path: str | os.PathLike[str],
    log_path: str | os.PathLike[str],
) -> str | None:
    """Punto de entrada unico del arranque: bitacora + respaldo tolerante.

    Configura el log (R5) y respalda la base (R1/R2/R6). **Ninguna** de las dos
    etapas puede propagar una excepcion al llamador: el arranque de la GUI nunca
    se bloquea (R3). Las dos etapas se protegen por separado porque el fallo de
    la primera deja al proceso sin bitacora en archivo.

    Si `setup_logging` falla no hay `FileHandler` donde registrar el fallo: el
    registro se emite igualmente por el logger, que degrada a los handlers que
    sigan presentes o, si no hay ninguno, al `logging.lastResort` de la
    biblioteca estandar (stderr). Nunca se descarta en silencio.

    Args:
        db_path: ruta de `inventario.db`.
        log_path: ruta de `inventario.log`.

    Returns:
        Ruta del respaldo creado, o `None` si se omitio o fallo.

    Time: O(n) sobre el tamano de la base | Space: O(1)
    """
    try:
        setup_logging(log_path)
    except Exception:  # noqa: BLE001 -- R3: el arranque nunca debe bloquearse
        logger.exception(
            "No se pudo configurar la bitacora en %s; la aplicacion continua",
            log_path,
        )

    try:
        return backup_db(db_path)
    except Exception:  # noqa: BLE001 -- R3: el arranque nunca debe bloquearse
        logger.exception("Fallo el respaldo de arranque; la aplicacion continua")
        return None
