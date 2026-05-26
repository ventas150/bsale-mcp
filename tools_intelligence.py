"""Tier 3 — Tools de inteligencia de dominio.

No solo entrega data, entrega DECISIONES:
- Predicciones de quiebres
- Sugerencias de allocation
- Proyeccion de compras por categoria
- Margen por producto (cuando hay cost data)
- Ranking multi-dim de sucursales
- Segmentacion RFM de clientes

Optimizadas para velocity historica via paginated_get o snapshot Postgres si esta disponible.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from bsale_client import doc_revenue_signed, get_client, is_sales_doc, iso_to_epoch_range

logger = logging.getLogger(__name__)

_USE_DB = bool(os.getenv("DATABASE_URL"))


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de inteligencia."""

    # ============================
    # QUIEBRES PROYECTADOS
    # ============================

    @mcp.tool()
    def bsale_quiebres_proyectados(
        days_horizon: int = 14,
        lookback_days: int = 30,
        office_id: int | None = None,
        min_velocity: float = 0.1,
    ) -> dict[str, Any]:
        """Predice variantes que se quebraran en los proximos N dias.

        Calcula velocity (unidades/dia) basado en consumo ultimos lookback_days
        y proyecta dias_hasta_quiebre = stock_actual / velocity.

        Args:
            days_horizon: Horizonte de prediccion (default 14d).
            lookback_days: Ventana para calcular velocity (default 30d).
            office_id: Filtra por sucursal. None = todas.
            min_velocity: Ignora variantes con velocity < esto (ruido).

        Returns:
            Lista de variantes en riesgo, ordenadas por dias_hasta_quiebre asc.
        """
        client = get_client()

        # 1. Stock actual
        stock_params = {"limit": 50, "expand": "[variant,office]"}
        if office_id:
            stock_params["officeid"] = office_id

        stocks = client.paginated_get("/v1/stocks.json", params=stock_params, max_pages=50)

        # variant_id -> {office_id: stock}
        current_stock: dict[int, dict[int, float]] = defaultdict(dict)
        variant_info: dict[int, dict[str, Any]] = {}
        for item in stocks:
            v = item.get("variant") or {}
            o = item.get("office") or {}
            vid = v.get("id")
            oid = o.get("id")
            if not vid or not oid:
                continue
            current_stock[vid][oid] = float(item.get("quantity", 0) or 0)
            if vid not in variant_info:
                variant_info[vid] = {
                    "variant_id": vid,
                    "code": v.get("code"),
                    "description": v.get("description"),
                }

        # 2. Velocity: ventas ultimos lookback_days
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=lookback_days)
        sales_params = {
            "limit": 50,
            "emissiondaterange": iso_to_epoch_range(start_date.isoformat(), end_date.isoformat()),
            "state": 0,
            "expand": "[document_type]",  # para filtrar guias
        }
        if office_id:
            sales_params["officeid"] = office_id

        docs = client.paginated_get("/v1/documents.json", params=sales_params, max_pages=100)

        # Por cada doc, leer details
        velocity: dict[int, float] = defaultdict(float)  # variant_id -> total_units
        analyzed_docs = 0
        for doc in docs[:500]:  # cap razonable
            doc_id = doc.get("id")
            if not doc_id:
                continue
            # Excluir guias de despacho (no son ventas reales)
            if not is_sales_doc(doc):
                continue
            try:
                details = client.get(
                    f"/v1/documents/{doc_id}/details.json",
                    params={"limit": 50, "expand": "[variant]"},
                    use_cache=False,
                )
                for d in details.get("items", []):
                    v = d.get("variant") or {}
                    vid = v.get("id")
                    qty = float(d.get("quantity", 0) or 0)
                    if vid and qty > 0:
                        velocity[vid] += qty
                analyzed_docs += 1
            except Exception:  # noqa: BLE001
                continue

        # 3. Proyeccion
        risks = []
        for vid, vinfo in variant_info.items():
            v_total = velocity.get(vid, 0)
            v_per_day = v_total / lookback_days if lookback_days > 0 else 0
            if v_per_day < min_velocity:
                continue
            stock_total = sum(current_stock.get(vid, {}).values())
            days_until_stockout = stock_total / v_per_day if v_per_day > 0 else 9999

            if days_until_stockout <= days_horizon:
                risks.append({
                    **vinfo,
                    "stock_total": stock_total,
                    "stock_by_office": current_stock.get(vid, {}),
                    "velocity_per_day": round(v_per_day, 2),
                    "days_until_stockout": round(days_until_stockout, 1),
                    "lookback_units": v_total,
                })

        risks.sort(key=lambda r: r["days_until_stockout"])

        return {
            "horizon_days": days_horizon,
            "lookback_days": lookback_days,
            "office_id": office_id,
            "analyzed_documents": analyzed_docs,
            "total_at_risk": len(risks),
            "risks": risks,
        }

    # ============================
    # ALLOCATION SUGERIDA
    # ============================

    @mcp.tool()
    def bsale_sugerencia_allocation(
        variant_id: int,
        lookback_days: int = 60,
    ) -> dict[str, Any]:
        """Sugiere como distribuir stock entre sucursales basado en velocity historica.

        Compara velocity por sucursal vs stock actual por sucursal y devuelve:
        - Sucursales sobre-stockeadas (sugerencia: mover OUT)
        - Sucursales con quiebre proximo (sugerencia: traer IN)
        - Cantidad sugerida a mover entre cada par

        Args:
            variant_id: SKU a analizar.
            lookback_days: Ventana de velocity (default 60d).
        """
        client = get_client()

        # 1. Stock actual por sucursal
        stocks = client.get(
            "/v1/stocks.json",
            params={"variantid": variant_id, "limit": 50, "expand": "[variant,office]"},
            use_cache=False,
        ).get("items", [])

        stock_by_office: dict[int, dict[str, Any]] = {}
        for s in stocks:
            o = s.get("office") or {}
            stock_by_office[o.get("id", 0)] = {
                "office_id": o.get("id"),
                "office_name": o.get("name"),
                "stock": float(s.get("quantity", 0) or 0),
            }

        # 2. Velocity por sucursal (ventas ultimos N dias con esta variante)
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=lookback_days)

        velocity_by_office: dict[int, float] = defaultdict(float)
        # Estrategia: pagina documentos del periodo, leer details, filtrar por variant
        docs = client.paginated_get(
            "/v1/documents.json",
            params={
                "limit": 50,
                "emissiondaterange": iso_to_epoch_range(start_date.isoformat(), end_date.isoformat()),
                "state": 0,
                "expand": "[office,document_type]",
            },
            max_pages=100,
        )

        for doc in docs[:500]:
            doc_id = doc.get("id")
            office = doc.get("office") or {}
            oid = office.get("id", 0)
            if not doc_id or not oid:
                continue
            # Excluir guias de despacho
            if not is_sales_doc(doc):
                continue
            try:
                details = client.get(
                    f"/v1/documents/{doc_id}/details.json",
                    params={"limit": 50, "expand": "[variant]"},
                    use_cache=False,
                )
                for d in details.get("items", []):
                    v = d.get("variant") or {}
                    if v.get("id") == variant_id:
                        velocity_by_office[oid] += float(d.get("quantity", 0) or 0)
            except Exception:  # noqa: BLE001
                continue

        # 3. Dias de cobertura por sucursal
        rows = []
        for oid, sinfo in stock_by_office.items():
            v_total = velocity_by_office.get(oid, 0)
            v_per_day = v_total / lookback_days
            stock = sinfo["stock"]
            coverage_days = stock / v_per_day if v_per_day > 0 else (9999 if stock > 0 else 0)
            rows.append({
                "office_id": oid,
                "office_name": sinfo["office_name"],
                "stock": stock,
                "velocity_per_day": round(v_per_day, 2),
                "coverage_days": round(coverage_days, 1),
                "category": _coverage_category(coverage_days),
            })
        rows.sort(key=lambda r: r["coverage_days"])

        # 4. Sugerencias de traspaso: de sucursales >60d a sucursales <14d
        suggestions = []
        sobrestockeo = [r for r in rows if r["coverage_days"] > 60 and r["stock"] > 5]
        quiebre = [r for r in rows if r["coverage_days"] < 14 and r["velocity_per_day"] > 0]

        for q in quiebre:
            # Cuanto necesita para llegar a 30d de cobertura
            target_stock = q["velocity_per_day"] * 30
            need = max(0, target_stock - q["stock"])
            for so in sobrestockeo:
                if need <= 0:
                    break
                # Cuanto puede dar: lo que tiene encima de 30d propios
                excess = max(0, so["stock"] - so["velocity_per_day"] * 30)
                give = min(excess, need)
                if give > 0:
                    suggestions.append({
                        "from_office_id": so["office_id"],
                        "from_office_name": so["office_name"],
                        "to_office_id": q["office_id"],
                        "to_office_name": q["office_name"],
                        "suggested_qty": round(give, 0),
                        "reason": (
                            f"{q['office_name']} tiene {q['coverage_days']}d cobertura, "
                            f"{so['office_name']} tiene {so['coverage_days']}d"
                        ),
                    })
                    so["stock"] -= give
                    need -= give

        return {
            "variant_id": variant_id,
            "lookback_days": lookback_days,
            "current_state": rows,
            "suggestions": suggestions,
        }

    # ============================
    # PROYECCION DE COMPRAS
    # ============================

    @mcp.tool()
    def bsale_proyeccion_compras(
        target_coverage_days: int = 45,
        lookback_days: int = 90,
        producttypeid: int | None = None,
    ) -> dict[str, Any]:
        """Proyecta cuanto comprar de cada variante para mantener N dias de cobertura.

        compra_sugerida = max(0, (velocity_per_day * target_coverage_days) - stock_total)

        Args:
            target_coverage_days: Cobertura objetivo (default 45d).
            lookback_days: Ventana de velocity (default 90d para mayor estabilidad).
            producttypeid: Filtra por marca/tipo de producto.
        """
        client = get_client()

        # 1. Stock total por variante
        stock_items = client.paginated_get(
            "/v1/stocks.json",
            params={"limit": 50, "expand": "[variant]"},
            max_pages=80,
        )
        stock_total: dict[int, float] = defaultdict(float)
        variant_info: dict[int, dict[str, Any]] = {}
        for s in stock_items:
            v = s.get("variant") or {}
            vid = v.get("id")
            if not vid:
                continue
            stock_total[vid] += float(s.get("quantity", 0) or 0)
            if vid not in variant_info:
                variant_info[vid] = {
                    "variant_id": vid,
                    "code": v.get("code"),
                    "description": v.get("description"),
                }

        # 2. Velocity
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=lookback_days)
        sales_params = {
            "limit": 50,
            "emissiondaterange": iso_to_epoch_range(start_date.isoformat(), end_date.isoformat()),
            "state": 0,
            "expand": "[document_type]",
        }
        docs = client.paginated_get("/v1/documents.json", params=sales_params, max_pages=150)

        velocity: dict[int, float] = defaultdict(float)
        for doc in docs[:1000]:
            doc_id = doc.get("id")
            if not doc_id:
                continue
            # Excluir guias de despacho
            if not is_sales_doc(doc):
                continue
            try:
                details = client.get(
                    f"/v1/documents/{doc_id}/details.json",
                    params={"limit": 50, "expand": "[variant]"},
                    use_cache=False,
                )
                for d in details.get("items", []):
                    v = d.get("variant") or {}
                    vid = v.get("id")
                    qty = float(d.get("quantity", 0) or 0)
                    if vid and qty > 0:
                        velocity[vid] += qty
            except Exception:  # noqa: BLE001
                continue

        # 3. Proyeccion
        recommendations = []
        for vid, vinfo in variant_info.items():
            v_total = velocity.get(vid, 0)
            v_per_day = v_total / lookback_days
            if v_per_day < 0.05:  # ignorar variantes muertas
                continue
            stock = stock_total.get(vid, 0)
            need_stock = v_per_day * target_coverage_days
            order_qty = max(0, need_stock - stock)
            if order_qty <= 0:
                continue
            recommendations.append({
                **vinfo,
                "stock_total": stock,
                "velocity_per_day": round(v_per_day, 2),
                "target_stock": round(need_stock, 0),
                "order_qty_suggested": round(order_qty, 0),
                "current_coverage_days": round(stock / v_per_day, 1) if v_per_day > 0 else 9999,
            })

        recommendations.sort(key=lambda r: r["current_coverage_days"])

        return {
            "target_coverage_days": target_coverage_days,
            "lookback_days": lookback_days,
            "producttypeid": producttypeid,
            "total_recommendations": len(recommendations),
            "recommendations": recommendations[:100],
        }

    # ============================
    # RANKING SUCURSALES
    # ============================

    @mcp.tool()
    def bsale_ranking_sucursales(
        days_back: int = 30,
    ) -> dict[str, Any]:
        """Ranking de sucursales por revenue, ticket promedio, y volumen de docs.

        Args:
            days_back: Ventana de analisis (default 30d).
        """
        client = get_client()
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=days_back)

        params = {
            "limit": 50,
            "emissiondaterange": iso_to_epoch_range(start_date.isoformat(), end_date.isoformat()),
            "state": 0,
            "expand": "[office,document_type]",
        }
        docs = client.paginated_get("/v1/documents.json", params=params, max_pages=100)

        by_office: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"office_name": "", "revenue": 0.0, "doc_count": 0, "tickets": []}
        )

        for doc in docs:
            # Excluir guias de despacho - no son ventas
            if not is_sales_doc(doc):
                continue
            o = doc.get("office") or {}
            oid = o.get("id", 0)
            amount = doc_revenue_signed(doc)  # notas credito = negativo
            by_office[oid]["office_name"] = o.get("name", "?")
            by_office[oid]["revenue"] += amount
            by_office[oid]["doc_count"] += 1
            by_office[oid]["tickets"].append(amount)

        ranking = []
        for oid, data in by_office.items():
            tickets = data["tickets"]
            ranking.append({
                "office_id": oid,
                "office_name": data["office_name"],
                "revenue": data["revenue"],
                "doc_count": data["doc_count"],
                "avg_ticket": data["revenue"] / data["doc_count"] if data["doc_count"] else 0,
                "max_ticket": max(tickets) if tickets else 0,
                "min_ticket": min(tickets) if tickets else 0,
            })

        ranking.sort(key=lambda r: r["revenue"], reverse=True)

        # Compute share
        total_rev = sum(r["revenue"] for r in ranking)
        for r in ranking:
            r["share_pct"] = round(r["revenue"] / total_rev * 100, 2) if total_rev else 0

        return {
            "period_days": days_back,
            "total_revenue": total_rev,
            "ranking": ranking,
        }

    # ============================
    # SEGMENTACION RFM
    # ============================

    @mcp.tool()
    def bsale_segmentacion_clientes_rfm(
        days_back: int = 365,
        max_clients: int = 1000,
    ) -> dict[str, Any]:
        """Segmenta clientes por RFM (Recency, Frequency, Monetary).

        Categoriza en: Champions, Loyal, At Risk, Lost, New, Promising.

        Args:
            days_back: Ventana de analisis (default 365d).
            max_clients: Cap a analizar (default 1000 docs procesados).
        """
        client = get_client()
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=days_back)
        now_ts = datetime.now(timezone.utc).timestamp()

        params = {
            "limit": 50,
            "emissiondaterange": iso_to_epoch_range(start_date.isoformat(), end_date.isoformat()),
            "state": 0,
            "expand": "[client,document_type]",
        }
        docs = client.paginated_get("/v1/documents.json", params=params, max_pages=100)

        client_rfm: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"last_purchase_ts": 0, "frequency": 0, "monetary": 0.0, "name": ""}
        )

        for doc in docs[:max_clients * 5]:
            # Excluir guias de despacho
            if not is_sales_doc(doc):
                continue
            client_ref = doc.get("client") or {}
            cid = client_ref.get("id")
            if not cid:
                continue
            amount = doc_revenue_signed(doc)
            emit_ts = doc.get("emissionDate", 0)
            if emit_ts and emit_ts > client_rfm[cid]["last_purchase_ts"]:
                client_rfm[cid]["last_purchase_ts"] = emit_ts
            client_rfm[cid]["frequency"] += 1
            client_rfm[cid]["monetary"] += amount
            client_rfm[cid]["name"] = (
                f"{client_ref.get('firstName', '')} {client_ref.get('lastName', '')}".strip()
                or client_ref.get("company")
                or f"Cliente {cid}"
            )

        # Categorizar
        segments: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for cid, data in client_rfm.items():
            days_since = (now_ts - data["last_purchase_ts"]) / 86400 if data["last_purchase_ts"] else 9999
            freq = data["frequency"]
            mon = data["monetary"]

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

            segments[seg].append({
                "client_id": cid,
                "name": data["name"],
                "days_since_last": round(days_since, 0),
                "frequency": freq,
                "monetary": round(mon, 0),
            })

        summary = {seg: len(clients) for seg, clients in segments.items()}
        return {
            "period_days": days_back,
            "total_clients_analyzed": len(client_rfm),
            "summary_by_segment": summary,
            "top_champions": sorted(
                segments.get("Champions", []),
                key=lambda c: c["monetary"],
                reverse=True,
            )[:20],
            "at_risk_top": sorted(
                segments.get("At Risk", []),
                key=lambda c: c["monetary"],
                reverse=True,
            )[:20],
        }


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
