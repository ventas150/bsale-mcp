"""Carga histórica (one-shot, manual) de Bsale a Postgres.

Se corre UNA vez para llenar la réplica; después el día a día lo mantiene
sync_incremental.py.

Pasos:
  documentos  -> recorre mes a mes desde --desde hasta hoy (upsert por document_id)
  detalle     -> completa el detalle de TODOS los docs sin líneas (los ~16k faltantes)
  variantes   -> catálogo completo (sin el tope de 5.000 del nocturno)
  stock       -> una foto del stock actual (punto de partida de la serie histórica)

IMPORTANTE:
  - El stock histórico NO se puede recuperar hacia atrás; Bsale solo da el actual.
    La serie diaria se construye desde hoy con sync_incremental.
  - Reanudable: si se corta, volver a correrlo retoma donde quedó (todo es upsert
    / on_conflict, y el detalle solo procesa lo que falta).
  - Respeta rate limits vía el cliente (backoff). Igual conviene correrlo fuera de
    horario peak.

Uso:
  python backfill.py --desde 2024-12-01
  python backfill.py --desde 2024-12-01 --solo documentos
  python backfill.py --solo detalle
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from bsale_client import get_client, is_sales_doc, iso_to_epoch_range
from db import documents_snapshot, session as db_session
from snapshot import _ts_to_dt, snapshot_details, snapshot_stock, snapshot_variants

logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill")


# ============================
# Documentos: mes a mes
# ============================

def _month_ranges(date_from: date, date_to: date) -> list[tuple[str, str]]:
    """Genera rangos [primer_día_mes, último_día_mes] (ISO) de date_from a date_to."""
    ranges: list[tuple[str, str]] = []
    y, m = date_from.year, date_from.month
    while (y, m) <= (date_to.year, date_to.month):
        start = date(y, m, 1)
        # primer día del mes siguiente
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = date(ny, nm, 1)  # exclusivo; usamos día anterior
        end_inclusive = date.fromordinal(end.toordinal() - 1)
        # no pasar de date_to
        if end_inclusive > date_to:
            end_inclusive = date_to
        ranges.append((start.isoformat(), end_inclusive.isoformat()))
        y, m = ny, nm
    return ranges


def _upsert_documents(rows: list[dict[str, Any]]) -> None:
    CHUNK = 500
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        with db_session() as s:
            stmt = pg_insert(documents_snapshot).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["document_id"],
                set_={
                    "snapshot_date": stmt.excluded.snapshot_date,
                    "emission_date": stmt.excluded.emission_date,
                    "office_id": stmt.excluded.office_id,
                    "office_name": stmt.excluded.office_name,
                    "document_type_id": stmt.excluded.document_type_id,
                    "document_type_name": stmt.excluded.document_type_name,
                    "document_type_use": stmt.excluded.document_type_use,
                    "client_id": stmt.excluded.client_id,
                    "total_amount": stmt.excluded.total_amount,
                    "net_amount": stmt.excluded.net_amount,
                    "tax_amount": stmt.excluded.tax_amount,
                    "state": stmt.excluded.state,
                    "raw": stmt.excluded.raw,
                },
            )
            s.execute(stmt)


def backfill_documents(desde: date, hasta: date | None = None) -> dict[str, Any]:
    """Descarga todos los documentos de venta desde 'desde' hasta hoy, mes a mes."""
    client = get_client()
    hasta = hasta or datetime.now(timezone.utc).date()
    snapshot_ts = datetime.now(timezone.utc)
    total = 0

    for start_iso, end_iso in _month_ranges(desde, hasta):
        params = {
            "limit": 50,
            "emissiondaterange": iso_to_epoch_range(start_iso, end_iso),
            "expand": "[document_type,office,client]",
        }
        docs = client.paginated_get("/v1/documents.json", params=params, max_pages=80)
        rows = []
        for doc in docs:
            if not is_sales_doc(doc):  # excluye guías (use=2)
                continue
            office = doc.get("office") or {}
            doctype = doc.get("document_type") or {}
            client_ref = doc.get("client") or {}
            rows.append({
                "snapshot_date": snapshot_ts,
                "document_id": doc.get("id"),
                "emission_date": _ts_to_dt(doc.get("emissionDate")),
                "office_id": office.get("id"),
                "office_name": office.get("name"),
                "document_type_id": doctype.get("id"),
                "document_type_name": doctype.get("name"),
                "document_type_use": doctype.get("use", 0),
                "client_id": client_ref.get("id"),
                "total_amount": float(doc.get("totalAmount", 0) or 0),
                "net_amount": float(doc.get("netAmount", 0) or 0),
                "tax_amount": float(doc.get("taxAmount", 0) or 0),
                "state": doc.get("state"),
                "raw": doc,
            })
        if rows:
            _upsert_documents(rows)
        total += len(rows)
        logger.info("Mes %s..%s: %d documentos (acumulado %d)", start_iso, end_iso, len(rows), total)
        time.sleep(0.5)  # respiro entre meses

    return {"documentos_total": total, "desde": desde.isoformat(), "hasta": hasta.isoformat()}


# ============================
# Detalle: hasta agotar
# ============================

def backfill_details(batch_size: int = 300, pausa_s: float = 0.3) -> dict[str, Any]:
    """Corre snapshot_details en loop hasta que no queden docs sin detalle."""
    total_docs = 0
    total_lines = 0
    total_errors = 0
    vueltas = 0
    while True:
        res = snapshot_details(batch_size=batch_size, max_docs=100_000, only_recent_days=None)
        total_docs += res.get("docs_processed", 0)
        total_lines += res.get("lines_inserted", 0)
        total_errors += res.get("errors", 0)
        vueltas += 1
        logger.info(
            "Detalle vuelta %d: %d docs, %d líneas, %d errores, faltan ~%d",
            vueltas, res.get("docs_processed", 0), res.get("lines_inserted", 0),
            res.get("errors", 0), res.get("remaining_to_process", 0),
        )
        # Si no procesó nada en esta vuelta, terminamos.
        if res.get("docs_processed", 0) == 0:
            break
        time.sleep(pausa_s)

    return {
        "docs_procesados": total_docs,
        "lineas_insertadas": total_lines,
        "errores": total_errors,
        "vueltas": vueltas,
    }


# ============================
# Orquestación
# ============================

def main() -> int:
    parser = argparse.ArgumentParser(description="Carga histórica Bsale -> Postgres (one-shot)")
    parser.add_argument("--desde", default="2024-12-01", help="Fecha inicio documentos (YYYY-MM-DD)")
    parser.add_argument(
        "--solo",
        choices=["documentos", "detalle", "variantes", "stock"],
        default=None,
        help="Correr solo un paso. Por defecto corre todos en orden.",
    )
    args = parser.parse_args()

    desde = datetime.strptime(args.desde, "%Y-%m-%d").date()
    pasos = [args.solo] if args.solo else ["documentos", "detalle", "variantes", "stock"]
    results: dict[str, Any] = {}

    if "documentos" in pasos:
        logger.info("=== Backfill documentos desde %s ===", desde)
        results["documentos"] = backfill_documents(desde)

    if "detalle" in pasos:
        logger.info("=== Backfill detalle (todos los docs sin líneas) ===")
        results["detalle"] = backfill_details()

    if "variantes" in pasos:
        logger.info("=== Backfill variantes (catálogo completo) ===")
        results["variantes"] = snapshot_variants(max_pages=2000)

    if "stock" in pasos:
        logger.info("=== Foto inicial de stock ===")
        results["stock"] = snapshot_stock(max_pages=6000)

    logger.info("Backfill terminado: %s", results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
