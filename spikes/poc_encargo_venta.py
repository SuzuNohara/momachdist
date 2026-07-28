"""Spike de de-risking de RT-2: conversion `encargo -> venta` (ENC-01).

Codigo **desechable**. Demuestra, contra el esquema real, que la conversion se
puede hacer sin (a) descontar el stock dos veces, (b) perder o duplicar el
anticipo, ni (c) dejar un commit parcial. Hallazgos y veredicto GO/NO-GO en
`spikes/FINDINGS_encargo_venta.md`. Standalone: `python spikes/poc_...venta.py`.

Desviacion D7 (aprobada): la Spec mandaba una BD auto-contenida "porque CLI-02
y FUND-02 aun no existen"; ya existen, asi que el POC levanta el **esquema real**
con `db.init_db(":memory:")` y reusa `core_ventas._validar_stock_canasta`.
Trampa de `vw_existencias`: `piezas_recibidas` suma **solo** `cantidad_casa +
cantidad_local`, asi que sembrar sin esas columnas da 0 disponibles.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Final

#: Bootstrap de `sys.path` **exclusivo del spike**: como script standalone
#: (T8/R11) `sys.path[0]` es `spikes/`, no la raiz. Bajo pytest es un no-op.
_RAIZ_PROYECTO: Final[Path] = Path(__file__).resolve().parent.parent
if str(_RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(_RAIZ_PROYECTO))

import core_ventas  # noqa: E402  (despues del bootstrap de sys.path)
import db  # noqa: E402
from core_ventas import VentaError  # noqa: E402

logger: Final[logging.Logger] = logging.getLogger(__name__)

STATUS_ENTREGADO: Final[str] = "Entregado"
STATUS_CANCELADO: Final[str] = "Cancelado"
STATUS_PENDIENTE: Final[str] = "Pendiente"
TIPO_NORMAL: Final[str] = "Normal (con descuento)"

_MSG_INEXISTENTE: Final[str] = "El encargo {id} no existe."
_MSG_VACIO: Final[str] = "El encargo {id} no tiene lineas que convertir."
_MSG_CANCELADO: Final[str] = "El encargo {id} esta cancelado."
_MSG_YA_CONVERTIDO: Final[str] = (
    "El encargo {id} ya se convirtio en la venta {venta}: convertirlo otra vez "
    "descontaria el stock dos veces.")

# --- SQL: todo parametrizado, ni una f-string ni un `%` en ninguna sentencia.
_SQL_INS_PEDIDO_DET: Final[str] = (
    "INSERT INTO pedido_detalle (pedido_id, codigo_articulo, ocurrencia, "
    "cantidad_solicitada, cantidad_surtida, cantidad_asociado, cantidad_casa, "
    "cantidad_local, precio_que_pagas, valor_total_con_iva, tipo) "
    "VALUES (?, ?, 1, ?, ?, 0, ?, 0, ?, ?, ?)")
_SQL_INS_ENCARGO: Final[str] = (
    "INSERT INTO encargos (cliente_id, observaciones) VALUES (?, ?)")
_SQL_INS_ENCARGO_DET: Final[str] = (
    "INSERT INTO encargo_detalle (encargo_id, codigo_articulo, "
    "cantidad_solicitada, precio_estimado) VALUES (?, ?, ?, ?)")
_SQL_INS_ENCARGO_PAGO: Final[str] = (
    "INSERT INTO encargo_pagos (encargo_id, forma_pago, monto) VALUES (?, ?, ?)")
_SQL_CABECERA: Final[str] = (
    "SELECT id, cliente_id, status, venta_id FROM encargos WHERE id = ?")
_SQL_LINEAS: Final[str] = (
    "SELECT codigo_articulo, cantidad_solicitada, precio_estimado "
    "FROM encargo_detalle WHERE encargo_id = ? ORDER BY id")
_SQL_PAGOS: Final[str] = (
    "SELECT forma_pago, monto, fecha_pago FROM encargo_pagos "
    "WHERE encargo_id = ? ORDER BY id")
_SQL_INS_VENTA: Final[str] = (
    "INSERT INTO ventas (cliente_id, observaciones) VALUES (?, ?)")
_SQL_INS_VENTA_DET: Final[str] = (
    "INSERT INTO venta_detalle (venta_id, codigo_articulo, cantidad, "
    "precio_costo, precio_publico, total, ganancia) VALUES (?, ?, ?, ?, ?, ?, ?)")
_SQL_INS_VENTA_PAGO: Final[str] = (
    "INSERT INTO venta_pagos (venta_id, forma_pago, monto, fecha_pago) "
    "VALUES (?, ?, ?, ?)")
_SQL_CERRAR_ENCARGO: Final[str] = (
    "UPDATE encargos SET venta_id = ?, status = ? WHERE id = ?")
_SQL_SOLICITADO: Final[str] = (
    "SELECT COALESCE(SUM(cantidad_solicitada), 0) FROM encargo_detalle "
    "WHERE encargo_id = ?")
_SQL_VENDIDO_DETALLE: Final[str] = (
    "SELECT COALESCE(SUM(cantidad), 0) FROM venta_detalle WHERE venta_id = ?")
_SQL_VENDIDO_VISTA: Final[str] = (
    "SELECT COALESCE(SUM(piezas_vendidas), 0) FROM vw_existencias "
    "WHERE codigo_articulo IN "
    "(SELECT codigo_articulo FROM encargo_detalle WHERE encargo_id = ?)")
_SQL_ANTICIPO: Final[str] = (
    "SELECT COALESCE(SUM(monto), 0) FROM encargo_pagos WHERE encargo_id = ?")
_SQL_PAGOS_VENTA: Final[str] = (
    "SELECT COALESCE(SUM(monto), 0) FROM venta_pagos WHERE venta_id = ?")


def nueva_bd() -> sqlite3.Connection:
    """Esquema **real** en memoria: canonico + encargos + ADR-6 (R1, D7).

    Time: O(n) sobre el esquema | Space: O(n)
    """
    return db.init_db(":memory:")


def _seed_stock(conn: sqlite3.Connection, codigo: str, descripcion: str,
                disponibles: int, costo: float) -> None:
    """Deja `disponibles` piezas del `codigo` visibles en `vw_existencias`.

    Reparto **entero a casa** (`cantidad_asociado = 0`): la vista excluye lo
    entregado al asociado, y el reparto por defecto de MERC-03 daria 0
    disponibles. `costo` es unitario; la tabla guarda el total de la linea.
    Time: O(1) | Space: O(1)
    """
    costo_total = costo * disponibles
    conn.execute("INSERT INTO productos (codigo_articulo, descripcion) "
                 "VALUES (?, ?)", (codigo, descripcion))
    cursor = conn.execute(
        "INSERT INTO pedidos (folio_pedido) VALUES (?)", ("POC-" + codigo,))
    conn.execute(_SQL_INS_PEDIDO_DET, (
        int(cursor.lastrowid or 0), codigo, disponibles, disponibles,
        disponibles, costo_total, costo_total * 1.5, TIPO_NORMAL))
    conn.commit()


def _seed_cliente(conn: sqlite3.Connection, nombre: str) -> int:
    """Alta de cliente (`encargos.cliente_id` es NOT NULL con RESTRICT).

    Time: O(1) | Space: O(1)
    """
    cursor = conn.execute("INSERT INTO clientes (nombre) VALUES (?)", (nombre,))
    conn.commit()
    return int(cursor.lastrowid or 0)


def _seed_encargo(conn: sqlite3.Connection, cliente_id: int,
                  lineas: list[dict[str, Any]],
                  anticipos: list[dict[str, Any]] | None = None) -> int:
    """Crea un encargo `Pendiente` con sus lineas y sus anticipos opcionales.

    `lineas` = `[{codigo, cantidad, precio}, ...]`;
    `anticipos` = `[{forma_pago, monto}, ...]` o `None`.
    Time: O(n + m) | Space: O(n + m)
    """
    with conn:
        cursor = conn.execute(_SQL_INS_ENCARGO, (cliente_id, "encargo POC"))
        encargo_id = int(cursor.lastrowid or 0)
        conn.executemany(_SQL_INS_ENCARGO_DET, [
            (encargo_id, l["codigo"], int(l["cantidad"]), float(l["precio"]))
            for l in lineas])
        conn.executemany(_SQL_INS_ENCARGO_PAGO, [
            (encargo_id, p["forma_pago"], float(p["monto"]))
            for p in (anticipos or [])])
    return encargo_id

def _leer_cabecera(conn: sqlite3.Connection, encargo_id: int) -> sqlite3.Row:
    """Lee el encargo y rechaza los estados que no se pueden convertir.

    El guarda de `venta_id` es lo que impide el **doble descuento**: sin el,
    reconvertir el mismo encargo generaria una segunda venta con su detalle y
    `vw_existencias` descontaria dos veces.
    Raises: `VentaError` si no existe, ya tiene venta, o esta cancelado.
    Time: O(1) | Space: O(1)
    """
    fila = conn.execute(_SQL_CABECERA, (encargo_id,)).fetchone()
    if fila is None:
        raise VentaError(_MSG_INEXISTENTE.format(id=encargo_id))
    if fila["venta_id"] is not None:
        raise VentaError(
            _MSG_YA_CONVERTIDO.format(id=encargo_id, venta=fila["venta_id"]))
    if fila["status"] == STATUS_CANCELADO:
        raise VentaError(_MSG_CANCELADO.format(id=encargo_id))
    return fila


def _leer_lineas(conn: sqlite3.Connection, encargo_id: int) -> list[dict[str, Any]]:
    """Traduce `encargo_detalle` a la canasta que espera CLI-02, en 1 consulta.

    `precio_estimado` pasa a ser el `precio_publico` de la venta.
    Time: O(n) | Space: O(n)
    """
    filas = conn.execute(_SQL_LINEAS, (encargo_id,)).fetchall()
    return [{"codigo": str(f["codigo_articulo"]),
             "cantidad": int(f["cantidad_solicitada"]),
             "precio_publico": float(f["precio_estimado"])} for f in filas]


def _calcular_lineas(lineas: list[dict[str, Any]],
                     datos: dict[str, dict]) -> list[dict[str, Any]]:
    """Aplica `core_ventas._calcular_linea` a cada linea del encargo (R8).

    El `precio_costo` sale de `vw_existencias` al convertir, no al encargar: el
    costo se congela al vender.
    Time: O(n) | Space: O(n)
    """
    calculadas: list[dict[str, Any]] = []
    for linea in lineas:
        info = datos[str(linea["codigo"])]
        calculo = core_ventas._calcular_linea(
            int(linea["cantidad"]), float(linea["precio_publico"]),
            float(info["precio_costo"]))
        calculadas.append({"codigo": str(linea["codigo"]),
                           "descripcion": info["descripcion"], **calculo})
    return calculadas


def _insertar_venta(conn: sqlite3.Connection, cliente_id: int, encargo_id: int,
                    calculadas: list[dict[str, Any]]) -> int:
    """Inserta 1 `ventas` + N `venta_detalle`. **Sin transaccion propia.**

    No abre `with conn:` a proposito: el llamador ya esta dentro de una. Es la
    razon exacta por la que no se puede componer `core_ventas.registrar_venta`,
    cuyo `_insertar_venta` abre y **cierra** la suya (FINDINGS, hallazgo H1).
    Time: O(n) | Space: O(n)
    """
    cursor = conn.execute(
        _SQL_INS_VENTA, (cliente_id, "Conversion de encargo " + str(encargo_id)))
    venta_id = int(cursor.lastrowid or 0)
    conn.executemany(_SQL_INS_VENTA_DET, [
        (venta_id, l["codigo"], l["cantidad"], l["precio_costo"],
         l["precio_publico"], l["total"], l["ganancia"]) for l in calculadas])
    return venta_id


def _traspasar_pagos(conn: sqlite3.Connection, encargo_id: int,
                     venta_id: int) -> float:
    """Copia cada `encargo_pagos` a `venta_pagos` **1:1** y devuelve el total.

    Se preservan `forma_pago`, `monto` y `fecha_pago` fila por fila: consolidar
    en un solo pago perderia forma y fecha. Los `encargo_pagos` **no se borran**:
    son el historial del encargo y borrarlos romperia la conservacion.
    Time: O(m) | Space: O(m)
    """
    pagos = conn.execute(_SQL_PAGOS, (encargo_id,)).fetchall()
    conn.executemany(_SQL_INS_VENTA_PAGO, [
        (venta_id, p["forma_pago"], float(p["monto"]), p["fecha_pago"])
        for p in pagos])
    return round(sum((float(p["monto"]) for p in pagos), 0.0), 2)


def convertir_encargo_a_venta(conn: sqlite3.Connection,
                              encargo_id: int) -> dict[str, Any]:
    """Convierte un encargo en venta en cuatro pasos (R2, R11).

    (1) validar stock **antes de escribir nada**, reusando
    `core_ventas._validar_stock_canasta`; y dentro de un unico `with conn:`,
    (2) insertar `ventas` + `venta_detalle`, (3) traspasar `encargo_pagos` ->
    `venta_pagos` 1:1, (4) fijar `encargos.venta_id` y `status`. Los pasos 2-4
    son **una sola transaccion**: si (3) o (4) fallan, la venta tampoco queda
    escrita. El paso 1 va fuera para que el rechazo por stock no abra ninguna.
    Returns: `{encargo_id, venta_id, cliente_id, total, anticipo, saldo,
        num_lineas, status}`.
    Raises: `VentaError` -- encargo inexistente/vacio/ya convertido/cancelado,
        stock insuficiente, codigo fuera del inventario, o fallo de escritura
        (con rollback completo).
    Time: O(n + m + k log p) | Space: O(n + m)
    """
    cabecera = _leer_cabecera(conn, encargo_id)
    lineas = _leer_lineas(conn, encargo_id)
    if not lineas:
        raise VentaError(_MSG_VACIO.format(id=encargo_id))

    calculadas = _calcular_lineas(
        lineas, core_ventas._validar_stock_canasta(conn, lineas))
    cliente_id = int(cabecera["cliente_id"])
    try:
        with conn:
            venta_id = _insertar_venta(conn, cliente_id, encargo_id, calculadas)
            anticipo = _traspasar_pagos(conn, encargo_id, venta_id)
            conn.execute(_SQL_CERRAR_ENCARGO,
                         (venta_id, STATUS_ENTREGADO, encargo_id))
    except sqlite3.Error as exc:
        raise VentaError(f"No se pudo convertir el encargo {encargo_id}: {exc}") from exc

    total = round(sum(float(l["total"]) for l in calculadas), 2)
    return {"encargo_id": encargo_id, "venta_id": venta_id,
            "cliente_id": cliente_id, "total": total, "anticipo": anticipo,
            "saldo": round(total - anticipo, 2), "num_lineas": len(calculadas),
            "status": STATUS_ENTREGADO}

def _escalar(conn: sqlite3.Connection, sql: str, parametro: int) -> float:
    """Agregado de un solo valor; `sql` siempre es una constante `_SQL_*`.

    Time: O(n) sobre las filas agregadas | Space: O(1)
    """
    return float(conn.execute(sql, (parametro,)).fetchone()[0])


def _resumen_riesgos(conn: sqlite3.Connection, encargo_id: int,
                     venta_id: int) -> dict[str, Any]:
    """Mide los dos riesgos transversales de RT-2 tras una conversion (R7, R8).

    Doble descuento: lo solicitado, lo escrito en `venta_detalle` y lo que
    `vw_existencias` reporta como vendido deben coincidir; una segunda
    conversion inflaria los dos ultimos. Anticipo conservado:
    `SUM(encargo_pagos) == SUM(venta_pagos)`, ni perdido ni duplicado.
    Numero fijo de consultas agregadas: sin N+1.
    Time: O(n) | Space: O(1)
    """
    solicitado = int(_escalar(conn, _SQL_SOLICITADO, encargo_id))
    vendido_detalle = int(_escalar(conn, _SQL_VENDIDO_DETALLE, venta_id))
    vendido_vista = int(_escalar(conn, _SQL_VENDIDO_VISTA, encargo_id))
    anticipo = round(_escalar(conn, _SQL_ANTICIPO, encargo_id), 2)
    pagado = round(_escalar(conn, _SQL_PAGOS_VENTA, venta_id), 2)
    return {"solicitado": solicitado, "vendido_detalle": vendido_detalle,
            "vendido_vista": vendido_vista, "anticipo_encargo": anticipo,
            "pagos_venta": pagado,
            "descuento_unico": solicitado == vendido_detalle == vendido_vista,
            "anticipo_conservado": anticipo == pagado}

def caso_sin_anticipo() -> dict[str, Any]:
    """Caso 1 (R4): encargo sin anticipo -> venta con 0 `venta_pagos`.

    Time: O(1) | Space: O(1)
    """
    conn = nueva_bd()
    try:
        _seed_stock(conn, "11111", "Sarten 24cm", 10, 100.0)
        encargo_id = _seed_encargo(
            conn, _seed_cliente(conn, "Ana"),
            [{"codigo": "11111", "cantidad": 2, "precio": 180.0}])
        resultado = convertir_encargo_a_venta(conn, encargo_id)
        riesgos = _resumen_riesgos(conn, encargo_id, resultado["venta_id"])
        ok = (resultado["anticipo"] == 0.0
              and resultado["saldo"] == resultado["total"]
              and riesgos["pagos_venta"] == 0.0 and riesgos["descuento_unico"])
        return {"caso": "sin anticipo", "ok": ok,
                "resultado": resultado, "riesgos": riesgos}
    finally:
        conn.close()


def caso_anticipo_parcial() -> dict[str, Any]:
    """Caso 2 (R5, R7): anticipo parcial -> saldo > 0 y conservacion 1:1.

    Time: O(1) | Space: O(1)
    """
    conn = nueva_bd()
    try:
        _seed_stock(conn, "22222", "Olla 5L", 8, 200.0)
        encargo_id = _seed_encargo(
            conn, _seed_cliente(conn, "Beto"),
            [{"codigo": "22222", "cantidad": 3, "precio": 300.0}],
            [{"forma_pago": "Transferencia", "monto": 400.0}])
        resultado = convertir_encargo_a_venta(conn, encargo_id)
        riesgos = _resumen_riesgos(conn, encargo_id, resultado["venta_id"])
        ok = (resultado["saldo"] > 0 and riesgos["anticipo_conservado"]
              and riesgos["descuento_unico"])
        return {"caso": "anticipo parcial", "ok": ok,
                "resultado": resultado, "riesgos": riesgos}
    finally:
        conn.close()


def caso_stock_insuficiente() -> dict[str, Any]:
    """Caso 3 (R6, R9): stock insuficiente -> `VentaError` y cero mutacion.

    Time: O(1) | Space: O(1)
    """
    conn = nueva_bd()
    try:
        _seed_stock(conn, "33333", "Vajilla 20pz", 2, 500.0)
        encargo_id = _seed_encargo(
            conn, _seed_cliente(conn, "Carla"),
            [{"codigo": "33333", "cantidad": 5, "precio": 900.0}],
            [{"forma_pago": "Efectivo", "monto": 300.0}])
        bloqueado = False
        try:
            convertir_encargo_a_venta(conn, encargo_id)
        except VentaError as exc:
            bloqueado = True
            logger.info("Caso 3 bloqueado como se esperaba: %s", exc)
        estado = conn.execute(_SQL_CABECERA, (encargo_id,)).fetchone()
        ok = (bloqueado and estado["venta_id"] is None
              and estado["status"] == STATUS_PENDIENTE
              and _escalar(conn, _SQL_VENDIDO_VISTA, encargo_id) == 0.0)
        return {"caso": "stock insuficiente", "ok": ok,
                "resultado": None, "riesgos": None}
    finally:
        conn.close()


def main() -> int:
    """Corre los tres casos y reporta pass/fail por `logging`; 0 si todo pasa.

    Time: O(1) | Space: O(1)
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    salidas = [caso_sin_anticipo(), caso_anticipo_parcial(),
               caso_stock_insuficiente()]
    for salida in salidas:
        logger.info("[%s] %s -- %s", "PASS" if salida["ok"] else "FAIL",
                    salida["caso"], salida["resultado"])
        if salida["riesgos"] is not None:
            logger.info("       riesgos: %s", salida["riesgos"])
    fallidos = [s["caso"] for s in salidas if not s["ok"]]
    if fallidos:
        logger.error("POC RT-2: FAIL en %s", fallidos)
        return 1
    logger.info("POC RT-2: los tres casos pasan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
