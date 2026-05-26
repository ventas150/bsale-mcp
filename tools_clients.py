"""Tools de clientes en Bsale."""
from __future__ import annotations

from typing import Any

from bsale_client import get_client


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de clientes."""

    @mcp.tool()
    def bsale_listar_clientes(
        limit: int = 25,
        offset: int = 0,
        firstname: str | None = None,
        lastname: str | None = None,
        code: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Lista clientes de Bsale con filtros opcionales."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "firstname": firstname,
            "lastname": lastname,
            "code": code,
            "email": email,
        }
        return client.get("/v1/clients.json", params=params)

    @mcp.tool()
    def bsale_obtener_cliente(client_id: int) -> dict[str, Any]:
        """Detalle de un cliente especifico."""
        client = get_client()
        return client.get(f"/v1/clients/{client_id}.json")

    @mcp.tool()
    def bsale_contar_clientes() -> dict[str, Any]:
        """Cuenta total de clientes activos en Bsale."""
        client = get_client()
        result = client.get("/v1/clients.json", params={"limit": 1})
        return {"count": result.get("count", 0)}
