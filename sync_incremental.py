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

    do_ventas = modo in ("ventas", "full", "auto")
    # stock: en full/stock siempre; en auto solo al top of hour
    do_stock = modo in ("stock", "full") or (modo == "auto" and now.minute < 15)
    # variantes: solo en auto, 1 vez al día
    do_variants = modo == "auto" and now.hour == VARIANTS_HOUR_UTC and now.minute < 15

    if do_ventas:
        try:
            results.update(sync_ventas())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_ventas: %s", e)
            results["ventas_error"] = str(e)

    if do_stock:
        try:
            results.update(sync_stock())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_stock: %s", e)
            results["stock_error"] = str(e)

    if do_variants:
        try:
            results.update(sync_variantes())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_variantes: %s", e)
            results["variants_error"] = str(e)

    # Regenerar la capa LLM al final, siempre.
    try:
        from digests import build_all
        results["digests"] = build_all()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en digests: %s", e)
        results["digests_error"] = str(e)

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
