"""HTTP client para la API de Bsale.

Production-grade:
- Retry con backoff exponencial en 429/5xx/timeouts
- Rate-limit awareness (respeta Retry-After header)
- Cache layer para data semi-estatica (offices, marcas, etc)
- Audit log para operaciones de escritura
- Sentry integration (opcional via env var)

Lee credentials de env vars. NUNCA loguea el token.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from audit import audit_log
from cache import get_cache

load_dotenv()

logger = logging.getLogger(__name__)


def iso_to_epoch_range(date_from: str, date_to: str) -> str:
    """Convierte YYYY-MM-DD,YYYY-MM-DD a EPOCH_START,EPOCH_END para Bsale.

    Bsale requiere Unix timestamps (segundos epoch) en emissiondaterange,
    no fechas ISO. start = 00:00:00 UTC, end = 23:59:59 UTC del dia.
    """
    start_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc,
    )
    return f"{int(start_dt.timestamp())},{int(end_dt.timestamp())}"


def emission_range_from_iso(emissiondate_range: str | None) -> str | None:
    """Acepta 'YYYY-MM-DD,YYYY-MM-DD' o 'EPOCH,EPOCH' y siempre devuelve EPOCH,EPOCH."""
    if not emissiondate_range:
        return None
    if "," not in emissiondate_range:
        return emissiondate_range
    parts = emissiondate_range.split(",", 1)
    # Si ya es epoch (numerico), pasar tal cual
    if parts[0].strip().isdigit():
        return emissiondate_range
    return iso_to_epoch_range(parts[0].strip(), parts[1].strip())


def is_sales_doc(doc: dict) -> bool:
    """True si el documento es venta real (no guia de despacho).

    Bsale document_type.use semantica:
      use=0 -> documento normal (boleta, factura)  -> True (venta)
      use=1 -> nota de credito  -> True (resta de venta)
      use=2 -> guia de despacho  -> False (NO venta, solo movimiento)
      use=4 -> nota de debito  -> True

    Excluir guias de despacho evita doble conteo (cuando luego se emite
    factura del mismo pedido).
    """
    doctype = doc.get("document_type") or {}
    return doctype.get("use") != 2


# Tipos de documento que Bsale marca isSalesNote=1 (NOTA VENTA, NOTA VENTA T,
# PEDIDO WEB, BETA PEDIDOS WEB, Cotizacion). NO son venta oficial.
# Es fallback: la fuente de verdad es /v1/document_types.json (endpoint cacheado).
SALES_NOTE_TYPE_IDS_FALLBACK = frozenset({3, 23, 24, 26, 27})

_sales_note_ids_cache: frozenset[int] | None = None


def sales_note_type_ids(refresh: bool = False) -> frozenset[int]:
    """Ids de document_type con isSalesNote=1, leidos de Bsale y memoizados.

    Si la API no responde, devuelve la lista fallback en vez de fallar: es
    preferible excluir los tipos conocidos a contar notas de venta como venta.
    """
    global _sales_note_ids_cache
    if _sales_note_ids_cache is not None and not refresh:
        return _sales_note_ids_cache
    try:
        data = get_client().get("/v1/document_types.json", params={"limit": 50})
        ids = {
            int(t["id"])
            for t in (data.get("items") or [])
            if int(t.get("isSalesNote") or 0) == 1 and t.get("id") is not None
        }
        _sales_note_ids_cache = frozenset(ids) if ids else SALES_NOTE_TYPE_IDS_FALLBACK
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo leer document_types.json; uso lista fallback de notas de venta")
        _sales_note_ids_cache = SALES_NOTE_TYPE_IDS_FALLBACK
    return _sales_note_ids_cache


def is_sales_note(doc: dict) -> bool:
    """True si el documento es nota de venta / pedido web / cotizacion."""
    doctype = doc.get("document_type") or {}
    if doctype.get("isSalesNote") is not None:
        return int(doctype.get("isSalesNote") or 0) == 1
    doc_type_id = doctype.get("id")
    return doc_type_id is not None and int(doc_type_id) in sales_note_type_ids()


def is_annulled(doc: dict) -> bool:
    """True si el documento esta anulado (state != 0)."""
    return int(doc.get("state", 0) or 0) != 0


def is_official_sale(doc: dict) -> bool:
    """Venta oficial = Boletas + Facturas + Notas de Debito - Notas de Credito.

    Regla permanente de MyScrubs (22-jul-2026): las NOTAS DE VENTA de Bsale NO
    cuentan como venta. Ademas excluye guias de despacho (use=2) y anulados.
    """
    return is_sales_doc(doc) and not is_sales_note(doc) and not is_annulled(doc)


def doc_revenue_signed(doc: dict) -> float:
    """Devuelve totalAmount con signo: negativo si es nota de credito.

    Notas de credito tienen use=1 -> restan del bruto/neto.
    """
    amount = float(doc.get("totalAmount", 0) or 0)
    doctype = doc.get("document_type") or {}
    if doctype.get("use") == 1:
        return -amount
    return amount


class BsaleError(Exception):
    """Error base para fallos del API de Bsale."""


class BsaleAuthError(BsaleError):
    """Token invalido o no configurado."""


class BsaleRateLimitError(BsaleError):
    """Rate limit alcanzado. Esperar y reintentar."""


# Endpoints que se cachean (data semi-estatica)
CACHEABLE_GET_PATHS = {
    "/v1/offices.json",
    "/v1/product_types.json",
    "/v1/product_categories.json",
    "/v1/price_lists.json",
    "/v1/document_types.json",
}

# Metodos que son writes (van a audit log)
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class BsaleClient:
    """Wrapper sobre httpx para llamar a la API de Bsale.

    Pattern: instancia global compartida via get_client() para reuso de conexiones.
    """

    def __init__(self) -> None:
        self.token = os.getenv("BSALE_ACCESS_TOKEN")
        if not self.token:
            raise BsaleAuthError(
                "BSALE_ACCESS_TOKEN env var no esta configurado. "
                "Setealo en Render Dashboard -> Environment."
            )

        self.base_url = os.getenv("BSALE_BASE_URL", "https://api.bsale.io").rstrip("/")
        self.timeout = float(os.getenv("BSALE_TIMEOUT", "30"))
        self.default_limit = int(os.getenv("BSALE_DEFAULT_LIMIT", "25"))
        self.max_retries = int(os.getenv("BSALE_MAX_RETRIES", "3"))
        self.cache_ttl = int(os.getenv("BSALE_CACHE_TTL_SECONDS", "900"))  # 15 min default

        self._client = httpx.Client(
            headers={
                "access_token": self.token,
                "Accept": "application/json",
                "User-Agent": "myscrubs-bsale-mcp/0.3.0",
            },
            timeout=self.timeout,
        )
        self._cache = get_cache()
        self._last_error: str | None = None
        self._last_success_ts: float | None = None
        self._total_requests = 0
        self._total_retries = 0

    def _cache_key(self, path: str, params: dict[str, Any] | None) -> str:
        """Genera key estable para cache."""
        if not params:
            return path
        # Sort para que el orden no afecte
        parts = sorted((k, v) for k, v in params.items() if v is not None)
        return f"{path}?{parts}"

    def _request_with_retry(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Hace request con retry + backoff exponencial."""
        url = f"{self.base_url}{path}"

        # Filtrar None de params (Bsale no los acepta)
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._total_requests += 1
                response = self._client.request(method, url, params=params, json=json_body)
            except httpx.TimeoutException as e:
                last_exc = BsaleError(f"Timeout llamando a Bsale: {e}")
                self._total_retries += 1
                # Backoff exponencial: 1s, 2s, 4s
                if attempt < self.max_retries:
                    sleep_s = 2 ** attempt
                    logger.warning("Timeout (intento %d), reintentando en %ds", attempt + 1, sleep_s)
                    time.sleep(sleep_s)
                    continue
                break
            except httpx.RequestError as e:
                last_exc = BsaleError(f"Error de red llamando a Bsale: {e}")
                self._total_retries += 1
                if attempt < self.max_retries:
                    sleep_s = 2 ** attempt
                    logger.warning("Network error (intento %d), reintentando en %ds", attempt + 1, sleep_s)
                    time.sleep(sleep_s)
                    continue
                break

            # Status codes
            if response.status_code == 401:
                self._last_error = "AUTH_401"
                raise BsaleAuthError("Token rechazado por Bsale (401). Verifica que sea valido.")

            if response.status_code == 429:
                # Rate limit - respeta Retry-After si viene
                retry_after = response.headers.get("Retry-After")
                wait_s = int(retry_after) if retry_after and retry_after.isdigit() else min(60, 2 ** attempt * 5)
                self._total_retries += 1
                logger.warning("Rate limit 429, esperando %ds (intento %d)", wait_s, attempt + 1)
                if attempt < self.max_retries:
                    time.sleep(wait_s)
                    continue
                self._last_error = "RATE_LIMIT"
                raise BsaleRateLimitError(f"Rate limit alcanzado tras {self.max_retries} reintentos.")

            if response.status_code >= 500:
                # Server error - retry con backoff
                self._total_retries += 1
                if attempt < self.max_retries:
                    sleep_s = 2 ** attempt
                    logger.warning(
                        "Server error %d (intento %d), reintentando en %ds",
                        response.status_code, attempt + 1, sleep_s,
                    )
                    time.sleep(sleep_s)
                    continue
                self._last_error = f"SERVER_{response.status_code}"
                raise BsaleError(
                    f"Bsale 5xx persistente: {response.status_code} - {response.text[:300]}"
                )

            if response.status_code >= 400:
                # 4xx que no es 401/429 - no reintentamos
                self._last_error = f"CLIENT_{response.status_code}"
                raise BsaleError(
                    f"Bsale respondio {response.status_code}: {response.text[:500]}"
                )

            try:
                payload = response.json()
            except ValueError as e:
                self._last_error = "INVALID_JSON"
                raise BsaleError(f"Bsale devolvio JSON invalido: {e}") from e

            self._last_success_ts = time.time()
            self._last_error = None
            return payload

        # Si salimos del loop sin return, todos los retries fallaron
        if last_exc:
            self._last_error = "EXHAUSTED_RETRIES"
            raise last_exc
        raise BsaleError("Request fallo sin razon determinada")

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        use_cache: bool | None = None,
    ) -> dict[str, Any]:
        """GET con cache opcional.

        use_cache:
          None  -> auto (cachea si path esta en CACHEABLE_GET_PATHS)
          True  -> fuerza cache
          False -> bypass cache
        """
        should_cache = use_cache if use_cache is not None else path in CACHEABLE_GET_PATHS

        if should_cache:
            key = self._cache_key(path, params)
            cached = self._cache.get(key)
            if cached is not None:
                logger.debug("Cache HIT: %s", key)
                return cached

        result = self._request_with_retry("GET", path, params=params)

        if should_cache:
            key = self._cache_key(path, params)
            self._cache.set(key, result, ttl_seconds=self.cache_ttl)

        return result

    def post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        result = self._request_with_retry("POST", path, json_body=json_body)
        audit_log("POST", path, params=None, body=json_body, result_summary=_summarize_result(result))
        return result

    def put(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        result = self._request_with_retry("PUT", path, json_body=json_body)
        audit_log("PUT", path, params=None, body=json_body, result_summary=_summarize_result(result))
        return result

    def delete(self, path: str) -> dict[str, Any]:
        result = self._request_with_retry("DELETE", path)
        audit_log("DELETE", path, params=None, body=None, result_summary=_summarize_result(result))
        return result

    def paginated_fetch(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_items: int = 20000,
        workers: int | None = None,
    ) -> dict[str, Any]:
        """Trae un listado completo y DECLARA si quedo truncado.

        Dos diferencias con el paginado viejo:
          1. Lee el campo `count` que Bsale devuelve en la primera pagina, asi
             sabe el total real ANTES de pedir el resto. Nunca mas un total
             silenciosamente parcial.
          2. Baja las paginas restantes en paralelo. Bsale aguanta concurrencia
             5 sin protestar (verificado en la app de sync, ago-2026).

        Devuelve {items, total_count, fetched, truncated, pages}.
        """
        params = dict(params or {})
        limit = int(params.get("limit") or self.default_limit)
        limit = max(1, min(limit, 50))  # 50 es el maximo que acepta Bsale
        params["limit"] = limit

        first = self.get(path, params={**params, "offset": 0}, use_cache=False)
        items: list[dict[str, Any]] = list(first.get("items") or [])
        total_count = int(first.get("count") or len(items))

        target = min(total_count, max_items)
        if len(items) >= target or len(items) < limit:
            return {
                "items": items[:target] if target else items,
                "total_count": total_count,
                "fetched": min(len(items), target) if target else len(items),
                "truncated": total_count > max_items,
                "pages": 1,
            }

        offsets = list(range(limit, target, limit))
        n_workers = workers or int(os.getenv("BSALE_PAGE_WORKERS", "4"))
        n_workers = max(1, min(n_workers, 8))

        pages: dict[int, list[dict[str, Any]]] = {0: items}

        def _fetch(off: int) -> tuple[int, list[dict[str, Any]]]:
            data = self.get(path, params={**params, "offset": off}, use_cache=False)
            return off, list(data.get("items") or [])

        if offsets:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                for off, page_items in pool.map(_fetch, offsets):
                    pages[off] = page_items

        merged: list[dict[str, Any]] = []
        for off in sorted(pages):
            merged.extend(pages[off])

        return {
            "items": merged[:target],
            "total_count": total_count,
            "fetched": min(len(merged), target),
            "truncated": total_count > max_items,
            "pages": len(pages),
        }

    def paginated_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Compatibilidad: junta multiples paginas en una lista.

        Preferir paginated_fetch(), que ademas dice si el resultado esta truncado.
        """
        limit = int((params or {}).get("limit") or self.default_limit)
        limit = max(1, min(limit, 50))
        return self.paginated_fetch(
            path, params=params, max_items=max_pages * limit
        )["items"]

    def health_status(self) -> dict[str, Any]:
        """Diagnostico para healthcheck profundo. NO golpea Bsale."""
        return {
            "token_configured": bool(self.token),
            "last_success_ts": self._last_success_ts,
            "last_error": self._last_error,
            "total_requests": self._total_requests,
            "total_retries": self._total_retries,
            "cache_size": self._cache.size(),
        }

    def ping(self) -> bool:
        """Verifica que el token funciona. Llama a un endpoint barato."""
        try:
            self._request_with_retry("GET", "/v1/offices.json", params={"limit": 1})
            return True
        except (BsaleError, BsaleAuthError):
            return False

    def close(self) -> None:
        self._client.close()


def _summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Resumen seguro para audit log (sin PII, sin payloads enormes)."""
    summary = {}
    if "id" in result:
        summary["id"] = result["id"]
    if "href" in result:
        summary["href"] = result["href"]
    if "count" in result:
        summary["count"] = result["count"]
    return summary or {"_keys": list(result.keys())[:5]}


# Singleton global
_client: BsaleClient | None = None


def get_client() -> BsaleClient:
    """Devuelve la instancia global del cliente (lazy init)."""
    global _client
    if _client is None:
        _client = BsaleClient()
    return _client
