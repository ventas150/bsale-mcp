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

# El directorio se resuelve con fallback a proposito. AUDIT_DIR apunta al disco
# persistente de Render (/var/data/...), que solo existe si el disco quedo
# efectivamente montado. Si no esta, este mkdir corria en tiempo de import y
# tumbaba el server entero: el proceso no arrancaba y el deploy quedaba caido
# por un problema de LOGGING. El audit es importante, pero no vale tirar abajo
# el ERP; si el disco no esta, se degrada a /tmp y se avisa fuerte.
_AUDIT_FALLBACK = Path("/tmp/bsale_audit")  # noqa: S108


def _resolver_audit_dir() -> Path:
    preferido = Path(os.getenv("AUDIT_DIR", str(_AUDIT_FALLBACK)))
    for candidato in (preferido, _AUDIT_FALLBACK):
        try:
            candidato.mkdir(parents=True, exist_ok=True)
            probe = candidato / ".probe"
            probe.touch()
            probe.unlink(missing_ok=True)
        except OSError as e:
            logger.error(
                "AUDIT_DIR %s no es escribible (%s). El audit log NO es persistente.",
                candidato, e,
            )
            continue
        if candidato != preferido:
            logger.error(
                "Audit log degradado a %s: se pierde en cada deploy. "
                "Revisar que el disco de Render este montado en %s.",
                candidato, preferido,
            )
        return candidato
    logger.error("Ningun directorio de audit escribible; solo queda el log a stdout.")
    return preferido


AUDIT_DIR = _resolver_audit_dir()
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
