"""Cruce Bsale <-> Shopify: boletas emitidas por sucursal contra fulfillment
orders asignadas a cada ubicacion.

Es la prueba diaria de que quien emite las boletas de los pedidos web
(Loadingplay hoy; la app propia manana) esta haciendo lo que dice la regla de
Roberto: UNA boleta por fulfillment order, en la sucursal Bsale de la
ubicacion de esa fulfillment order. El 09-sep-2026 se hizo a mano en tres
llamadas (25 pedidos -> 22 FO en Bodega = 22 boletas en E-Commerce). Aca queda
en una.

Lado Bsale: siempre disponible, lee documentos en vivo (un dia son ~200 docs,
4 paginas). Lado Shopify: solo si estan SHOPIFY_SHOP y SHOPIFY_ADMIN_TOKEN
(read_orders + read_merchant_managed_fulfillment_orders). Sin token, el tool
devuelve el lado Bsale y dice que el cruce no se hizo; no inventa ceros.

Los documentos web se distinguen de los de caja por `salesId`: Loadingplay lo
manda (su id interno de venta), el POS lo deja null.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from bsale_client import get_client, is_official_sale, iso_to_epoch_range

TZ_NEGOCIO = "America/Santiago"

# Ubicacion de Shopify -> sucursal de Bsale. Fuente: skill myscrubs-operacion.
SHOPIFY_LOCATION_A_OFFICE: dict[int, int] = {
    104335016258: 1,   # Myscrubs Bodega -> E-Commerce
    106079191362: 2,   # Providencia (Los Leones)
    106969071938: 3,   # Dos Caracoles
    117047886146: 4,   # Outlet
    106079060290: 5,   # Concepcion
    106079158594: 6,   # Temuco
    106079256898: 7,   # La Serena
    106079093058: 8,   # Vina del Mar
    106079027522: 10,  # Antofagasta
    106079224130: 11,  # Vitacura
}

_QUERY_FO = """
query($q: String!, $after: String) {
  orders(first: 50, query: $q, after: $after) {
    nodes { id name createdAt
      fulfillmentOrders(first: 10) { nodes { id assignedLocation { location { id name } } } } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _rango_dia_chile(fecha: str) -> tuple[str, str]:
    """El dia calendario de Chile, en ISO UTC, para el query de Shopify."""
    d = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=ZoneInfo(TZ_NEGOCIO))
    ini = d.astimezone(timezone.utc)
    fin = (d + timedelta(days=1)).astimezone(timezone.utc)
    return ini.strftime("%Y-%m-%dT%H:%M:%SZ"), fin.strftime("%Y-%m-%dT%H:%M:%SZ")


def _gid_a_int(gid: str | None) -> int | None:
    if not gid:
        return None
    try:
        return int(str(gid).rsplit("/", 1)[-1])
    except ValueError:
        return None


def fulfillment_orders_shopify(fecha: str, *, fetch=None) -> dict[str, Any]:
    """Pedidos pagados del dia (hora de Chile) con sus fulfillment orders.

    `fetch(variables) -> dict` se inyecta en tests; por default pega a la Admin
    API con httpx. Devuelve pedidos, FO por office_id de Bsale y las FO cuya
    ubicacion no esta mapeada (que se declaran, no se pierden).
    """
    shop = os.getenv("SHOPIFY_SHOP")
    token = os.getenv("SHOPIFY_ADMIN_TOKEN")
    if fetch is None:
        if not shop or not token:
            return {"disponible": False, "motivo": "faltan SHOPIFY_SHOP / SHOPIFY_ADMIN_TOKEN en el entorno"}
        version = os.getenv("SHOPIFY_API_VERSION", "2025-07")
        url = f"https://{shop}/admin/api/{version}/graphql.json"

        def fetch(variables):  # noqa: ANN001
            r = httpx.post(url, json={"query": _QUERY_FO, "variables": variables},
                           headers={"X-Shopify-Access-Token": token}, timeout=30)
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(f"Shopify GraphQL: {body['errors']}")
            return body["data"]["orders"]

    ini, fin = _rango_dia_chile(fecha)
    q = f"created_at:>='{ini}' AND created_at:<'{fin}' AND financial_status:paid"
    pedidos: list[dict[str, Any]] = []
    after = None
    paginas = 0
    while True:
        page = fetch({"q": q, "after": after})
        pedidos.extend(page.get("nodes") or [])
        paginas += 1
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage") or paginas >= 20:
            break
        after = info.get("endCursor")

    por_office: dict[int, int] = defaultdict(int)
    sin_mapear: list[dict[str, Any]] = []
    total_fo = 0
    for p in pedidos:
        for fo in ((p.get("fulfillmentOrders") or {}).get("nodes") or []):
            total_fo += 1
            loc = ((fo.get("assignedLocation") or {}).get("location") or {})
            loc_id = _gid_a_int(loc.get("id"))
            office = SHOPIFY_LOCATION_A_OFFICE.get(loc_id or -1)
            if office is None:
                sin_mapear.append({"pedido": p.get("name"), "location_id": loc_id, "location": loc.get("name")})
            else:
                por_office[office] += 1
    return {
        "disponible": True,
        "pedidos_pagados": len(pedidos),
        "fulfillment_orders": total_fo,
        "truncado": paginas >= 20,
        "fo_por_office": dict(por_office),
        "fo_sin_mapear": sin_mapear,
    }


def boletas_bsale_dia(fecha: str, office_id: int | None = None, max_documents: int = 2000) -> dict[str, Any]:
    """Documentos de venta oficial del dia por sucursal, separando los que
    traen salesId (emitidos por una integracion) de los de caja."""
    client = get_client()
    params = {
        "limit": 50, "state": 0, "officeid": office_id,
        "emissiondaterange": iso_to_epoch_range(fecha, fecha),
        "expand": "[document_type,office]",
    }
    fetch = client.paginated_fetch("/v1/documents.json", params=params, max_items=max_documents)
    docs = [d for d in fetch["items"] if is_official_sale(d)]
    por_office: dict[int, dict[str, Any]] = {}
    for d in docs:
        off = d.get("office") or {}
        oid = int(off.get("id") or 0)
        tipo = (d.get("document_type") or {})
        cubo = por_office.setdefault(oid, {
            "office_name": (off.get("name") or "").strip(), "con_salesId": 0, "sin_salesId": 0,
            "notas_de_credito": 0, "documentos": [],
        })
        if int(tipo.get("use") or 0) == 1:
            cubo["notas_de_credito"] += 1
            continue
        if d.get("salesId"):
            cubo["con_salesId"] += 1
        else:
            cubo["sin_salesId"] += 1
        cubo["documentos"].append({
            "id": d.get("id"), "numero": d.get("number"), "tipo": tipo.get("name"),
            "salesId": d.get("salesId"), "total": d.get("totalAmount"),
            "generado": datetime.fromtimestamp(int(d.get("generationDate") or 0), tz=timezone.utc)
            .astimezone(ZoneInfo(TZ_NEGOCIO)).strftime("%H:%M:%S") if d.get("generationDate") else None,
        })
    return {
        "documentos_en_bsale": fetch["total_count"],
        "venta_oficial": len(docs),
        "truncado": fetch["truncated"],
        "por_office": por_office,
    }


def register(mcp) -> None:  # noqa: ANN001
    """Registra el cruce."""

    @mcp.tool()
    def bsale_boletas_vs_shopify(
        fecha: str | None = None,
        office_id: int | None = None,
        incluir_documentos: bool = False,
    ) -> dict[str, Any]:
        """Cruza las boletas del dia por sucursal contra las fulfillment orders
        de Shopify asignadas a cada ubicacion.

        Regla que verifica: una boleta por fulfillment order, emitida en la
        sucursal Bsale de la ubicacion de esa FO (Bodega -> E-Commerce). Para
        cada sucursal devuelve boletas CON salesId (las que emite la
        integracion), boletas SIN salesId (caja), FO asignadas y la diferencia.

        `fecha` YYYY-MM-DD en dia de Chile; default hoy. Lee Bsale en vivo
        (~4 paginas por dia). El lado Shopify requiere SHOPIFY_SHOP y
        SHOPIFY_ADMIN_TOKEN en Render; si faltan, lo dice y entrega solo el
        lado Bsale. Una diferencia de 1-2 en el dia en curso suele ser un
        pedido pagado hace minutos que la integracion aun no factura: mirar
        `generado` con incluir_documentos=True antes de acusar.
        """
        if not fecha:
            fecha = datetime.now(ZoneInfo(TZ_NEGOCIO)).date().isoformat()
        bsale = boletas_bsale_dia(fecha, office_id)
        shopify = fulfillment_orders_shopify(fecha)

        filas = []
        offices = set(bsale["por_office"]) | set(shopify.get("fo_por_office") or {})
        for oid in sorted(offices):
            b = bsale["por_office"].get(oid) or {"office_name": "", "con_salesId": 0, "sin_salesId": 0, "notas_de_credito": 0, "documentos": []}
            fo = (shopify.get("fo_por_office") or {}).get(oid)
            fila = {
                "office_id": oid, "office_name": b["office_name"],
                "boletas_con_salesId": b["con_salesId"], "boletas_sin_salesId": b["sin_salesId"],
                "notas_de_credito": b["notas_de_credito"],
                "fo_shopify": fo,
                "diferencia": (b["con_salesId"] - fo) if fo is not None and shopify.get("disponible") else None,
            }
            if incluir_documentos:
                fila["documentos"] = b["documentos"]
            filas.append(fila)

        cuadra = None
        if shopify.get("disponible"):
            cuadra = all((f["diferencia"] or 0) == 0 for f in filas if f["fo_shopify"] is not None or f["boletas_con_salesId"])
        return {
            "fecha": fecha,
            "regla": "una boleta por fulfillment order, en la sucursal Bsale de la ubicacion de esa FO",
            "cruce_hecho": bool(shopify.get("disponible")),
            "motivo_sin_cruce": None if shopify.get("disponible") else shopify.get("motivo"),
            "cuadra": cuadra,
            "shopify": {k: v for k, v in shopify.items() if k in ("pedidos_pagados", "fulfillment_orders", "truncado", "fo_sin_mapear")},
            "bsale": {"documentos_en_bsale": bsale["documentos_en_bsale"], "venta_oficial": bsale["venta_oficial"], "truncado": bsale["truncado"]},
            "por_sucursal": filas,
            "nota": "Las boletas SIN salesId son de caja (POS), no entran en la comparacion. "
                    "Los pedidos por transferencia y los de exportacion se facturan a mano y aparecen despues.",
        }
