-- ============================================================
-- Bsale MCP — Migración P0: corrige el doble conteo de ventas
-- ============================================================
-- Qué hace y por qué:
--   documents_snapshot tiene PK (snapshot_date, document_id) y el snapshot
--   corre con days_back=1, por lo que cada documento se guarda ~2 noches.
--   Resultado: ~1,57x filas por documento -> los SUM(total_amount) de
--   ranking/RFM/briefing sobrecuentan ~57%.
--   Esta migración colapsa duplicados, agrega document_type_use (para poder
--   restar notas de crédito) y deja una fila por documento (PK = document_id).
--
-- SEGURIDAD:
--   * Hacer BACKUP/snapshot de la base ANTES de correr (Render: Database ->
--     Backups, o pg_dump). El paso 2 BORRA filas duplicadas.
--   * Correr dentro de una transacción para poder revertir si algo sale mal.
--   * Idempotente: se puede correr de nuevo sin romper.
--
-- Cómo correr:
--   Render -> bsale-mcp-db -> Connect -> PSQL Command  (o "Shell")
--   Pegar este archivo completo.
-- ============================================================

BEGIN;

-- 0. Verificación previa (informativa): cuántas filas vs documentos únicos
--    Antes de migrar deberías ver total_rows > unique_documents.
SELECT count(*)                         AS total_rows,
       count(DISTINCT document_id)      AS unique_documents
FROM documents_snapshot;

-- 1. Columna nueva: document_type_use (0=venta, 1=nota de crédito, 2=guía)
ALTER TABLE documents_snapshot
    ADD COLUMN IF NOT EXISTS document_type_use INTEGER;

-- Backfill desde el JSON crudo
UPDATE documents_snapshot
   SET document_type_use = NULLIF(raw -> 'document_type' ->> 'use', '')::int
 WHERE document_type_use IS NULL
   AND raw -> 'document_type' ->> 'use' IS NOT NULL;

-- 2. Colapsar duplicados: conservar la fila más reciente por document_id
DELETE FROM documents_snapshot a
 USING documents_snapshot b
 WHERE a.document_id = b.document_id
   AND a.snapshot_date < b.snapshot_date;

-- (por si quedaran empates exactos de snapshot_date, deduplicar por ctid)
DELETE FROM documents_snapshot a
 USING documents_snapshot b
 WHERE a.document_id = b.document_id
   AND a.snapshot_date = b.snapshot_date
   AND a.ctid < b.ctid;

-- 3. Reemplazar la PK compuesta por PK simple en document_id
ALTER TABLE documents_snapshot DROP CONSTRAINT IF EXISTS documents_snapshot_pkey;
ALTER TABLE documents_snapshot ADD  PRIMARY KEY (document_id);

-- 4. Índices para acelerar los agregados por fecha y sucursal
CREATE INDEX IF NOT EXISTS ix_docs_emission ON documents_snapshot (emission_date);
CREATE INDEX IF NOT EXISTS ix_docs_office   ON documents_snapshot (office_id);
CREATE INDEX IF NOT EXISTS ix_docs_use      ON documents_snapshot (document_type_use);

-- 5. Verificación posterior: ahora total_rows == unique_documents
SELECT count(*)                    AS total_rows,
       count(DISTINCT document_id) AS unique_documents
FROM documents_snapshot;

-- Si todo se ve bien:
COMMIT;
-- Si algo salió mal, en vez de COMMIT ejecutar:  ROLLBACK;

-- ============================================================
-- NOTA: tras esta migración, snapshot.py debe pasar a upsert por document_id
-- (on_conflict_do_update con index_elements=["document_id"]) y guardar
-- document_type_use. Ver el PR de código correspondiente. Mientras tanto,
-- el snapshot seguirá funcionando: on_conflict sobre la PK nueva simplemente
-- actualizará/ignorará en vez de duplicar.
-- ============================================================
