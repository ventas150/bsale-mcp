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
from typing import Any

import httpx
from dotenv import load_dotenv

from audit import audit_log
from cache import get_cache

load_dotenv()

logger = logging.getLogger(__name__)


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
                "User-Agent": "myscrubs-bsale-mcp/0.2.0",
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

    def paginated_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Junta multiples paginas en una lista."""
        params = dict(params or {})
        limit = params.get("limit", self.default_limit)
        params["limit"] = limit

        results: list[dict[str, Any]] = []
        for page in range(max_pages):
            params["offset"] = page * limit
            data = self.get(path, params=params, use_cache=False)
            items = data.get("items", [])
            if not items:
                break
            results.extend(items)
            if len(items) < limit:
                break
        return results

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
