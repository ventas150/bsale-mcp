"""Tools de productos en Bsale."""
from __future__ import annotations

from typing import Any

from bsale_client import get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de productos."""

    @mcp.tool()
    def bsale_listar_productos(
        limit: int = 25,
        offset: int = 0,
        name: str | None = None,
        code: str | None = None,
        state: int | None = None,
        producttypeid: int | None = None,
        expand: str = "[product_type,variants]",
    ) -> dict[str, Any]:
        """Lista productos de Bsale con filtros opcionales.

        Args:
            limit: Cantidad de resultados (max 50). Default 25.
            offset: Desplazamiento para paginacion. Default 0.
            name: Filtrar por nombre del producto (substring match).
            code: Filtrar por codigo del producto (exact match).
            state: 0=activo, 1=inactivo.
            producttypeid: ID del tipo de producto.
            expand: Relaciones a incluir (ej. "[product_type,variants]").

        Returns:
            Dict con `items` (lista de productos), `count`, `href` y links de paginacion.
        """
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "name": name,
            "code": code,
            "state": state,
            "producttypeid": producttypeid,
            "expand": expand,
        }
        return client.get("/v1/products.json", params=params)

    @mcp.tool()
    def bsale_obtener_producto(
        product_id: int,
        expand: str = "[product_type,variants,prices]",
    ) -> dict[str, Any]:
        """Obtiene el detalle de un producto especifico."""
        client = get_client()
        return client.get(f"/v1/products/{product_id}.json", params={"expand": expand})

    @mcp.tool()
    def bsale_listar_variantes(
        product_id: int | None = None,
        limit: int = 25,
        offset: int = 0,
        code: str | None = None,
        barcode: str | None = None,
        state: int | None = None,
    ) -> dict[str, Any]:
        """Lista variantes de productos (SKUs)."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "code": code,
            "barcode": barcode,
            "state": state,
        }
        if product_id:
            return client.get(f"/v1/products/{product_id}/variants.json", params=params)
        return client.get("/v1/variants.json", params=params)

    @mcp.tool()
    def bsale_actualizar_producto(
        product_id: int,
        name: str | None = None,
        state: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Actualiza campos basicos de un producto en Bsale. WRITE OPERATION."""
        client = get_client()
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if state is not None:
            body["state"] = state
        if description is not None:
            body["description"] = description

        if not body:
            return {"error": "Debe especificar al menos un campo a actualizar"}

        return client.put(f"/v1/products/{product_id}.json", json_body=body)
