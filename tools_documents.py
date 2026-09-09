"""Tools de documentos (facturas, boletas, notas de credito) en Bsale."""
from __future__ import annotations

from typing import Any

from bsale_client import (
    doc_revenue_signed,
    emission_range_from_iso,
    get_client,
    is_official_sale,
    is_sales_doc,
    is_sales_note,
    iso_to_epoch_range,
)


# Campos que Bsale manda en cada documento y que ningun agente usa: el HTML
# del correo de notificacion (messageBodyFormat, ~600 tokens), el TED (el XML
# del timbre, ~400 tokens), las URLs duplicadas y el bloque de configuracion del
# tipo de documento. Medido el 09-sep-2026: bsale_listar_documentos costaba
# ~1.200 tokens por documento, o sea ~7.000 por pagina de 6. Con esto queda en
# ~150 por documento. `compacto=False` devuelve el JSON crudo de Bsale.
_DOC_CAMPOS_FUERA = frozenset({
    "ted", "urlTimbre", "urlXml", "urlPublicViewOriginal", "urlPdfOriginal",
    "exportTotalAmount", "exportNetAmount", "exportTaxAmount", "exportExemptAmount",
    "commissionRate", "commissionNetAmount", "commissionTaxAmount", "commissionTotalAmount",
    "percentageTaxWithheld", "purchaseTaxAmount", "purchaseTotalAmount",
    "coin", "user", "priceList", "book_type", "sellers", "attributes", "payments",
    "document_taxes",
})
_TIPO_CAMPOS_DENTRO = ("id", "name", "codeSii", "use", "isSalesNote", "isCreditNote", "isElectronicDocument")


def _compactar_documento(d: dict[str, Any]) -> dict[str, Any]:
    """Saca lo que pesa y no informa. Conserva id/numero/fechas/montos/estado,
    salesId, trackingNumber, urlPublicView y urlPdf, y deja document_type y
    office reducidos a lo que la regla de venta oficial necesita."""
    out = {k: v for k, v in d.items() if k not in _DOC_CAMPOS_FUERA}
    tipo = d.get("document_type")
    if isinstance(tipo, dict):
        out["document_type"] = {k: tipo.get(k) for k in _TIPO_CAMPOS_DENTRO if k in tipo}
    office = d.get("office")
    if isinstance(office, dict):
        out["office"] = {"id": office.get("id"), "name": (office.get("name") or "").strip()}
    for k in ("references", "details"):
        v = d.get(k)
        if isinstance(v, dict) and "items" not in v:
            out.pop(k, None)  # solo el href: no informa nada
    return out


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
        compacto: bool = True,
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
            compacto: True (default) saca el HTML del correo, el TED y las
                URLs duplicadas (~1.200 -> ~150 tokens por documento). False
                devuelve el JSON crudo de Bsale.
        """
        client = get_client()
        rango = None
        if start_date and end_date:
            rango = iso_to_epoch_range(start_date, end_date)
        elif emissiondate_range:
            # emission_range_from_iso ya acepta "YYYY-MM-DD,YYYY-MM-DD" y
            # "EPOCH,EPOCH" y siempre devuelve epochs. Antes esto estaba
            # duplicado aca a mano.
            rango = emission_range_from_iso(emissiondate_range)
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
        if compacto:
            items = [_compactar_documento(d) for d in items]

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
                "bsale_ventas_fast, que suma todo el periodo."
            ),
            "items": items,
        }

    @mcp.tool()
    def bsale_obtener_documento(document_id: int, compacto: bool = True) -> dict[str, Any]:
        """Obtiene detalle de un documento (incluye items, totales, cliente).

        compacto=True (default) saca el HTML del correo, el TED y las URLs
        duplicadas; las lineas (details.items) y el cliente quedan enteros.
        compacto=False devuelve el JSON crudo de Bsale.
        """
        client = get_client()
        d = client.get(
            f"/v1/documents/{document_id}.json",
            params={"expand": "[document_type,office,client,details,references]"},
        )
        return _compactar_documento(d) if compacto and isinstance(d, dict) else d

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
