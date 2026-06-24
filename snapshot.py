"""Snapshot nocturno de data Bsale a Postgres.

Llamado por el Render Cron Job (cron_snapshot.py) o manualmente via tool.
Hace upsert idempotente para que correrlo dos veces no rompa nada.

documents_snapshot: una fila por document_id (PK = document_id) -> upsert.
Esto evita el doble conteo que existia con la PK compuesta (snapshot_date, document_id).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bsale_client import doc_revenue_signed, get_client, is_sales_doc, iso_to_epoch_range
from db import (
    document_details_snapshot,
    documents_snapshot,
    stock_snapshot,
    variants_snapshot,
    session as db_session,
)

logger = logging.getLogger(__name__)


def _ts_to_dt(ts: Any) -> datetime | None:
    """Convierte timestamp Bsale (segundos epoch) a datetime UTC."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def snapshot_documents(days_back: int = 1, max_pages: int = 200) -> dict[str, Any]:
    """Snapshot de documents emitidos en los ultimos N dias.

    Default 1 dia (para snapshot nocturno).
    Para backfill historico, llamar con days_back grande.
    """
    client = get_client()
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days_back)
    snapshot_ts = datetime.now(timezone.utc)

    params = {
        "limit": 50,
        "emissiondaterange": iso_to_epoch_range(start_date.isoformat(), end_date.isoformat()),
        "expand": "[document_type,office,client]",
    }
    docs = client.paginated_get("/v1/documents.json", params=params, max_pages=max_pages)

    rows = []
    for doc in docs:
        # Excluir guias de despacho (use=2) del snapshot tambien
        if not is_sales_doc(doc):
            continue
        office = doc.get("office") or {}
        doctype = doc.get("document_type") or {}
        client_ref = doc.get("client") or {}
        rows.append({
            "snapshot_date": snapshot_ts,
            "document_id": doc.get("id"),
            "emission_date": _ts_to_dt(doc.get("emissionDate")),
            "office_id": office.get("id"),
            "office_name": office.get("name"),
            "document_type_id": doctype.get("id"),
            "document_type_name": doctype.get("name"),
            "document_type_use": doctype.get("use", 0),
            "client_id": client_ref.get("id"),
            "total_amount": float(doc.get("totalAmount", 0) or 0),
            "net_amount": float(doc.get("netAmount", 0) or 0),
            "tax_amount": float(doc.get("taxAmount", 0) or 0),
            "state": doc.get("state"),
            "raw": doc,
        })

    # Dedupe por document_id: ON CONFLICT DO UPDATE no permite afectar la misma
    # fila dos veces en un mismo statement (CardinalityViolation). Bsale a veces
    # devuelve el mismo documento en dos paginas, asi que limpiamos antes del upsert.
    if rows:
        dedup: dict[Any, dict[str, Any]] = {}
        for r in rows:
            dedup[r["document_id"]] = r
        rows = list(dedup.values())

    if rows:
        # Chunked insert (500 por chunk) para evitar SSL EOF en queries enormes
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            with db_session() as s:
                stmt = pg_insert(documents_snapshot).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["document_id"],
                    set_={
                        "snapshot_date": stmt.excluded.snapshot_date,
                        "emission_date": stmt.excluded.emission_date,
                        "office_id": stmt.excluded.office_id,
                        "office_name": stmt.excluded.office_name,
                        "document_type_id": stmt.excluded.document_type_id,
                        "document_type_name": stmt.excluded.document_type_name,
                        "document_type_use": stmt.excluded.document_type_use,
                        "client_id": stmt.excluded.client_id,
                        "total_amount": stmt.excluded.total_amount,
                        "net_amount": stmt.excluded.net_amount,
                        "tax_amount": stmt.excluded.tax_amount,
                        "state": stmt.excluded.state,
                        "raw": stmt.excluded.raw,
                    },
                )
                s.execute(stmt)

    return {"snapshot_ts": snapshot_ts.isoformat(), "rows": len(rows), "days_back": days_back}


def snapshot_stock(max_pages: int = 500) -> dict[str, Any]:
    """Snapshot del stock actual por sucursal.

    Pagina incrementalmente y persiste cada N paginas para no perder data si timeout.
    """
    client = get_client()
    snapshot_ts = datetime.now(timezone.utc)

    PERSIST_EVERY = 10  # persiste cada 10 paginas (500 rows) para no perder progreso
    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    total_persisted = 0

    def _flush(buffer: list[dict[str, Any]]) -> int:
        if not buffer:
            return 0
        with db_session() as s:
            stmt = pg_insert(stock_snapshot).values(buffer)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["snapshot_date", "variant_id", "office_id"]
            )
            s.execute(stmt)
        return len(buffer)

    for page in range(max_pages):
        try:
            data = client.get(
                "/v1/stocks.json",
                params={"limit": 50, "offset": page * 50, "expand": "[variant,office]"},
                use_cache=False,
            )
        except Exception:  # noqa: BLE001
            break
        items = data.get("items", []) or []
        if not items:
            break

        for item in items:
            variant = item.get("variant") or {}
            office = item.get("office") or {}
            vid = variant.get("id")
            oid = office.get("id")
            if vid is None or oid is None:
                continue
            key = (snapshot_ts, vid, oid)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "snapshot_date": snapshot_ts,
                "variant_id": vid,
                "office_id": oid,
                "quantity": float(item.get("quantity", 0) or 0),
                "variant_code": variant.get("code"),
                "office_name": office.get("name"),
            })

        # Flush incremental cada N paginas
        if (page + 1) % PERSIST_EVERY == 0 and rows:
            total_persisted += _flush(rows)
            rows = []

        if len(items) < 50:
            break

    # Flush remaining
    if rows:
        total_persisted += _flush(rows)

    return {
        "snapshot_ts": snapshot_ts.isoformat(),
        "rows": total_persisted,
        "max_pages_attempted": max_pages,
    }


def snapshot_variants(max_pages: int = 100) -> dict[str, Any]:
    """Snapshot del catalogo de variantes."""
    client = get_client()
    snapshot_ts = datetime.now(timezone.utc)

    items = client.paginated_get(
        "/v1/variants.json",
        params={"limit": 50},
        max_pages=max_pages,
    )

    rows = []
    seen = set()
    for v in items:
        vid = v.get("id")
        if vid is None or vid in seen:
            continue
        seen.add(vid)
        product = v.get("product") or {}
        rows.append({
            "snapshot_date": snapshot_ts,
            "variant_id": vid,
            "product_id": product.get("id"),
            "code": v.get("code"),
            "barcode": v.get("barCode"),
            "description": v.get("description"),
            "state": v.get("state"),
            "raw": v,
        })

    if rows:
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            with db_session() as s:
                stmt = pg_insert(variants_snapshot).values(chunk)
                stmt = stmt.on_conflict_do_nothing(index_elements=["snapshot_date", "variant_id"])
                s.execute(stmt)

    return {"snapshot_ts": snapshot_ts.isoformat(), "rows": len(rows)}


def snapshot_details(
    batch_size: int = 50,
    max_docs: int = 500,
    only_recent_days: int | None = None,
) -> dict[str, Any]:
    """Para docs en documents_snapshot que aun no tienen details, fetch y store.

    Bsale requiere 1 API call por documento para sus details. Esta funcion
    es la mas costosa - se ejecuta en batches para no timeout.

    Args:
        batch_size: Cuantos docs procesar por llamada.
        max_docs: Cap absoluto de docs a procesar en esta llamada.
        only_recent_days: Si pasa N, solo procesa docs con emission_date >= now - N dias.
            None = todos los docs sin details aun.

    Returns:
        Dict con count de docs procesados, lineas insertadas, errores.
    """
    client = get_client()

    # 1. Encuentra docs sin details aun (LEFT JOIN antimatch)
    with db_session() as s:
        existing_doc_ids = set(s.execute(
            select(document_details_snapshot.c.document_id).distinct()
        ).scalars().all())

        # Docs candidatos. Seleccionamos document_type_use (columna liviana) en vez
        # de raw (JSONB completo) para no cargar miles de documentos enteros en
        # memoria — esto evita los OOM al ampliar la ventana de dias.
        cand_stmt = select(
            documents_snapshot.c.document_id,
            documents_snapshot.c.emission_date,
            documents_snapshot.c.office_id,
            documents_snapshot.c.document_type_use,
        )
        if only_recent_days:
            from datetime import timedelta as _td
            cutoff = datetime.now(timezone.utc) - _td(days=only_recent_days)
            cand_stmt = cand_stmt.where(documents_snapshot.c.emission_date >= cutoff)
        cand_stmt = cand_stmt.order_by(documents_snapshot.c.emission_date.desc())

        candidates = s.execute(cand_stmt).fetchall()

    # Filtra los que ya tienen details
    todo = [c for c in candidates if c.document_id not in existing_doc_ids][:max_docs]

    rows_inserted = 0
    docs_processed = 0
    errors = 0

    for cand in todo[:batch_size]:
        doc_id = cand.document_id
        emission_date = cand.emission_date
        office_id = cand.office_id
        use = cand.document_type_use or 0

        try:
            resp = client.get(
                f"/v1/documents/{doc_id}/details.json",
                params={"limit": 50, "expand": "[variant]"},
                use_cache=False,
            )
        except Exception:  # noqa: BLE001
            errors += 1
            continue

        items = resp.get("items", []) or []
        line_rows = []
        for line in items:
            variant = line.get("variant") or {}
            line_id = line.get("id")
            if line_id is None:
                continue
            line_rows.append({
                "document_id": doc_id,
                "line_id": line_id,
                "variant_id": variant.get("id"),
                "variant_code": variant.get("code"),
                "variant_description": (variant.get("description") or "")[:500],
                "office_id": office_id,
                "emission_date": emission_date,
                "document_type_use": use,
                "quantity": float(line.get("quantity", 0) or 0),
                "net_amount": float(line.get("netAmount", 0) or 0),
                "total_amount": float(line.get("totalAmount", 0) or 0),
                "fetched_at": datetime.now(timezone.utc),
            })

        if line_rows:
            # Dedupe por (document_id, line_id) por la misma razon que en documentos.
            dedup_lines: dict[tuple, dict[str, Any]] = {}
            for lr in line_rows:
                dedup_lines[(lr["document_id"], lr["line_id"])] = lr
            line_rows = list(dedup_lines.values())

            with db_session() as s:
                stmt = pg_insert(document_details_snapshot).values(line_rows)
                stmt = stmt.on_conflict_do_nothing(index_elements=["document_id", "line_id"])
                s.execute(stmt)
            rows_inserted += len(line_rows)
        docs_processed += 1

    remaining = max(0, len(todo) - batch_size)

    return {
        "docs_processed": docs_processed,
        "lines_inserted": rows_inserted,
        "errors": errors,
        "remaining_to_process": remaining,
        "candidates_total": len(todo),
    }


def nightly_snapshot() -> dict[str, Any]:
    """Job nocturno: ejecuta todos los snapshots en orden."""
    logger.info("Iniciando snapshot nocturno")
    results = {}
    try:
        results["documents"] = snapshot_documents(days_back=1)
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_documents: %s", e)
        results["documents_error"] = str(e)

    try:
        results["stock"] = snapshot_stock()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_stock: %s", e)
        results["stock_error"] = str(e)

    try:
        results["variants"] = snapshot_variants()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_variants: %s", e)
        results["variants_error"] = str(e)

    # Details para docs recientes. Ahora corre en el Cron Job dedicado (no compite
    # con el web service), por lo que se puede subir el batch para mejor cobertura.
    try:
        results["details"] = snapshot_details(batch_size=400, max_docs=400, only_recent_days=3)
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_details: %s", e)
        results["details_error"] = str(e)

    logger.info("Snapshot nocturno completado: %s", results)
    return results
