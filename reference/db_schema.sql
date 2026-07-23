-- ============================================================
-- Esquema de base de datos: Inventario Betterware (SQLite)
-- ============================================================
-- Convenciones:
--   - Los montos son REAL (2 decimales manejados en la app).
--   - Las fechas se guardan como TEXT en formato 'YYYY-MM-DD HH:MM'.
--   - "ON DELETE RESTRICT" evita borrar un producto/asociado/cliente
--     que ya tiene movimientos ligados (protege el historial).
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- Catalogo maestro de productos
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS productos (
    codigo_articulo     TEXT PRIMARY KEY,
    descripcion         TEXT NOT NULL,
    categoria           TEXT,
    es_regalo_o_promo   INTEGER NOT NULL DEFAULT 0,   -- 0/1 (boolean)
    fecha_creacion      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ------------------------------------------------------------
-- Semanas / catalogos de Betterware (ciclo de 6 semanas)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS semanas_catalogo (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    semana_texto        TEXT UNIQUE NOT NULL,   -- ej. "27 - 2026" (tal como viene del PDF)
    numero_semana       INTEGER,
    anio                INTEGER,
    puntos_bw_acumulados INTEGER DEFAULT 0
);

-- ------------------------------------------------------------
-- Directorio de Asociados (equipo de la distribuidora)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asociados (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre              TEXT NOT NULL,
    codigo_asociado     TEXT,
    telefono            TEXT,
    fecha_alta          TEXT DEFAULT (date('now', 'localtime')),
    status              TEXT NOT NULL DEFAULT 'Activo' CHECK (status IN ('Activo', 'Inactivo')),
    notas               TEXT,
    saldo_pendiente     REAL NOT NULL DEFAULT 0   -- se mantiene actualizado por trigger
);

-- ------------------------------------------------------------
-- Directorio de Clientes (compradores finales, ventas propias)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clientes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre              TEXT NOT NULL,
    telefono            TEXT,
    direccion           TEXT,
    notas               TEXT,
    fecha_alta          TEXT DEFAULT (date('now', 'localtime'))
);

-- ------------------------------------------------------------
-- Pedidos (encabezado de cada remision/nota cargada de un PDF)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pedidos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    folio_pedido        TEXT NOT NULL,
    semana_id           INTEGER REFERENCES semanas_catalogo(id),
    codigo_nota         TEXT,
    distribuidora       TEXT,
    nombre_asociado_pdf TEXT,   -- nombre tal como viene en el PDF (informativo)
    fecha_registro      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    archivo_origen      TEXT,
    UNIQUE (folio_pedido)
);

-- ------------------------------------------------------------
-- Detalle de pedido (una fila por producto de cada pedido)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pedido_detalle (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id           INTEGER NOT NULL REFERENCES pedidos(id) ON DELETE CASCADE,
    codigo_articulo     TEXT NOT NULL REFERENCES productos(codigo_articulo) ON DELETE RESTRICT,
    ocurrencia          INTEGER NOT NULL DEFAULT 1,  -- para productos repetidos en el mismo pedido
    cantidad_solicitada INTEGER NOT NULL DEFAULT 0,
    cantidad_surtida    INTEGER NOT NULL DEFAULT 0,
    cantidad_asociado   INTEGER NOT NULL DEFAULT 0,
    asociado_id         INTEGER REFERENCES asociados(id),   -- a quien se le entrego (si aplica)
    cantidad_casa       INTEGER NOT NULL DEFAULT 0,
    cantidad_local      INTEGER NOT NULL DEFAULT 0,
    precio_catalogo     REAL NOT NULL DEFAULT 0,   -- precio unitario sin IVA (catalogo)
    precio_con_iva      REAL NOT NULL DEFAULT 0,   -- precio unitario con IVA
    precio_que_pagas    REAL NOT NULL DEFAULT 0,   -- total de la linea (tu costo real)
    valor_total_con_iva REAL NOT NULL DEFAULT 0,   -- total de la linea a precio de catalogo
    tipo                TEXT NOT NULL CHECK (tipo IN ('Normal (con descuento)', 'Sin descuento')),
    UNIQUE (pedido_id, codigo_articulo, tipo, ocurrencia),
    CHECK (cantidad_asociado + cantidad_casa + cantidad_local = cantidad_surtida)
);

CREATE INDEX IF NOT EXISTS idx_detalle_producto ON pedido_detalle(codigo_articulo);
CREATE INDEX IF NOT EXISTS idx_detalle_asociado ON pedido_detalle(asociado_id);

-- ------------------------------------------------------------
-- Entregas a Asociado (una por cada linea con cantidad_asociado > 0)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entregas_asociado (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_detalle_id   INTEGER NOT NULL REFERENCES pedido_detalle(id) ON DELETE CASCADE,
    asociado_id         INTEGER NOT NULL REFERENCES asociados(id) ON DELETE RESTRICT,
    cantidad_entregada  INTEGER NOT NULL,
    monto_que_debe      REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'Pendiente de recoger'
                        CHECK (status IN ('Pendiente de recoger', 'Recogido - no pagado', 'Pagado')),
    fecha_entrega       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    observaciones       TEXT,
    UNIQUE (pedido_detalle_id)
);

-- ------------------------------------------------------------
-- Pagos de entregas a Asociado (permite N pagos parciales)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entrega_pagos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entrega_id          INTEGER NOT NULL REFERENCES entregas_asociado(id) ON DELETE CASCADE,
    forma_pago          TEXT NOT NULL CHECK (forma_pago IN ('Efectivo', 'Transferencia', 'Tarjeta', 'Otro')),
    monto               REAL NOT NULL CHECK (monto > 0),
    fecha_pago          TEXT NOT NULL DEFAULT (date('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_entrega_pagos_entrega ON entrega_pagos(entrega_id);

-- ------------------------------------------------------------
-- Ventas propias (a Clientes finales, NO a Asociados)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ventas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id          INTEGER REFERENCES clientes(id),   -- puede ser NULL (venta de mostrador sin registrar cliente)
    fecha               TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    observaciones       TEXT
);

CREATE TABLE IF NOT EXISTS venta_detalle (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    venta_id            INTEGER NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
    codigo_articulo     TEXT NOT NULL REFERENCES productos(codigo_articulo) ON DELETE RESTRICT,
    cantidad            INTEGER NOT NULL CHECK (cantidad > 0),
    precio_costo        REAL NOT NULL,     -- costo unitario en el momento de la venta
    precio_publico      REAL NOT NULL,     -- precio unitario al que se vendio
    total               REAL NOT NULL,     -- cantidad * precio_publico
    ganancia            REAL NOT NULL      -- total - (cantidad * precio_costo)
);

CREATE INDEX IF NOT EXISTS idx_venta_detalle_venta ON venta_detalle(venta_id);
CREATE INDEX IF NOT EXISTS idx_venta_detalle_producto ON venta_detalle(codigo_articulo);

CREATE TABLE IF NOT EXISTS venta_pagos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    venta_id            INTEGER NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
    forma_pago          TEXT NOT NULL CHECK (forma_pago IN ('Efectivo', 'Transferencia', 'Tarjeta', 'Otro')),
    monto               REAL NOT NULL CHECK (monto > 0)
);

CREATE INDEX IF NOT EXISTS idx_venta_pagos_venta ON venta_pagos(venta_id);


-- ============================================================
-- VISTAS (el equivalente a tus antiguas hojas calculadas de Excel)
-- ============================================================

-- Piezas recibidas (Casa+Local, SIN contar lo entregado a Asociado)
-- y su costo real proporcional, por producto.
CREATE VIEW IF NOT EXISTS vw_stock_recibido AS
SELECT
    p.codigo_articulo,
    pr.descripcion,
    SUM(p.cantidad_casa + p.cantidad_local)                                   AS piezas_recibidas,
    SUM(
        CASE WHEN p.cantidad_surtida > 0
             THEN p.precio_que_pagas * (p.cantidad_casa + p.cantidad_local) * 1.0 / p.cantidad_surtida
             ELSE 0 END
    )                                                                          AS total_pagado_real,
    SUM(
        CASE WHEN p.cantidad_surtida > 0
             THEN p.valor_total_con_iva * (p.cantidad_casa + p.cantidad_local) * 1.0 / p.cantidad_surtida
             ELSE 0 END
    )                                                                          AS valor_catalogo_total
FROM pedido_detalle p
JOIN productos pr ON pr.codigo_articulo = p.codigo_articulo
GROUP BY p.codigo_articulo, pr.descripcion;

-- Piezas vendidas por producto (de venta_detalle)
CREATE VIEW IF NOT EXISTS vw_piezas_vendidas AS
SELECT codigo_articulo, SUM(cantidad) AS piezas_vendidas
FROM venta_detalle
GROUP BY codigo_articulo;

-- Existencias: la vista principal que reemplaza tu hoja "Existencias"
CREATE VIEW IF NOT EXISTS vw_existencias AS
SELECT
    r.codigo_articulo,
    r.descripcion,
    r.piezas_recibidas,
    COALESCE(v.piezas_vendidas, 0)                                  AS piezas_vendidas,
    r.piezas_recibidas - COALESCE(v.piezas_vendidas, 0)             AS piezas_disponibles,
    ROUND(r.total_pagado_real, 2)                                   AS total_pagado_real,
    ROUND(r.valor_catalogo_total, 2)                                AS valor_catalogo_total,
    CASE WHEN r.piezas_recibidas > 0
         THEN ROUND(r.total_pagado_real / r.piezas_recibidas, 2)
         ELSE 0 END                                                 AS precio_unitario_costo
FROM vw_stock_recibido r
LEFT JOIN vw_piezas_vendidas v ON v.codigo_articulo = r.codigo_articulo;

-- Saldo pendiente por Asociado (recalculado; el trigger de abajo
-- mantiene ademas la columna asociados.saldo_pendiente al vuelo)
CREATE VIEW IF NOT EXISTS vw_saldo_asociados AS
SELECT
    a.id                                                             AS asociado_id,
    a.nombre,
    a.telefono,
    a.status,
    COUNT(DISTINCT e.id)                                             AS num_entregas,
    ROUND(COALESCE(SUM(e.monto_que_debe), 0), 2)                     AS total_debe,
    ROUND(COALESCE((
        SELECT SUM(ep.monto) FROM entrega_pagos ep
        JOIN entregas_asociado e2 ON e2.id = ep.entrega_id
        WHERE e2.asociado_id = a.id
    ), 0), 2)                                                        AS total_pagado,
    ROUND(COALESCE(SUM(e.monto_que_debe), 0) - COALESCE((
        SELECT SUM(ep.monto) FROM entrega_pagos ep
        JOIN entregas_asociado e2 ON e2.id = ep.entrega_id
        WHERE e2.asociado_id = a.id
    ), 0), 2)                                                        AS saldo_pendiente
FROM asociados a
LEFT JOIN entregas_asociado e ON e.asociado_id = a.id
GROUP BY a.id, a.nombre, a.telefono, a.status;


-- ============================================================
-- TRIGGERS: mantienen asociados.saldo_pendiente actualizado
-- (evita tener que recalcular la vista completa en cada consulta
-- del Dashboard; la vista vw_saldo_asociados sigue disponible
-- como fuente de verdad para auditar/recalcular si hiciera falta)
-- ============================================================

CREATE TRIGGER IF NOT EXISTS trg_entrega_insert
AFTER INSERT ON entregas_asociado
BEGIN
    UPDATE asociados
    SET saldo_pendiente = saldo_pendiente + NEW.monto_que_debe
    WHERE id = NEW.asociado_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_pago_insert
AFTER INSERT ON entrega_pagos
BEGIN
    UPDATE asociados
    SET saldo_pendiente = saldo_pendiente - NEW.monto
    WHERE id = (SELECT asociado_id FROM entregas_asociado WHERE id = NEW.entrega_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_pago_delete
AFTER DELETE ON entrega_pagos
BEGIN
    UPDATE asociados
    SET saldo_pendiente = saldo_pendiente + OLD.monto
    WHERE id = (SELECT asociado_id FROM entregas_asociado WHERE id = OLD.entrega_id);
END;
