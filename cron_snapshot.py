"""Entry point del snapshot nocturno como proceso aparte (Render Cron Job).

Reemplaza al APScheduler in-process de server.py: el snapshot pesado deja de
competir por memoria con el web service y ya no muere en cada redeploy.
Render ejecuta este script segun el schedule del cron job; corre una vez y
termina con exit code 0 (ok) o !=0 (falla, visible en los eventos del cron).
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("cron_snapshot")


def run() -> int:
    from snapshot import nightly_snapshot

    logger.info("Iniciando snapshot via cron job")
    result = nightly_snapshot()
    logger.info("Snapshot terminado: %s", result)

    # Si alguno de los pasos registro error, salir con codigo !=0 para que
    # Render marque la corrida como fallida y dispare la notificacion.
    failed = [k for k in result if k.endswith("_error")]
    if failed:
        logger.error("Pasos con error: %s", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
