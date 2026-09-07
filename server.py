"""MCP server principal de Bsale.

v0.3.0 — production grade:
- Healthcheck profundo (token vigente, ultima request, errores, lag de snapshot)
- Sentry SDK opcional (via env var SENTRY_DSN)
- Audit log endpoint (/audit) para revisar writes recientes
- Cache stats endpoint
- El snapshot nocturno corre como Render Cron Job (cron_snapshot.py), NO in-process.
"""
from __future__ import annotations

import logging
import os
import secrets
import sys

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

# ---- Sentry (opcional) ----
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE", "0.1")),
            environment=os.getenv("ENVIRONMENT", "production"),
            release=os.getenv("RELEASE_VERSION", "0.3.0"),
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
        "MCP de Bsale para MyScrubs Uniformes Clinicos (v0.3.0). "
        "Expone tools para consultar y MODIFICAR datos en Bsale: productos, stock, "
        "ventas, documentos, sucursales, clientes, precios, traspasos. "
        "Para analisis usa los tools de lectura (top sellers, quiebres, allocacion). "
        "Para automatizacion usa write tools con cuidado — todos pasan por audit log. "
        "Cache automatica para data semi-estatica (offices, marcas). "
        "Snapshot nocturno a Postgres si DATABASE_URL esta configurado."
    ),
)

# ============================
# Autenticacion
# ============================
# Hasta el 07-sep-2026 este servidor estaba abierto a internet: cualquiera con la
# URL podia llamar los tools de escritura sobre el ERP de produccion, sin
# credenciales, porque el token de Bsale lo pone el propio servidor. Verificado
# abriendo /audit desde un navegador sin sesion.
#
# El candado es opt-in a proposito: apenas se define MCP_AUTH_TOKEN en Render,
# TODO (incluido /mcp) exige `Authorization: Bearer <token>`. Se deja opcional
# para que activarlo sea una decision consciente y coordinada con la config del
# cliente MCP — no para dejarlo apagado.

def _auth_token() -> str | None:
    tok = os.getenv("MCP_AUTH_TOKEN", "").strip()
    return tok or None


def _auth_ok(request) -> bool:  # noqa: ANN001
    expected = _auth_token()
    if not expected:
        return True  # sin token configurado, no se exige (ver nota de arriba)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    return secrets.compare_digest(header[7:].strip(), expected)


def _unauthorized():
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"error": "unauthorized", "detail": "Falta o no coincide el header Authorization: Bearer <MCP_AUTH_TOKEN>."},
        status_code=401,
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Exige bearer token en todo, salvo /health (que Render necesita libre)."""

    async def dispatch(self, request, call_next):  # noqa: ANN001
        if request.url.path == "/health" or not _auth_token():
            return await call_next(request)
        if not _auth_ok(request):
            return _unauthorized()
        return await call_next(request)


# ============================
# Health & Diagnostics
# ============================

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):  # noqa: ARG001
    """Healthcheck profundo. Render lo usa para autoDeploy."""
    from starlette.responses import JSONResponse

    status: dict = {
        "status": "ok",
        "service": "bsale-mcp-myscrubs",
        "version": "0.3.0",
        "auth": "bearer" if _auth_token() else "ABIERTO — definir MCP_AUTH_TOKEN en Render",
        "escritura_precios": "habilitada" if os.getenv("BSALE_PRICE_WRITES_ENABLED", "0") in ("1","true","yes","on") else "bloqueada por politica",
    }
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
                # degraded en el body, pero 200: `healthCheckPath: /health` en
                # render.yaml interpreta un 503 como "servicio caido" y hace
                # rollback del deploy o reinicia el servicio por un problema de
                # DATOS. La alerta va en el body y en /health/data.
                status["status"] = "degraded"
                status["motivo"] = f"snapshot con {round(lag,1)}h de atraso"
        except Exception as e:  # noqa: BLE001
            status["db_error"] = str(e)[:200]

    return JSONResponse(status, status_code=code)

@mcp.custom_route("/health/data", methods=["GET"])
async def health_data(request):  # noqa: ARG001
    """Frescura de los datos. ESTE si devuelve 503 — apuntar aca el monitoreo,
    no el healthCheckPath de Render."""
    from starlette.responses import JSONResponse

    out: dict = {"status": "ok"}
    code = 200
    if os.getenv("DATABASE_URL"):
        try:
            from db import snapshot_lag_hours

            lag = snapshot_lag_hours()
            out["snapshot_lag_hours"] = round(lag, 1) if lag is not None else None
            if lag is not None and lag > 26:
                out["status"] = "degraded"
                code = 503
        except Exception as e:  # noqa: BLE001
            out["status"] = "error"
            out["error"] = str(e)[:200]
            code = 503
    return JSONResponse(out, status_code=code)

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

    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 1000))
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

        # init_db() se movio a main(): abre una conexion TCP real, y hacerlo en
        # tiempo de import cuelga cualquier `import server` (un test, un script)
        # cuando la base no es alcanzable.

        logger.info("Snapshot + mapping + intelligence-DB + digests tools registrados (DATABASE_URL detected)")
    except Exception as e:  # noqa: BLE001
        logger.exception("No se pudieron registrar DB tools: %s", e)

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
    logger.info("Starting bsale-mcp-myscrubs v0.3.0 on %s:%d", host, port)
    logger.info("Sentry: %s", "enabled" if SENTRY_DSN else "disabled")
    logger.info("DB: %s", "configured" if os.getenv("DATABASE_URL") else "not configured")
    if _auth_token():
        logger.info("Auth: bearer token exigido")
    else:
        logger.warning(
            "Auth: NO HAY MCP_AUTH_TOKEN — el servidor acepta llamadas sin credenciales. "
            "Definir MCP_AUTH_TOKEN en Render y mandarlo como 'Authorization: Bearer <token>'."
        )
    if os.getenv("DATABASE_URL"):
        try:
            from db import init_db

            init_db()
        except Exception as e:  # noqa: BLE001
            logger.warning("init_db fallo: %s", e)

    # Se construye la app a mano en vez de mcp.run() para poder montar el
    # middleware de autenticacion de forma explicita y verificable. Si algun dia
    # FastMCP cambia la firma, esto revienta al arrancar (visible) en vez de
    # dejar el servidor abierto en silencio (invisible), que es el modo de fallo
    # que hay que evitar.
    import uvicorn
    from starlette.middleware import Middleware

    app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
        middleware=[Middleware(BearerAuthMiddleware)],
    )
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info").lower())

if __name__ == "__main__":
    main()
