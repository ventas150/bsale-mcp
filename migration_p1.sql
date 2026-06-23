-- ============================================================
-- Bsale MCP — Migración P1: capa LLM (digests) + vista stock actual
-- ============================================================
-- Qué hace y por qué:
--   1. Crea la tabla llm_digests: resúmenes JSON pre-calculados que el
--      agente lee al instante (capa "lenguaje LLM"), sin correr SQL pesado.
--   2. Crea la vista stock_current: el stock de la foto más reciente.
--      stock_snapshot acumula una fila por (snapshot_date, variant, office),
--      así que "stock actual" = filtrar al último snapshot_date. Sin esto,
--      sumar la tabla entera mezcla varios días.
--
-- SEGURIDAD:
--   * Idempotente: usa IF NOT EXISTS / OR REPLACE. Se puede correr de nuevo.
--   * No borra datos. No toca documents/stock/variants existentes.
--
-- Cómo correr:
--   Render -> bsale-mcp-db -> Connect -> PSQL Command (o "Shell")
--   Pegar este archivo completo.
-- ============================================================

BEGIN;

-- 1. Tabla de digests (capa LLM)
CREATE TABLE IF NOT EXISTS llm_digests (
    digest_key    TEXT PRIMARY KEY,            -- ej. 'ventas_hoy', 'stock_resumen'
    data          JSONB NOT NULL,              -- el resumen compacto
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE llm_digests IS
  'Resúmenes JSON pre-calculados para consumo instantáneo del LLM. Se regeneran en cada sync.';

-- 2. Vista de stock actual (última foto)
--    Devuelve solo las filas del snapshot_date más reciente.
CREATE OR REPLACE VIEW stock_current AS
SELECT s.*
FROM stock_snapshot s
WHERE s.snapshot_date = (SELECT max(snapshot_date) FROM stock_snapshot);

COMMENT ON VIEW stock_current IS
  'Stock de la foto más reciente (último snapshot_date de stock_snapshot).';

-- 3. Índices útiles para los digests y la serie histórica de stock
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_date ON stock_snapshot (snapshot_date);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_office ON stock_snapshot (office_id);

-- details ya trae índices por variant_code / emission_date desde db.py;
-- este compuesto acelera el "top SKU del día".
CREATE INDEX IF NOT EXISTS ix_details_emission_variant
    ON document_details_snapshot (emission_date, variant_code);

COMMIT;
-- Si algo salió mal, en vez de COMMIT ejecutar:  ROLLBACK;
