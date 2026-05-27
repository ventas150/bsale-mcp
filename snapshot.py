"""Snapshot nocturno de data Bsale a Postgres.

Llamado por scheduler (apscheduler) o manualmente via tool.
Hace upsert idempotente para que correrlo dos veces no rompa nada.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from bsale_client import doc_revenue_signed, get_client, is_sales_doc, iso_to_epoch_range
from db import (
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
            "client_id": client_ref.get("id"),
            "total_amount": float(doc.get("totalAmount", 0) or 0),
            "net_amount": float(doc.get("netAmount", 0) or 0),
            "tax_amount": float(doc.get("taxAmount", 0) or 0),
            "state": doc.get("state"),
            "raw": doc,
        })

    if rows:
        with db_session() as s:
            stmt = pg_insert(documents_snapshot).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["snapshot_date", "document_id"])
            s.execute(stmt)

    return {"snapshot_ts": snapshot_ts.isoformat(), "rows": len(rows), "days_back": days_back}


def snapshot_stock(max_pages: int = 50) -> dict[str, Any]:
    """Snapshot del stock actual por sucursal."""
    client = get_client()
    snapshot_ts = datetime.now(timezone.utc)

    items = client.paginated_get(
        "/v1/stocks.json",
        params={"limit": 50, "expand": "[variant,office]"},
        max_pages=max_pages,
    )

    rows = []
    for item in items:
        variant = item.get("variant") or {}
        office = item.get("office") or {}
        rows.append({
            "snapshot_date": snapshot_ts,
            "variant_id": variant.get("id"),
            "office_id": office.get("id"),
            "quantity": float(item.get("quantity", 0) or 0),
            "variant_code": variant.get("code"),
            "office_name": office.get("name"),
        })

    # Dedup por (snapshot_date, variant_id, office_id)
    seen = set()
    deduped = []
    for r in rows:
        key = (r["snapshot_date"], r["variant_id"], r["office_id"])
        if r["variant_id"] is None or r["office_id"] is None:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    if deduped:
        with db_session() as s:
            stmt = pg_insert(stock_snapshot).values(deduped)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["snapshot_date", "variant_id", "office_id"]
            )
            s.execute(stmt)

    return {"snapshot_ts": snapshot_ts.isoformat(), "rows": len(deduped)}


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
        with db_session() as s:
            stmt = pg_insert(variants_snapshot).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["snapshot_date", "variant_id"])
            s.execute(stmt)

    return {"snapshot_ts": snapshot_ts.isoformat(), "rows": len(rows)}


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

    logger.info("Snapshot nocturno completado: %s", results)
    return results
