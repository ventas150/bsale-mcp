"""Tools que exponen el snapshot Postgres como MCP tools.

Solo se registran si DATABASE_URL esta seteado (ver server.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select

from db import documents_snapshot, session as db_session, stock_snapshot, variants_snapshot
from snapshot import nightly_snapshot, snapshot_documents, snapshot_stock, snapshot_variants


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de snapshot."""

    @mcp.tool()
    def bsale_snapshot_run_now(target: str = "all") -> dict[str, Any]:
        """Corre snapshot ahora mismo (manual). WRITE OP (a DB local).

        Args:
            target: 'all', 'documents', 'stock' o 'variants'.
        """
        if target == "documents":
            return snapshot_documents(days_back=1)
        if target == "stock":
            return snapshot_stock()
        if target == "variants":
            return snapshot_variants()
        return nightly_snapshot()

    @mcp.tool()
    def bsale_snapshot_status() -> dict[str, Any]:
        """Devuelve cuando fue el ultimo snapshot exitoso de cada tabla."""
        with db_session() as s:
            doc_max = s.execute(select(func.max(documents_snapshot.c.snapshot_date))).scalar()
            stock_max = s.execute(select(func.max(stock_snapshot.c.snapshot_date))).scalar()
            var_max = s.execute(select(func.max(variants_snapshot.c.snapshot_date))).scalar()

            doc_count = s.execute(select(func.count()).select_from(documents_snapshot)).scalar()
            stock_count = s.execute(select(func.count()).select_from(stock_snapshot)).scalar()
            var_count = s.execute(select(func.count()).select_from(variants_snapshot)).scalar()

        return {
            "documents": {
                "last_snapshot": doc_max.isoformat() if doc_max else None,
                "total_rows": doc_count,
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
        # Convertir strings a datetime con timezone
        start_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        with db_session() as s:
            stmt = select(documents_snapshot).where(
                documents_snapshot.c.emission_date.between(start_dt, end_dt)
            )
            if office_id:
                stmt = stmt.where(documents_snapshot.c.office_id == office_id)
            stmt = stmt.order_by(desc(documents_snapshot.c.emission_date)).limit(limit)

            rows = s.execute(stmt).fetchall()

        docs = [dict(r._mapping) for r in rows]
        total_amount = sum(d.get("total_amount", 0) or 0 for d in docs)
        return {
            "source": "snapshot",
            "period": {"from": date_from, "to": date_to},
            "office_id": office_id,
            "count": len(docs),
            "total_amount": total_amount,
            "documents": docs[:200],  # cap para no explotar response
        }
