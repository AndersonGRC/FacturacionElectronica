-- ============================================================
-- Microservicio Facturación Electrónica DIAN — Multi-Tenant
-- schema.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- TABLA: tenants
-- Una fila por empresa cliente del microservicio
-- ============================================================
CREATE TABLE IF NOT EXISTS tenants (
    id                  UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    nombre              VARCHAR(200) NOT NULL,
    nit                 VARCHAR(20)  NOT NULL,
    digito_verificacion SMALLINT     NOT NULL,
    razon_social        VARCHAR(300) NOT NULL,
    api_key_hash        CHAR(64)     NOT NULL,   -- SHA256 hex del API Key, nunca en claro
    ambiente            VARCHAR(20)  NOT NULL DEFAULT 'habilitacion'
                            CHECK (ambiente IN ('habilitacion', 'produccion')),
    -- Certificado digital
    cert_path           VARCHAR(500),            -- ruta absoluta al .p12
    cert_password_enc   TEXT,                    -- contraseña cifrada con Fernet
    -- Credenciales DIAN
    clave_tecnica       VARCHAR(200),            -- asignada por DIAN al registrar el software
    token_dian          TEXT,                    -- JWT OAuth DIAN (renovar cada ~50 min)
    token_dian_expira   TIMESTAMPTZ,             -- timestamp de expiración del token
    -- Resolución de facturación
    resolucion_dian     VARCHAR(50),
    resolucion_fecha    DATE,
    resolucion_desde    BIGINT,                  -- número inicial autorizado
    resolucion_hasta    BIGINT,                  -- número final autorizado
    resolucion_vigencia DATE,                    -- fecha de vencimiento resolución
    prefijo             VARCHAR(10)  DEFAULT '',
    consecutivo_actual  BIGINT       NOT NULL DEFAULT 0,
    -- Estado
    activo              BOOLEAN      NOT NULL DEFAULT TRUE,
    creado_en           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actualizado_en      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Constraints
    CONSTRAINT tenants_nit_unique    UNIQUE (nit),
    CONSTRAINT tenants_apikey_unique UNIQUE (api_key_hash)
);

-- Índice para autenticación en cada request (búsqueda por API Key hash)
CREATE INDEX IF NOT EXISTS idx_tenants_api_key_hash ON tenants (api_key_hash);
CREATE INDEX IF NOT EXISTS idx_tenants_activo        ON tenants (activo) WHERE activo = TRUE;

-- ============================================================
-- TABLA: facturas
-- Tabla transaccional central con aislamiento por tenant_id
-- ============================================================
CREATE TABLE IF NOT EXISTS facturas (
    id                UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id         UUID         NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    referencia_pedido VARCHAR(100) NOT NULL,
    -- Ciclo de vida
    estado            VARCHAR(20)  NOT NULL DEFAULT 'PENDIENTE'
                          CHECK (estado IN ('PENDIENTE','PROCESANDO','ACEPTADA','RECHAZADA','ERROR')),
    -- Resultado DIAN
    numero_factura    VARCHAR(50),
    cufe              VARCHAR(200),              -- Código Único de Factura Electrónica (SHA384)
    -- Rutas de archivos (NO blobs — preservar memoria de BD)
    xml_path          VARCHAR(500),             -- ruta al XML UBL 2.1 firmado
    pdf_path          VARCHAR(500),             -- ruta al PDF representación (Fase 2)
    response_path     VARCHAR(500),             -- ruta al ApplicationResponse DIAN
    -- Payload original del cliente
    datos_json        JSONB        NOT NULL,
    -- Control de errores y reintentos
    error_mensaje     TEXT,
    intentos          SMALLINT     NOT NULL DEFAULT 0,
    celery_task_id    VARCHAR(200),
    -- Timestamps
    creado_en         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actualizado_en    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- ÍNDICE DE IDEMPOTENCIA: un pedido = una factura por tenant (anti doble facturación)
    CONSTRAINT facturas_tenant_ref_unique UNIQUE (tenant_id, referencia_pedido)
);

-- Índices de acceso frecuente
CREATE INDEX IF NOT EXISTS idx_facturas_tenant_id     ON facturas (tenant_id);
CREATE INDEX IF NOT EXISTS idx_facturas_estado        ON facturas (estado);
CREATE INDEX IF NOT EXISTS idx_facturas_referencia    ON facturas (referencia_pedido);
CREATE INDEX IF NOT EXISTS idx_facturas_tenant_estado ON facturas (tenant_id, estado);
CREATE INDEX IF NOT EXISTS idx_facturas_creado_en     ON facturas (creado_en DESC);
-- Índice parcial: solo facturas que aún necesitan procesarse (usado por worker/retry)
CREATE INDEX IF NOT EXISTS idx_facturas_pendientes    ON facturas (tenant_id, creado_en)
    WHERE estado IN ('PENDIENTE','ERROR') AND intentos < 3;

-- ============================================================
-- TABLA: factura_eventos
-- Audit log inmutable del ciclo de vida de cada factura
-- ============================================================
CREATE TABLE IF NOT EXISTS factura_eventos (
    id         BIGSERIAL    PRIMARY KEY,
    factura_id UUID         NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
    evento     VARCHAR(50)  NOT NULL,
    -- Valores de evento: ENCOLADA | PROCESANDO | FIRMADA | ENVIADA |
    --                    ACEPTADA | RECHAZADA | ERROR | REINTENTO
    detalle    TEXT,
    creado_en  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eventos_factura_id ON factura_eventos (factura_id);
CREATE INDEX IF NOT EXISTS idx_eventos_creado_en  ON factura_eventos (creado_en DESC);

-- ============================================================
-- FUNCIÓN: set_updated_at
-- Trigger para actualizar updated_at automáticamente
-- ============================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.actualizado_en = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_facturas_updated_at
    BEFORE UPDATE ON facturas
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- FUNCIÓN: siguiente_consecutivo
-- Incremento atómico del consecutivo de numeración.
-- Usa UPDATE...RETURNING para evitar race conditions entre workers Celery
-- (dos facturas del mismo tenant procesadas en paralelo no pueden
--  obtener el mismo número de factura).
-- ============================================================
CREATE OR REPLACE FUNCTION siguiente_consecutivo(p_tenant_id UUID)
RETURNS BIGINT AS $$
DECLARE
    v_siguiente BIGINT;
BEGIN
    UPDATE tenants
    SET consecutivo_actual = consecutivo_actual + 1
    WHERE id = p_tenant_id AND activo = TRUE
    RETURNING consecutivo_actual INTO v_siguiente;

    IF v_siguiente IS NULL THEN
        RAISE EXCEPTION 'Tenant no encontrado o inactivo: %', p_tenant_id;
    END IF;

    RETURN v_siguiente;
END;
$$ LANGUAGE plpgsql;
