"""Tools de escritura en Bsale.

Todas las operaciones aqui pasan por audit log automaticamente
(via bsale_client.post/put/delete).

Use con cuidado: estos tools modifican data real en Bsale.
"""
from __future__ import annotations

from typing import Any

from bsale_client import get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de escritura."""

    # ============================
    # STOCK
    # ============================

    @mcp.tool()
    def bsale_ajustar_stock(
        variant_id: int,
        office_id: int,
        quantity: float,
        note: str = "Ajuste via MCP",
    ) -> dict[str, Any]:
        """Ajusta el stock de una variante en una sucursal a un valor especifico.

        WRITE OPERATION. Pasa por audit log.

        Args:
            variant_id: ID de la variante (SKU).
            office_id: ID de la sucursal.
            quantity: Cantidad final que debe quedar en stock.
            note: Nota explicativa (queda en historial Bsale).

        Returns:
            Dict con el ajuste creado en Bsale.
        """
        client = get_client()
        body = {
            "officeId": office_id,
            "note": note,
            "details": [
                {
                    "variantId": variant_id,
                    "quantity": quantity,
                }
            ],
        }
        return client.post("/v1/stocks/adjustments.json", json_body=body)

    @mcp.tool()
    def bsale_consumir_stock(
        variant_id: int,
        office_id: int,
        quantity: float,
        note: str = "Consumo via MCP",
    ) -> dict[str, Any]:
        """Reduce stock de una variante (consumo). WRITE OPERATION.

        Util para reflejar ventas externas, mermas, regalos, etc.
        """
        client = get_client()
        body = {
            "officeId": office_id,
            "note": note,
            "details": [{"variantId": variant_id, "quantity": quantity}],
        }
        return client.post("/v1/stocks/consumptions.json", json_body=body)

    @mcp.tool()
    def bsale_recepcionar_stock(
        variant_id: int,
        office_id: int,
        quantity: float,
        cost: float | None = None,
        note: str = "Recepcion via MCP",
    ) -> dict[str, Any]:
        """Recepciona stock (entrada). WRITE OPERATION.

        Util para reflejar compras a proveedor, devoluciones de clientes, etc.
        """
        client = get_client()
        detail: dict[str, Any] = {"variantId": variant_id, "quantity": quantity}
        if cost is not None:
            detail["cost"] = cost
        body = {
            "officeId": office_id,
            "note": note,
            "details": [detail],
        }
        return client.post("/v1/stocks/receptions.json", json_body=body)

    @mcp.tool()
    def bsale_crear_traspaso_stock(
        variant_id: int,
        office_origin_id: int,
        office_destination_id: int,
        quantity: float,
        note: str = "Traspaso via MCP",
    ) -> dict[str, Any]:
        """Traspasa stock entre sucursales. WRITE OPERATION.

        Hace consumo en sucursal origen + recepcion en destino, en una operacion.

        Args:
            variant_id: SKU a mover.
            office_origin_id: Sucursal de origen.
            office_destination_id: Sucursal de destino.
            quantity: Unidades a mover.
            note: Nota que queda en historial.

        Returns:
            Dict con resultado de consumo y recepcion.
        """
        client = get_client()

        # 1. Consumo en origen
        consumption_body = {
            "officeId": office_origin_id,
            "note": f"[Traspaso] {note} -> office {office_destination_id}",
            "details": [{"variantId": variant_id, "quantity": quantity}],
        }
        consumption = client.post("/v1/stocks/consumptions.json", json_body=consumption_body)

        # 2. Recepcion en destino
        reception_body = {
            "officeId": office_destination_id,
            "note": f"[Traspaso] {note} <- office {office_origin_id}",
            "details": [{"variantId": variant_id, "quantity": quantity}],
        }
        reception = client.post("/v1/stocks/receptions.json", json_body=reception_body)

        return {
            "variant_id": variant_id,
            "from_office": office_origin_id,
            "to_office": office_destination_id,
            "quantity": quantity,
            "consumption": consumption,
            "reception": reception,
        }

    # ============================
    # PRECIOS
    # ============================

    @mcp.tool()
    def bsale_actualizar_precio_variante(
        variant_id: int,
        price_list_id: int,
        new_price: float,
    ) -> dict[str, Any]:
        """Actualiza el precio de una variante en una lista de precios. WRITE OPERATION.

        Args:
            variant_id: ID de la variante (SKU).
            price_list_id: ID de la lista de precios. Usa bsale_listar_listas_precio.
            new_price: Precio nuevo (sin IVA si la lista esta config sin IVA).

        Returns:
            Dict con la lista de precios actualizada.
        """
        client = get_client()
        # Bsale usa endpoint de listas de precio: POST /price_lists/{id}/details.json
        body = {
            "details": [
                {
                    "variantId": variant_id,
                    "variantValue": new_price,
                }
            ]
        }
        return client.post(f"/v1/price_lists/{price_list_id}/details.json", json_body=body)

    @mcp.tool()
    def bsale_actualizar_precios_masivo(
        price_list_id: int,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Actualiza precios de multiples variantes en una sola operacion. WRITE OPERATION.

        Args:
            price_list_id: ID de la lista de precios.
            updates: Lista de dicts con keys `variant_id` y `new_price`.

        Returns:
            Dict con detalles del bulk update.
        """
        client = get_client()
        details = [
            {"variantId": u["variant_id"], "variantValue": u["new_price"]}
            for u in updates
        ]
        body = {"details": details}
        return client.post(f"/v1/price_lists/{price_list_id}/details.json", json_body=body)

    # ============================
    # PRODUCTOS
    # ============================

    @mcp.tool()
    def bsale_activar_variante(variant_id: int) -> dict[str, Any]:
        """Activa una variante (state=0). WRITE OPERATION."""
        client = get_client()
        return client.put(f"/v1/variants/{variant_id}.json", json_body={"state": 0})

    @mcp.tool()
    def bsale_desactivar_variante(variant_id: int) -> dict[str, Any]:
        """Desactiva una variante (state=1). WRITE OPERATION."""
        client = get_client()
        return client.put(f"/v1/variants/{variant_id}.json", json_body={"state": 1})

    @mcp.tool()
    def bsale_actualizar_variante(
        variant_id: int,
        code: str | None = None,
        barcode: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Actualiza campos de una variante. WRITE OPERATION.

        Solo se actualizan los campos que se pasan.
        """
        client = get_client()
        body: dict[str, Any] = {}
        if code is not None:
            body["code"] = code
        if barcode is not None:
            body["barCode"] = barcode
        if description is not None:
            body["description"] = description

        if not body:
            return {"error": "Debe especificar al menos un campo a actualizar"}

        return client.put(f"/v1/variants/{variant_id}.json", json_body=body)
