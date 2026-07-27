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
```

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
* `CargaError`              -- error de dominio de la carga de remisiones.

Directorio de asociados (`core_asociados`):

* `obtener_o_crear_asociado` -- resuelve (o da de alta) el asociado de una nota.
* `_normalizar_nombre`       -- recorte + colapso de espacios del nombre.

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
    _normalizar_nombre,
    obtener_o_crear_asociado,
)
from core_comun import CoreError, _entero, _es_cero, _real, _texto
from core_entregas import EntregaError, generar_entregas
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

__all__ = [
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
    "INSERT_ASOCIADO_SQL",
    "INSERT_DETALLE_SQL",
    "INSERT_PEDIDO_SQL",
    "MARCA_REVISAR",
    "SELECT_ASOCIADO_ID_SQL",
    "SELECT_CATALOGO_SQL",
    "SELECT_PEDIDO_ID_SQL",
    "STOCK_BAJO_UMBRAL",
    "UPSERT_PRODUCTO_SQL",
    "CargaError",
    "CoreError",
    "EntregaError",
    "aplicar_default_post_extraccion",
    "aplicar_reparto_default_asociado",
    "confirmar_carga",
    "estampar_asociado_id",
    "generar_entregas",
    "guardar_pedido",
    "guardar_pedido_detalle",
    "normalizar_reparto_carga",
    "obtener_catalogo",
    "obtener_existencias",
    "obtener_o_crear_asociado",
    "obtener_resumen_dashboard",
    "upsert_producto",
    "upsert_productos",
]
