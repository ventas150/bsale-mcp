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
   2025 completo); despues, los ultimos 40 dias en cada corrida, y una vez al dia todo el
   historial desde dic-2024 otra vez, para que los costos que van llegando de a 250 por
   corrida tambien corrijan los meses viejos (sin eso el costo historico quedaba incompleto).

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
    -- 05-oct-2026: el filtro por nombre no atrapaba "NOTA VENTA" ni pedidos web/cotizaciones e inflaba 2025.
    -- Misma regla que official_sale_conditions: fuera los tipos isSalesNote.
    and (document_type_id is null or not (document_type_id = any(:notas)))
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

    from bsale_client import sales_note_type_ids

    _asegurar_tabla()
    # v2 (05-oct-2026): rehace el backfill completo con el filtro oficial de notas de venta.
    hecho = (_leer_estado("planner_backfill_v2") or {}).get("completo")
    ahora_dt = datetime.now(timezone.utc)
    ult_completo = (_leer_estado("planner_resumen_completo") or {}).get("ts")
    completo = not hecho or not ult_completo or (ahora_dt - datetime.fromisoformat(ult_completo)).total_seconds() >= 20 * 3600
    desde = INICIO if completo else (ahora_dt.date() - timedelta(days=40)).isoformat()
    notas = [int(x) for x in (sales_note_type_ids() or [])] or [-1]
    with db_session() as s:
        s.execute(text(f"set local statement_timeout = '{300 if completo else 120}s'"))
        rows = s.execute(text(SQL_RESUMEN), {"desde": desde, "notas": notas}).mappings().all()
    ahora = ahora_dt.isoformat()
    filas = [{
        "fecha": r["fecha"].isoformat(), "office_id": r["office_id"], "office_name": r["office_name"],
        "documentos": r["documentos"], "notas_credito": r["notas_credito"],
        "venta_bruta": float(r["venta_bruta"] or 0), "venta_neta": float(r["venta_neta"] or 0),
        "unidades": float(r["unidades"]) if r["unidades"] is not None else None,
        "costo": float(r["costo"]) if r["costo"] is not None else None,
        "costo_cobertura": float(r["cobertura"]) if r["cobertura"] is not None else None,
        "updated_at": ahora,
    } for r in rows if r["office_id"] is not None]
    if not hecho:
        # Tabla derivada: en el backfill se reemplaza completa para no dejar días/sucursales que antes
        # solo tenían notas de venta (quedarían inflados si solo se hace upsert).
        url = os.environ["SUPABASE_URL"].rstrip("/") + f"/rest/v1/bsale_resumen_diario?fecha=gte.{INICIO}"
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        with httpx.Client(timeout=60) as c:
            r = c.delete(url, headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Profile": "myscrubs"})
            if r.status_code >= 300:
                raise RuntimeError(f"Supabase delete bsale_resumen_diario {r.status_code}: {r.text[:300]}")
    n = _post("bsale_resumen_diario", filas, "fecha,office_id")
    if not hecho:
        _registrar_estado("planner_backfill_v2", {"completo": True, "desde": INICIO, "filas": n, "ts": ahora})
    if completo:
        _registrar_estado("planner_resumen_completo", {"ts": ahora, "filas": n})
    return {"desde": desde, "filas": n, "backfill": not hecho, "completo": completo}


SQL_CLIENTES = """
with f as (
  select client_id, (emission_date at time zone 'UTC')::date fecha,
         case when coalesce(document_type_use,0) = 1 then -net_amount else net_amount end neto
  from documents_snapshot
  where client_id is not null and emission_date >= now() - interval '365 days' and coalesce(state,0) = 0
    and coalesce(document_type_use,0) in (0,1)
    and (document_type_name ilike '%factura%' or (coalesce(document_type_use,0) = 1 and client_id in (
         select client_id from documents_snapshot where document_type_name ilike '%factura%' and client_id is not null)))
)
select client_id, count(*) filter (where neto > 0) facturas_12m, sum(neto) neto_12m,
       sum(neto) filter (where fecha >= current_date - 90) neto_90d,
       min(fecha) primera_12m, max(fecha) ultima_compra,
       case when count(*) filter (where neto > 0) > 1
            then (max(fecha) - min(fecha))::numeric / (count(*) filter (where neto > 0) - 1) end dias_entre_compras
from f group by 1 having sum(neto) > 0
"""


def clientes_b2b(max_nuevos: int = 80) -> dict[str, Any]:
    """Clientes con factura (B2B) de los últimos 12 meses, con nombre/RUT/crédito desde /v1/clients (cacheado 30 días)."""
    from bsale_client import get_client

    with db_session() as s:
        s.execute(text(
            "create table if not exists client_cache (client_id integer primary key, data jsonb, fetched_at timestamptz not null default now())"))
        s.execute(text("set local statement_timeout = '120s'"))
        rows = s.execute(text(SQL_CLIENTES)).mappings().all()
        cache = {r[0]: r[1] for r in s.execute(text(
            "select client_id, data from client_cache where fetched_at > now() - interval '30 days'")).fetchall()}
    faltan = [r["client_id"] for r in sorted(rows, key=lambda r: -(r["neto_12m"] or 0)) if r["client_id"] not in cache][:max_nuevos]
    if faltan:
        client = get_client()
        nuevos = []
        for cid in faltan:
            try:
                d = client.get(f"/v1/clients/{int(cid)}.json", use_cache=False) or {}
            except Exception:  # noqa: BLE001
                continue
            keep = {k: d.get(k) for k in ("company", "firstName", "lastName", "code", "email", "phone", "hasCredit", "maxCredit",
                                          "companyOrPerson", "municipality", "city", "activity", "state")}
            cache[cid] = keep
            nuevos.append({"client_id": int(cid), "data": keep})
        if nuevos:
            import json as _json
            with db_session() as s:
                s.execute(text("""insert into client_cache (client_id, data, fetched_at) values (:client_id, cast(:data as jsonb), now())
                                  on conflict (client_id) do update set data = excluded.data, fetched_at = now()"""),
                          [{"client_id": n["client_id"], "data": _json.dumps(n["data"])} for n in nuevos])
    ahora = datetime.now(timezone.utc).isoformat()
    filas = []
    for r in rows:
        c = cache.get(r["client_id"]) or {}
        nombre = (c.get("company") or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x) or None)
        filas.append({
            "client_id": int(r["client_id"]), "nombre": nombre, "rut": c.get("code"), "email": c.get("email"),
            "comuna": c.get("municipality"), "giro": c.get("activity"),
            "tiene_credito": bool(c.get("hasCredit")) if c.get("hasCredit") is not None else None,
            "credito_max": float(c["maxCredit"]) if c.get("maxCredit") not in (None, "") else None,
            "facturas_12m": int(r["facturas_12m"] or 0), "neto_12m": float(r["neto_12m"] or 0),
            "neto_90d": float(r["neto_90d"] or 0), "primera_12m": r["primera_12m"].isoformat() if r["primera_12m"] else None,
            "ultima_compra": r["ultima_compra"].isoformat() if r["ultima_compra"] else None,
            "dias_entre_compras": float(r["dias_entre_compras"]) if r["dias_entre_compras"] is not None else None,
            "updated_at": ahora,
        })
    n = _post("bsale_clientes_b2b", filas, "client_id")
    return {"clientes": n, "nombres_nuevos": len(faltan), "sin_nombre": sum(1 for f in filas if not f["nombre"])}


SQL_SKU = """
with docs as (
  select document_id, coalesce(document_type_use,0) u
  from documents_snapshot
  where emission_date >= :desde and coalesce(document_type_use,0) <> 2 and coalesce(state,0) = 0
    and (document_type_id is null or not (document_type_id = any(:notas)))
)
select {periodo} periodo, det.variant_code sku, det.office_id,
       sum(case when d.u = 1 then -det.quantity else det.quantity end) unidades,
       sum(case when d.u = 1 then -det.net_amount else det.net_amount end) neto
from docs d join document_details_snapshot det on det.document_id = d.document_id
where det.variant_code is not null and det.office_id is not null and det.emission_date >= :desde
group by 1, 2, 3
having sum(abs(det.quantity)) > 0
"""


def ventas_sku() -> dict[str, Any]:
    """Venta por SKU y sucursal hacia Supabase: diario (90 días) y mensual (desde dic-2024).
    Una vez al día; el primer run hace el backfill completo."""
    from datetime import timedelta
    from sync_incremental import _leer_estado, _registrar_estado
    from bsale_client import sales_note_type_ids

    est = _leer_estado("ventas_sku") or {}
    ahora_dt = datetime.now(timezone.utc)
    if est.get("ts") and est.get("backfill") and (ahora_dt - datetime.fromisoformat(est["ts"])).total_seconds() < 20 * 3600:
        return {"omitido": "ya corrió hoy", "ultimo": est.get("ts")}
    backfill = not est.get("backfill")
    notas = [int(x) for x in (sales_note_type_ids() or [])] or [-1]
    hoy = ahora_dt.date()
    desde_dia = (hoy - timedelta(days=90 if backfill else 7)).isoformat()
    desde_mes = INICIO if backfill else hoy.replace(day=1).replace(month=hoy.month - 1 if hoy.month > 1 else 12,
                                                                     year=hoy.year if hoy.month > 1 else hoy.year - 1).isoformat()
    ahora = ahora_dt.isoformat()
    with db_session() as s:
        s.execute(text("set local statement_timeout = '300s'"))
        dias = s.execute(text(SQL_SKU.format(periodo="(det.emission_date at time zone 'UTC')::date")),
                         {"desde": desde_dia, "notas": notas}).mappings().all()
        meses = s.execute(text(SQL_SKU.format(periodo="date_trunc('month', det.emission_date at time zone 'UTC')::date")),
                          {"desde": desde_mes, "notas": notas}).mappings().all()
    fd = [{"fecha": r["periodo"].isoformat(), "sku": r["sku"], "office_id": r["office_id"], "unidades": float(r["unidades"] or 0),
           "neto": float(r["neto"] or 0), "updated_at": ahora} for r in dias]
    fm = [{"mes": r["periodo"].isoformat(), "sku": r["sku"], "office_id": r["office_id"], "unidades": float(r["unidades"] or 0),
           "neto": float(r["neto"] or 0), "updated_at": ahora} for r in meses]
    n_d = _post("bsale_venta_sku_diaria", fd, "fecha,sku,office_id")
    n_m = _post("bsale_venta_sku_mensual", fm, "mes,sku,office_id")
    _registrar_estado("ventas_sku", {"ts": ahora, "backfill": True, "dias": n_d, "meses": n_m})
    return {"backfill": backfill, "diario": n_d, "mensual": n_m, "desde_dia": desde_dia, "desde_mes": desde_mes}


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
    try:
        out["clientes_b2b"] = clientes_b2b()
    except Exception as e:  # noqa: BLE001
        logger.warning("planner clientes_b2b: %s", e)
        out["clientes_b2b_warning"] = str(e)[:300]
    try:
        out["ventas_sku"] = ventas_sku()
    except Exception as e:  # noqa: BLE001
        logger.warning("planner ventas_sku: %s", e)
        out["ventas_sku_warning"] = str(e)[:300]
    return out
