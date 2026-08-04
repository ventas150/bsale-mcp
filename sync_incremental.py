"""Sync incremental frecuente (Render Cron Job).

A diferencia del nocturno (cron_snapshot.py -> nightly_snapshot), este corre
seguido para mantener venta y stock casi en vivo, y regenera la capa LLM.

Modos (flag --modo):
  ventas  -> documentos del día + detalle reciente + digests   (barato)
  stock   -> foto completa de stock + digests                  (pesado)
  full    -> ventas + stock + digests
  auto    -> RECOMENDADO para correr cada 30 min en un solo cron:
               * ventas + detalle + digests  -> SIEMPRE
               * stock                        -> 1 vez por hora (top of hour)
               * variantes (catálogo)         -> 1 vez al día (~05:xx UTC)
             Así, con schedule */30 * * * *, obtienes ventas c/30min,
             stock c/hora y catálogo diario, sin reventar el rate limit.

Uso:
  python sync_incremental.py --modo auto    # para el cron */30 * * * *
  python sync_incremental.py --modo ventas
  python sync_incremental.py --modo stock

Reanudable e idempotente: todos los snapshots hacen upsert / on_conflict.
Sale con código !=0 si algún paso falla (para que Render marque la corrida).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("sync_incremental")

# Hora UTC en la que el modo auto refresca el catálogo de variantes (1x/día).
VARIANTS_HOUR_UTC = 5

# La foto completa de stock via API tarda ~2h (235k registros a 50/pagina).
# Correrla cada hora bloqueaba todas las corridas del cron (incidente ago-2026):
# ahora solo corre si la ultima foto tiene mas de STOCK_EVERY_HOURS horas, y
# SIEMPRE al final de la corrida para no retrasar ventas/backfill/digests.
STOCK_EVERY_HOURS = float(os.getenv("STOCK_EVERY_HOURS", "12"))


def _stock_photo_age_hours() -> float:
    """Horas desde la ultima foto de stock. Infinito si no hay ninguna."""
    from sqlalchemy import text
    from db import session as db_session
    try:
        with db_session() as s:
            ts = s.execute(text("select max(snapshot_date) from stock_snapshot")).scalar()
        if not ts:
            return float("inf")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception as e:  # noqa: BLE001
        logger.warning("_stock_photo_age_hours: %s", e)
        return 0.0  # ante la duda, no correr el paso pesado


def _variants_empty() -> bool:
    """True si el catalogo de variantes aun no se carga en esta base."""
    from sqlalchemy import text
    from db import session as db_session
    try:
        with db_session() as s:
            row = s.execute(text("select 1 from variants_snapshot limit 1")).first()
        return row is None
    except Exception:  # noqa: BLE001
        return False

# Backfill histórico de documentos por tramos (hasta HIST_MESES_POR_CORRIDA
# meses por corrida :30 del cron). Recorre desde HIST_START hasta HIST_END
# (exclusivo). Tras el incidente de storage ago-2026 la base se reconstruye
# desde cero, por lo que el rango cubre hasta hoy.
HIST_START = "2024-12"
HIST_END = "2026-09"
HIST_MESES_POR_CORRIDA = 4


def _month_bounds(ym: str):
    y, m = int(ym[:4]), int(ym[5:7])
    start = "%04d-%02d-01" % (y, m)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    nxt = "%04d-%02d-01" % (ny, nm)
    return start, nxt, "%04d-%02d" % (ny, nm)


def _hist_cursor_get() -> str:
    from sqlalchemy import text
    from db import session as db_session
    with db_session() as s:
        row = s.execute(text(
            "select data->>'cursor' from llm_digests where digest_key='hist_backfill_cursor'"
        )).first()
    return row[0] if row and row[0] else HIST_START


def _hist_cursor_set(ym: str) -> None:
    import json as _json
    from sqlalchemy import text
    from db import session as db_session
    with db_session() as s:
        s.execute(text(
            "insert into llm_digests (digest_key, data, generated_at) "
            "values ('hist_backfill_cursor', cast(:d as jsonb), now()) "
            "on conflict (digest_key) do update set data=excluded.data, generated_at=excluded.generated_at"
        ), {"d": _json.dumps({"cursor": ym})})


def backfill_historico_step() -> dict[str, Any]:
    """Carga UN mes histórico de documentos y avanza el cursor. Idempotente."""
    cur = _hist_cursor_get()
    if cur >= HIST_END:
        return {"hist": "completo", "cursor": cur}
    from snapshot import snapshot_documents_range
    start, nxt, nxt_ym = _month_bounds(cur)
    res = snapshot_documents_range(start, nxt, max_pages=200)
    _hist_cursor_set(nxt_ym)
    return {"hist_mes": cur, "rows": res.get("rows"), "next_cursor": nxt_ym}


def sync_ventas() -> dict[str, Any]:
    """Documentos recientes + detalle reciente. Barato (usa emissiondaterange)."""
    from snapshot import snapshot_documents, snapshot_details

    out: dict[str, Any] = {}
    # days_back=2 cubre documentos de ayer que entran tarde; upsert evita duplicar.
    out["documents"] = snapshot_documents(days_back=2, max_pages=200)
    # Ventana de 90 dias: cada corrida procesa hasta 400 documentos sin detalle,
    # asi el cron va completando el backlog historico de ~90 dias por si solo
    # (de lo mas reciente a lo mas viejo). Cuando esta al dia, solo mantiene lo nuevo.
    out["details"] = snapshot_details(batch_size=400, max_docs=100000, only_recent_days=90)
    return out


def sync_stock() -> dict[str, Any]:
    """Foto completa del stock actual. Pesado: ~235k registros (sin filtro incremental)."""
    from snapshot import snapshot_stock

    return {"stock": snapshot_stock(max_pages=6000)}


def sync_variantes() -> dict[str, Any]:
    """Catálogo completo de variantes (pesado; correr 1x/día)."""
    from snapshot import snapshot_variants

    return {"variants": snapshot_variants(max_pages=2000)}


def run(modo: str) -> int:
    results: dict[str, Any] = {}
    now = datetime.now(timezone.utc)

    # Asegura el esquema de digests ANTES de todo: el cursor del backfill
    # historico vive en llm_digests y en una base recien creada aun no existe.
    try:
        from digests import ensure_schema
        ensure_schema()
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_schema fallo: %s", e)

    do_ventas = modo in ("ventas", "full", "auto")
    # stock: en full/stock siempre; en auto solo si la foto esta vieja (ver arriba)
    do_stock = modo in ("stock", "full") or (
        modo == "auto" and _stock_photo_age_hours() >= STOCK_EVERY_HOURS
    )
    # variantes: 1 vez al día, o si el catalogo aun no existe en esta base
    do_variants = modo == "auto" and (
        (now.hour == VARIANTS_HOUR_UTC and now.minute < 15) or _variants_empty()
    )
    # backfill histórico de documentos: siempre que quede pendiente (no-op al completar)
    do_hist = modo == "auto"

    if do_ventas:
        try:
            results.update(sync_ventas())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_ventas: %s", e)
            results["ventas_error"] = str(e)

    if do_hist:
        try:
            pasos = []
            for _ in range(HIST_MESES_POR_CORRIDA):
                paso = backfill_historico_step()
                pasos.append(paso)
                if paso.get("hist") == "completo":
                    break
            results["historico"] = pasos
        except Exception as e:  # noqa: BLE001
            logger.error("Error en backfill_historico_step: %s", e)
            results["hist_error"] = str(e)

    if do_variants:
        try:
            results.update(sync_variantes())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_variantes: %s", e)
            results["variants_error"] = str(e)

    # Regenerar la capa LLM (antes del stock, que es el paso lento).
    try:
        from digests import build_all
        results["digests"] = build_all()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en digests: %s", e)
        results["digests_error"] = str(e)

    # Retencion: purga fotos viejas de stock/variantes para que la DB no
    # crezca sin limite (incidente storage ago-2026). Barato; corre siempre.
    # Los errores de retencion NO marcan la corrida como fallida.
    try:
        from retention import apply_retention
        results["retention"] = apply_retention()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en retention: %s", e)
        results["retention_warning"] = str(e)

    # Stock AL FINAL: es el paso lento (~2h por la API de Bsale) y no debe
    # bloquear ventas, backfill ni digests si la corrida se corta.
    if do_stock:
        try:
            results.update(sync_stock())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_stock: %s", e)
            results["stock_error"] = str(e)

    logger.info("Sync (%s) terminado [stock=%s variants=%s]: %s",
                modo, do_stock, do_variants, results)

    failed = [k for k in results if k.endswith("_error")]
    if failed:
        logger.error("Pasos con error: %s", failed)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync incremental Bsale -> Postgres + digests")
    parser.add_argument(
        "--modo",
        choices=["ventas", "stock", "full", "auto"],
        default="auto",
        help="auto (recomendado para cron */30), ventas, stock, o full.",
    )
    args = parser.parse_args()
    return run(args.modo)


if __name__ == "__main__":
    sys.exit(main())
