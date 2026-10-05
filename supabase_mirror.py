"""Espejo de documentos Bsale hacia Supabase (myscrubs.bsale_documents).

Lo llama el snapshot de documentos DESPUES de guardar en Postgres propio, con las
mismas filas: cero lecturas extra a Bsale (la cuota de la API es compartida con
Loadingplay y la app de documentos).

Opt-in: si no estan SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY en el entorno, no hace nada.
Nunca rompe el snapshot: cualquier error se registra y se devuelve en el resumen.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CHUNK = 500


def activo() -> bool:
    return bool(os.getenv("SUPABASE_URL", "").strip() and os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())


def _fila(r: dict[str, Any]) -> dict[str, Any]:
    raw = r.get("raw") or {}
    em = r.get("emission_date")
    return {
        "document_id": r.get("document_id"),
        "emission_date": em.date().isoformat() if em is not None else None,
        "office_id": r.get("office_id"),
        "office_name": r.get("office_name"),
        "document_type_id": r.get("document_type_id"),
        "document_type_name": r.get("document_type_name"),
        "document_type_use": r.get("document_type_use"),
        "client_id": r.get("client_id"),
        "number": raw.get("number"),
        "sales_id": raw.get("salesId") or None,
        "total_amount": r.get("total_amount"),
        "net_amount": r.get("net_amount"),
        "tax_amount": r.get("tax_amount"),
        "state": r.get("state"),
    }


def espejar(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert por document_id en Supabase. Devuelve {espejadas, error?}."""
    if not activo():
        return {"espejadas": 0, "omitido": "sin SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY"}
    if not rows:
        return {"espejadas": 0}
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/bsale_documents?on_conflict=document_id"
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Content-Profile": "myscrubs",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    filas = [_fila(r) for r in rows if r.get("document_id") is not None]
    n = 0
    try:
        with httpx.Client(timeout=30) as c:
            for i in range(0, len(filas), CHUNK):
                resp = c.post(url, headers=headers, json=filas[i:i + CHUNK])
                if resp.status_code >= 300:
                    raise RuntimeError(f"Supabase {resp.status_code}: {resp.text[:300]}")
                n += len(filas[i:i + CHUNK])
        return {"espejadas": n}
    except Exception as e:  # el snapshot nunca falla por el espejo
        logger.warning("espejo supabase fallo: %s", e)
        return {"espejadas": n, "error": str(e)[:300]}
