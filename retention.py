"""Politica de retencion para las tablas de snapshot.

Evita que la base crezca sin limite (la causa del incidente de storage
de agosto 2026: stock_snapshot llego a 13 GB con fotos horarias que
ningun tool consultaba).

Reglas (configurables por env var):
- stock_snapshot:
    * fotos horarias se conservan STOCK_HOURLY_RETENTION_HOURS (default 48h)
    * mas alla de eso, solo la ULTIMA foto de cada dia
    * todo lo anterior a STOCK_DAILY_RETENTION_DAYS se borra (default 30d)
- variants_snapshot:
    * se conservan las ultimas VARIANTS_KEEP_SNAPSHOTS fotos del catalogo
      (default 2)
- documents_snapshot / document_details_snapshot: NO se tocan.
  Ahi vive el historico de ventas (backfill desde 2024-12) que alimenta
  los comparativos; pesa poco porque es una fila por documento/linea.

Llamado desde sync_incremental.run() al final de cada corrida. Es barato:
en estado estacionario borra solo un punado de fotos viejas.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text

from db import session as db_session

logger = logging.getLogger(__name__)

STOCK_HOURLY_RETENTION_HOURS = int(os.getenv("STOCK_HOURLY_RETENTION_HOURS", "48"))
STOCK_DAILY_RETENTION_DAYS = int(os.getenv("STOCK_DAILY_RETENTION_DAYS", "30"))
VARIANTS_KEEP_SNAPSHOTS = int(os.getenv("VARIANTS_KEEP_SNAPSHOTS", "2"))


def purge_stock_snapshots() -> int:
    """Borra fotos de stock fuera de la politica. Devuelve filas borradas."""
    sql = text(
        """
        DELETE FROM stock_snapshot s
        WHERE s.snapshot_date < now() - make_interval(hours => :hours)
          AND (
            s.snapshot_date < now() - make_interval(days => :days)
            OR s.snapshot_date NOT IN (
              SELECT max(snapshot_date)
              FROM stock_snapshot
              GROUP BY snapshot_date::date
            )
          )
        """
    )
    with db_session() as s:
        res = s.execute(sql, {
            "hours": STOCK_HOURLY_RETENTION_HOURS,
            "days": STOCK_DAILY_RETENTION_DAYS,
        })
        return res.rowcount or 0


def purge_variants_snapshots() -> int:
    """Conserva solo las ultimas N fotos del catalogo. Devuelve filas borradas."""
    sql = text(
        """
        DELETE FROM variants_snapshot
        WHERE snapshot_date NOT IN (
          SELECT snapshot_date FROM (
            SELECT DISTINCT snapshot_date
            FROM variants_snapshot
            ORDER BY snapshot_date DESC
            LIMIT :keep
          ) k
        )
        """
    )
    with db_session() as s:
        res = s.execute(sql, {"keep": VARIANTS_KEEP_SNAPSHOTS})
        return res.rowcount or 0


def apply_retention() -> dict[str, Any]:
    """Aplica toda la politica de retencion. Nunca lanza (loggea y sigue)."""
    out: dict[str, Any] = {}
    try:
        out["stock_rows_deleted"] = purge_stock_snapshots()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en purge_stock_snapshots: %s", e)
        out["stock_error"] = str(e)
    try:
        out["variants_rows_deleted"] = purge_variants_snapshots()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en purge_variants_snapshots: %s", e)
        out["variants_error"] = str(e)
    if out.get("stock_rows_deleted") or out.get("variants_rows_deleted"):
        logger.info("Retencion aplicada: %s", out)
    return out
