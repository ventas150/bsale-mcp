"""Tools que exponen el snapshot Postgres como MCP tools.

Solo se registran si DATABASE_URL esta seteado (ver server.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, func, select

from db import (
    document_details_snapshot,
    documents_snapshot,
    official_sale_conditions,
    official_sale_supported,
    session as db_session,
    signed_amount,
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
        start_date: str | None = None,
        end_date: str | None = None,
        office_id: int | None = None,
        incluir_documentos: bool = False,
        limit: int = 200,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Venta oficial de un periodo leida del SNAPSHOT (Postgres), no de Bsale.

        Devuelve AGREGADOS por defecto. Los totales se calculan siempre sobre
        el periodo completo en SQL, nunca sobre las filas devueltas: `limit`
        solo recorta la lista de documentos cuando se piden explicitamente.

        Venta oficial = Boletas + Facturas + ND - NC. Excluye guias de
        despacho, notas de venta / pedidos web / cotizaciones y anulados.

        Args:
            start_date: YYYY-MM-DD inicio (alias: date_from).
            end_date: YYYY-MM-DD fin (alias: date_to).
            office_id: Filtra por sucursal.
            incluir_documentos: True para adjuntar el detalle documento a
                documento. Por defecto False: pesa mucho y casi nunca se usa.
            limit: Tope de documentos a listar cuando incluir_documentos=True.
        """
        start_date = start_date or date_from
        end_date = end_date or date_to
        if not start_date or not end_date:
            return {
                "error": "Faltan fechas. Usar start_date y end_date en formato YYYY-MM-DD.",
            }

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        d = documents_snapshot.c
        amt = signed_amount(d.total_amount, d.document_type_use)
        net = signed_amount(d.net_amount, d.document_type_use)

        where = [d.emission_date.between(start_dt, end_dt)]
        where += official_sale_conditions(documents_snapshot)
        if office_id:
            where.append(d.office_id == office_id)
        cond = and_(*where)

        with db_session() as s:
            total_row = s.execute(
                select(
                    func.count().label("docs"),
                    func.coalesce(func.sum(amt), 0.0).label("total"),
                    func.coalesce(func.sum(net), 0.0).label("neto"),
                ).where(cond)
            ).one()

            by_office = [
                {
                    "office_id": r.office_id,
                    "office_name": (r.office_name or "").strip(),
                    "count": r.docs,
                    "amount": float(r.total or 0),
                }
                for r in s.execute(
                    select(
                        d.office_id,
                        d.office_name,
                        func.count().label("docs"),
                        func.sum(amt).label("total"),
                    )
                    .where(cond)
                    .group_by(d.office_id, d.office_name)
                    .order_by(desc(func.sum(amt)))
                ).all()
            ]

            by_doctype = [
                {
                    "document_type_id": r.document_type_id,
                    "type_name": (r.document_type_name or "").strip(),
                    "count": r.docs,
                    "amount": float(r.total or 0),
                }
                for r in s.execute(
                    select(
                        d.document_type_id,
                        d.document_type_name,
                        func.count().label("docs"),
                        func.sum(amt).label("total"),
                    )
                    .where(cond)
                    .group_by(d.document_type_id, d.document_type_name)
                ).all()
            ]

            day = func.date_trunc("day", d.emission_date)
            by_day = [
                {
                    "day": r.day.date().isoformat() if r.day else None,
                    "count": r.docs,
                    "amount": float(r.total or 0),
                }
                for r in s.execute(
                    select(
                        day.label("day"),
                        func.count().label("docs"),
                        func.sum(amt).label("total"),
                    )
                    .where(cond)
                    .group_by(day)
                    .order_by(day)
                ).all()
            ]

            # Lo que se dejo fuera, para que nadie tenga que adivinar el delta
            excl_where = [d.emission_date.between(start_dt, end_dt)]
            if office_id:
                excl_where.append(d.office_id == office_id)
            excluidos = s.execute(
                select(
                    func.count().label("docs"),
                    func.coalesce(func.sum(d.total_amount), 0.0).label("total"),
                ).where(and_(*excl_where, ~and_(*official_sale_conditions(documents_snapshot))))
            ).one()

            documentos = []
            if incluir_documentos:
                rows = s.execute(
                    select(
                        d.document_id,
                        d.emission_date,
                        d.office_id,
                        d.office_name,
                        d.document_type_name,
                        d.client_id,
                        amt.label("signed_total"),
                        d.total_amount,
                        d.net_amount,
                    )
                    .where(cond)
                    .order_by(desc(d.emission_date))
                    .limit(limit)
                ).all()
                documentos = [
                    {
                        "document_id": r.document_id,
                        "emission_date": r.emission_date.isoformat() if r.emission_date else None,
                        "office_id": r.office_id,
                        "office_name": (r.office_name or "").strip(),
                        "document_type_name": (r.document_type_name or "").strip(),
                        "client_id": r.client_id,
                        "amount_signed": float(r.signed_total or 0),
                        "total_amount": float(r.total_amount or 0),
                        "net_amount": float(r.net_amount or 0),
                    }
                    for r in rows
                ]

        return {
            "source": "snapshot",
            "period": {"start": start_date, "end": end_date},
            "office_id": office_id,
            "regla": "venta oficial = Boletas + Facturas + ND - NC (sin notas de venta, sin guias, sin anulados)",
            "reglas_aplicadas": official_sale_supported(documents_snapshot),
            "documentos_de_venta": total_row.docs,
            "venta_oficial": float(total_row.total or 0),
            "venta_oficial_neta": float(total_row.neto or 0),
            "excluidos": {
                "documentos": excluidos.docs,
                "monto_bruto": float(excluidos.total or 0),
                "detalle": "guias de despacho + notas de venta/pedidos web/cotizaciones + anulados",
            },
            "by_office": by_office,
            "by_document_type": by_doctype,
            "by_day": by_day,
            "documentos": documentos,
            "nota_documentos": (
                None
                if incluir_documentos
                else "Agregados solamente. Pasar incluir_documentos=True para el detalle."
            ),
        }

    @mcp.tool()
    def bsale_conciliacion_venta(
        start_date: str,
        end_date: str,
        office_id: int | None = None,
        max_documents: int = 40000,
    ) -> dict[str, Any]:
        """Concilia la venta del SNAPSHOT contra Bsale EN VIVO y explica la brecha.

        Existe para cerrar el KPI "Conciliacion interna Bsale", en rojo desde
        el 22-jul-2026. Devuelve las dos cifras, la diferencia, y el desglose
        de por que difieren (documentos que faltan en el snapshot, documentos
        que sobran, montos distintos).

        Args:
            start_date: YYYY-MM-DD inicio.
            end_date: YYYY-MM-DD fin.
            office_id: Filtrar por sucursal.
            max_documents: Tope de documentos a leer de Bsale en vivo.
        """
        from bsale_client import doc_revenue_signed, get_client, is_official_sale

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        # --- Lado Bsale en vivo ---
        client = get_client()
        fetch = client.paginated_fetch(
            "/v1/documents.json",
            params={
                "limit": 50,
                "emissiondaterange": f"{int(start_dt.timestamp())},{int(end_dt.timestamp())}",
                "officeid": office_id,
                "state": 0,
                "expand": "[document_type,office]",
            },
            max_items=max_documents,
        )
        vivo = {
            int(d["id"]): doc_revenue_signed(d)
            for d in fetch["items"]
            if d.get("id") is not None and is_official_sale(d)
        }

        # --- Lado snapshot ---
        d = documents_snapshot.c
        amt = signed_amount(d.total_amount, d.document_type_use)
        where = [d.emission_date.between(start_dt, end_dt)]
        where += official_sale_conditions(documents_snapshot)
        if office_id:
            where.append(d.office_id == office_id)

        with db_session() as s:
            snap = {
                int(r.document_id): float(r.monto or 0)
                for r in s.execute(
                    select(d.document_id, amt.label("monto")).where(and_(*where))
                ).all()
            }

        solo_vivo = sorted(set(vivo) - set(snap))
        solo_snap = sorted(set(snap) - set(vivo))
        distintos = [
            {"document_id": k, "vivo": vivo[k], "snapshot": snap[k]}
            for k in (set(vivo) & set(snap))
            if abs(vivo[k] - snap[k]) > 1
        ]

        total_vivo = sum(vivo.values())
        total_snap = sum(snap.values())
        diff = total_snap - total_vivo

        return {
            "period": {"start": start_date, "end": end_date},
            "office_id": office_id,
            "venta_oficial_vivo": total_vivo,
            "venta_oficial_snapshot": total_snap,
            "diferencia": diff,
            "diferencia_pct": round(diff / total_vivo * 100, 2) if total_vivo else None,
            "documentos_vivo": len(vivo),
            "documentos_snapshot": len(snap),
            "brecha": {
                "faltan_en_snapshot": {
                    "count": len(solo_vivo),
                    "monto": sum(vivo[k] for k in solo_vivo),
                    "ejemplos": solo_vivo[:20],
                },
                "sobran_en_snapshot": {
                    "count": len(solo_snap),
                    "monto": sum(snap[k] for k in solo_snap),
                    "ejemplos": solo_snap[:20],
                },
                "monto_distinto": {
                    "count": len(distintos),
                    "ejemplos": distintos[:20],
                },
            },
            "truncado_lado_vivo": fetch["truncated"],
            "documentos_en_bsale": fetch["total_count"],
        }
