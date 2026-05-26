"""Tools de documentos (facturas, boletas, notas de credito) en Bsale."""
from __future__ import annotations

from typing import Any

from bsale_client import get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de documentos."""

    @mcp.tool()
    def bsale_listar_documentos(
        limit: int = 25,
        offset: int = 0,
        emissiondate_range: str | None = None,
        documenttypeid: int | None = None,
        officeid: int | None = None,
        state: int | None = None,
    ) -> dict[str, Any]:
        """Lista documentos de Bsale (facturas, boletas, notas de credito)."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "emissiondaterange": emissiondate_range,
            "documenttypeid": documenttypeid,
            "officeid": officeid,
            "state": state,
            "expand": "[document_type,office,client]",
        }
        return client.get("/v1/documents.json", params=params)

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
