"""Tools de analitica agregada sobre datos de Bsale (lectura en vivo).

REGLA PERMANENTE DE MYSCRUBS (22-jul-2026):
    Venta oficial = Boletas + Facturas + Notas de Debito - Notas de Credito.
    Las NOTAS DE VENTA de Bsale (isSalesNote=1: NOTA VENTA, NOTA VENTA T,
    PEDIDO WEB, BETA PEDIDOS WEB, Cotizacion) NO cuentan como venta.

Todos los tools de este archivo devuelven `venta_oficial` como cifra principal
y declaran `truncado` cuando no alcanzaron a leer todo el periodo. Un total
parcial presentado como total fue el bug que dejo el KPI de conciliacion en
rojo desde julio: 50 paginas x 50 documentos = 2.500 documentos, poco mas de
12 dias de volumen de MyScrubs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from bsale_client import (
    doc_revenue_signed,
    get_client,
    is_official_sale,
    is_sales_doc,
    is_sales_note,
    iso_to_epoch_range,
)

# 40.000 documentos ~ 6 meses de MyScrubs. Suficiente para cualquier consulta
# de gestion sin dejar totales a medias.
DEFAULT_MAX_DOCUMENTS = 40000


def _truncation_note(fetch: dict[str, Any]) -> dict[str, Any]:
    """Bloque comun que declara si el resultado esta completo."""
    return {
        "documentos_en_bsale": fetch["total_count"],
        "documentos_leidos": fetch["fetched"],
        "truncado": fetch["truncated"],
        "advertencia": (
            "RESULTADO PARCIAL: no se leyo todo el periodo, los totales son "
            "menores a la realidad. Subir max_documents."
            if fetch["truncated"]
            else None
        ),
    }


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de analitica."""

    @mcp.tool()
    def bsale_ventas_por_periodo(
        start_date: str,
        end_date: str,
        officeid: int | None = None,
        documenttypeid: int | None = None,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        incluir_notas_de_venta: bool = False,
    ) -> dict[str, Any]:
        """Venta oficial entre dos fechas (YYYY-MM-DD), leida de Bsale en vivo.

        Args:
            start_date: Fecha inicio YYYY-MM-DD.
            end_date: Fecha fin YYYY-MM-DD.
            officeid: Filtrar por sucursal.
            documenttypeid: Filtrar por tipo de documento.
            max_documents: Tope de documentos a leer. Si se alcanza, la
                respuesta lo declara en `truncado`.
            incluir_notas_de_venta: Solo para diagnostico. La venta oficial
                NUNCA las incluye; esto agrega un bloque aparte con su monto.
        """
        client = get_client()

        params = {
            "limit": 50,
            "emissiondaterange": iso_to_epoch_range(start_date, end_date),
            "officeid": officeid,
            "documenttypeid": documenttypeid,
            "state": 0,  # solo documentos vigentes
            "expand": "[document_type,office]",
        }

        fetch = client.paginated_fetch(
            "/v1/documents.json", params=params, max_items=max_documents
        )
        docs = fetch["items"]

        venta_oficial = 0.0
        n_oficial = 0
        n_guias = 0
        notas_venta_monto = 0.0
        n_notas_venta = 0

        by_office: dict[Any, dict[str, Any]] = defaultdict(
            lambda: {"office_name": "", "count": 0, "amount": 0.0}
        )
        by_doctype: dict[Any, dict[str, Any]] = defaultdict(
            lambda: {"type_name": "", "count": 0, "amount": 0.0}
        )
        by_day: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "amount": 0.0}
        )

        for doc in docs:
            if not is_sales_doc(doc):  # guia de despacho
                n_guias += 1
                continue
            if is_sales_note(doc):
                n_notas_venta += 1
                notas_venta_monto += float(doc.get("totalAmount", 0) or 0)
                continue
            if not is_official_sale(doc):  # anulado
                continue

            amount = doc_revenue_signed(doc)  # nota de credito = negativo
            venta_oficial += amount
            n_oficial += 1

            office = doc.get("office") or {}
            doctype = doc.get("document_type") or {}

            office_id = office.get("id", 0)
            by_office[office_id]["office_name"] = office.get("name", "?")
            by_office[office_id]["count"] += 1
            by_office[office_id]["amount"] += amount

            doctype_id = doctype.get("id", 0)
            by_doctype[doctype_id]["type_name"] = doctype.get("name", "?")
            by_doctype[doctype_id]["count"] += 1
            by_doctype[doctype_id]["amount"] += amount

            emit_date_ts = doc.get("emissionDate")
            if emit_date_ts:
                try:
                    day = datetime.fromtimestamp(int(emit_date_ts)).strftime("%Y-%m-%d")
                    by_day[day]["count"] += 1
                    by_day[day]["amount"] += amount
                except (ValueError, TypeError):
                    pass

        out: dict[str, Any] = {
            "period": {"start": start_date, "end": end_date},
            "filters": {"officeid": officeid, "documenttypeid": documenttypeid},
            "regla": "venta oficial = Boletas + Facturas + ND - NC (sin notas de venta, sin guias, sin anulados)",
            "venta_oficial": venta_oficial,
            "documentos_de_venta": n_oficial,
            "excluidos": {
                "guias_de_despacho": n_guias,
                "notas_de_venta": n_notas_venta,
            },
            "by_office": dict(by_office),
            "by_document_type": dict(by_doctype),
            "by_day": dict(sorted(by_day.items())),
            **_truncation_note(fetch),
        }
        if incluir_notas_de_venta:
            out["notas_de_venta"] = {
                "count": n_notas_venta,
                "amount": notas_venta_monto,
                "nota": "NO forma parte de la venta oficial. Solo diagnostico.",
            }
        return out

    @mcp.tool()
    def bsale_top_productos(
        start_date: str,
        end_date: str,
        top_n: int = 20,
        max_documents: int = 2000,
    ) -> dict[str, Any]:
        """Top N productos vendidos en un periodo (YYYY-MM-DD), en vivo.

        Las notas de credito RESTAN unidades y monto (una devolucion no es una
        venta). Se excluyen guias, notas de venta y anulados.

        Ojo: este tool pide el detalle documento por documento. Para periodos
        largos usar bsale_top_productos_fast, que lee el snapshot.
        """
        client = get_client()

        params = {
            "limit": 50,
            "emissiondaterange": iso_to_epoch_range(start_date, end_date),
            "state": 0,
            "expand": "[document_type]",
        }
        fetch = client.paginated_fetch(
            "/v1/documents.json", params=params, max_items=max_documents
        )
        docs = [d for d in fetch["items"] if is_official_sale(d)]

        product_counter: Counter[str] = Counter()
        product_revenue: dict[str, float] = defaultdict(float)
        product_names: dict[str, str] = {}

        for doc in docs:
            doc_id = doc.get("id")
            if not doc_id:
                continue
            doctype = doc.get("document_type") or {}
            sign = -1.0 if doctype.get("use") == 1 else 1.0
            try:
                details = client.get(
                    f"/v1/documents/{doc_id}/details.json",
                    params={"limit": 50, "expand": "[variant,product]"},
                )
                for detail in details.get("items", []):
                    variant = detail.get("variant") or {}
                    code = variant.get("code") or f"variant_{variant.get('id', '?')}"
                    qty = float(detail.get("quantity", 0) or 0) * sign
                    amount = float(detail.get("totalAmount", 0) or 0) * sign
                    product_counter[code] += qty
                    product_revenue[code] += amount
                    product_names[code] = variant.get("description") or "Sin nombre"
            except Exception:  # noqa: BLE001
                continue

        top = [
            {
                "code": code,
                "name": product_names.get(code, ""),
                "units_sold": qty,
                "revenue": product_revenue[code],
            }
            for code, qty in product_counter.most_common(top_n)
        ]

        return {
            "period": {"start": start_date, "end": end_date},
            "documentos_de_venta_analizados": len(docs),
            "top_products": top,
            **_truncation_note(fetch),
        }

    @mcp.tool()
    def bsale_comparativo_meses(
        year: int,
        month1: int,
        month2: int,
        officeid: int | None = None,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
    ) -> dict[str, Any]:
        """Compara la venta oficial de dos meses del mismo anio.

        Antes este tool leia como maximo 2.500 documentos por mes (50 paginas)
        y devolvia ese total parcial sin avisar: para MyScrubs eso es ~40% del
        mes. Ahora lee el mes completo y declara si quedo truncado.
        """
        client = get_client()

        def _last_day(y: int, m: int) -> int:
            if m == 12:
                return 31
            next_m = datetime(y, m + 1, 1)
            return (next_m - timedelta(days=1)).day

        def _total(m: int) -> dict[str, Any]:
            params = {
                "limit": 50,
                "emissiondaterange": iso_to_epoch_range(
                    f"{year:04d}-{m:02d}-01",
                    f"{year:04d}-{m:02d}-{_last_day(year, m):02d}",
                ),
                "officeid": officeid,
                "state": 0,
                "expand": "[document_type]",
            }
            fetch = client.paginated_fetch(
                "/v1/documents.json", params=params, max_items=max_documents
            )
            sales_docs = [d for d in fetch["items"] if is_official_sale(d)]
            return {
                "count": len(sales_docs),
                "amount": sum(doc_revenue_signed(d) for d in sales_docs),
                **_truncation_note(fetch),
            }

        m1 = _total(month1)
        m2 = _total(month2)
        delta_amount = m2["amount"] - m1["amount"]
        delta_pct = (delta_amount / m1["amount"] * 100) if m1["amount"] else 0

        return {
            "year": year,
            "regla": "venta oficial = Boletas + Facturas + ND - NC",
            "month1": {"number": month1, **m1},
            "month2": {"number": month2, **m2},
            "delta_amount": delta_amount,
            "delta_pct": round(delta_pct, 2),
            "delta_count": m2["count"] - m1["count"],
            "truncado": bool(m1["truncado"] or m2["truncado"]),
        }
