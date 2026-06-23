"""Tools MCP para la capa 'lenguaje LLM' (digests).

Devuelven resúmenes pre-calculados al instante, sin tocar Bsale ni correr SQL
pesado. Se registran solo si DATABASE_URL está configurado (igual que el resto
de los tools de DB en server.py).

Registrar en server.py, dentro del bloque `if os.getenv("DATABASE_URL")`:
    import tools_digests
    tools_digests.register(mcp)
"""
from __future__ import annotations

from typing import Any


def register(mcp) -> None:  # noqa: ANN001
    """Registra los tools de digests."""

    @mcp.tool()
    def bsale_digest(nombre: str) -> dict[str, Any]:
        """Devuelve un resumen pre-calculado (capa LLM), instantáneo.

        Digests disponibles:
          - 'ventas_hoy'     : ventas del día por sucursal + top 10 SKU
          - 'ventas_30d'     : total + top 20 productos de los últimos 30 días
          - 'ventas_90d'     : total + top 20 productos de los últimos 90 días
          - 'stock_resumen'  : stock actual por sucursal, quiebres y bajo stock

        Args:
            nombre: clave del digest (ver lista arriba).

        Cada respuesta incluye '_generated_at' con la frescura del dato.
        Si está viejo o falta, usá bsale_digests_listar() para ver el estado.
        """
        from digests import get_digest

        data = get_digest(nombre)
        if data is None:
            from digests import list_digests
            return {
                "error": f"digest '{nombre}' no existe o aún no se ha generado",
                "disponibles": list_digests(),
            }
        return data

    @mcp.tool()
    def bsale_digests_listar() -> dict[str, Any]:
        """Lista los digests disponibles y cuándo se generó cada uno (frescura)."""
        from digests import list_digests

        return {"digests": list_digests()}
