"""Tools de documentos (facturas, boletas, notas de credito) en Bsale."""
from __future__ import annotations

from typing import Any

from bsale_client import emission_range_from_iso, get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de documentos."""

    @mcp.tool()
    def bsale_listar_documentos(
        start_date: str | None = None,
        end_date: str | None = None,
        officeid: int | None = None,
        documenttypeid: int | None = None,
        limit: int = 25,
        offset: int = 0,
        state: int | None = 0,
        solo_venta_oficial: bool = True,
        incluir_cliente: bool = False,
        emissiondate_range: str | None = None,
    ) -> dict[str, Any]:
        """Lista documentos de Bsale, filtrados a VENTA OFICIAL por default.

        Venta oficial = Boletas + Facturas + Notas de Debito - Notas de Credito.
        Quedan fuera las guias de despacho (doble conteo con la factura del mismo
        pedido), las notas de venta / pedidos web / cotizaciones y los anulados.
        Cada documento trae `monto_firmado`, que ya viene negativo en las notas de
        credito: nunca las sumes por `totalAmount`.

        Args:
            start_date: YYYY-MM-DD inicio.
            end_date: YYYY-MM-DD fin.
            officeid: Filtrar por sucursal.
            documenttypeid: Filtrar por tipo de documento.
            limit: Documentos por pagina (max 50 en Bsale).
            offset: Desplazamiento.
            state: 0 = vigentes (default). None = incluye anulados.
            solo_venta_oficial: True (default) aplica la regla de venta oficial.
            incluir_cliente: True agrega la ficha completa del cliente (pesa mucho).
            emissiondate_range: Alias legacy "YYYY-MM-DD,YYYY-MM-DD".
        """
        client = get_client()
        rango = None
        if start_date and end_date:
            rango = iso_to_epoch_range(start_date, end_date)
        elif emissiondate_range:
            partes = emissiondate_range.split(",", 1)
            rango = (
                iso_to_epoch_range(partes[0].strip(), partes[1].strip())
                if len(partes) == 2 and not partes[0].strip().isdigit()
                else emissiondate_range
            )
        expand = "[document_type,office,client]" if incluir_cliente else "[document_type,office]"
        params = {
            "limit": max(1, min(limit, 50)),
            "offset": offset,
            "emissiondaterange": rango,
            "officeid": officeid,
            "documenttypeid": documenttypeid,
            "state": state,
            "expand": expand,
        }
        data = client.get("/v1/documents.json", params=params)
        items = data.get("items") or []

        guias = sum(1 for d in items if not is_sales_doc(d))
        notas = sum(1 for d in items if is_sales_note(d))
        if solo_venta_oficial:
            items = [d for d in items if is_official_sale(d)]
        for d in items:
            d["monto_firmado"] = doc_revenue_signed(d)

        return {
            "regla": (
                "venta oficial = Boletas + Facturas + ND - NC"
                if solo_venta_oficial else "sin filtro de venta oficial"
            ),
            "documentos_en_bsale": data.get("count"),
            "documentos_en_esta_pagina": len(items),
            "excluidos_en_esta_pagina": {"guias_de_despacho": guias, "notas_de_venta": notas},
            "advertencia_paginacion": (
                "Esto es UNA pagina. Para totales de un periodo usar "
                "bsale_ventas_por_periodo o bsale_ventas_fast, que suman todo."
            ),
            "items": items,
        }

    @mcp.tool()
    def bsale_obtener_documento(document_id: int) -> dict[str, Any]:
        """Obtiene detalle de un documento (incluye items, totales, cliente)."""
        client = get_client()
        return client.get(
            f"/v1/documents/{document_id}.json",
            params={"expand": "[document_type,office,client,details,references]"},
        )

    @mcp.tool()
    def bsale_obtener_detalle_documento(
        document_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Obtiene los items (lineas) de un documento."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "expand": "[variant,product]",
        }
        return client.get(f"/v1/documents/{document_id}/details.json", params=params)

    @mcp.tool()
    def bsale_listar_tipos_documento(limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """Lista tipos de documento configurados en Bsale (Factura, Boleta, etc)."""
        client = get_client()
        params = {"limit": min(limit, 50), "offset": offset}
        return client.get("/v1/document_types.json", params=params)
