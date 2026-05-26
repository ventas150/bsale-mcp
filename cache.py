"""Cache layer file-based simple para Bsale MCP.

Diseño:
- Storage en disco (Render disco persistente o /tmp)
- TTL por entry
- Invalidacion manual via clear()
- Thread-safe via lock
- No requiere Redis ni Postgres (zero dependencies extra)

Para entornos de alta escala, swappable a Redis cambiando solo esta clase.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FileCache:
    """Cache simple basado en archivo JSON con TTL."""

    def __init__(self, cache_dir: str | None = None) -> None:
        # En Render, /tmp es ephemeral pero persiste durante el proceso
        # Si hay disco persistente, usarlo
        self.cache_dir = Path(cache_dir or os.getenv("CACHE_DIR", "/tmp/bsale_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "cache.json"
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """Carga cache de disco si existe."""
        if not self.cache_file.exists():
            return
        try:
            with self.cache_file.open("r", encoding="utf-8") as f:
                self._data = json.load(f)
            logger.info("Cache cargado: %d entries", len(self._data))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("No se pudo cargar cache, empezando vacio: %s", e)
            self._data = {}

    def _persist(self) -> None:
        """Persiste a disco (best-effort, no falla si hay error de IO)."""
        try:
            with self.cache_file.open("w", encoding="utf-8") as f:
                json.dump(self._data, f)
        except OSError as e:
            logger.warning("No se pudo persistir cache: %s", e)

    def get(self, key: str) -> Any | None:
        """Devuelve valor si existe y no esta expirado, sino None."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.get("expires_at", 0) < time.time():
                # Expirado, eliminar
                del self._data[key]
                return None
            return entry.get("value")

    def set(self, key: str, value: Any, ttl_seconds: int = 900) -> None:
        """Setea valor con TTL en segundos."""
        with self._lock:
            self._data[key] = {
                "value": value,
                "expires_at": time.time() + ttl_seconds,
                "stored_at": time.time(),
            }
            # Persiste en background sin bloquear
            self._persist()

    def delete(self, key: str) -> None:
        """Borra una entry."""
        with self._lock:
            self._data.pop(key, None)
            self._persist()

    def clear(self) -> int:
        """Limpia todo el cache. Devuelve cuantas entries habia."""
        with self._lock:
            count = len(self._data)
            self._data.clear()
            self._persist()
            return count

    def size(self) -> int:
        """Cuantas entries hay (incluye expiradas)."""
        with self._lock:
            return len(self._data)

    def stats(self) -> dict[str, Any]:
        """Estadisticas del cache."""
        with self._lock:
            now = time.time()
            valid = sum(1 for e in self._data.values() if e.get("expires_at", 0) >= now)
            expired = len(self._data) - valid
            return {
                "total_entries": len(self._data),
                "valid": valid,
                "expired": expired,
                "cache_file": str(self.cache_file),
            }


_cache: FileCache | None = None


def get_cache() -> FileCache:
    """Singleton del cache global."""
    global _cache
    if _cache is None:
        _cache = FileCache()
    return _cache
