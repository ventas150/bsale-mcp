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

    # Postgres es el destino REAL. El archivo queda como respaldo por si la
    # base no esta disponible: el audit nunca puede tumbar una escritura al
    # ERP, pero tampoco puede perderse en silencio.
    if not _escribir_en_postgres(event):
        with _lock:
            try:
                _rotar_si_hace_falta()
                with AUDIT_FILE.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                logger.warning("No se pudo escribir audit log a disco: %s", e)


def _escribir_en_postgres(event: dict[str, Any]) -> bool:
    """Inserta el evento en audit_log. Devuelve False si no se pudo.

    Nunca lanza: esto lo llama cada escritura hacia Bsale y un problema de
    logging no puede voltear una operacion del ERP.
    """
    try:
        from datetime import datetime, timezone

        from db import DATABASE_URL, audit_log as tabla, session as db_session

        if not DATABASE_URL:
            return False

        with db_session() as s:
            s.execute(tabla.insert().values(
                ts=datetime.fromtimestamp(event["ts"], tz=timezone.utc),
                method=event.get("method"),
                path=(event.get("path") or "")[:500],
                actor=(event.get("actor") or "")[:100],
                evento=event,
            ))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Audit a Postgres fallo, se usa el archivo: %s", e)
        return False


_SENSIBLES = {
    "password", "token", "access_token", "secret", "api_key",
    "authorization", "apikey", "clientsecret", "client_secret",
    "mcp_auth_token", "mcp_url_secret",
}


def _redact(data: Any, _prof: int = 0) -> Any:
    """Censura campos sensibles, TAMBIEN dentro de estructuras anidadas.

    Antes solo recorria el primer nivel, y los bodies de escritura son
    anidados ({"details": [...]}). El audit va tambien a stdout y de ahi a los
    logs de Render y a Sentry, asi que cualquier campo sensible enterrado
    quedaba en claro.
    """
    if _prof > 8:
        return "***PROFUNDIDAD_MAXIMA***"
    if isinstance(data, dict):
        return {
            k: ("***REDACTED***" if str(k).lower() in _SENSIBLES
                else _redact(v, _prof + 1))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [_redact(v, _prof + 1) for v in data]
    return data


MAX_BYTES = int(os.getenv("AUDIT_MAX_BYTES", str(20 * 1024 * 1024)))
"""Tamano al que se rota el audit log. El disco de Render es de 1 GB y lo
comparte con el cache."""

_COLA_BYTES = 2 * 1024 * 1024
"""Cuanto se lee desde el final en read_recent. Antes se hacia f.readlines()
del archivo COMPLETO y se descartaba todo menos las ultimas N lineas: con un
log de cientos de MB, un GET /audit intentaba cargarlo entero en 512 MB de
RAM."""


def _rotar_si_hace_falta() -> None:
    """Rota el log cuando pasa MAX_BYTES. Sin esto crecia sin limite."""
    try:
        if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size > MAX_BYTES:
            previo = AUDIT_FILE.with_suffix(".jsonl.1")
            previo.unlink(missing_ok=True)
            AUDIT_FILE.rename(previo)
            logger.info("Audit log rotado a %s", previo)
    except OSError as e:
        logger.warning("No se pudo rotar el audit log: %s", e)


def _leer_de_postgres(limit: int) -> list[dict[str, Any]] | None:
    """Ultimos N eventos desde audit_log. None si la base no esta disponible.

    Se distingue None (no pude leer) de [] (lei y no hay nada): con [] el
    llamador se queda tranquilo creyendo que no hubo escrituras, que es
    justamente la clase de mentira silenciosa que se saco de este repo.
    """
    try:
        from sqlalchemy import desc, select

        from db import DATABASE_URL, audit_log as tabla, session as db_session

        if not DATABASE_URL:
            return None

        with db_session() as s:
            filas = s.execute(
                select(tabla.c.evento).order_by(desc(tabla.c.ts)).limit(limit)
            ).scalars().all()
        # El mas nuevo primero desde SQL; se devuelve en orden cronologico,
        # que es como venia del archivo.
        return [f for f in reversed(filas) if f]
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo leer el audit de Postgres: %s", e)
        return None


def read_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Lee los ultimos N eventos del audit log."""
    limit = max(1, min(int(limit), 1000))

    desde_db = _leer_de_postgres(limit)
    if desde_db is not None:
        return desde_db

    # Respaldo: el archivo, que puede tener lo escrito mientras la base no
    # estaba disponible.
    if not AUDIT_FILE.exists():
        return []
    with _lock:
        try:
            tam = AUDIT_FILE.stat().st_size
            with AUDIT_FILE.open("rb") as f:
                if tam > _COLA_BYTES:
                    f.seek(tam - _COLA_BYTES)
                    f.readline()  # descartar la linea partida por el seek
                crudo = f.read()
        except OSError:
            return []
    lines = crudo.decode("utf-8", errors="replace").splitlines()
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
