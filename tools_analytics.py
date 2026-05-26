"""Tools de analitica agregada sobre datos de Bsale."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from bsale_client import get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de analitica."""

    @mcp.tool()
    def bsale_ventas_por_periodo(
        start_date: str,
        end_date: str,
        officeid: int | None = None,
        documenttypeid: int | None = None,
        max_pages: int = 50,
    ) -> dict[str, Any]:
        """Agrega las ventas entre dos fechas (YYYY-MM-DD).

        Args:
            start_date: Fecha inicio formato YYYY-MM-DD.
            end_date: Fecha fin formato YYYY-MM-DD.
            officeid: Filtrar por sucursal especifica.
            documenttypeid: 1=Factura, 2=Boleta. None=todos.
            max_pages: Limite de paginas a recorrer.
        """
        client = get_client()

        params = {
            "limit": 50,
            "emissiondaterange": f"{start_date},{end_date}",
            "officeid": officeid,
            "documenttypeid": documenttypeid,
            "state": 0,
            "expand": "[document_type,office]",
        }

        docs = client.paginated_get(
            "/v1/documents.json", params=params, max_pages=max_pages
        )

        total_amount = 0.0
        by_office: dict[Any, dict[str, Any]] = defaultdict(
            lambda: {"office_name": "", "count": 0, "amount": 0.0}
        )
        by_doctype: dict[Any, dict[str, Any]] = defaultdict(
            lambda: {"type_name": "", "count": 0, "amount": 0.0}
        )
        by_day: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0.0})

        for doc in docs:
            amount = float(doc.get("totalAmount", 0) or 0)
            total_amount += amount

            office = doc.get("office") or {}
            doctype = doc.get("document_type") or {}
            emit_date_ts = doc.get("emissionDate")

            office_id = office.get("id", 0)
            by_office[office_id]["office_name"] = office.get("name", "?")
            by_office[office_id]["count"] += 1
            by_office[office_id]["amount"] += amount

            doctype_id = doctype.get("id", 0)
            by_doctype[doctype_id]["type_name"] = doctype.get("name", "?")
            by_doctype[doctype_id]["count"] += 1
            by_doctype[doctype_id]["amount"] += amount

            if emit_date_ts:
                try:
                    day = datetime.fromtimestamp(int(emit_date_ts)).strftime("%Y-%m-%d")
                    by_day[day]["count"] += 1
                    by_day[day]["amount"] += amount
                except (ValueError, TypeError):
                    pass

        return {
            "period": {"start": start_date, "end": end_date},
            "filters": {"officeid": officeid, "documenttypeid": documenttypeid},
            "total_documents": len(docs),
            "total_amount": total_amount,
            "by_office": dict(by_office),
            "by_document_type": dict(by_doctype),
            "by_day": dict(sorted(by_day.items())),
        }

    @mcp.tool()
    def bsale_top_productos(
        start_date: str,
        end_date: str,
        top_n: int = 20,
        max_documents: int = 500,
    ) -> dict[str, Any]:
        """Devuelve los top N productos mas vendidos en un periodo (YYYY-MM-DD)."""
        client = get_client()

        params = {
            "limit": 50,
            "emissiondaterange": f"{start_date},{end_date}",
            "state": 0,
        }
        max_pages = max(1, max_documents // 50)
        docs = client.paginated_get(
            "/v1/documents.json", params=params, max_pages=max_pages
        )

        product_counter: Counter[str] = Counter()
        product_revenue: dict[str, float] = defaultdict(float)
        product_names: dict[str, str] = {}

        for doc in docs[:max_documents]:
            doc_id = doc.get("id")
            if not doc_id:
                continue
            try:
                details = client.get(
                    f"/v1/documents/{doc_id}/details.json",
                    params={"limit": 50, "expand": "[variant,product]"},
                )
                for detail in details.get("items", []):
                    variant = detail.get("variant") or {}
                    code = variant.get("code") or f"variant_{variant.get('id', '?')}"
                    qty = float(detail.get("quantity", 0) or 0)
                    amount = float(detail.get("totalAmount", 0) or 0)
                    name = variant.get("description") or "Sin nombre"

                    product_counter[code] += qty
                    product_revenue[code] += amount
                    product_names[code] = name
            except Exception:  # noqa: BLE001
                continue

        top = []
        for code, qty in product_counter.most_common(top_n):
            top.append({
                "code": code,
                "name": product_names.get(code, ""),
                "units_sold": qty,
                "revenue": product_revenue[code],
            })

        return {
            "period": {"start": start_date, "end": end_date},
            "analyzed_documents": min(len(docs), max_documents),
            "top_products": top,
        }

    @mcp.tool()
    def bsale_comparativo_meses(
        year: int,
        month1: int,
        month2: int,
        officeid: int | None = None,
    ) -> dict[str, Any]:
        """Compara ventas entre dos meses del mismo anio."""
        client = get_client()

        def _last_day(y: int, m: int) -> int:
            if m == 12:
                return 31
            next_m = datetime(y, m + 1, 1)
            return (next_m - timedelta(days=1)).day

        def _range(m: int) -> str:
            start = f"{year:04d}-{m:02d}-01"
            end = f"{year:04d}-{m:02d}-{_last_day(year, m):02d}"
            return f"{start},{end}"

        def _total(m: int) -> dict[str, Any]:
            params = {
                "limit": 50,
                "emissiondaterange": _range(m),
                "officeid": officeid,
                "state": 0,
            }
            docs = client.paginated_get("/v1/documents.json", params=params, max_pages=50)
            total = sum(float(d.get("totalAmount", 0) or 0) for d in docs)
            return {"count": len(docs), "amount": total}

        m1 = _total(month1)
        m2 = _total(month2)
        delta_amount = m2["amount"] - m1["amount"]
        delta_pct = (delta_amount / m1["amount"] * 100) if m1["amount"] else 0
        delta_count = m2["count"] - m1["count"]

        return {
            "year": year,
            "month1": {"number": month1, **m1},
            "month2": {"number": month2, **m2},
            "delta_amount": delta_amount,
            "delta_pct": round(delta_pct, 2),
            "delta_count": delta_count,
        }
