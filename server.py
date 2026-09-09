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
import re
import sys

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

# ---- Sentry (opcional) ----
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk

        def _sin_secreto_en_la_url(event, hint):  # noqa: ANN001
            """La integracion ASGI adjunta request.url a cada evento, y aca la
            URL ES la credencial (/mcp/<secreto>). access_log=False cerro la
            puerta de uvicorn; esta es la misma puerta un piso mas arriba."""
            req = event.get("request") or {}
            for k in ("url", "query_string"):
                v = req.get(k)
                if isinstance(v, str):
                    req[k] = re.sub(r"/mcp/[^/?#\s]+", "/mcp/<redacted>", v)
            if req:
                event["request"] = req
            return event

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE", "0.1")),
            environment=os.getenv("ENVIRONMENT", "production"),
            release=os.getenv("RELEASE_VERSION", "0.3.0"),
            send_default_pii=False,
            before_send=_sin_secreto_en_la_url,
            before_send_transaction=_sin_secreto_en_la_url,
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

# 2 MB: un lote de 50 cambios de precio son ~5 KB; un listado grande de
# respuesta no pasa por aqui (es salida, no entrada).
MAX_REQUEST_BYTES = int(os.getenv("MCP_MAX_REQUEST_BYTES", str(2_000_000)))


def _auth_token() -> str | None:
    tok = os.getenv("MCP_AUTH_TOKEN", "").strip()
    return tok or None


# El cliente MCP de Cowork no permite configurar headers: el conector solo
# expone la URL. Un candado que solo entiende `Authorization: Bearer` deja
# fuera al unico consumidor legitimo, asi que se acepta tambien el secreto
# como segmento de la URL: /mcp/<secreto> en vez de /mcp.
#
# Es mas debil que un header — una URL puede quedar en logs, historial o
# referers — pero es la unica forma que este cliente soporta, y la
# alternativa real no es "header", es "abierto a internet".
def _url_secret() -> str | None:
    sec = os.getenv("MCP_URL_SECRET", "").strip()
    return sec or None


def _mcp_path() -> str:
    """Ruta donde se monta el MCP. Con secreto: /mcp/<secreto>."""
    sec = _url_secret()
    return f"/mcp/{sec}" if sec else "/mcp"


def _credenciales_validas() -> set[str]:
    """Secretos aceptados como bearer. El de la URL sirve tambien como token,
    para que Roberto administre UNA sola variable y no dos."""
    return {c for c in (_auth_token(), _url_secret()) if c}


def _con_candado() -> bool:
    return bool(_credenciales_validas())


def _auth_ok(request) -> bool:  # noqa: ANN001
    validas = _credenciales_validas()
    if not validas:
        # FALLA CERRADO. Hasta el 09-sep-2026 esto devolvia True: sin
        # MCP_URL_SECRET ni MCP_AUTH_TOKEN, todo quedaba publico, incluidos
        # los tools de escritura sobre el ERP, y /health lo anunciaba con
        # "auth: ABIERTO". Las dos variables son sync:false en el blueprint,
        # o sea que un servicio creado desde ahi arrancaba abierto. Ahora sin
        # credencial no entra nadie, y main() ni siquiera arranca.
        return False
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    presentado = header[7:].strip()
    # compare_digest contra cada una: comparacion en tiempo constante.
    return any(secrets.compare_digest(presentado, v) for v in validas)


def _unauthorized():
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"error": "unauthorized", "detail": "Falta o no coincide el header Authorization: Bearer <MCP_AUTH_TOKEN>."},
        status_code=401,
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Exige credencial en todo, con dos excepciones:

    - /health: Render lo necesita libre para el healthcheck del deploy.
    - La ruta del MCP cuando lleva el secreto embebido: ahi la credencial ya
      viaja en la URL y exigir ademas un header dejaria fuera al conector.
    """

    async def dispatch(self, request, call_next):  # noqa: ANN001
        path = request.url.path
        if path == "/health":
            return await call_next(request)
        # Tope de tamano de request ANTES de que Starlette parsee el body: el
        # guardrail de 50 items se evalua despues del parseo, y la instancia
        # es Starter (512 MB) compartida con /health.
        try:
            largo = int(request.headers.get("content-length") or 0)
        except ValueError:
            largo = 0
        if largo > MAX_REQUEST_BYTES:
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"error": "payload_too_large",
                 "detail": f"El cuerpo supera {MAX_REQUEST_BYTES} bytes."},
                status_code=413,
            )
        if _con_candado() and _url_secret():
            # Comparacion en tiempo constante tambien en el camino de la URL,
            # que es el unico que usa el conector de Cowork. _auth_ok ya lo
            # hacia para el header; aca se comparaba con ==.
            partes = path.split("/", 3)  # "", "mcp", "<secreto>", resto
            if (len(partes) >= 3 and partes[1] == "mcp"
                    and secrets.compare_digest(partes[2], _url_secret())):
                return await call_next(request)
        if not _auth_ok(request):
            return _unauthorized()
        return await call_next(request)


def _describir_auth() -> str:
    """Describe el modo de autenticacion SIN filtrar el secreto."""
    if _url_secret() and _auth_token():
        return "url-secreta + bearer"
    if _url_secret():
        return "url-secreta"
    if _auth_token():
        return "bearer"
    return "ABIERTO — definir MCP_URL_SECRET en Render"


# ============================
# Health & Diagnostics
# ============================

async def _con_limite(fn, segundos: float):
    """Corre una funcion SINCRONA en un hilo, con tope de tiempo.

    /health es una corrutina, pero db_health(), snapshot_lag_hours() y
    get_cache().stats() son sincronas y hacen I/O. Llamarlas directo bloquea el
    event loop de uvicorn: si Postgres deja de responder a nivel de red, no se
    atiende NADA, /health incluido, y Render reinicia en bucle. Aca el hilo
    puede quedarse colgado, pero el event loop sigue vivo y el healthcheck
    responde igual.
    """
    import asyncio

    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), timeout=segundos)
    except Exception:  # noqa: BLE001  (incluye TimeoutError)
        return None


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):  # noqa: ARG001
    """Healthcheck para Render. Liviano y sin detalles internos a proposito.

    Lo que se saco del body y por que: `escritura_precios` le decia a cualquiera
    en internet CUANDO esta abierta la ventana de escritura de precios — justo
    el momento a atacar. `cache_file` exponia rutas del filesystem, y los
    str(e) de la conexion a Postgres traen host, puerto y usuario. Todo ese
    detalle sigue disponible en /health/deep y /health/data, que estan detras
    del candado.
    """
    from starlette.responses import JSONResponse

    status: dict = {
        "status": "ok",
        "service": "bsale-mcp-myscrubs",
        "version": "0.3.0",
        "auth": _describir_auth(),
    }

    # Que el cliente Bsale se pueda construir (no golpea la API)
    def _cliente_ok():
        from bsale_client import get_client

        get_client()
        return True

    if await _con_limite(_cliente_ok, 3) is not True:
        status["status"] = "degraded"
        status["motivo"] = "no se pudo inicializar el cliente de Bsale"
        # 200 igual: un 503 aca hace que Render reinicie el servicio, y si el
        # problema es Bsale o la base, reiniciar no arregla nada y solo agrega
        # una caida. El detalle esta en /health/deep.
        return JSONResponse(status, status_code=200)

    if os.getenv("DATABASE_URL"):
        def _lag():
            from db import snapshot_lag_hours

            return snapshot_lag_hours()

        lag = await _con_limite(_lag, 3)
        status["snapshot_lag_hours"] = round(lag, 1) if lag is not None else None
        if lag is not None and lag > 26:
            status["status"] = "degraded"
            status["motivo"] = f"snapshot con {round(lag,1)}h de atraso"

    return JSONResponse(status, status_code=200)

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
    out = {
        "status": "ok" if bsale_ok else "degraded",
        "bsale_reachable": bsale_ok,
        "bsale_client": client.health_status(),
        "escritura_precios": (
            "habilitada"
            if os.getenv("BSALE_PRICE_WRITES_ENABLED", "0") in ("1", "true", "yes", "on")
            else "bloqueada por politica"
        ),
    }
    # Detalle que antes vivia en /health y ahora vive aca, detras del candado:
    # rutas del filesystem y errores de conexion con host/puerto/usuario.
    try:
        from cache import get_cache

        out["cache"] = get_cache().stats()
    except Exception as e:  # noqa: BLE001
        out["cache_error"] = str(e)[:200]
    if os.getenv("DATABASE_URL"):
        try:
            from db import db_health, snapshot_lag_hours

            out["db"] = db_health()
            lag = snapshot_lag_hours()
            out["snapshot_lag_hours"] = round(lag, 1) if lag is not None else None
        except Exception as e:  # noqa: BLE001
            out["db_error"] = str(e)[:200]
    return JSONResponse(out, status_code=200 if bsale_ok else 503)

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
    if _con_candado():
        # Nunca loguear el secreto: los logs de Render los ve cualquiera con
        # acceso al dashboard, y el secreto de la URL es la credencial entera.
        logger.info("Auth: %s (MCP montado en /mcp/<secreto>)"
                    if _url_secret() else "Auth: %s", _describir_auth())
    else:
        # Falla VISIBLE en el deploy en vez de servicio abierto en silencio.
        # Este servidor da acceso de escritura al ERP de produccion.
        logger.error(
            "Auth: NO HAY MCP_URL_SECRET ni MCP_AUTH_TOKEN. Me niego a arrancar "
            "abierto a internet. Definir MCP_URL_SECRET en Render (el conector "
            "de Cowork usa /mcp/<secreto>) y redesplegar."
        )
        sys.exit(1)
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
        path=_mcp_path(),
        transport="streamable-http",
        middleware=[Middleware(BearerAuthMiddleware)],
    )
    # access_log=False NO es cosmetico. La autenticacion va como segmento de
    # la URL (/mcp/<secreto>), asi que el log de acceso de uvicorn escribe la
    # credencial completa del ERP en stdout -> logs de Render -> Sentry, en
    # CADA request. Verificado el 08-sep-2026: uvicorn 0.52.4 trae
    # Config.access_log=True por defecto. Mas abajo en este mismo archivo se
    # evita loguear el secreto al arrancar, por esta misma razon; faltaba
    # cerrar la otra puerta, que es la que se usa cientos de veces al dia.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        access_log=False,
        # Un solo consumidor legitimo (el conector) y un healthcheck. 32 es
        # holgado para eso y corta un agente en bucle antes del OOM.
        limit_concurrency=int(os.getenv("MCP_LIMIT_CONCURRENCY", "32")),
    )

if __name__ == "__main__":
    main()
