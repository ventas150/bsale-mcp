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

from bsale_client import (
    doc_revenue_signed,
    get_client,
    is_official_sale,
    is_sales_doc,
    is_sales_note,
    iso_to_epoch_range,
)

logger = logging.getLogger(__name__)

_USE_DB = bool(os.getenv("DATABASE_URL"))


# ============================================================
# Helper compartido de velocity (tools EN VIVO)
# ============================================================
# Los tres tools que calculan velocity leyendo Bsale documento por documento
# (quiebres, allocation, proyeccion de compras) repetian los mismos tres
# errores. Se centralizan aca para que no vuelvan a divergir:
#
#   1. Filtraban con is_sales_doc, que solo saca las guias. Las NOTAS DE VENTA
#      seguian entrando y el canal web se contaba dos veces (PEDIDO WEB +
#      boleta del mismo pedido).
#   2. `if qty > 0` hacia que las lineas de NOTA DE CREDITO sumaran en
#      POSITIVO: una devolucion aumentaba la velocity del producto devuelto.
#   3. Leian una muestra (docs[:500]) pero dividian por lookback_days completo,
#      sin declarar el muestreo. Con ~4.500 documentos en 30 dias eso subestima
#      la velocity ~9x, y nadie se enteraba.
#
# El (3) no se puede "arreglar" leyendo mas: es 1 request por documento. Lo que
# se hace es DECLARARLO y escalar la velocity por la cobertura real, con la
# advertencia de que asume uniformidad. Para numeros exactos estan las
# versiones _fast, que leen el snapshot.

CAP_DOCS_DETALLE = 500
"""Cuantos documentos se abren para leer sus lineas. Es 1 request HTTP por
documento: subirlo alarga el bloqueo del hilo y acerca el 429."""


def _velocity_en_vivo(client, docs, cap=CAP_DOCS_DETALLE, por_sucursal=False,
                      variant_id=None):
    """Suma unidades por variante leyendo el detalle de una MUESTRA de docs.

    Devuelve (velocity, cobertura). `velocity` es {variant_id: unidades} o,
    con por_sucursal=True, {office_id: unidades} para `variant_id`.
    Las notas de credito RESTAN.
    """
    from collections import defaultdict as _dd

    oficiales = [d for d in docs if is_official_sale(d)]
    notas_de_venta = sum(1 for d in docs if is_sales_doc(d) and is_sales_note(d))
    muestra = oficiales[:cap]

    velocity = _dd(float)
    analizados = 0
    fallidos = 0
    for doc in muestra:
        doc_id = doc.get("id")
        if not doc_id:
            continue
        oid = (doc.get("office") or {}).get("id", 0)
        if por_sucursal and not oid:
            continue
        es_nc = (doc.get("document_type") or {}).get("use") == 1
        signo = -1.0 if es_nc else 1.0
        try:
            detalle = client.paginated_fetch(
                f"/v1/documents/{doc_id}/details.json",
                params={"limit": 50, "expand": "[variant]"},
                max_items=1000,
            )
        except Exception:  # noqa: BLE001
            fallidos += 1
            continue
        for d in detalle["items"]:
            v = d.get("variant") or {}
            vid = v.get("id")
            if not vid:
                continue
            qty = float(d.get("quantity", 0) or 0) * signo
            if por_sucursal:
                if vid == variant_id:
                    velocity[oid] += qty
            else:
                velocity[vid] += qty
        analizados += 1

    cobertura = {
        "documentos_de_venta_en_el_periodo": len(oficiales),
        "documentos_analizados": analizados,
        "documentos_que_fallaron": fallidos,
        "excluidos_notas_de_venta": notas_de_venta,
        "cobertura_pct": (
            round(100 * analizados / len(oficiales), 1) if oficiales else 0.0
        ),
        "muestreado": analizados < len(oficiales),
    }
    factor = (len(oficiales) / analizados) if analizados else 1.0
    if cobertura["muestreado"]:
        cobertura["factor_de_escala_aplicado"] = round(factor, 2)
        cobertura["advertencia"] = (
            f"Solo se leyo el detalle de {analizados} de "
            f"{len(oficiales)} documentos ({cobertura['cobertura_pct']}%). La "
            "velocity se escalo por la cobertura, lo que ASUME que los "
            "documentos no leidos se parecen a los leidos. Para numeros "
            "exactos usar la version _fast, que lee el snapshot."
        )
        for k in list(velocity):
            velocity[k] *= factor
    return velocity, cobertura


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

        fetch_docs = client.paginated_fetch(
            "/v1/documents.json", params=sales_params, max_items=40000
        )
        docs = fetch_docs["items"]

        velocity, cobertura_velocity = _velocity_en_vivo(client, docs)
        cobertura_velocity["documentos_truncados"] = bool(fetch_docs.get("truncated"))
        analyzed_docs = cobertura_velocity["documentos_analizados"]

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
            "cobertura_velocity": cobertura_velocity,
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

        vel_por_oficina, cobertura_velocity = _velocity_en_vivo(
            client, docs, por_sucursal=True, variant_id=variant_id
        )
        for _oid, _q in vel_por_oficina.items():
            velocity_by_office[_oid] += _q

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
            "cobertura_velocity": cobertura_velocity,
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
        fetch_docs = client.paginated_fetch(
            "/v1/documents.json", params=sales_params, max_items=40000
        )
        docs = fetch_docs["items"]

        velocity, cobertura_velocity = _velocity_en_vivo(client, docs)
        cobertura_velocity["documentos_truncados"] = bool(fetch_docs.get("truncated"))

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
            "cobertura_velocity": cobertura_velocity,
            "recommendations": recommendations[:100],
        }

    # ============================
    # RANKING SUCURSALES
    # ============================

    @mcp.tool()
    def bsale_ranking_sucursales(
        days_back: int = 30,
        max_documents: int = 40000,
    ) -> dict[str, Any]:
        """Ranking de sucursales por revenue, ticket promedio, y volumen de docs.

        Lee Bsale EN VIVO. Para periodos largos preferir
        bsale_ranking_sucursales_fast, que lee el snapshot y es sub-segundo.

        Venta oficial = Boletas + Facturas + ND - NC. Excluye guias, notas de
        venta / pedidos web / cotizaciones y anulados. `doc_count` cuenta solo
        documentos de venta; las notas de credito van aparte.

        Args:
            days_back: Ventana de analisis (default 30d).
            max_documents: Tope de documentos a leer. Si se alcanza, la
                respuesta lo declara en `truncado`.
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
        # paginated_fetch, no paginated_get: este ultimo topaba en 100 paginas
        # (5.000 documentos) y devolvia el parcial sin avisar. Con ~4.500
        # documentos mensuales, cualquier ventana de mas de 30 dias se cortaba.
        fetch = client.paginated_fetch(
            "/v1/documents.json", params=params, max_items=max_documents
        )
        docs = fetch["items"]

        by_office: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"office_name": "", "revenue": 0.0, "doc_count": 0,
                     "nc_count": 0, "tickets": []}
        )
        excluidas_notas_de_venta = 0

        for doc in docs:
            # Antes filtraba con is_sales_doc, que solo saca las guias: las
            # NOTAS DE VENTA seguian entrando y el canal web se contaba dos
            # veces (PEDIDO WEB + boleta del mismo pedido). Contra la version
            # _fast daban $24,8 millones de diferencia sobre 30 dias.
            if is_sales_doc(doc) and is_sales_note(doc):
                excluidas_notas_de_venta += 1
            if not is_official_sale(doc):
                continue
            o = doc.get("office") or {}
            oid = o.get("id", 0)
            amount = doc_revenue_signed(doc)  # notas credito = negativo
            by_office[oid]["office_name"] = o.get("name", "?")
            by_office[oid]["revenue"] += amount
            if amount < 0:
                by_office[oid]["nc_count"] += 1
            else:
                by_office[oid]["doc_count"] += 1
                by_office[oid]["tickets"].append(amount)

        ranking = []
        for oid, data in by_office.items():
            # tickets solo trae documentos de venta: antes incluia las notas de
            # credito, asi que `min_ticket` era siempre la devolucion mas grande
            # (un numero negativo) presentada como "ticket minimo".
            tickets = data["tickets"]
            docs_venta = data["doc_count"]
            ranking.append({
                "office_id": oid,
                "office_name": data["office_name"],
                "revenue": data["revenue"],
                "doc_count": docs_venta,
                "notas_de_credito": data["nc_count"],
                "avg_ticket": data["revenue"] / docs_venta if docs_venta else 0,
                "max_ticket": max(tickets) if tickets else 0,
                "min_ticket": min(tickets) if tickets else 0,
            })

        ranking.sort(key=lambda r: r["revenue"], reverse=True)

        # Compute share
        total_rev = sum(r["revenue"] for r in ranking)
        for r in ranking:
            r["share_pct"] = round(r["revenue"] / total_rev * 100, 2) if total_rev else 0

        out = {
            "period_days": days_back,
            "regla": "venta oficial = Boletas + Facturas + ND - NC",
            "total_revenue": total_rev,
            "ranking": ranking,
            "documentos_leidos": fetch.get("fetched"),
            "documentos_en_bsale": fetch.get("total_count"),
            "excluidos": {"notas_de_venta": excluidas_notas_de_venta},
            "truncado": bool(fetch.get("truncated")),
        }
        if out["truncado"]:
            out["advertencia"] = (
                "TRUNCADO: no se leyeron todos los documentos del periodo, el "
                "ranking esta INCOMPLETO. Subir max_documents o usar "
                "bsale_ranking_sucursales_fast, que lee el snapshot."
            )
        return out

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
