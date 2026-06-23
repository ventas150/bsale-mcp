"""Sync incremental frecuente (Render Cron Job).

A diferencia del nocturno (cron_snapshot.py -> nightly_snapshot), este corre
seguido para mantener venta y stock casi en vivo, y regenera la capa LLM.

Modos (flag --modo):
  ventas  -> documentos del día + detalle reciente + digests   (barato, cada ~20 min)
  stock   -> foto completa de stock + digests                  (pesado, cada ~60 min)
  full    -> ventas + stock + digests                          (default)

Uso:
  python sync_incremental.py --modo ventas
  python sync_incremental.py --modo stock
  python sync_incremental.py            # full

Reanudable e idempotente: todos los snapshots hacen upsert / on_conflict.
Sale con código !=0 si algún paso falla (para que Render marque la corrida).
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("sync_incremental")


def sync_ventas() -> dict[str, Any]:
    """Documentos recientes + detalle reciente. Barato (usa emissiondaterange)."""
    from snapshot import snapshot_documents, snapshot_details

    out: dict[str, Any] = {}
    # days_back=2 cubre documentos de ayer que entran tarde; upsert evita duplicar.
    out["documents"] = snapshot_documents(days_back=2, max_pages=200)
    # Detalle de los docs recientes que aún no lo tengan.
    out["details"] = snapshot_details(batch_size=400, max_docs=400, only_recent_days=3)
    return out


def sync_stock() -> dict[str, Any]:
    """Foto completa del stock actual. Pesado: ~235k registros (sin filtro incremental)."""
    from snapshot import snapshot_stock

    return {"stock": snapshot_stock(max_pages=6000)}


def run(modo: str) -> int:
    results: dict[str, Any] = {}

    if modo in ("ventas", "full"):
        try:
            results.update(sync_ventas())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_ventas: %s", e)
            results["ventas_error"] = str(e)

    if modo in ("stock", "full"):
        try:
            results.update(sync_stock())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_stock: %s", e)
            results["stock_error"] = str(e)

    # Regenerar la capa LLM al final, siempre.
    try:
        from digests import build_all
        results["digests"] = build_all()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en digests: %s", e)
        results["digests_error"] = str(e)

    logger.info("Sync incremental (%s) terminado: %s", modo, results)

    failed = [k for k in results if k.endswith("_error")]
    if failed:
        logger.error("Pasos con error: %s", failed)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync incremental Bsale -> Postgres + digests")
    parser.add_argument(
        "--modo",
        choices=["ventas", "stock", "full"],
        default="full",
        help="ventas (barato), stock (pesado), full (ambos). Default full.",
    )
    args = parser.parse_args()
    return run(args.modo)


if __name__ == "__main__":
    sys.exit(main())
