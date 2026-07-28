"""Capa de servicios de dominio (core) -- fachada de los submodulos por dominio.

La GUI nunca ejecuta SQL: solo llama a las funciones de esta capa (ADR-2). La
capa tampoco abre conexiones -- la `sqlite3.Connection` siempre viene inyectada
desde el call-site (`db.get_conn`), lo que la mantiene testeable con una base
en memoria.

El codigo vivia en un unico `core.py` hasta que supero el limite de 400 lineas
de `.langs/python.md`. Se dividio por dominio conservando `core` como fachada,
de modo que `import core` sigue funcionando exactamente igual:

```
core.py                  <- fachada: reexporta todo (este archivo)
  |
  +-- core_pedidos.py    <- remisiones PDF -> pedidos + pedido_detalle
  |     |
  |     +-- core_productos.py  <- catalogo de productos (FK del detalle)
  |     +-- core_reparto.py    <- reparto Asociado / Casa / Local
  |     +-- core_asociados.py  <- directorio de asociados (FK del detalle)
  |           |
  |           +-- core_comun.py <- CoreError + coercion de valores
  |
  |     +-- core_semanas.py    <- semanas de catalogo + puntos Betterware
  |
  +-- core_clientes.py   <- directorio de clientes finales (hoja del grafo)
  +-- core_ventas.py     <- ventas multi-producto e historial
  +-- core_entregas.py   <- entregas a asociado + ciclo de status
  +-- core_pagos.py      <- componente de pagos agnostico de tabla (ADR-6)
        |
        +-- core_comun.py
```

**Transacciones:** toda funcion de escritura de la capa core delimita su propia
transaccion con `with conn:` (commit al salir, rollback ante excepcion) salvo las
que documentan explicitamente lo contrario porque un orquestador las gobierna
(`obtener_o_crear_asociado`, `obtener_o_crear_semana`, `guardar_pedido`,
`guardar_pedido_detalle`, que corren dentro del `with conn:` de
`confirmar_carga`). No se usa `conn.commit()` suelto: deja la transaccion
implicita abierta ante un fallo posterior.

> **Limitacion conocida (spike ENC-01, hallazgo H1).** `registrar_venta` y
> `agregar_pago` abren y **cierran** su propia transaccion, asi que no se pueden
> componer dentro de una transaccion mayor: quien los llame y falle despues se
> queda con un commit parcial. ENC-03 (conversion encargo->venta) necesita
> insertar la venta y traspasar los anticipos en una sola transaccion, de modo
> que antes de escribirla hay que extraer variantes sin `with conn:` propio y
> dejar que el llamador gobierne el limite. Ver `spikes/FINDINGS_encargo_venta.md`.

Las dependencias apuntan siempre hacia abajo: `core_comun` no importa a nadie y
ningun submodulo importa `core`, asi que no hay ciclos.

Catalogo de productos (`core_productos`):

* `upsert_producto`   -- alta/actualizacion idempotente de un producto.
* `upsert_productos`  -- lote deduplicado y atomico; devuelve codigos distintos.
* `obtener_catalogo`  -- lectura del catalogo ordenada por codigo.
* `CoreError`         -- error de dominio base de la capa core (`core_comun`).

Entrada de mercancia (`core_pedidos`):

* `guardar_pedido`          -- cabecera idempotente por folio.
* `guardar_pedido_detalle`  -- lineas del pedido, sin duplicar.
* `confirmar_carga`         -- orquestador transaccional del lote completo.
* `obtener_movimientos`     -- historial de lineas de pedido, con JOINs resueltos.
* `CargaError`              -- error de dominio de la carga de remisiones.

Directorio de asociados (`core_asociados`):

* `obtener_o_crear_asociado` -- resuelve (o da de alta) el asociado de una nota.
* `_normalizar_nombre`       -- recorte + colapso de espacios del nombre.
* `listar_asociados`         -- directorio completo con saldo individual (ADR-3).
* `crear_asociado`           -- alta manual desde el directorio.
* `editar_asociado`          -- actualizacion parcial de campos.
* `eliminar_asociado`        -- baja protegida por las FKs de entregas/detalle.
* `AsociadoError`            -- error de dominio del directorio de asociados.

Directorio de clientes (`core_clientes`):

* `listar_clientes`  -- CRM de compradores finales, ordenado por nombre.
* `crear_cliente` / `editar_cliente` / `eliminar_cliente` -- CRUD; la baja esta
  protegida por las FKs de `ventas` y `encargos`.
* `CAMPOS_CLIENTE`   -- contrato de claves que consume la GUI.
* `ClienteError`     -- error de dominio del directorio de clientes.

Reparto de la carga (`core_reparto`):

* `aplicar_reparto_default_asociado` -- default de la fila al asociado.
* `aplicar_default_post_extraccion`  -- inversion del default tras extraer.
* `estampar_asociado_id`             -- sella el asociado resuelto en las filas.
* `normalizar_reparto_carga`         -- default por fila, con fallback a Casa.

Entregas a asociado (`core_entregas`):

* `generar_entregas`  -- una entrega por linea con cantidad de asociado > 0.
* `EntregaError`      -- error de dominio de la generacion de entregas.

Existencias y dashboard (`core_existencias`):

* `obtener_existencias`         -- stock calculado desde la vista `vw_existencias`.
* `obtener_resumen_dashboard`   -- agregados del dashboard.
* `STOCK_BAJO_UMBRAL`           -- umbral de alerta de stock bajo.
"""

from __future__ import annotations

from core_asociados import (
    CLAVE_NOMBRE_ASOCIADO,
    INSERT_ASOCIADO_SQL,
    SELECT_ASOCIADO_ID_SQL,
    AsociadoError,
    _normalizar_nombre,
    crear_asociado,
    editar_asociado,
    eliminar_asociado,
    listar_asociados,
    obtener_o_crear_asociado,
)
from core_clientes import (
    CAMPOS_CLIENTE,
    ClienteError,
    crear_cliente,
    editar_cliente,
    eliminar_cliente,
    listar_clientes,
)
from core_comun import CoreError, _entero, _es_cero, _real, _texto
from core_entregas import (
    CAMPOS_ENTREGA,
    ENTREGA_STATUS_VALIDOS,
    EntregaError,
    StatusEntregaInvalidoError,
    actualizar_status_entrega,
    generar_entregas,
    listar_entregas,
)
from core_existencias import (
    STOCK_BAJO_UMBRAL,
    obtener_existencias,
    obtener_resumen_dashboard,
)
from core_pedidos import (
    CLAVE_FOLIO,
    CONTAR_PEDIDOS_SQL,
    INSERT_DETALLE_SQL,
    INSERT_PEDIDO_SQL,
    SELECT_PEDIDO_ID_SQL,
    CargaError,
    _agrupar_por_folio,
    _contar_pedidos,
    _parametros_detalle,
    confirmar_carga,
    guardar_pedido,
    guardar_pedido_detalle,
    obtener_movimientos,
)
from core_productos import (
    CLAVE_CODIGO,
    CLAVE_DESCRIPCION,
    CLAVE_PRECIO_PAGAS,
    CLAVE_VALOR_TOTAL,
    SELECT_CATALOGO_SQL,
    UPSERT_PRODUCTO_SQL,
    _COLUMNAS_CATALOGO,
    _aplicar_upsert_productos,
    _ejecutar_upsert,
    _fusionar,
    _mapear_fila,
    obtener_catalogo,
    upsert_producto,
    upsert_productos,
)
from core_pagos import (
    CAMPOS_PAGO,
    FORMAS_PAGO_VALIDAS,
    PAGO_TABLAS,
    FormaPagoInvalidaError,
    MontoInvalidoError,
    PagoError,
    TablaPagoInvalidaError,
    agregar_pago,
    listar_pagos,
    saldo_pendiente,
    total_pagado,
)
from core_reparto import (
    CLAVE_ASOCIADO_ID,
    CLAVE_CANTIDAD_ASOCIADO,
    CLAVE_CANTIDAD_CASA,
    CLAVE_CANTIDAD_LOCAL,
    CLAVE_SURTIDA,
    MARCA_REVISAR,
    _aplicar_reparto_default_casa,
    _tiene_asociado,
    aplicar_default_post_extraccion,
    aplicar_reparto_default_asociado,
    estampar_asociado_id,
    normalizar_reparto_carga,
)

from core_ventas import (
    CAMPOS_HISTORIAL,
    CLIENTE_MOSTRADOR,
    VentaError,
    obtener_ventas_historial,
    registrar_venta,
)

from core_semanas import (
    CAMPOS_SEMANA,
    CLAVE_SEMANA,
    _parsear_semana,
    actualizar_puntos_semana,
    listar_semanas,
    obtener_o_crear_semana,
    procesar_puntos_bw,
)


__all__ = [
    "CAMPOS_CLIENTE",
    "CAMPOS_ENTREGA",
    "CAMPOS_PAGO",
    "CAMPOS_SEMANA",
    "CLAVE_SEMANA",
    "FORMAS_PAGO_VALIDAS",
    "PAGO_TABLAS",
    "CAMPOS_HISTORIAL",
    "CLIENTE_MOSTRADOR",
    "CLAVE_ASOCIADO_ID",
    "CLAVE_CANTIDAD_ASOCIADO",
    "CLAVE_CANTIDAD_CASA",
    "CLAVE_CANTIDAD_LOCAL",
    "CLAVE_CODIGO",
    "CLAVE_DESCRIPCION",
    "CLAVE_FOLIO",
    "CLAVE_NOMBRE_ASOCIADO",
    "CLAVE_PRECIO_PAGAS",
    "CLAVE_SURTIDA",
    "CLAVE_VALOR_TOTAL",
    "CONTAR_PEDIDOS_SQL",
    "ENTREGA_STATUS_VALIDOS",
    "INSERT_ASOCIADO_SQL",
    "INSERT_DETALLE_SQL",
    "INSERT_PEDIDO_SQL",
    "MARCA_REVISAR",
    "SELECT_ASOCIADO_ID_SQL",
    "SELECT_CATALOGO_SQL",
    "SELECT_PEDIDO_ID_SQL",
    "STOCK_BAJO_UMBRAL",
    "UPSERT_PRODUCTO_SQL",
    "AsociadoError",
    "CargaError",
    "ClienteError",
    "CoreError",
    "EntregaError",
    "StatusEntregaInvalidoError",
    "FormaPagoInvalidaError",
    "MontoInvalidoError",
    "PagoError",
    "TablaPagoInvalidaError",
    "VentaError",
    "actualizar_puntos_semana",
    "actualizar_status_entrega",
    "agregar_pago",
    "aplicar_default_post_extraccion",
    "aplicar_reparto_default_asociado",
    "confirmar_carga",
    "crear_asociado",
    "crear_cliente",
    "editar_asociado",
    "editar_cliente",
    "eliminar_asociado",
    "eliminar_cliente",
    "estampar_asociado_id",
    "generar_entregas",
    "guardar_pedido",
    "guardar_pedido_detalle",
    "listar_asociados",
    "listar_entregas",
    "listar_pagos",
    "listar_semanas",
    "listar_clientes",
    "normalizar_reparto_carga",
    "obtener_catalogo",
    "obtener_existencias",
    "obtener_movimientos",
    "obtener_o_crear_asociado",
    "obtener_o_crear_semana",
    "procesar_puntos_bw",
    "obtener_resumen_dashboard",
    "obtener_ventas_historial",
    "registrar_venta",
    "saldo_pendiente",
    "total_pagado",
    "upsert_producto",
    "upsert_productos",
]
