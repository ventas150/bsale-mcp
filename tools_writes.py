"""Tools de escritura en Bsale.

Todas las operaciones aqui pasan por audit log automaticamente
(via bsale_client.post/put/delete).

Use con cuidado: estos tools modifican data real en Bsale.
"""
from __future__ import annotations

from typing import Any

from bsale_client import get_client
from guardrails import (
    GuardrailError,
    guard_price_write,
    guard_stock_write,
    issue_confirm_token,
    consume_confirm_token,
    validate_price_updates,
)


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
        try:
            guard_stock_write()
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

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
        try:
            guard_stock_write()
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

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
        try:
            guard_stock_write()
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

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
        try:
            guard_stock_write()
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

        if office_origin_id == office_destination_id:
            return {
                "aplicado": False,
                "bloqueado_por": "Origen y destino son la misma sucursal; el traspaso no hace nada.",
            }
        if quantity <= 0:
            return {"aplicado": False, "bloqueado_por": f"Cantidad invalida: {quantity}."}

        # 1. Consumo en origen
        consumption_body = {
            "officeId": office_origin_id,
            "note": f"[Traspaso] {note} -> office {office_destination_id}",
            "details": [{"variantId": variant_id, "quantity": quantity}],
        }
        consumption = client.post("/v1/stocks/consumptions.json", json_body=consumption_body)

        # 2. Recepcion en destino.
        # Si esta falla y dejamos que la excepcion suba, las unidades ya salieron de
        # origen y nunca entraron a destino: stock evaporado, sin rastro del consumo
        # que si se hizo. Por eso se captura y se intenta compensar.
        reception_body = {
            "officeId": office_destination_id,
            "note": f"[Traspaso] {note} <- office {office_origin_id}",
            "details": [{"variantId": variant_id, "quantity": quantity}],
        }
        try:
            reception = client.post("/v1/stocks/receptions.json", json_body=reception_body)
        except Exception as e:  # noqa: BLE001
            compensacion = None
            compensacion_error = None
            try:
                compensacion = client.post(
                    "/v1/stocks/receptions.json",
                    json_body={
                        "officeId": office_origin_id,
                        "note": f"[Traspaso REVERTIDO] fallo la recepcion en {office_destination_id}",
                        "details": [{"variantId": variant_id, "quantity": quantity}],
                    },
                )
            except Exception as e2:  # noqa: BLE001
                compensacion_error = str(e2)
            return {
                "aplicado": False,
                "error_recepcion": str(e),
                "consumo_si_se_hizo": consumption,
                "compensacion_en_origen": compensacion,
                "compensacion_error": compensacion_error,
                "accion_requerida": (
                    "El consumo en origen SI se ejecuto. La compensacion se intento y "
                    "su resultado esta arriba. Si compensacion_error no es null, hay "
                    f"{quantity} unidades de la variante {variant_id} fuera de inventario: "
                    "hay que reingresarlas a mano en la sucursal de origen."
                ),
            }

        return {
            "aplicado": True,
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

    def _leer_precios_actuales(client, price_list_id: int, variant_ids: list[int]) -> dict[int, float]:
        """Lee el precio vigente de cada variante en la lista. Sin esto no hay rollback."""
        actuales: dict[int, float] = {}
        for vid in variant_ids:
            try:
                data = client.get(
                    f"/v1/price_lists/{price_list_id}/details.json",
                    params={"variantid": vid, "limit": 1},
                    use_cache=False,
                )
                items = data.get("items") or []
                if items:
                    valor = items[0].get("variantValue")
                    if valor is not None:
                        actuales[int(vid)] = float(valor)
            except Exception:  # noqa: BLE001
                continue  # queda fuera de `actuales` -> el guardrail aborta
        return actuales

    @mcp.tool()
    def bsale_actualizar_precios_masivo(
        price_list_id: int,
        updates: list[dict[str, Any]],
        dry_run: bool = True,
        confirm_token: str | None = None,
        max_delta_pct: float = 5.0,
    ) -> dict[str, Any]:
        """Cambia precios en una lista de precios. ESCRITURA CON CANDADO.

        Regla permanente de MyScrubs: los precios no los cambia un agente. Este
        tool esta deshabilitado por default (BSALE_PRICE_WRITES_ENABLED=0) y
        exige, ademas, que la lista este en la allowlist, un dry_run previo y un
        confirm_token de un solo uso.

        Flujo obligatorio:
          1. Llamar con dry_run=True (default). Devuelve la tabla de cambios con
             precio actual, precio nuevo y delta%, mas un confirm_token.
          2. Roberto revisa esa tabla.
          3. Volver a llamar con dry_run=False y ese confirm_token.

        Args:
            price_list_id: ID de la lista de precios (tiene que estar en la allowlist).
            updates: Lista de dicts con las claves `variant_id` y `new_price`.
            dry_run: True (default) solo simula y devuelve la tabla de cambios.
            confirm_token: El token que devolvio el dry_run. Obligatorio para escribir.
            max_delta_pct: Tope de variacion permitida por variante. Sobre eso, aborta.

        Returns:
            En dry_run, la tabla de cambios y el confirm_token. En escritura, el
            resultado de Bsale mas la tabla de lo aplicado (con los precios previos,
            que son los que permiten revertir).
        """
        client = get_client()
        try:
            guard_price_write(price_list_id)
            variant_ids = []
            for u in updates or []:
                if isinstance(u, dict) and u.get("variant_id") is not None:
                    try:
                        variant_ids.append(int(u["variant_id"]))
                    except (TypeError, ValueError):
                        pass
            actuales = _leer_precios_actuales(client, price_list_id, variant_ids)
            tabla = validate_price_updates(
                updates, current=actuales, max_delta_pct=max_delta_pct
            )
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e), "cambios": 0}

        payload = {"price_list_id": price_list_id, "tabla": tabla}
        if dry_run:
            return {
                "aplicado": False,
                "dry_run": True,
                "price_list_id": price_list_id,
                "cambios": len(tabla),
                "tabla_de_cambios": tabla,
                "confirm_token": issue_confirm_token(payload),
                "siguiente_paso": (
                    "Roberto revisa la tabla. Si aprueba, repetir la MISMA llamada con "
                    "dry_run=False y este confirm_token."
                ),
            }

        try:
            consume_confirm_token(confirm_token, payload)
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e), "cambios": 0}

        body = {
            "details": [
                {"variantId": f["variant_id"], "variantValue": f["precio_nuevo"]}
                for f in tabla
            ]
        }
        resultado = client.post(
            f"/v1/price_lists/{price_list_id}/details.json", json_body=body
        )
        return {
            "aplicado": True,
            "price_list_id": price_list_id,
            "cambios": len(tabla),
            "tabla_aplicada": tabla,
            "para_revertir": [
                {"variant_id": f["variant_id"], "new_price": f["precio_actual"]}
                for f in tabla
            ],
            "respuesta_bsale": resultado,
        }

    @mcp.tool()
    def bsale_actualizar_precio_variante(
        variant_id: int,
        price_list_id: int,
        new_price: float,
        dry_run: bool = True,
        confirm_token: str | None = None,
        max_delta_pct: float = 5.0,
    ) -> dict[str, Any]:
        """Cambia el precio de UNA variante. ESCRITURA CON CANDADO.

        Mismos candados que bsale_actualizar_precios_masivo: kill-switch, allowlist
        de listas, dry_run por default y confirm_token. Ver ese tool para el flujo.
        """
        return bsale_actualizar_precios_masivo(
            price_list_id=price_list_id,
            updates=[{"variant_id": variant_id, "new_price": new_price}],
            dry_run=dry_run,
            confirm_token=confirm_token,
            max_delta_pct=max_delta_pct,
        )

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
