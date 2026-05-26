"""Tools de marcas, listas de precio, categorias."""
from __future__ import annotations

from typing import Any

from bsale_client import get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools varios."""

    @mcp.tool()
    def bsale_listar_marcas(limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """Lista marcas de productos configuradas en Bsale."""
        client = get_client()
        params = {"limit": min(limit, 50), "offset": offset}
        return client.get("/v1/product_types.json", params=params)

    @mcp.tool()
    def bsale_listar_categorias(limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """Lista categorias de productos en Bsale."""
        client = get_client()
        params = {"limit": min(limit, 50), "offset": offset}
        return client.get("/v1/product_categories.json", params=params)

    @mcp.tool()
    def bsale_listar_listas_precio(limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """Lista las listas de precios configuradas."""
        client = get_client()
        params = {"limit": min(limit, 50), "offset": offset}
        return client.get("/v1/price_lists.json", params=params)
