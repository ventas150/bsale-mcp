"""Planner MyScrubs: costo real y resumen diario por sucursal hacia Supabase.

Corre dentro del cron (sync_incremental.run, paso "planner"), DESPUES del snapshot.
Opt-in: sin SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY no hace nada.

1. refrescar_costos(): costo promedio de Bsale (GET /v1/variants/{id}/costs.json) de las
   variantes vendidas desde dic-2024, de la mas vendida a la menos, N por corrida
   (PLANNER_COSTOS_POR_CORRIDA, default 250) y se refresca cada 30 dias. Es la UNICA parte
   que lee la API de Bsale; la cuota es compartida con Loadingplay, por eso va de a poco.
2. resumen_diario(): agrega el snapshot propio (cero lecturas a Bsale) por dia y sucursal:
   venta oficial (NC restan; sin guias, sin notas de venta, solo state=0), unidades y
   costo = unidades x costo promedio. La primera vez manda todo desde dic-2024 (backfill
   2025 completo); despues, los ultimos 40 dias en cada corrida.

OJO: el costo es el costo PROMEDIO ACTUAL de cada variante aplicado a ventas pasadas: es
una aproximacion del costo historico, buena para contribucion, no para contabilidad.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

from db import session as db_session

logger = logging.getLogger(__name__)
INICIO = "2024-12-01"


def activo() -> bool:
    return bool(os.getenv("SUPABASE_URL", "").strip() and os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())


def _post(tabla: str, filas: list[dict[str, Any]], on_conflict: str) -> int:
    if not filas:
        return 0
    url = os.environ["SUPABASE_URL"].rstrip("/") + f"/rest/v1/{tabla}?on_conflict={on_conflict}"
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Content-Profile": "myscrubs", "Prefer": "resolution=merge-duplicates,return=minimal"}
    n = 0
    with httpx.Client(timeout=60) as c:
        for i in range(0, len(filas), 500):
            r = c.post(url, headers=h, json=filas[i:i + 500])
            if r.status_code >= 300:
                raise RuntimeError(f"Supabase {tabla} {r.status_code}: {r.text[:300]}")
            n += len(filas[i:i + 500])
    return n


def _asegurar_tabla() -> None:
    with db_session() as s:
        s.execute(text(
            "create table if not exists variant_costs ("
            " variant_id integer primary key, code varchar(100), average_cost double precision,"
            " last_cost double precision, fetched_at timestamptz not null default now())"))


def refrescar_costos(max_n: int | None = None) -> dict[str, Any]:
    from bsale_client import get_client

    max_n = int(max_n or os.getenv("PLANNER_COSTOS_POR_CORRIDA", "250"))
    _asegurar_tabla()
    with db_session() as s:
        cands = s.execute(text("""
            select d.variant_id, max(d.variant_code) code, sum(d.quantity) q
            from document_details_snapshot d
            left join variant_costs vc on vc.variant_id = d.variant_id
            where d.variant_id is not null and d.emission_date >= :ini
              and (vc.variant_id is null or vc.fetched_at < now() - interval '30 days')
            group by d.variant_id order by q desc limit :n"""), {"ini": INICIO, "n": max_n}).fetchall()
    if not cands:
        return {"costos": "al dia", "leidos": 0}
    client = get_client()
    filas, fallas = [], 0
    for vid, code, _ in cands:
        try:
            data = client.get(f"/v1/variants/{int(vid)}/costs.json", use_cache=False) or {}
        except Exception:  # noqa: BLE001
            fallas += 1
            continue
        avg = data.get("averageCost") if isinstance(data, dict) else None
        last = data.get("lastCost") if isinstance(data, dict) else None
        filas.append({"variant_id": int(vid), "code": code,
                      "average_cost": float(avg) if avg not in (None, "") and float(avg) > 0 else None,
                      "last_cost": float(last) if last not in (None, "") and float(last) > 0 else None})
    if filas:
        with db_session() as s:
            s.execute(text("""
                insert into variant_costs (variant_id, code, average_cost, last_cost, fetched_at)
                values (:variant_id, :code, :average_cost, :last_cost, now())
                on conflict (variant_id) do update set code = excluded.code, average_cost = excluded.average_cost,
                  last_cost = excluded.last_cost, fetched_at = now()"""), filas)
    espejadas = _post("bsale_costos", [{**f, "updated_at": datetime.now(timezone.utc).isoformat()} for f in filas], "variant_id")
    with db_session() as s:
        pend = s.execute(text("""
            select count(distinct d.variant_id) from document_details_snapshot d
            left join variant_costs vc on vc.variant_id = d.variant_id
            where d.variant_id is not null and d.emission_date >= :ini and vc.variant_id is null"""), {"ini": INICIO}).scalar()
    return {"leidos": len(filas), "fallas": fallas, "espejadas": espejadas, "pendientes_sin_costo": int(pend or 0)}


SQL_RESUMEN = """
with docs as (
  select document_id, (emission_date at time zone 'UTC')::date fecha, office_id, office_name,
         coalesce(document_type_use, 0) u, total_amount, net_amount
  from documents_snapshot
  where emission_date >= :desde and coalesce(document_type_use, 0) <> 2 and coalesce(state, 0) = 0
    and coalesce(document_type_name, '') not ilike '%nota de venta%'
), v as (
  select fecha, office_id, max(office_name) office_name,
         count(*) filter (where u <> 1) documentos, count(*) filter (where u = 1) notas_credito,
         sum(case when u = 1 then -total_amount else total_amount end) venta_bruta,
         sum(case when u = 1 then -net_amount else net_amount end) venta_neta
  from docs group by 1, 2
), l as (
  select d.fecha, d.office_id,
         sum(case when d.u = 1 then -det.quantity else det.quantity end) unidades,
         sum(case when d.u = 1 then -1 else 1 end * det.quantity * vc.average_cost) costo,
         sum(abs(det.quantity)) filter (where vc.average_cost is not null) / nullif(sum(abs(det.quantity)), 0) cobertura
  from docs d join document_details_snapshot det on det.document_id = d.document_id
  left join variant_costs vc on vc.variant_id = det.variant_id
  group by 1, 2
)
select v.fecha, v.office_id, v.office_name, v.documentos, v.notas_credito, v.venta_bruta, v.venta_neta,
       l.unidades, l.costo, l.cobertura
from v left join l using (fecha, office_id)
"""


def resumen_diario() -> dict[str, Any]:
    from datetime import timedelta
    from sync_incremental import _leer_estado, _registrar_estado  # helpers de sync_estado

    _asegurar_tabla()
    hecho = (_leer_estado("planner_backfill") or {}).get("completo")
    desde = INICIO if not hecho else (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    with db_session() as s:
        s.execute(text("set local statement_timeout = '120s'"))
        rows = s.execute(text(SQL_RESUMEN), {"desde": desde}).mappings().all()
    ahora = datetime.now(timezone.utc).isoformat()
    filas = [{
        "fecha": r["fecha"].isoformat(), "office_id": r["office_id"], "office_name": r["office_name"],
        "documentos": r["documentos"], "notas_credito": r["notas_credito"],
        "venta_bruta": float(r["venta_bruta"] or 0), "venta_neta": float(r["venta_neta"] or 0),
        "unidades": float(r["unidades"]) if r["unidades"] is not None else None,
        "costo": float(r["costo"]) if r["costo"] is not None else None,
        "costo_cobertura": float(r["cobertura"]) if r["cobertura"] is not None else None,
        "updated_at": ahora,
    } for r in rows if r["office_id"] is not None]
    n = _post("bsale_resumen_diario", filas, "fecha,office_id")
    if not hecho:
        _registrar_estado("planner_backfill", {"completo": True, "desde": INICIO, "filas": n, "ts": ahora})
    return {"desde": desde, "filas": n, "backfill": not hecho}


def planner_step() -> dict[str, Any]:
    if not activo():
        return {"omitido": "sin SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY"}
    out: dict[str, Any] = {}
    try:
        out["costos"] = refrescar_costos()
    except Exception as e:  # noqa: BLE001
        logger.warning("planner costos: %s", e)
        out["costos_warning"] = str(e)[:300]
    try:
        out["resumen"] = resumen_diario()
    except Exception as e:  # noqa: BLE001
        logger.warning("planner resumen: %s", e)
        out["resumen_warning"] = str(e)[:300]
    return out
