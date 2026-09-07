"""Tools de diagnostico y observabilidad del propio MCP.

Estos tools no llaman a Bsale, son self-introspection.
"""
from __future__ import annotations

from typing import Any

from audit import read_recent
from bsale_client import get_client
from cache import get_cache


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de diagnostico."""

    @mcp.tool()
    def bsale_mcp_health() -> dict[str, Any]:
        """Diagnostico del MCP (sin golpear Bsale): estado del cliente, cache, errores."""
        client = get_client()
        return {
            "status": "ok",
            "version": "0.3.0",
            "client": client.health_status(),
            "cache": get_cache().stats(),
        }

    @mcp.tool()
    def bsale_mcp_audit_log(limit: int = 20) -> dict[str, Any]:
        """Devuelve los ultimos N eventos de escritura registrados en audit log.

        Args:
            limit: Cuantos eventos retornar (default 20).
        """
        events = read_recent(limit=limit)
        return {"count": len(events), "events": events}

    @mcp.tool()
    def bsale_mcp_cache_clear() -> dict[str, Any]:
        """Limpia el cache. Forzara que la proxima query golpee Bsale en vivo.

        Util cuando hiciste cambios manuales en Bsale y necesitas que el MCP
        los vea inmediatamente.
        """
        cleared = get_cache().clear()
        return {"cleared_entries": cleared, "status": "ok"}

    @mcp.tool()
    def bsale_mcp_ping() -> dict[str, Any]:
        """Verifica que el token Bsale funciona haciendo un request real minimo."""
        client = get_client()
        ok = client.ping()
        return {
            "bsale_reachable": ok,
            "client_status": client.health_status(),
        }
