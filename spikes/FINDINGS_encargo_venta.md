# Spike RT-2 — Conversión `encargo → venta`

**Actividad:** `momachdist-ENC-01` · **Riesgo:** RT-2 (score 6, el más alto abierto)
**Fecha:** 2026-07-27 · **Código:** `spikes/poc_encargo_venta.py` (desechable)
**Estado:** 14 tests en verde, POC standalone `exit=0`, suite general sin regresiones.

---

## 1. Enfoque

El POC **no** arma una BD auto-contenida. Levanta el esquema **real** con
`db.init_db(":memory:")` — que ejecuta `reference/db_schema.sql`, el DDL de encargos
(ADR-5) y la armonización de `venta_pagos` (ADR-6) — y reusa
`core_ventas._validar_stock_canasta` y `core_ventas._calcular_linea`.

Esto es la **desviación D7**, aprobada por el desarrollador. La Spec original (2026-07-22)
mandaba reconstruir un mini-esquema "porque CLI-02 y FUND-02 aún no existen"; hoy existen,
mergeadas y en verde. Importa porque un POC que valida stock contra un esquema paralelo
sólo demuestra que *ese* esquema funciona: si diverge del real, el GO/NO-GO sería sobre
algo que no es el sistema. Con D7, lo que aquí se prueba es literalmente lo que ENC-03
va a heredar.

Lo que sí se escribió a mano (el corazón del spike): el sketch
`convertir_encargo_a_venta`, los helpers de seed, los tres casos y esta nota.

**Trampa confirmada de `vw_existencias`:** `piezas_recibidas` suma sólo
`cantidad_casa + cantidad_local` (excluye a propósito lo entregado al asociado), y el
reparto por defecto de MERC-03 manda todo al asociado. Sembrar sin poner esas columnas
explícitamente da `piezas_disponibles = 0` y hace parecer que toda validación de stock
falla. `_seed_stock` las pone respetando el CHECK
`cantidad_asociado + cantidad_casa + cantidad_local = cantidad_surtida`.

## 2. Diseño de la conversión (4 pasos)

```
1. validar stock                      <- FUERA de transacción, antes de escribir nada
2. with conn:  INSERT ventas + N venta_detalle
3.             encargo_pagos -> venta_pagos, 1:1
4.             UPDATE encargos SET venta_id = ?, status = 'Entregado'
```

Los pasos 2–4 son **una sola transacción**. El paso 1 va deliberadamente fuera: un rechazo
por stock no debe llegar siquiera a abrir transacción.

## 3. Traspaso de anticipos

Cada fila de `encargo_pagos` se copia a `venta_pagos` **1:1**, preservando `forma_pago`,
`monto` y `fecha_pago`. **No se consolidan** en un pago único: hacerlo perdería la forma
y la fecha de cada anticipo, y con ello la trazabilidad del cobro.

Los `encargo_pagos` **no se borran**: son el historial del encargo. Consecuencia
(ver hallazgo H4) es que el mismo dinero queda registrado en dos tablas.

`saldo = round(total_venta − Σ anticipos, 2)`, calculado una sola vez, con la misma
semántica de redondeo que ya usa `core_ventas._fila_historial`.

## 4. Validación de stock

Reusa `core_ventas._validar_stock_canasta` tal cual: agrega la cantidad **por
`codigo_articulo`** antes de consultar y resuelve toda la canasta con un solo `IN (?, …)`
(sin N+1). Verificado en el spike: un encargo con dos líneas de 2 piezas del mismo
artículo sobre 3 disponibles se rechaza, aunque ninguna línea por sí sola sobrevenda.

El stock **no se descuenta con un `UPDATE`**: `vw_existencias.piezas_disponibles` se
deriva de `venta_detalle`. No hay contador que mantener sincronizado — esto reduce
materialmente RT-2 (ver H3).

## 5. Los tres casos

| # | Caso | Resultado observado |
|---|---|---|
| 1 | **Sin anticipo** — 10 disponibles, encargo de 2 @ 180 | venta creada, 1 `venta_detalle`, **0** `venta_pagos`; `total = 360.00`, `anticipo = 0.00`, `saldo = 360.00`; `encargos.venta_id` fijado y `status = 'Entregado'` |
| 2 | **Anticipo parcial** — 8 disponibles, encargo de 3 @ 300, anticipo 400 Transferencia | `total = 900.00`, `anticipo = 400.00`, **`saldo = 500.00 > 0`**; `venta_pagos` tiene 1 fila con `forma_pago = 'Transferencia'`; `SUM(encargo_pagos)` antes == `SUM(venta_pagos)` después |
| 3 | **Stock insuficiente** — 2 disponibles, encargo de 5, con anticipo 300 de por medio | `VentaError("Stock insuficiente de 'Vajilla 20pz' (33333): pediste 5 y hay 2 disponibles.")`; **0** filas en `ventas` / `venta_detalle` / `venta_pagos`; `encargos.venta_id IS NULL`; `status = 'Pendiente'` intacto; los `encargo_pagos` intactos; `piezas_disponibles` sigue en 2 |

Caso extra (no pedido, pero relevante): dos anticipos de formas distintas
(Efectivo 100 + Tarjeta 250) llegan a `venta_pagos` como **dos filas separadas**.

## 6. Riesgos resueltos

### Doble descuento de stock — RESUELTO
`_resumen_riesgos` compara tres números que deben coincidir: lo solicitado en
`encargo_detalle`, lo escrito en `venta_detalle`, y lo que `vw_existencias` reporta como
vendido. Con 10 disponibles y un encargo de 3, la vista queda en **7** (no 4).

El vector real de doble descuento **no** es un `UPDATE` mal hecho, sino **reconvertir el
mismo encargo**: eso generaría una segunda venta con su propio detalle y la vista
descontaría dos veces. El sketch lo bloquea con un guarda explícito en `_leer_cabecera`
(`encargos.venta_id IS NOT NULL` → `VentaError`), verificado por
`test_convertir_rechaza_segunda_conversion_del_mismo_encargo`.

### Anticipo perdido o duplicado — RESUELTO
`SUM(encargo_pagos) == SUM(venta_pagos)` se verifica en cada caso convertido. Ni perdido
(traspaso omitido) ni duplicado (traspaso repetido) — el segundo caso queda además cubierto
por el mismo guarda anti-reconversión.

### Commit parcial — RESUELTO
`test_rollback_deja_el_encargo_intacto_si_falla_el_cierre` rompe el `UPDATE` final
(paso 4) con un `monkeypatch` y comprueba que la venta, su detalle y los pagos traspasados
**tampoco quedan escritos**, y que el encargo sigue `Pendiente` con `venta_id NULL`.

---

## 7. Hallazgos que cambian el diseño previsto para ENC-03

### H1 — `registrar_venta` **no se puede componer**. Requiere refactor de `core_ventas`. (alto)
`core_ventas.registrar_venta` delega en `_insertar_venta`, que abre y **cierra** su propia
transacción (`with conn:`). Si ENC-03 lo llamara y luego traspasara los anticipos, la venta
ya estaría *committeada* cuando el traspaso fallara → commit parcial, exactamente el riesgo
que RT-2 nombra. El sketch por eso escribe su propio `INSERT` dentro de la transacción del
llamador (`_insertar_venta` del POC, sin `with conn:`).

**Recomendación:** extraer en `core_ventas` un `_insertar_venta_en_transaccion(conn, …)`
**sin** `with conn:`, y que `registrar_venta` sea el que abra la transacción y lo envuelva.
ENC-03 abre su propia transacción y reusa el helper. Sin esto, ENC-03 duplica el SQL de
`venta_detalle` y las dos copias divergirán.

### H2 — El esquema no impide la reconversión; hoy sólo la impide código. (medio)
`encargos.venta_id` es *nullable* y **sin `UNIQUE`**. El guarda de H1/§6 vive únicamente en
la capa de dominio: un `UPDATE` manual o un segundo camino de código lo saltaría.
**Recomendación:** agregar `UNIQUE` (o índice único parcial `WHERE venta_id IS NOT NULL`)
a `encargos.venta_id` como defensa en profundidad. Es un cambio de ADR-5.

### H3 — El stock derivado baja el riesgo real de RT-2. (informativo, positivo)
No existe contador de existencias que mantener: `vw_existencias` se recalcula desde
`pedido_detalle` y `venta_detalle`. Eso elimina de raíz toda una familia de bugs de
sincronización. RT-2 debería re-puntuarse a la baja una vez ENC-03 incorpore H1 y H2.

### H4 — El anticipo queda registrado en dos tablas a la vez. (medio, decisión de producto)
Tras convertir, el mismo dinero vive en `encargo_pagos` **y** en `venta_pagos`. El POC
elige no borrar el origen (es el historial del encargo, y borrarlo haría inverificable la
conservación). **Consecuencia:** ningún reporte de ingresos puede sumar las dos tablas.
**Recomendación para ENC-03:** dejar por escrito que `venta_pagos` es la única fuente de
verdad del cobro una vez `encargos.venta_id` no es NULL, y que `encargo_pagos` es
histórico. Cualquier reporte de caja debe excluir `encargo_pagos` de encargos ya
convertidos.

### H5 — `venta_pagos.fecha_pago` hay que pasarla explícitamente. (bajo)
Esa columna no está en el esquema canónico: la agrega `db._harmonize_venta_pagos` con un
`ALTER TABLE`. Como SQLite rechaza `DEFAULT` no constante en `ADD COLUMN`, el propio módulo
prevé un fallback que la crea **sin DEFAULT**. Si ENC-03 omite `fecha_pago` al traspasar, el
anticipo migrado puede quedar con fecha `NULL`. El POC la pasa explícita, copiada de
`encargo_pagos.fecha_pago` — que además es el dato correcto: la fecha en que el cliente
pagó, no la de la conversión.

### H6 — `precio_estimado` → `precio_publico` sin re-confirmación. (medio, decisión de producto)
El sketch usa `encargo_detalle.precio_estimado` como precio de venta, mientras que el
`precio_costo` lo toma de `vw_existencias` **en el momento de convertir**. Es decir: el
precio se congela al encargar y el costo al vender. Si el catálogo subió entre ambos
momentos, la ganancia registrada sale de un precio viejo y puede ser irrealmente baja
(o negativa). **ENC-03 necesita una decisión explícita:** ¿el precio del encargo es firme,
o la pantalla de conversión debe permitir re-cotizar antes de confirmar?

### H7 — El estado `'Surtido'` queda sin usar. (bajo)
El CHECK de ADR-5 admite `Pendiente / Surtido / Entregado / Cancelado`, pero la conversión
salta de `Pendiente` a `Entregado`. ENC-03 debe decidir si `Surtido` es un paso obligatorio
previo (mercancía llegó, cliente no la ha recogido) o si se elimina del CHECK.

### H8 — Se está dependiendo de un privado entre módulos. (bajo)
ENC-03 va a reusar `core_ventas._validar_stock_canasta`, que es privado por convención.
**Recomendación:** promoverlo a API pública (`validar_stock_canasta`) y re-exportarlo desde
la fachada `core`, junto con el helper de H1.

---

## 8. Veredicto

# GO para ENC-03.

La conversión `encargo → venta` es viable sobre el esquema real tal como está: los tres
riesgos que definían RT-2 —doble descuento, anticipo perdido/duplicado y commit parcial—
quedaron reproducidos y resueltos, cada uno con su test. El diseño de 4 pasos con una única
transacción es directamente portable a ENC-03.

**Condiciones del GO** (entran al alcance de ENC-03, ninguna es bloqueante para arrancar):

1. **H1 — obligatoria.** Refactorizar `core_ventas` para exponer un insert de venta sin
   transacción propia, antes de escribir ENC-03. Es la única forma de no duplicar SQL y
   la única forma de mantener la atomicidad.
2. **H2 — recomendada.** `UNIQUE` sobre `encargos.venta_id` (cambio de ADR-5).
3. **H4 y H6 — requieren decisión del desarrollador antes de implementar**, no son
   técnicas: quién es la fuente de verdad del cobro, y si el precio del encargo es firme.
4. **H5, H7, H8 — ajustes menores** a incorporar durante la implementación.

Sin H1 el GO se degrada: ENC-03 seguiría siendo posible, pero al costo de duplicar el SQL
de `venta_detalle` en dos módulos, que es cómo se vuelven a abrir riesgos de este tipo.
