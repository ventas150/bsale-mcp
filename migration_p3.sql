-- Migracion P3 (07-sep-2026). Salio de la auditoria del MCP.
--
-- /health llama snapshot_lag_hours() en cada chequeo, y esa funcion hace
-- max(snapshot_date) sobre documents_snapshot. La tabla tiene indices en
-- emission_date, office_id, document_type_use y PK en document_id, pero
-- NINGUNO en snapshot_date: era un seq scan sobre ~160k filas con una columna
-- raw JSONB, en cada healthcheck, en una instancia de 0,5 vCPU.
CREATE INDEX IF NOT EXISTS ix_documents_snapshot_date
    ON documents_snapshot (snapshot_date DESC);

-- cobertura_de_detalle() cuenta document_id distintos por rango de fecha.
CREATE INDEX IF NOT EXISTS ix_details_emission_document
    ON document_details_snapshot (emission_date, document_id);
