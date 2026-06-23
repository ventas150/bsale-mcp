"""MCP server principal de Bsale.

v0.2.0 — production grade:
- Healthcheck profundo (token vigente, ultima request, errores, lag de snapshot)
- Sentry SDK opcional (via env var SENTRY_DSN)
- Audit log endpoint (/audit) para revisar writes recientes
- Cache stats endpoint
- El snapshot nocturno corre como Render Cron Job (cron_snapshot.py), NO in-process.
"""
from __future__ import annotations

import logging
import os
import sys

from fastmcp import FastMCP

# ---- Sentry (opcional) ----
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE", "0.1")),
            environment=os.getenv("ENVIRONMENT", "production"),
            release=os.getenv("RELEASE_VERSION", "0.2.0"),
        )
    except ImportError:
        pass  # sentry-sdk no instalado, seguir sin

# ---- Logging ----
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---- MCP instance ----
mcp = FastMCP(
    name="bsale-mcp-myscrubs",
    instructions=(
        "MCP de Bsale para MyScrubs Uniformes Clinicos (v0.2.0). "
        "Expone tools para consultar y MODIFICAR datos en Bsale: productos, stock, "
        "ventas, documentos, sucursales, clientes, precios, traspasos. "
        "Para analisis usa los tools de lectura (top sellers, quiebres, allocacion). "
        "Para automatizacion usa write tools con cuidado — todos pasan por audit log. "
        "Cache automatica para data semi-estatica (offices, marcas). "
        "Snapshot nocturno a Postgres si DATABASE_URL esta configurado."
    ),
)

# ============================
# Health & Diagnostics
# ============================

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):  # noqa: ARG001
    """Healthcheck profundo. Render lo usa para autoDeploy."""
    from starlette.responses import JSONResponse

    status: dict = {"status": "ok", "service": "bsale-mcp-myscrubs", "version": "0.2.0"}
    code = 200

    # Verifica que el cliente Bsale arranca (no requiere golpear API)
    try:
        from bsale_client import get_client

        client = get_client()
        status["bsale_client"] = client.health_status()
    except Exception as e:  # noqa: BLE001
        status["status"] = "degraded"
        status["bsale_client_error"] = str(e)[:200]
        code = 503

    # Cache stats
    try:
        from cache import get_cache

        status["cache"] = get_cache().stats()
    except Exception as e:  # noqa: BLE001
        status["cache_error"] = str(e)[:200]

    # DB + frescura del snapshot si aplica
    if os.getenv("DATABASE_URL"):
        try:
            from db import db_health, snapshot_lag_hours

            status["db"] = db_health()
            lag = snapshot_lag_hours()
            status["snapshot_lag_hours"] = round(lag, 1) if lag is not None else None
            # Si el snapshot quedo viejo (>26h), marcar degraded para alertar.
            if lag is not None and lag > 26:
                status["status"] = "degraded"
                code = 503
        except Exception as e:  # noqa: BLE001
            status["db_error"] = str(e)[:200]

    return JSONResponse(status, status_code=code)

@mcp.custom_route("/health/deep", methods=["GET"])
async def health_check_deep(request):  # noqa: ARG001
    """Healthcheck profundo que SI golpea Bsale (mas lento, no usar en autoDeploy)."""
    from starlette.responses import JSONResponse
    from bsale_client import get_client

    client = get_client()
    bsale_ok = client.ping()
    return JSONResponse({
        "status": "ok" if bsale_ok else "degraded",
        "bsale_reachable": bsale_ok,
        "bsale_client": client.health_status(),
    }, status_code=200 if bsale_ok else 503)

@mcp.custom_route("/audit", methods=["GET"])
async def audit_endpoint(request):  # noqa: ARG001
    """Devuelve los ultimos N eventos del audit log (writes)."""
    from starlette.responses import JSONResponse
    from audit import read_recent

    limit = int(request.query_params.get("limit", "50"))
    events = read_recent(limit=limit)
    return JSONResponse({"count": len(events), "events": events})

@mcp.custom_route("/cache/clear", methods=["POST"])
async def cache_clear(request):  # noqa: ARG001
    """Limpia el cache. Util tras un sync o cambios manuales en Bsale."""
    from starlette.responses import JSONResponse
    from cache import get_cache

    cleared = get_cache().clear()
    return JSONResponse({"cleared_entries": cleared})

# ============================
# Registrar tools
# ============================

import tools_products
import tools_stocks
import tools_documents
import tools_offices
import tools_clients
import tools_analytics
import tools_writes
import tools_diagnostics
import tools_intelligence

tools_products.register(mcp)
tools_stocks.register(mcp)
tools_documents.register(mcp)
tools_offices.register(mcp)
tools_clients.register(mcp)
tools_analytics.register(mcp)
tools_writes.register(mcp)
tools_diagnostics.register(mcp)
tools_intelligence.register(mcp)

# Snapshot + mapping + intelligence-DB tools si DB configurada
if os.getenv("DATABASE_URL"):
    try:
        import tools_snapshot
        import tools_mapping
        import tools_intelligence_db
        import tools_digests

        tools_snapshot.register(mcp)
        tools_mapping.register(mcp)
        tools_intelligence_db.register(mcp)
        tools_digests.register(mcp)

        # Auto-init schema
        try:
            from db import init_db
            init_db()
        except Exception as e:  # noqa: BLE001
            logger.warning("init_db fallo: %s", e)

        logger.info("Snapshot + mapping + intelligence-DB + digests tools registrados (DATABASE_URL detected)")
    except ImportError as e:
        logger.warning("No se pudieron registrar DB tools: %s", e)

# ============================
# Entry point
# ============================

# NOTA: el snapshot nocturno YA NO corre in-process con APScheduler.
# Ahora se ejecuta como Render Cron Job separado (ver cron_snapshot.py),
# para no competir por memoria con el web service ni morir en redeploys.

def main() -> None:
    """Entry point. Render llama esto via startCommand."""
    port = int(os.getenv("PORT", "8000"))
    host = "0.0.0.0"  # noqa: S104 (necesario para Render)
    logger.info("Starting bsale-mcp-myscrubs v0.2.0 on %s:%d", host, port)
    logger.info("Sentry: %s", "enabled" if SENTRY_DSN else "disabled")
    logger.info("DB: %s", "configured" if os.getenv("DATABASE_URL") else "not configured")

    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path="/mcp",
    )

if __name__ == "__main__":
    main()
