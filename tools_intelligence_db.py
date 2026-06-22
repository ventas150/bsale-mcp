"""Versiones SQL-powered de las intelligence tools.

Estas leen del snapshot Postgres en vez de paginar Bsale en vivo.
~100x mas rapidas. Requieren DATABASE_URL.

Se registran solo si DATABASE_URL esta configurado (ver server.py).

Sobreescriben las versiones de tools_intelligence.py si esta cargado.
Decision: registrar nombres DISTINTOS con sufijo _fast para que ambas coexistan
y el agente pueda elegir. Si las _fast funcionan, las antiguas se pueden retirar.

Correctitud (P0):
- documents_snapshot tiene PK = document_id (una fila por documento), asi que
  los SUM ya no doble-cuentan.
- Las notas de credito (document_type_use=1) se RESTAN via signed_amount().
- Las guias (use=2) se excluyen.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, desc, func, not_, select, text

from bsale_client import get_client
from db import (
    document_details_snapshot,
    documents_snapshot,
    session as db_session,
    signed_amount,
    stock_snapshot,
    variants_snapshot,
)


def _service_variant_ids_subquery():
    """Subquery: variant_ids que son servicios (unlimitedStock=1) -> EXCLUIR de quiebres.

    Bsale marca servicios con unlimitedStock=1 (ej. bordado, servicios intangibles).
    Esos no deben aparecer en quiebres/proyeccion porque no se quiebran.
    """
    return (
        select(variants_snapshot.c.variant_id)
        .where(text("(raw ->> 'unlimitedStock') = '1'"))
        .scalar_subquery()
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
        min_velocity: float = 0.5,
        top_velocity_check: int = 100,
    ) -> dict[str, Any]:
        """Quiebres proyectados. HIBRIDO: velocity SQL + stock LIVE Bsale.

        Pasos:
        1. SQL: top N variantes por velocity en lookback period (de snapshot completo)
        2. Bsale live: stock actual de esas variantes (1 API call por variante)
        3. Computa dias_hasta_quiebre y filtra por horizon

        Tiempo tipico: 30-90s para top_velocity_check=100.

        Args:
            days_horizon: Horizonte de prediccion (default 14d).
            lookback_days: Ventana de velocity (default 30d).
            office_id: Filtra por sucursal. None = totales.
            min_velocity: Velocity minima (units/d) para considerar.
            top_velocity_check: Cuantas variantes top-velocity revisar contra stock live.
                Mas alto = mas completo pero mas lento.
        """
        now = datetime.now(timezone.utc)
        lookback_cutoff = now - timedelta(days=lookback_days)

        # velocity firmada: las notas de credito (use=1) restan unidades
        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )

        # 1. Top variantes por velocity (snapshot SQL, instantaneo)
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(qty).label("total_qty"),
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    document_details_snapshot.c.document_type_use != 2,
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id)
            if office_id:
                vel_stmt = vel_stmt.where(document_details_snapshot.c.office_id == office_id)

            # Excluir servicios (unlimitedStock=1)
            vel_stmt = vel_stmt.where(
                not_(document_details_snapshot.c.variant_id.in_(_service_variant_ids_subquery()))
            )
            vel_stmt = vel_stmt.having(
                func.sum(qty) >= min_velocity * lookback_days
            ).order_by(desc("total_qty")).limit(top_velocity_check)

            vel_rows = s.execute(vel_stmt).fetchall()

        # 2. Stock live de Bsale por cada variante (paralelizable pero por simplicidad serial)
        client = get_client()
        risks = []
        for r in vel_rows:
            vid = r.variant_id
            try:
                stock_params = {"variantid": vid, "limit": 50, "expand": "[office]"}
                if office_id:
                    stock_params["officeid"] = office_id
                stock_data = client.get("/v1/stocks.json", params=stock_params, use_cache=False)
                stock_items = stock_data.get("items", []) or []
            except Exception:  # noqa: BLE001
                continue

            stock_by_office = {}
            stock_total = 0.0
            for item in stock_items:
                office = item.get("office") or {}
                oid = office.get("id")
                if oid is None:
                    continue
                qv = float(item.get("quantity", 0) or 0)
                stock_by_office[oid] = qv
                stock_total += qv

            vtot = float(r.total_qty or 0)
            vpd = vtot / lookback_days
            days = stock_total / vpd if vpd > 0 else 9999

            if days <= days_horizon:
                risks.append({
                    "variant_id": vid,
                    "code": r.code,
                    "description": r.desc,
                    "stock_total": stock_total,
                    "stock_by_office": stock_by_office,
                    "velocity_per_day": round(vpd, 2),
                    "lookback_units": round(vtot, 0),
                    "days_until_stockout": round(days, 1),
                    "category": _coverage_category(days),
                })

        risks.sort(key=lambda x: x["days_until_stockout"])

        return {
            "source": "hybrid (velocity:snapshot, stock:live)",
            "horizon_days": days_horizon,
            "lookback_days": lookback_days,
            "office_id": office_id,
            "min_velocity": min_velocity,
            "checked_variants": len(vel_rows),
            "total_at_risk": len(risks),
            "risks": risks,
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

        # 2. Velocity desde snapshot (rapido SQL), firmada (NC restan)
        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.office_id,
                func.sum(qty).label("total_qty"),
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
        min_velocity: float = 0.5,
        top_velocity_check: int = 100,
    ) -> dict[str, Any]:
        """Proyeccion de compras. HIBRIDO: velocity SQL + stock LIVE Bsale.

        Calcula compra sugerida = max(0, velocity_per_day * target_coverage_days - stock_total).
        Solo recorre las top_velocity_check variantes por venta historica.

        Tiempo tipico: 30-90s para top_velocity_check=100.
        """
        now = datetime.now(timezone.utc)
        lookback_cutoff = now - timedelta(days=lookback_days)

        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )

        # 1. Top variantes por velocity
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(qty).label("total_qty"),
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    document_details_snapshot.c.document_type_use != 2,
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).having(
                func.sum(qty) >= min_velocity * lookback_days
            ).order_by(desc("total_qty")).limit(top_velocity_check)
            # Excluir servicios (unlimitedStock=1)
            vel_stmt = vel_stmt.where(
                not_(document_details_snapshot.c.variant_id.in_(_service_variant_ids_subquery()))
            )

            vel_rows = s.execute(vel_stmt).fetchall()

        # 2. Stock live por variante
        client = get_client()
        recs = []
        for r in vel_rows:
            vid = r.variant_id
            try:
                stock_data = client.get(
                    "/v1/stocks.json",
                    params={"variantid": vid, "limit": 50},
                    use_cache=False,
                )
                stock_items = stock_data.get("items", []) or []
            except Exception:  # noqa: BLE001
                continue

            stock_total = sum(float(item.get("quantity", 0) or 0) for item in stock_items)
            vtot = float(r.total_qty or 0)
            vpd = vtot / lookback_days
            target = vpd * target_coverage_days
            order_qty = max(0, target - stock_total)

            if order_qty <= 0:
                continue
            recs.append({
                "variant_id": vid,
                "code": r.code,
                "description": r.desc,
                "stock_total": stock_total,
                "velocity_per_day": round(vpd, 2),
                "target_stock": round(target, 0),
                "order_qty_suggested": round(order_qty, 0),
                "current_coverage_days": round(stock_total / vpd, 1) if vpd > 0 else 9999,
            })
        recs.sort(key=lambda x: x["current_coverage_days"])

        return {
            "source": "hybrid (velocity:snapshot, stock:live)",
            "target_coverage_days": target_coverage_days,
            "lookback_days": lookback_days,
            "min_velocity": min_velocity,
            "checked_variants": len(vel_rows),
            "total_recommendations": len(recs),
            "recommendations": recs,
        }

    # ============================
    # 3b. SOBRESTOCKEOS DETECTADOS (FAST)
    # ============================

    @mcp.tool()
    def bsale_sobrestockeos_detectados(
        min_coverage_days: int = 180,
        lookback_days: int = 30,
        min_velocity: float = 0.05,
        top_check: int = 150,
    ) -> dict[str, Any]:
        """Detecta SKUs sobrestockeados (cobertura > N dias). Inversa de quiebres.

        Identifica capital muerto en bodega. Hibrido: velocity + precio del snapshot,
        stock LIVE de Bsale.

        Args:
            min_coverage_days: Umbral. Default 180 = 6 meses de stock = sobrestock.
            lookback_days: Ventana de velocity (default 30d).
            min_velocity: Velocity minima (units/d) para incluir. <esto = stock muerto, no sobrestockeo.
            top_check: Cuantas variantes top-velocity revisar (max 200 por timeout).

        Returns:
            Lista de SKUs sobrestockeados con capital_tied calculado.
        """
        now = datetime.now(timezone.utc)
        lookback_cutoff = now - timedelta(days=lookback_days)

        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        rev = signed_amount(
            document_details_snapshot.c.total_amount,
            document_details_snapshot.c.document_type_use,
        )

        # 1. Top variantes por velocity con precio promedio (SQL puro)
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(qty).label("total_qty"),
                func.sum(rev).label("total_revenue"),
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    document_details_snapshot.c.document_type_use != 2,
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).having(
                func.sum(qty) >= min_velocity * lookback_days
            ).order_by(desc("total_qty")).limit(top_check)

            # Excluir servicios (unlimitedStock=1)
            vel_stmt = vel_stmt.where(
                not_(document_details_snapshot.c.variant_id.in_(_service_variant_ids_subquery()))
            )

            vel_rows = s.execute(vel_stmt).fetchall()

        # 2. Stock live por variante + calculo cobertura
        client = get_client()
        sobrestockeos = []
        for r in vel_rows:
            vid = r.variant_id
            try:
                stock_data = client.get(
                    "/v1/stocks.json",
                    params={"variantid": vid, "limit": 50, "expand": "[office]"},
                    use_cache=False,
                )
                stock_items = stock_data.get("items", []) or []
            except Exception:  # noqa: BLE001
                continue

            stock_by_office: dict[int, dict[str, Any]] = {}
            stock_total = 0.0
            for item in stock_items:
                office = item.get("office") or {}
                oid = office.get("id")
                if oid is None:
                    continue
                qv = float(item.get("quantity", 0) or 0)
                if qv > 0:
                    stock_by_office[oid] = {
                        "office_name": office.get("name"),
                        "stock": qv,
                    }
                    stock_total += qv

            if stock_total == 0:
                continue

            vtot = float(r.total_qty or 0)
            vpd = vtot / lookback_days
            coverage = stock_total / vpd if vpd > 0 else 9999

            if coverage < min_coverage_days:
                continue

            avg_price = float(r.total_revenue or 0) / vtot if vtot > 0 else 0
            capital_tied = stock_total * avg_price

            # Detectar concentracion en una sola sucursal (>70% en una office)
            largest_office_stock = max((o["stock"] for o in stock_by_office.values()), default=0)
            concentration_pct = (largest_office_stock / stock_total * 100) if stock_total else 0
            largest_office = next(
                (o for o in stock_by_office.values() if o["stock"] == largest_office_stock),
                None,
            )

            sobrestockeos.append({
                "variant_id": vid,
                "code": r.code,
                "description": r.desc,
                "stock_total": stock_total,
                "stock_by_office": stock_by_office,
                "velocity_per_day": round(vpd, 2),
                "lookback_units_sold": round(vtot, 0),
                "coverage_days": round(coverage, 0),
                "avg_price": round(avg_price, 0),
                "capital_tied_clp": round(capital_tied, 0),
                "concentration_pct": round(concentration_pct, 1),
                "concentrated_in": largest_office["office_name"] if largest_office else None,
            })

        # Ordenar por capital_tied descendente (donde hay mas plata muerta)
        sobrestockeos.sort(key=lambda x: x["capital_tied_clp"], reverse=True)

        total_capital_tied = sum(s["capital_tied_clp"] for s in sobrestockeos)
        return {
            "source": "hybrid (velocity:snapshot, stock:live)",
            "min_coverage_days": min_coverage_days,
            "lookback_days": lookback_days,
            "checked_variants": len(vel_rows),
            "total_sobrestockeos": len(sobrestockeos),
            "total_capital_tied_clp": total_capital_tied,
            "sobrestockeos": sobrestockeos,
        }

    # ============================
    # 4. RANKING SUCURSALES (FAST)
    # ============================

    @mcp.tool()
    def bsale_ranking_sucursales_fast(
        days_back: int = 30,
    ) -> dict[str, Any]:
        """Ranking sucursales desde snapshot. Lectura SQL pura, sub-segundo.

        Neto correcto: notas de credito (use=1) restan; guias (use=2) excluidas;
        sin doble conteo (PK = document_id).
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days_back)

        amt = signed_amount(
            documents_snapshot.c.total_amount,
            documents_snapshot.c.document_type_use,
        )

        with db_session() as s:
            stmt = select(
                documents_snapshot.c.office_id,
                func.max(documents_snapshot.c.office_name).label("office_name"),
                func.sum(amt).label("revenue"),
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("doc_count"),
                func.avg(amt).label("avg_ticket"),
                func.max(amt).label("max_ticket"),
                func.min(amt).label("min_ticket"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date >= cutoff,
                    documents_snapshot.c.document_type_use != 2,
                )
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

        amt = signed_amount(
            documents_snapshot.c.total_amount,
            documents_snapshot.c.document_type_use,
        )

        with db_session() as s:
            stmt = select(
                documents_snapshot.c.client_id,
                func.max(documents_snapshot.c.emission_date).label("last_purchase"),
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("frequency"),
                func.sum(amt).label("monetary"),
                func.max(documents_snapshot.c.raw["firstName"].astext).label("first_name"),
                func.max(documents_snapshot.c.raw["lastName"].astext).label("last_name"),
                func.max(documents_snapshot.c.raw["company"].astext).label("company"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date >= cutoff,
                    documents_snapshot.c.client_id.isnot(None),
                    documents_snapshot.c.document_type_use != 2,
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
    # 6. BRIEFING DIARIO (PURE SQL + HYBRID)
    # ============================

    @mcp.tool()
    def bsale_briefing_diario(
        lookback_days: int = 7,
    ) -> dict[str, Any]:
        """Briefing matinal: ventas dia anterior + quiebres + proyeccion + top sellers + at-risk RFM.

        Aggregator que combina lo mas accionable en una sola call.
        Tipico runtime: 30-60s.

        Args:
            lookback_days: Ventana para top_sellers y ranking (default 7d).
        """
        now = datetime.now(timezone.utc)
        yesterday = now.date() - timedelta(days=1)
        week_ago = now.date() - timedelta(days=lookback_days)
        lookback30_cutoff = now - timedelta(days=30)

        amt = signed_amount(
            documents_snapshot.c.total_amount,
            documents_snapshot.c.document_type_use,
        )
        det_qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        det_rev = signed_amount(
            document_details_snapshot.c.total_amount,
            document_details_snapshot.c.document_type_use,
        )

        with db_session() as s:
            # 1. Ventas ayer
            yesterday_dt_start = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=timezone.utc)
            yesterday_dt_end = datetime.combine(yesterday, datetime.max.time()).replace(tzinfo=timezone.utc)

            yest = s.execute(select(
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("docs"),
                func.sum(amt).label("revenue"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date.between(yesterday_dt_start, yesterday_dt_end),
                    documents_snapshot.c.document_type_use != 2,
                )
            )).first()

            # 2. Ranking sucursales ultimos N dias
            week_dt = datetime.combine(week_ago, datetime.min.time()).replace(tzinfo=timezone.utc)
            ranking = s.execute(select(
                documents_snapshot.c.office_id,
                func.max(documents_snapshot.c.office_name).label("name"),
                func.sum(amt).label("rev"),
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("docs"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date >= week_dt,
                    documents_snapshot.c.document_type_use != 2,
                )
            ).group_by(documents_snapshot.c.office_id).order_by(desc("rev")).limit(5)).fetchall()

            # 3. Top 5 productos vendidos ultima semana
            top_prod = s.execute(select(
                document_details_snapshot.c.variant_id,
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.sum(det_qty).label("units"),
                func.sum(det_rev).label("revenue"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= week_dt,
                    document_details_snapshot.c.document_type_use != 2,
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).order_by(desc("units")).limit(5)).fetchall()

            # 4. Top 5 variantes por velocity 30d (para chequeo de quiebres live)
            top_vel = s.execute(select(
                document_details_snapshot.c.variant_id,
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.sum(det_qty).label("units"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback30_cutoff,
                    document_details_snapshot.c.document_type_use != 2,
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).order_by(desc("units")).limit(30)).fetchall()

            # Filtra servicios (unlimitedStock=1) - mismo session
            svc_rows = s.execute(
                select(variants_snapshot.c.variant_id).where(text("(raw ->> 'unlimitedStock') = '1'"))
            ).fetchall()
            service_ids = {r.variant_id for r in svc_rows}

            top_vel = [r for r in top_vel if r.variant_id not in service_ids]

        # 5. Stock live para top 30 velocity → detectar quiebres + proyeccion
        client = get_client()
        quiebres_criticos = []
        compras_urgentes = []
        for r in top_vel:
            vid = r.variant_id
            vtot = float(r.units or 0)
            vpd = vtot / 30
            try:
                stock_data = client.get(
                    "/v1/stocks.json",
                    params={"variantid": vid, "limit": 50},
                    use_cache=False,
                )
                stocks = stock_data.get("items", []) or []
                stock_total = sum(float(it.get("quantity", 0) or 0) for it in stocks)
            except Exception:  # noqa: BLE001
                continue

            days_to_stockout = stock_total / vpd if vpd > 0 else 9999
            if days_to_stockout <= 14:
                quiebres_criticos.append({
                    "variant_id": vid,
                    "code": r.code,
                    "stock": stock_total,
                    "vel_per_day": round(vpd, 2),
                    "days_until_stockout": round(days_to_stockout, 1),
                })
            target_45d = vpd * 45
            if stock_total < target_45d:
                compras_urgentes.append({
                    "variant_id": vid,
                    "code": r.code,
                    "stock": stock_total,
                    "vel_per_day": round(vpd, 2),
                    "order_qty": round(target_45d - stock_total, 0),
                    "current_coverage": round(days_to_stockout, 1),
                })

        quiebres_criticos.sort(key=lambda x: x["days_until_stockout"])
        compras_urgentes.sort(key=lambda x: x["current_coverage"])

        return {
            "fecha_briefing": now.date().isoformat(),
            "ventas_ayer": {
                "fecha": yesterday.isoformat(),
                "documentos": yest.docs if yest else 0,
                "revenue": float(yest.revenue or 0) if yest else 0,
            },
            "ranking_sucursales_semana": [
                {"office_id": r.office_id, "name": r.name, "revenue": float(r.rev or 0), "docs": r.docs}
                for r in ranking
            ],
            "top_productos_semana": [
                {"variant_id": r.variant_id, "code": r.code, "units": float(r.units or 0), "revenue": float(r.revenue or 0)}
                for r in top_prod
            ],
            "quiebres_criticos_proximos_14d": quiebres_criticos[:10],
            "compras_urgentes_top_velocity": compras_urgentes[:10],
        }

    # ============================
    # 7. TOP PRODUCTOS (FAST)
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

        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        rev = signed_amount(
            document_details_snapshot.c.total_amount,
            document_details_snapshot.c.document_type_use,
        )

        with db_session() as s:
            stmt = select(
                document_details_snapshot.c.variant_id,
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
                func.sum(qty).label("units"),
                func.sum(rev).label("revenue"),
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
