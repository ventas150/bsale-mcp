"""Tools de analitica agregada sobre datos de Bsale (lectura en vivo).

09-sep-2026: bsale_ventas_por_periodo y bsale_top_productos se RETIRARON.
Leian Bsale en vivo (cuota compartida, topados, 33 s por mes) y tenian par
_fast sobre el snapshot con la misma regla; coexistiendo, los dos daban
distinto para la misma pregunta (auditoria del 09-sep, #14). Queda
bsale_comparativo_meses, que no tiene par, y _tope_de_rango, que usa la
conciliacion.

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

from datetime import datetime, timedelta
from typing import Any

from bsale_client import (
    doc_revenue_signed,
    get_client,
    is_official_sale,
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


def _tope_de_rango(start_date: str, end_date: str, max_dias: int = 92,
                   alternativa: str = "") -> dict | None:
    """Rechaza rangos largos. Devuelve el dict de error, o None si esta OK.

    Estos tools leen Bsale EN VIVO desde el web service, que es el mismo
    proceso que responde /health. El 07-sep-2026 un backfill de 46.738
    documentos dejo sin responder el healthcheck y Render reinicio la
    instancia. Ademas 40.000 documentos parseados son 150-250 MB de dicts
    vivos a la vez, en una instancia de 512 MB.
    """
    from datetime import date

    try:
        d1 = date.fromisoformat(start_date)
        d2 = date.fromisoformat(end_date)
    except ValueError:
        return {"error": "Fechas invalidas, usar YYYY-MM-DD."}
    if d2 < d1:
        return {"error": f"end_date ({end_date}) es anterior a start_date ({start_date})."}
    dias = (d2 - d1).days + 1
    if dias > max_dias:
        return {
            "aplicado": False,
            "motivo": (
                f"Rango de {dias} dias. El tope es {max_dias} porque este tool "
                "lee Bsale en vivo DENTRO del web service, el mismo proceso que "
                "responde el healthcheck de Render."
            ),
            "alternativa": alternativa or "partirlo en tramos mas cortos",
        }
    return None


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de analitica."""

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
