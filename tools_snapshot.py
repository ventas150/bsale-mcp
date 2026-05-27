"""Tools que exponen el snapshot Postgres como MCP tools.

Solo se registran si DATABASE_URL esta seteado (ver server.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select

from db import (
    document_details_snapshot,
    documents_snapshot,
    session as db_session,
    stock_snapshot,
    variants_snapshot,
)
from snapshot import (
    nightly_snapshot,
    snapshot_details,
    snapshot_documents,
    snapshot_stock,
    snapshot_variants,
)


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de snapshot."""

    @mcp.tool()
    def bsale_snapshot_run_now(
        target: str = "all",
        days_back: int = 1,
    ) -> dict[str, Any]:
        """Corre snapshot ahora mismo (manual). WRITE OP (a DB local).

        Args:
            target: 'all', 'documents', 'stock', 'variants', 'details'.
            days_back: Solo aplica para 'documents'. Para backfill usar 30, 60, 90.
        """
        if target == "documents":
            return snapshot_documents(days_back=days_back)
        if target == "stock":
            return snapshot_stock()
        if target == "variants":
            return snapshot_variants()
        if target == "details":
            return snapshot_details(batch_size=100, max_docs=500)
        return nightly_snapshot()

    @mcp.tool()
    def bsale_snapshot_details_batch(
        batch_size: int = 100,
        max_docs: int = 200,
        only_recent_days: int | None = None,
    ) -> dict[str, Any]:
        """Fetch details (line items) para docs sin details aun. WRITE OP.

        Una sola llamada procesa hasta `batch_size` docs (1 API call/doc a Bsale).
        Para backfill grande, llamar varias veces hasta remaining_to_process=0.

        Args:
            batch_size: Docs a procesar por llamada (default 100).
            max_docs: Cap absoluto de docs a procesar en esta llamada.
            only_recent_days: Si pasa N, solo procesa docs ultimos N dias.
        """
        return snapshot_details(
            batch_size=batch_size, max_docs=max_docs, only_recent_days=only_recent_days,
        )

    @mcp.tool()
    def bsale_snapshot_status() -> dict[str, Any]:
        """Devuelve cuando fue el ultimo snapshot exitoso de cada tabla."""
        with db_session() as s:
            doc_max = s.execute(select(func.max(documents_snapshot.c.snapshot_date))).scalar()
            stock_max = s.execute(select(func.max(stock_snapshot.c.snapshot_date))).scalar()
            var_max = s.execute(select(func.max(variants_snapshot.c.snapshot_date))).scalar()
            det_max = s.execute(select(func.max(document_details_snapshot.c.fetched_at))).scalar()

            doc_count = s.execute(select(func.count()).select_from(documents_snapshot)).scalar()
            stock_count = s.execute(select(func.count()).select_from(stock_snapshot)).scalar()
            var_count = s.execute(select(func.count()).select_from(variants_snapshot)).scalar()
            det_count = s.execute(select(func.count()).select_from(document_details_snapshot)).scalar()
            det_docs_count = s.execute(
                select(func.count(func.distinct(document_details_snapshot.c.document_id)))
            ).scalar()

            # Min y max emission_date en documents_snapshot
            doc_min_emit = s.execute(select(func.min(documents_snapshot.c.emission_date))).scalar()
            doc_max_emit = s.execute(select(func.max(documents_snapshot.c.emission_date))).scalar()

        return {
            "documents": {
                "last_snapshot": doc_max.isoformat() if doc_max else None,
                "total_rows": doc_count,
                "emission_date_min": doc_min_emit.isoformat() if doc_min_emit else None,
                "emission_date_max": doc_max_emit.isoformat() if doc_max_emit else None,
            },
            "details": {
                "last_fetched": det_max.isoformat() if det_max else None,
                "total_line_items": det_count,
                "unique_documents": det_docs_count,
            },
            "stock": {
                "last_snapshot": stock_max.isoformat() if stock_max else None,
                "total_rows": stock_count,
            },
            "variants": {
                "last_snapshot": var_max.isoformat() if var_max else None,
                "total_rows": var_count,
            },
        }

    @mcp.tool()
    def bsale_ventas_fast(
        date_from: str,
        date_to: str,
        office_id: int | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Lee ventas desde el SNAPSHOT (Postgres local), no de Bsale en vivo.

        ~100x mas rapido que bsale_ventas_agregadas para rangos historicos.
        Requiere que el snapshot este al dia.

        Args:
            date_from: YYYY-MM-DD inicio.
            date_to: YYYY-MM-DD fin.
            office_id: Filtra por sucursal.
            limit: Max documentos a retornar.
        """
        start_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        with db_session() as s:
            stmt = select(
                documents_snapshot.c.document_id,
                documents_snapshot.c.emission_date,
                documents_snapshot.c.office_id,
                documents_snapshot.c.office_name,
                documents_snapshot.c.document_type_name,
                documents_snapshot.c.client_id,
                documents_snapshot.c.total_amount,
                documents_snapshot.c.net_amount,
            ).where(
                documents_snapshot.c.emission_date.between(start_dt, end_dt)
            )
            if office_id:
                stmt = stmt.where(documents_snapshot.c.office_id == office_id)
            stmt = stmt.order_by(desc(documents_snapshot.c.emission_date)).limit(limit)

            rows = s.execute(stmt).fetchall()

        docs = [dict(r._mapping) for r in rows]
        for d in docs:
            if d.get("emission_date"):
                d["emission_date"] = d["emission_date"].isoformat()
        total_amount = sum(d.get("total_amount", 0) or 0 for d in docs)
        return {
            "source": "snapshot",
            "period": {"from": date_from, "to": date_to},
            "office_id": office_id,
            "count": len(docs),
            "total_amount": total_amount,
            "documents": docs[:200],
        }
