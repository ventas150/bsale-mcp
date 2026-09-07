"""Prueba de aceptacion del MCP de Bsale: velocidad y exactitud.

Correr DESPUES de desplegar la rama fix/venta-oficial-y-paginacion, en el
mismo entorno que el servicio (necesita BSALE_ACCESS_TOKEN y, para la parte
de snapshot, DATABASE_URL).

    python bench_aceptacion.py                 # todo
    python bench_aceptacion.py --solo-vivo     # sin base de datos

Que verifica:
  EXACTITUD  la venta oficial de un rango conocido calza con el valor de
             referencia medido a mano el 07-sep-2026 contra Bsale;
             las notas de venta, guias y anulados quedan fuera;
             el snapshot y Bsale en vivo dan lo mismo.
  VELOCIDAD  cuanto demora leer 1, 7 y 30 dias, y cuanto de eso es el
             paginado en paralelo contra el secuencial viejo.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

from bsale_client import (
    doc_revenue_signed,
    get_client,
    is_official_sale,
    is_sales_note,
    is_sales_doc,
    iso_to_epoch_range,
)

# Referencia medida a mano contra Bsale el 07-sep-2026 (rango 04-05 sep 2026):
#   Boletas 18.045.622 + Facturas 2.177.178 + ND 39.990 - NC 1.659.096
REF_DESDE = "2026-09-04"
REF_HASTA = "2026-09-05"
REF_VENTA_OFICIAL = 18_603_694
REF_NOTAS_DE_VENTA = 774_872
REF_DOCUMENTOS = 218  # documentos de venta (sin guias)

VERDE, ROJO, GRIS = "\033[92m", "\033[91m", "\033[0m"


def _ok(cond: bool) -> str:
    return f"{VERDE}OK{GRIS}" if cond else f"{ROJO}FALLA{GRIS}"


def _params(desde: str, hasta: str) -> dict:
    return {
        "limit": 50,
        "emissiondaterange": iso_to_epoch_range(desde, hasta),
        "state": 0,
        "expand": "[document_type,office]",
    }


def exactitud() -> int:
    client = get_client()
    fetch = client.paginated_fetch(
        "/v1/documents.json", params=_params(REF_DESDE, REF_HASTA), max_items=40000
    )
    docs = fetch["items"]

    oficial = [d for d in docs if is_official_sale(d)]
    venta = sum(doc_revenue_signed(d) for d in oficial)
    notas = sum(
        float(d.get("totalAmount", 0) or 0) for d in docs if is_sales_note(d)
    )
    guias = sum(1 for d in docs if not is_sales_doc(d))
    anulados = sum(1 for d in docs if int(d.get("state", 0) or 0) != 0)

    fallas = 0
    print(f"\n== EXACTITUD ({REF_DESDE} a {REF_HASTA}) ==")
    checks = [
        ("no truncado", not fetch["truncated"]),
        (f"venta oficial = {REF_VENTA_OFICIAL:,}", abs(venta - REF_VENTA_OFICIAL) < 1),
        (f"documentos de venta = {REF_DOCUMENTOS}", len(oficial) == REF_DOCUMENTOS),
        (f"notas de venta fuera = {REF_NOTAS_DE_VENTA:,}", abs(notas - REF_NOTAS_DE_VENTA) < 1),
    ]
    for nombre, cond in checks:
        fallas += 0 if cond else 1
        print(f"  [{_ok(cond)}] {nombre}")
    print(f"  medido: venta {venta:,.0f} | docs {len(oficial)} | "
          f"notas de venta {notas:,.0f} | guias {guias} | anulados {anulados}")
    print(f"  (si la referencia ya no aplica porque cambiaron documentos de esas "
          f"fechas, actualizar las constantes REF_* de este archivo)")
    return fallas


def velocidad() -> None:
    client = get_client()
    hoy = date.today()
    print("\n== VELOCIDAD (lectura en vivo) ==")
    print(f"  {'rango':>10} | {'docs':>6} | {'paralelo':>9} | {'secuencial':>11} | mejora")
    for dias in (1, 7, 30):
        hasta = hoy - timedelta(days=1)
        desde = hasta - timedelta(days=dias - 1)
        p = _params(desde.isoformat(), hasta.isoformat())

        t0 = time.perf_counter()
        r = client.paginated_fetch("/v1/documents.json", params=dict(p), max_items=40000)
        t_par = time.perf_counter() - t0

        t0 = time.perf_counter()
        client.paginated_fetch(
            "/v1/documents.json", params=dict(p), max_items=40000, workers=1
        )
        t_seq = time.perf_counter() - t0

        mejora = f"{t_seq / t_par:.1f}x" if t_par > 0 else "-"
        print(f"  {dias:>7} d | {r['total_count']:>6} | {t_par:>8.1f}s | "
              f"{t_seq:>10.1f}s | {mejora}")


def snapshot_vs_vivo() -> int:
    try:
        from db import documents_snapshot, official_sale_conditions, session, signed_amount
        from sqlalchemy import and_, func, select
    except ImportError as e:
        print(f"\n== SNAPSHOT ==\n  omitido (sin dependencias de base: {e})")
        return 0

    from datetime import datetime, timezone

    d0 = datetime.strptime(REF_DESDE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    d1 = datetime.strptime(REF_HASTA, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    c = documents_snapshot.c
    amt = signed_amount(c.total_amount, c.document_type_use)
    where = [c.emission_date.between(d0, d1), *official_sale_conditions(documents_snapshot)]

    t0 = time.perf_counter()
    with session() as s:
        row = s.execute(
            select(func.count().label("n"), func.coalesce(func.sum(amt), 0.0).label("t"))
            .where(and_(*where))
        ).one()
    dt = time.perf_counter() - t0

    cond = abs(float(row.t) - REF_VENTA_OFICIAL) < 1
    print("\n== SNAPSHOT vs VIVO ==")
    print(f"  [{_ok(cond)}] snapshot = venta oficial ({float(row.t):,.0f}, {row.n} docs) "
          f"en {dt * 1000:.0f} ms")
    if not cond:
        print("      correr bsale_conciliacion_venta para ver la brecha documento a documento")
    return 0 if cond else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo-vivo", action="store_true", help="no tocar la base de datos")
    ap.add_argument("--sin-velocidad", action="store_true")
    args = ap.parse_args()

    fallas = exactitud()
    if not args.sin_velocidad:
        velocidad()
    if not args.solo_vivo:
        fallas += snapshot_vs_vivo()

    print(f"\n{'TODO OK' if not fallas else f'{fallas} FALLAS'}\n")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
