"""Versiones SQL-powered de las intelligence tools.

Estas leen del snapshot Postgres en vez de paginar Bsale en vivo.
~100x mas rapidas. Requieren DATABASE_URL.

Se registran solo si DATABASE_URL esta configurado (ver server.py).

Sobreescriben las versiones de tools_intelligence.py si esta cargado.
Decision: registrar nombres DISTINTOS con sufijo _fast para que ambas coexistan
y el agente pueda elegir. Si las _fast funcionan, las antiguas se pueden retirar.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, desc, func, select

from bsale_client import get_client
from db import (
    document_details_snapshot,
    documents_snapshot,
    session as db_session,
    stock_snapshot,
)


def _coverage_category(days: float) -> str:
    if days < 7:
        return "critico"
    if days < 14:
        return "bajo"
    if days < 30:
        return "ok"
    if days < 60:
        return "alto"
    return "sobrestockeo"


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools SQL-powered."""

    # ============================
    # 1. QUIEBRES PROYECTADOS (FAST)
    # ============================

    @mcp.tool()
    def bsale_quiebres_proyectados_fast(
        days_horizon: int = 14,
        lookback_days: int = 30,
        office_id: int | None = None,
        min_velocity: float = 0.1,
        top_n: int = 100,
    ) -> dict[str, Any]:
        """Quiebres proyectados desde snapshot Postgres. ~100x mas rapido que la version live.

        Requiere snapshot de stock + document_details (corre bsale_snapshot_details_batch primero).

        Args:
            days_horizon: Horizonte de prediccion (default 14d).
            lookback_days: Ventana para calcular velocity (default 30d).
            office_id: Filtra por sucursal. None = totales por variante.
            min_velocity: Ignora variantes con velocity < esto.
            top_n: Max risks a retornar.
        """
        now = datetime.now(timezone.utc)
        lookback_cutoff = now - timedelta(days=lookback_days)

        with db_session() as s:
            # 1. Stock actual: ultimo snapshot por variante×office
            latest_stock = (
                select(
                    stock_snapshot.c.variant_id,
                    stock_snapshot.c.office_id,
                    func.max(stock_snapshot.c.snapshot_date).label("max_ts"),
                )
                .group_by(stock_snapshot.c.variant_id, stock_snapshot.c.office_id)
                .subquery()
            )
            stock_stmt = select(
                stock_snapshot.c.variant_id,
                stock_snapshot.c.office_id,
                stock_snapshot.c.quantity,
                stock_snapshot.c.variant_code,
            ).join(
                latest_stock,
                and_(
                    stock_snapshot.c.variant_id == latest_stock.c.variant_id,
                    stock_snapshot.c.office_id == latest_stock.c.office_id,
                    stock_snapshot.c.snapshot_date == latest_stock.c.max_ts,
                ),
            )
            if office_id:
                stock_stmt = stock_stmt.where(stock_snapshot.c.office_id == office_id)

            stock_rows = s.execute(stock_stmt).fetchall()

            # 2. Velocity: suma de quantity por variante en lookback (excluyendo guias)
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(document_details_snapshot.c.quantity).label("total_qty"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    document_details_snapshot.c.document_type_use != 2,  # excluir guias
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id)
            if office_id:
                vel_stmt = vel_stmt.where(document_details_snapshot.c.office_id == office_id)

            vel_rows = s.execute(vel_stmt).fetchall()
            velocity_by_variant = {r.variant_id: float(r.total_qty or 0) for r in vel_rows}

        # 3. Agregar stock por variante
        stock_by_variant: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"stock_total": 0.0, "stock_by_office": {}, "variant_code": None}
        )
        for r in stock_rows:
            stock_by_variant[r.variant_id]["stock_total"] += float(r.quantity or 0)
            stock_by_variant[r.variant_id]["stock_by_office"][r.office_id] = float(r.quantity or 0)
            if not stock_by_variant[r.variant_id]["variant_code"]:
                stock_by_variant[r.variant_id]["variant_code"] = r.variant_code

        # 4. Computar dias hasta quiebre
        risks = []
        for vid, info in stock_by_variant.items():
            vtot = velocity_by_variant.get(vid, 0)
            vpd = vtot / lookback_days if lookback_days > 0 else 0
            if vpd < min_velocity:
                continue
            stock = info["stock_total"]
            days = stock / vpd if vpd > 0 else 9999
            if days <= days_horizon:
                risks.append({
                    "variant_id": vid,
                    "code": info["variant_code"],
                    "stock_total": stock,
                    "stock_by_office": info["stock_by_office"],
                    "velocity_per_day": round(vpd, 2),
                    "lookback_units": round(vtot, 0),
                    "days_until_stockout": round(days, 1),
                    "category": _coverage_category(days),
                })

        risks.sort(key=lambda r: r["days_until_stockout"])

        return {
            "source": "snapshot",
            "horizon_days": days_horizon,
            "lookback_days": lookback_days,
            "office_id": office_id,
            "total_at_risk": len(risks),
            "risks": risks[:top_n],
        }

    # ============================
    # 2. ALLOCATION SUGERIDA (FAST)
    # ============================

    @mcp.tool()
    def bsale_sugerencia_allocation_fast(
        variant_id: int,
        lookback_days: int = 60,
    ) -> dict[str, Any]:
        """Sugerencia de allocation. Hibrido: stock LIVE de Bsale + velocity desde snapshot.

        Es ~10x mas rapido que la version full-live (1 API call a Bsale en vez de paginar
        miles de docs). Stock siempre actualizado, velocity precalculada.
        """
        now = datetime.now(timezone.utc)
        lookback_cutoff = now - timedelta(days=lookback_days)

        # 1. Stock LIVE de Bsale (1 API call - rapido y siempre actualizado)
        client = get_client()
        stock_data = client.get(
            "/v1/stocks.json",
            params={"variantid": variant_id, "limit": 50, "expand": "[variant,office]"},
            use_cache=False,
        )
        stock_items = stock_data.get("items", []) or []

        # 2. Velocity desde snapshot (rapido SQL)
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.office_id,
                func.sum(document_details_snapshot.c.quantity).label("total_qty"),
            ).where(
                and_(
                    document_details_snapshot.c.variant_id == variant_id,
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    document_details_snapshot.c.document_type_use != 2,
                )
            ).group_by(document_details_snapshot.c.office_id)

            vel_rows = s.execute(vel_stmt).fetchall()

        velocity_by_office = {r.office_id: float(r.total_qty or 0) for r in vel_rows}

        rows = []
        for item in stock_items:
            office = item.get("office") or {}
            oid = office.get("id")
            if oid is None:
                continue
            stock = float(item.get("quantity", 0) or 0)
            v_total = velocity_by_office.get(oid, 0)
            vpd = v_total / lookback_days if lookback_days > 0 else 0
            cov = stock / vpd if vpd > 0 else (9999 if stock > 0 else 0)
            rows.append({
                "office_id": oid,
                "office_name": office.get("name"),
                "stock": stock,
                "velocity_per_day": round(vpd, 2),
                "coverage_days": round(cov, 1),
                "category": _coverage_category(cov),
            })
        rows.sort(key=lambda x: x["coverage_days"])

        # Sugerencias de traspaso
        suggestions = []
        sobre = [r for r in rows if r["coverage_days"] > 60 and r["stock"] > 5]
        quiebre = [r for r in rows if r["coverage_days"] < 14 and r["velocity_per_day"] > 0]
        for q in quiebre:
            target = q["velocity_per_day"] * 30
            need = max(0, target - q["stock"])
            for so in sobre:
                if need <= 0:
                    break
                excess = max(0, so["stock"] - so["velocity_per_day"] * 30)
                give = min(excess, need)
                if give > 0:
                    suggestions.append({
                        "from_office_id": so["office_id"],
                        "from_office_name": so["office_name"],
                        "to_office_id": q["office_id"],
                        "to_office_name": q["office_name"],
                        "suggested_qty": round(give, 0),
                        "reason": f"{q['office_name']} {q['coverage_days']}d cobertura, {so['office_name']} {so['coverage_days']}d",
                    })
                    so["stock"] -= give
                    need -= give

        return {
            "source": "snapshot",
            "variant_id": variant_id,
            "lookback_days": lookback_days,
            "current_state": rows,
            "suggestions": suggestions,
        }

    # ============================
    # 3. PROYECCION DE COMPRAS (FAST)
    # ============================

    @mcp.tool()
    def bsale_proyeccion_compras_fast(
        target_coverage_days: int = 45,
        lookback_days: int = 90,
        min_velocity: float = 0.05,
        top_n: int = 100,
    ) -> dict[str, Any]:
        """Proyeccion de compras desde snapshot. ~100x mas rapido que version live."""
        now = datetime.now(timezone.utc)
        lookback_cutoff = now - timedelta(days=lookback_days)

        with db_session() as s:
            # Stock total por variante (ultimo snapshot)
            latest_stock = (
                select(
                    stock_snapshot.c.variant_id,
                    stock_snapshot.c.office_id,
                    func.max(stock_snapshot.c.snapshot_date).label("max_ts"),
                )
                .group_by(stock_snapshot.c.variant_id, stock_snapshot.c.office_id)
                .subquery()
            )
            stock_stmt = select(
                stock_snapshot.c.variant_id,
                func.sum(stock_snapshot.c.quantity).label("stock_total"),
                func.max(stock_snapshot.c.variant_code).label("code"),
            ).join(
                latest_stock,
                and_(
                    stock_snapshot.c.variant_id == latest_stock.c.variant_id,
                    stock_snapshot.c.office_id == latest_stock.c.office_id,
                    stock_snapshot.c.snapshot_date == latest_stock.c.max_ts,
                ),
            ).group_by(stock_snapshot.c.variant_id)

            stock_rows = s.execute(stock_stmt).fetchall()

            # Velocity por variante
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(document_details_snapshot.c.quantity).label("total_qty"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    document_details_snapshot.c.document_type_use != 2,
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id)

            vel_rows = s.execute(vel_stmt).fetchall()

        velocity = {r.variant_id: float(r.total_qty or 0) for r in vel_rows}

        recs = []
        for r in stock_rows:
            vid = r.variant_id
            vtot = velocity.get(vid, 0)
            vpd = vtot / lookback_days if lookback_days > 0 else 0
            if vpd < min_velocity:
                continue
            stock = float(r.stock_total or 0)
            target = vpd * target_coverage_days
            order_qty = max(0, target - stock)
            if order_qty <= 0:
                continue
            recs.append({
                "variant_id": vid,
                "code": r.code,
                "stock_total": stock,
                "velocity_per_day": round(vpd, 2),
                "target_stock": round(target, 0),
                "order_qty_suggested": round(order_qty, 0),
                "current_coverage_days": round(stock / vpd, 1) if vpd > 0 else 9999,
            })
        recs.sort(key=lambda x: x["current_coverage_days"])

        return {
            "source": "snapshot",
            "target_coverage_days": target_coverage_days,
            "lookback_days": lookback_days,
            "total_recommendations": len(recs),
            "recommendations": recs[:top_n],
        }

    # ============================
    # 4. RANKING SUCURSALES (FAST)
    # ============================

    @mcp.tool()
    def bsale_ranking_sucursales_fast(
        days_back: int = 30,
    ) -> dict[str, Any]:
        """Ranking sucursales desde snapshot. Lectura SQL pura, sub-segundo.

        NOTA: notas de credito (use=1) ya estan en raw, pero para simplicidad
        sumamos total_amount; el cliente que quiera neto puede sumar - notas_credito.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days_back)

        with db_session() as s:
            stmt = select(
                documents_snapshot.c.office_id,
                func.max(documents_snapshot.c.office_name).label("office_name"),
                func.sum(documents_snapshot.c.total_amount).label("revenue"),
                func.count().label("doc_count"),
                func.avg(documents_snapshot.c.total_amount).label("avg_ticket"),
                func.max(documents_snapshot.c.total_amount).label("max_ticket"),
                func.min(documents_snapshot.c.total_amount).label("min_ticket"),
            ).where(
                documents_snapshot.c.emission_date >= cutoff
            ).group_by(documents_snapshot.c.office_id)

            rows = s.execute(stmt).fetchall()

        ranking = []
        total_rev = 0.0
        for r in rows:
            rev = float(r.revenue or 0)
            total_rev += rev
            ranking.append({
                "office_id": r.office_id,
                "office_name": r.office_name,
                "revenue": rev,
                "doc_count": r.doc_count,
                "avg_ticket": float(r.avg_ticket or 0),
                "max_ticket": float(r.max_ticket or 0),
                "min_ticket": float(r.min_ticket or 0),
            })

        ranking.sort(key=lambda x: x["revenue"], reverse=True)
        for r in ranking:
            r["share_pct"] = round(r["revenue"] / total_rev * 100, 2) if total_rev else 0

        return {
            "source": "snapshot",
            "period_days": days_back,
            "total_revenue": total_rev,
            "ranking": ranking,
        }

    # ============================
    # 5. SEGMENTACION CLIENTES RFM (FAST)
    # ============================

    @mcp.tool()
    def bsale_segmentacion_clientes_rfm_fast(
        days_back: int = 365,
    ) -> dict[str, Any]:
        """RFM desde snapshot. Lectura SQL pura, segundos."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days_back)

        with db_session() as s:
            stmt = select(
                documents_snapshot.c.client_id,
                func.max(documents_snapshot.c.emission_date).label("last_purchase"),
                func.count().label("frequency"),
                func.sum(documents_snapshot.c.total_amount).label("monetary"),
                func.max(documents_snapshot.c.raw["firstName"].astext).label("first_name"),
                func.max(documents_snapshot.c.raw["lastName"].astext).label("last_name"),
                func.max(documents_snapshot.c.raw["company"].astext).label("company"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date >= cutoff,
                    documents_snapshot.c.client_id.isnot(None),
                )
            ).group_by(documents_snapshot.c.client_id)

            rows = s.execute(stmt).fetchall()

        # Categorize
        segments: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            cid = r.client_id
            last = r.last_purchase
            if not last:
                continue
            days_since = (now - last).days
            freq = r.frequency
            mon = float(r.monetary or 0)

            if days_since <= 30 and freq >= 3:
                seg = "Champions"
            elif days_since <= 60 and freq >= 2:
                seg = "Loyal"
            elif days_since <= 30 and freq == 1:
                seg = "New"
            elif days_since <= 90 and freq >= 1:
                seg = "Promising"
            elif days_since > 180 and freq >= 2:
                seg = "At Risk"
            elif days_since > 365:
                seg = "Lost"
            else:
                seg = "Other"

            name_parts = [r.first_name or "", r.last_name or ""]
            name = " ".join(p for p in name_parts if p).strip() or r.company or f"Cliente {cid}"

            segments[seg].append({
                "client_id": cid,
                "name": name,
                "days_since_last": days_since,
                "frequency": freq,
                "monetary": round(mon, 0),
            })

        summary = {seg: len(c) for seg, c in segments.items()}
        return {
            "source": "snapshot",
            "period_days": days_back,
            "total_clients_analyzed": sum(summary.values()),
            "summary_by_segment": summary,
            "top_champions": sorted(segments.get("Champions", []), key=lambda c: c["monetary"], reverse=True)[:20],
            "at_risk_top": sorted(segments.get("At Risk", []), key=lambda c: c["monetary"], reverse=True)[:20],
        }

    # ============================
    # 6. TOP PRODUCTOS (FAST)
    # ============================

    @mcp.tool()
    def bsale_top_productos_fast(
        date_from: str,
        date_to: str,
        top_n: int = 20,
        office_id: int | None = None,
    ) -> dict[str, Any]:
        """Top productos desde snapshot. SQL puro, sub-segundo."""
        start_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        with db_session() as s:
            stmt = select(
                document_details_snapshot.c.variant_id,
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
                func.sum(document_details_snapshot.c.quantity).label("units"),
                func.sum(document_details_snapshot.c.total_amount).label("revenue"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date.between(start_dt, end_dt),
                    document_details_snapshot.c.document_type_use != 2,
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id)

            if office_id:
                stmt = stmt.where(document_details_snapshot.c.office_id == office_id)

            stmt = stmt.order_by(desc("units")).limit(top_n)
            rows = s.execute(stmt).fetchall()

        top = [{
            "variant_id": r.variant_id,
            "code": r.code,
            "description": r.desc,
            "units_sold": float(r.units or 0),
            "revenue": float(r.revenue or 0),
        } for r in rows]

        return {
            "source": "snapshot",
            "period": {"from": date_from, "to": date_to},
            "office_id": office_id,
            "top_products": top,
        }
