"""Tools de stock en Bsale."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from bsale_client import get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de stock."""

    @mcp.tool()
    def bsale_listar_stock(
        limit: int = 25,
        offset: int = 0,
        variantid: int | None = None,
        officeid: int | None = None,
        quantity: float | None = None,
    ) -> dict[str, Any]:
        """Lista stock de variantes en Bsale."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "variantid": variantid,
            "officeid": officeid,
            "quantity": quantity,
            "expand": "[variant,office]",
        }
        return client.get("/v1/stocks.json", params=params)

    @mcp.tool()
    def bsale_stock_agregado(
        officeid: int | None = None,
        max_items: int = 40000,
    ) -> dict[str, Any]:
        """Stock agregado por sucursal y por producto. Util para allocacion."""
        client = get_client()
        params = {
            "limit": 50,
            "officeid": officeid,
            "expand": "[variant,office]",
        }
        fetch = client.paginated_fetch("/v1/stocks.json", params=params, max_items=max_items)
        items = fetch["items"]

        by_office: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"office_name": "", "total_units": 0, "sku_count": 0}
        )
        low_stock: list[dict[str, Any]] = []
        out_of_stock: list[dict[str, Any]] = []

        for item in items:
            quantity = float(item.get("quantity", 0) or 0)
            office = item.get("office") or {}
            office_id = office.get("id", 0)
            variant = item.get("variant") or {}

            by_office[office_id]["office_name"] = office.get("name", "Sin nombre")
            by_office[office_id]["total_units"] += quantity
            by_office[office_id]["sku_count"] += 1

            entry = {
                "stock_id": item.get("id"),
                "variant_id": variant.get("id"),
                "variant_code": variant.get("code"),
                "variant_description": variant.get("description"),
                "office_id": office_id,
                "office_name": office.get("name"),
                "quantity": quantity,
            }
            if quantity <= 0:
                # Bsale permite cantidades negativas (sobreventa, ajustes
                # pendientes). Con `== 0` un SKU en -3 caia en "poco stock" en
                # vez de "quebrado", que es al reves de lo que hay que priorizar.
                out_of_stock.append(entry)
            elif quantity <= 5:
                low_stock.append(entry)

        return {
            "total_items": len(items),
            "filas_en_bsale": fetch["total_count"],
            "truncado": fetch["truncated"],
            "advertencia": (
                "RESULTADO PARCIAL: no se leyo todo el stock, los conteos son "
                "menores a la realidad. Subir max_items."
                if fetch["truncated"] else None
            ),
            "by_office": dict(by_office),
            "low_stock_count": len(low_stock),
            "out_of_stock_count": len(out_of_stock),
            "low_stock": low_stock[:50],
            "out_of_stock": out_of_stock[:50],
        }

    @mcp.tool()
    def bsale_listar_sucursales(
        limit: int = 25,
        offset: int = 0,
        state: int | None = None,
    ) -> dict[str, Any]:
        """Lista sucursales (oficinas) de Bsale."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "state": state,
        }
        return client.get("/v1/offices.json", params=params)
