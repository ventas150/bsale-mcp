"""Audit log para operaciones de escritura en Bsale.

Cada write (POST/PUT/DELETE) se loguea como JSON line en disco + stdout.
Util para:
- Compliance (quien modifico que y cuando)
- Debugging (rastrear porque cambio un dato)
- Rollback (saber que revertir)

Formato JSONL, una linea por evento.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("audit")
_lock = threading.Lock()

AUDIT_DIR = Path(os.getenv("AUDIT_DIR", "/tmp/bsale_audit"))
AUDIT_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_FILE = AUDIT_DIR / "writes.jsonl"


def audit_log(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    actor: str | None = None,
) -> None:
    """Registra un evento de escritura.

    Args:
        method: HTTP method (POST, PUT, DELETE).
        path: Endpoint llamado.
        params: Query params (raras veces en writes pero por completitud).
        body: Body de la request (atencion: puede contener data sensible).
        result_summary: Resumen reducido del response (solo id, href, count).
        actor: Identificador opcional del actor que origino el cambio.
    """
    event = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": method,
        "path": path,
        "actor": actor or os.getenv("AUDIT_DEFAULT_ACTOR", "bsale-mcp"),
    }
    if params:
        event["params"] = _redact(params)
    if body:
        event["body"] = _redact(body)
    if result_summary:
        event["result"] = result_summary

    line = json.dumps(event, ensure_ascii=False)

    # Stdout para que Render lo capture en logs
    logger.info("AUDIT %s", line)

    # Tambien a disco
    with _lock:
        try:
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.warning("No se pudo escribir audit log a disco: %s", e)


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """Censura campos sensibles del log."""
    sensitive_keys = {"password", "token", "access_token", "secret", "api_key"}
    redacted = {}
    for k, v in data.items():
        if k.lower() in sensitive_keys:
            redacted[k] = "***REDACTED***"
        else:
            redacted[k] = v
    return redacted


def read_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Lee los ultimos N eventos del audit log."""
    if not AUDIT_FILE.exists():
        return []
    with _lock:
        try:
            with AUDIT_FILE.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
