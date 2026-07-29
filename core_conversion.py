"""Conversion `encargo -> venta`: el cierre de O7 y del riesgo RT-2 (ENC-03, ENC-04).

Surtir un encargo es convertirlo en la venta que siempre fue: se crea la `ventas`
con su `venta_detalle`, se traspasan los anticipos a `venta_pagos`, y el encargo
queda ligado (`venta_id`) y `Entregado`. Todo eso es **una sola transaccion**.

Este modulo es la capa mas alta del dominio: **compone** tres dominios que ya
existen (`core_encargos`, `core_ventas`, `core_pagos`) y ninguno de ellos lo
importa a el. No importa la fachada `core` (seria un ciclo); es `core` quien
re-exporta lo que aqui se define.

Decisiones que conviene no re-descubrir (salen del spike ENC-01, ver
`spikes/FINDINGS_encargo_venta.md`):

* **El precio del encargo es FIRME (H6).** `venta_detalle.precio_publico` es el
  `encargo_detalle.precio_estimado` pactado al encargar: no se re-cotiza al
  surtir aunque el catalogo haya subido. El `precio_costo` si se lee fresco de
  `vw_existencias` en el momento de surtir. Consecuencia **aceptada**: si el
  costo subio por encima del precio pactado, la ganancia registrada sale baja o
  negativa y asi debe quedar -- es una perdida real del negocio, no un error de
  calculo. Una linea con `precio_estimado = 0` vende a 0 (regalo o precio
  pendiente) y tambien es intencional.
* **`venta_pagos` es la fuente de verdad del cobro (H4).** Los anticipos se
  copian 1:1 y el origen **no se borra**: `encargo_pagos` queda como historico.
  Ver la advertencia de doble conteo en el docstring de `surtir_encargo`.
* **El stock no se descuenta con un `UPDATE`** (H3): `vw_existencias` es una
  vista derivada de `venta_detalle`, asi que insertar el detalle ya descuenta.
  Por eso el vector real de doble descuento no es una resta mal hecha, sino
  **reconvertir el mismo encargo**: eso crearia una segunda venta con su detalle
  y la vista descontaria dos veces. Lo impide `_exigir_no_convertido`.
* **Se componen las variantes sin transaccion propia** (H1, DEUDA-05):
  `core_ventas.insertar_venta_en_transaccion` y
  `core_pagos.agregar_pago_en_transaccion`. Las versiones publicas cierran su
  propia transaccion, de modo que un fallo posterior dejaria la venta ya
  committeada -- el commit parcial que define RT-2. Duplicar aqui su SQL era la
  otra salida, y las dos copias habrian divergido.

Nota sobre `Surtido` (H7): el CHECK de ADR-5 lo admite, pero esta conversion va
directo de `Pendiente` a `Entregado`; `Surtido` queda sin usar en este camino.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Final

import core_encargos
import core_pagos
import core_ventas
from core_encargos import EncargoError
from core_ventas import VentaError

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Tabla de pagos destino, resuelta por `core_pagos` contra su whitelist
#: `PAGO_TABLAS`: el nombre nunca se interpola en el SQL (R15).
TABLA_PAGOS_VENTA: Final[str] = "venta_pagos"

#: Claves del resumen que devuelve `surtir_encargo` (contrato de R14).
CAMPOS_RESUMEN: Final[tuple[str, ...]] = (
    "encargo_id",
    "venta_id",
    "cliente_id",
    "total",
    "ganancia",
    "num_lineas",
    "anticipo_transferido",
    "saldo",
    "status",
    "lineas",
)

_ACCION: Final[str] = "surtir"

_MSG_SIN_LINEAS: Final[str] = (
    "El encargo {encargo_id} no tiene lineas que surtir: no hay nada que vender."
)
_MSG_YA_CONVERTIDO: Final[str] = (
    "El encargo {encargo_id} ya se convirtio en la venta {venta_id}: surtirlo otra "
    "vez descontaria el stock dos veces."
)
_MSG_FALLO: Final[str] = "No se pudo surtir el encargo {encargo_id}: {error}"

#: Marca de trazabilidad que la venta lleva en `observaciones` (R7).
_MARCA_TRAZABILIDAD: Final[str] = "Surtido de encargo #{encargo_id}"

_SQL_VENTA_ID: Final[str] = "SELECT venta_id FROM encargos WHERE id = ?"
_SQL_STATUS: Final[str] = "SELECT status, venta_id FROM encargos WHERE id = ?"
_SQL_CERRAR_ENCARGO: Final[str] = "UPDATE encargos SET venta_id = ?, status = ? WHERE id = ?"

#: Disponibilidad del encargo entero en **una sola consulta**, sin N+1.
#: Agrupa por `codigo_articulo` antes de comparar --igual que
#: `core_ventas._validar_stock_canasta`-- para que dos lineas del mismo articulo
#: no parezcan surtibles por separado cuando juntas sobrevenden. El `LEFT JOIN`
#: mas `COALESCE` hace que un producto ausente de `vw_existencias` cuente como 0.
_SQL_SURTIBLE: Final[str] = """
SELECT COUNT(*) AS grupos, COALESCE(SUM(faltante), 0) AS faltantes
FROM (
    SELECT CASE
               WHEN SUM(d.cantidad_solicitada) > COALESCE(MAX(v.piezas_disponibles), 0)
               THEN 1 ELSE 0
           END AS faltante
      FROM encargo_detalle d
      LEFT JOIN vw_existencias v ON v.codigo_articulo = d.codigo_articulo
     WHERE d.encargo_id = ?
     GROUP BY d.codigo_articulo
) AS grupos_del_encargo
"""


def _exigir_no_convertido(conn: sqlite3.Connection, encargo_id: int) -> None:
    """Guarda anti-reconversion: `encargos.venta_id` tiene que seguir NULL.

    Es la unica defensa contra el **doble descuento de stock**. El esquema no
    ayuda: `encargos.venta_id` es nullable y **sin `UNIQUE`** (hallazgo H2 del
    spike, registrado como DEUDA-06), asi que hoy solo el dominio lo impide.
    En el camino normal `_exigir_pendiente` ya rechaza un encargo convertido
    --queda en `Entregado`--, pero una fila `Pendiente` con `venta_id` puesto a
    mano pasaria esa guarda y crearia una segunda venta con su propio detalle,
    que `vw_existencias` descontaria otra vez.

    Se llama **dentro** de la transaccion que va a escribir, para que leer y
    actuar sean la misma operacion atomica.

    Raises:
        EncargoError: si el encargo ya tiene una venta asociada.

    Time: O(log n) por el indice de clave primaria | Space: O(1)
    """
    fila = conn.execute(_SQL_VENTA_ID, (encargo_id,)).fetchone()
    if fila is not None and fila["venta_id"] is not None:
        raise EncargoError(
            _MSG_YA_CONVERTIDO.format(encargo_id=encargo_id, venta_id=fila["venta_id"])
        )


def _leer_encargo_pendiente(conn: sqlite3.Connection, encargo_id: int) -> dict[str, Any]:
    """Carga el encargo y rechaza todo lo que no se puede surtir (R3, R4, R5).

    Reusa `core_encargos.obtener_encargo`, que ya levanta `EncargoError` si el
    encargo no existe (R4) y trae la cabecera con sus lineas. Encima se exigen
    las dos condiciones restantes: seguir en `Pendiente` (R3) y tener al menos
    una linea (R5, defensivo -- ENC-02 ya impide crear un encargo vacio).

    Corre **fuera** de la transaccion: un encargo cancelado o inexistente no
    debe llegar siquiera a abrirla. Las mismas guardas se repiten dentro, ya con
    la transaccion abierta, porque entre ambas lecturas el estado puede cambiar.

    Raises:
        EncargoError: encargo inexistente, ya convertido, en un status distinto
            de `Pendiente`, o sin lineas.

    Time: O(k log n) sobre las lineas del encargo | Space: O(k)
    """
    encargo = core_encargos.obtener_encargo(conn, encargo_id)
    core_encargos._exigir_pendiente(conn, encargo_id, _ACCION)
    _exigir_no_convertido(conn, encargo_id)
    if not encargo["lineas"]:
        raise EncargoError(_MSG_SIN_LINEAS.format(encargo_id=encargo_id))
    return encargo


def _lineas_de_venta(encargo: dict[str, Any]) -> list[dict[str, Any]]:
    """Traduce `encargo_detalle` a la canasta que espera CLI-02 (R2).

    Aqui se aplica la regla de **precio firme** (H6): el `precio_publico` de la
    venta es el `precio_estimado` que se pacto al levantar el encargo, sin
    re-cotizar. El `precio_costo` no se toca en este paso: lo resuelve mas tarde
    `_validar_stock_canasta` leyendo `vw_existencias` al momento de surtir.

    Time: O(n) sobre las lineas | Space: O(n)
    """
    return [
        {
            "codigo": str(linea["codigo_articulo"]),
            "cantidad": int(linea["cantidad_solicitada"]),
            "precio_publico": float(linea["precio_estimado"]),
        }
        for linea in encargo["lineas"]
    ]


def _observaciones_venta(encargo: dict[str, Any]) -> str:
    """Observaciones de la venta con la marca de trazabilidad al encargo (R7).

    La nota original del encargo se conserva detras de la marca: sin ella se
    perderia el unico texto libre que la clienta dejo al encargar.

    Time: O(n) sobre la longitud del texto | Space: O(n)
    """
    marca = _MARCA_TRAZABILIDAD.format(encargo_id=encargo["id"])
    nota = str(encargo["observaciones"] or "").strip()
    return f"{marca} - {nota}" if nota else marca


def _traspasar_anticipos(conn: sqlite3.Connection, encargo_id: int, venta_id: int) -> float:
    """Copia los anticipos del encargo a `venta_pagos` **1:1** (R8, R15).

    Se preservan `forma_pago`, `monto` y `fecha_pago` fila por fila: consolidar
    los abonos en un pago unico perderia la forma y la fecha de cada uno, y con
    ellas la trazabilidad del cobro. La `fecha_pago` viaja explicita (hallazgo
    H5): esa columna la agrega `db._harmonize_venta_pagos` con un `ALTER TABLE`
    que puede quedarse **sin DEFAULT**, asi que omitirla dejaria el anticipo
    migrado con fecha `NULL`; ademas la fecha correcta es la del pago original,
    no la de la conversion.

    Los `encargo_pagos` **no se borran**: son el historial del encargo (H4).

    La lectura es **una sola consulta** (`listar_pagos`); las escrituras son un
    insert por anticipo porque se delega en la variante componible de
    `core_pagos`, que valida tabla, forma y monto en cada llamada. No es un N+1:
    no hay ninguna consulta *de lectura* dentro del bucle, y agrupar los inserts
    en un `executemany` exigiria duplicar aqui el SQL de pagos, que es
    exactamente lo que DEUDA-05 vino a evitar.

    Returns:
        La suma traspasada, redondeada a dos decimales con la misma semantica
        que `core_pagos.total_pagado`.

    Time: O(m) sobre los anticipos del encargo | Space: O(m)
    """
    pagos = core_pagos.listar_pagos(conn, core_encargos.TABLA_PAGOS, encargo_id)
    for pago in pagos:
        core_pagos.agregar_pago_en_transaccion(
            conn,
            TABLA_PAGOS_VENTA,
            venta_id,
            pago["forma_pago"],
            pago["monto"],
            pago["fecha"],
        )
    return round(sum(float(pago["monto"]) for pago in pagos), 2)


def _marcar_convertido(conn: sqlite3.Connection, encargo_id: int, venta_id: int) -> None:
    """Liga el encargo a su venta y lo pasa a `Entregado` (R11).

    Ultimo paso de la transaccion, y el que cierra la puerta a una segunda
    conversion: a partir de aqui `_exigir_pendiente` y `_exigir_no_convertido`
    rechazan el encargo por dos motivos independientes.

    Time: O(log n) por el indice de clave primaria | Space: O(1)
    """
    conn.execute(_SQL_CERRAR_ENCARGO, (venta_id, core_encargos.STATUS_ENTREGADO, encargo_id))


def _construir_resumen(
    encargo_id: int,
    venta_id: int,
    cliente_id: int,
    calculadas: list[dict[str, Any]],
    anticipo: float,
) -> dict[str, Any]:
    """Arma el resumen de retorno con las claves de `CAMPOS_RESUMEN` (R14).

    `saldo = round(total - anticipo, 2)`, la misma formula que
    `core_pagos.saldo_pendiente`: si las dos divergieran, el dialogo de pagos y
    esta pantalla mostrarian cifras distintas para la misma venta. Puede salir
    negativo si el anticipo cubrio de mas, y se muestra tal cual.

    Time: O(n) sobre las lineas | Space: O(n)
    """
    total = round(sum(float(linea["total"]) for linea in calculadas), 2)
    return {
        "encargo_id": encargo_id,
        "venta_id": venta_id,
        "cliente_id": cliente_id,
        "total": total,
        "ganancia": round(sum(float(linea["ganancia"]) for linea in calculadas), 2),
        "num_lineas": len(calculadas),
        "anticipo_transferido": anticipo,
        "saldo": round(total - anticipo, 2),
        "status": core_encargos.STATUS_ENTREGADO,
        "lineas": calculadas,
    }


def surtir_encargo(conn: sqlite3.Connection, encargo_id: int) -> dict[str, Any]:
    """Convierte un encargo `Pendiente` en venta, atomicamente (R1-R14).

    Cuatro pasos, uno solo de ellos fuera de la transaccion:

    1. Validar estado y stock -- **fuera** del `with conn:`, a proposito: un
       rechazo por stock no debe llegar siquiera a abrir una transaccion.
    2. Insertar `ventas` + `venta_detalle` con
       `core_ventas.insertar_venta_en_transaccion`.
    3. Traspasar `encargo_pagos` -> `venta_pagos` 1:1.
    4. `UPDATE encargos SET venta_id = ?, status = 'Entregado'`.

    Los pasos 2-4 comparten **un unico `with conn:`**: si el traspaso o el
    cierre fallan, la venta y su detalle tampoco quedan escritos y el encargo
    sigue `Pendiente` con `venta_id` NULL. Ese es el commit parcial que define
    RT-2, y la razon de componer las variantes `*_en_transaccion` en vez de
    `registrar_venta` / `agregar_pago`, que cierran la suya (hallazgo H1).

    **Precio firme (H6).** Cada `venta_detalle.precio_publico` es el
    `precio_estimado` pactado al encargar; no hay re-cotizacion. El
    `precio_costo` se lee fresco de `vw_existencias`. Si el costo subio por
    encima del precio pactado, la ganancia sale baja o negativa **y asi debe
    quedar**: es una perdida real, no un error de calculo.

    **`venta_pagos` es la fuente de verdad del cobro (H4).** Los anticipos se
    copian sin borrar el origen, de modo que tras convertir el mismo dinero
    figura en `encargo_pagos` **y** en `venta_pagos`. Una vez que
    `encargos.venta_id` no es NULL, el cobro vigente es el de la venta y
    `encargo_pagos` es historico: **cualquier reporte de caja o de ingresos debe
    excluir los `encargo_pagos` de encargos ya convertidos**, o contaria el
    mismo dinero dos veces.

    El stock baja **una sola vez** porque `vw_existencias` se deriva de
    `venta_detalle` (H3): no hay contador que mantener. Lo unico que podria
    descontarlo dos veces es reconvertir el mismo encargo, y eso lo bloquea
    `_exigir_no_convertido`.

    Args:
        conn: conexion inyectada por el call-site (ADR-2, `foreign_keys ON`).
        encargo_id: encargo `Pendiente` a surtir.

    Returns:
        Las claves de `CAMPOS_RESUMEN`; cada entrada de `lineas` lleva `codigo`,
        `descripcion`, `cantidad`, `precio_costo`, `precio_publico`, `total` y
        `ganancia`.

    Raises:
        EncargoError: encargo inexistente (R4), en un status distinto de
            `Pendiente` (R3), ya convertido, sin lineas (R5), o fallo de
            escritura -- siempre con rollback completo (R12).
        VentaError: stock insuficiente o articulo fuera del inventario (R6);
            bloquea la conversion entera, sin venta parcial.

    Time: O(n + m + k log p) | Space: O(n + m)
    """
    encargo = _leer_encargo_pendiente(conn, encargo_id)
    lineas = _lineas_de_venta(encargo)
    datos = core_ventas._validar_stock_canasta(conn, lineas)
    calculadas = core_ventas._calcular_canasta(lineas, datos)
    cliente_id = int(encargo["cliente_id"])
    try:
        with conn:
            core_encargos._exigir_pendiente(conn, encargo_id, _ACCION)
            _exigir_no_convertido(conn, encargo_id)
            venta_id = core_ventas.insertar_venta_en_transaccion(
                conn, cliente_id, _observaciones_venta(encargo), calculadas
            )
            anticipo = _traspasar_anticipos(conn, encargo_id, venta_id)
            _marcar_convertido(conn, encargo_id, venta_id)
    except sqlite3.Error as exc:
        raise EncargoError(_MSG_FALLO.format(encargo_id=encargo_id, error=exc)) from exc
    return _construir_resumen(encargo_id, venta_id, cliente_id, calculadas, anticipo)


def encargo_surtible(conn: sqlite3.Connection, encargo_id: int) -> bool:
    """Indica si el encargo se puede surtir ahora mismo (ENC-04 R1, R2, R3).

    Es una **consulta de lectura y nada mas**: la GUI la usa para habilitar o no
    el boton Surtir, asi que no lanza -- cualquier problema se responde `False`.
    La autoridad atomica sigue siendo `surtir_encargo`, que revalida estado y
    stock dentro de su transaccion; si el stock cayo entre el chequeo y el clic,
    el error sale de alli como `VentaError`.

    Devuelve `True` solo si el encargo esta `Pendiente`, **no tiene ya una venta
    ligada** (R2: cualquier otro caso responde `False` sin evaluar stock) y cada
    articulo cabe en lo disponible (R3). Un producto ausente de `vw_existencias` cuenta como `0`, y
    un encargo sin lineas no es surtible: no hay nada que vender.

    La disponibilidad se resuelve en **una sola consulta parametrizada** que
    agrupa por `codigo_articulo`, de modo que dos lineas del mismo articulo se
    suman antes de compararse -- el mismo criterio anti-oversell de
    `core_ventas._validar_stock_canasta`, para que el boton no habilite una
    conversion que despues rebotaria.

    Time: O(k log n) sobre las lineas del encargo | Space: O(1)
    """
    try:
        cabecera = conn.execute(_SQL_STATUS, (encargo_id,)).fetchone()
        if cabecera is None or str(cabecera["status"]) != core_encargos.STATUS_PENDIENTE:
            return False
        # `venta_id` puesto con el status aun en `Pendiente` es un estado que
        # `surtir_encargo` rechaza (guarda anti-reconversion) y que el esquema
        # todavia permite mientras `encargos.venta_id` no tenga UNIQUE (H2 del
        # spike / DEUDA-06). Sin esta linea el boton se habilitaria y el clic
        # rebotaria: los dos criterios tienen que coincidir.
        if cabecera["venta_id"] is not None:
            return False
        fila = conn.execute(_SQL_SURTIBLE, (encargo_id,)).fetchone()
    except sqlite3.Error as exc:
        logger.warning("No se pudo evaluar si el encargo %s es surtible: %s", encargo_id, exc)
        return False
    return int(fila["grupos"]) > 0 and int(fila["faltantes"]) == 0


__all__ = [
    "CAMPOS_RESUMEN",
    "TABLA_PAGOS_VENTA",
    "EncargoError",
    "VentaError",
    "encargo_surtible",
    "surtir_encargo",
]
