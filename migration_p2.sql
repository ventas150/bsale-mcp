-- migration_p2.sql — 07-sep-2026
-- Indices. Verificar con pg_stat_user_indexes antes de correr los DROP.

-- 1) Sostiene el EXISTS que hereda la venta oficial del documento cabecera
--    a las lineas (ver _detalle_de_venta_oficial en tools_intelligence_db.py).
CREATE INDEX IF NOT EXISTS ix_details_document_id
    ON document_details_snapshot (document_id);

-- 2) El patron dominante de los tools de velocity es
--    WHERE emission_date >= X GROUP BY variant_id.
--    El indice que habia era (emission_date, variant_code), que sirve para el
--    top por codigo, no para agrupar por variant_id.
CREATE INDEX IF NOT EXISTS ix_details_emission_variant_id
    ON document_details_snapshot (emission_date, variant_id);

-- 3) La subquery de servicios (unlimitedStock=1) escaneaba el JSONB de todo el
--    catalogo, tres veces por tool, sin ningun indice que la sostenga.
CREATE INDEX IF NOT EXISTS ix_variants_service
    ON variants_snapshot (variant_id)
    WHERE (raw ->> 'unlimitedStock') = '1';

-- 4) Redundantes sobre una tabla de 7,4 millones de filas: encarecen cada
--    insercion de la foto de stock (235k filas) sin acelerar ninguna consulta.
--    ix_stock_snapshot_date duplica la columna lider de la PK
--    (snapshot_date, variant_id, office_id); ix_stock_snapshot_office indexa
--    ~10 valores distintos y el planner nunca lo elige.
--    Descomentar despues de confirmar idx_scan = 0:
--      SELECT indexrelname, idx_scan FROM pg_stat_user_indexes
--       WHERE relname = 'stock_snapshot';
-- DROP INDEX IF EXISTS ix_stock_snapshot_date;
-- DROP INDEX IF EXISTS ix_stock_snapshot_office;
