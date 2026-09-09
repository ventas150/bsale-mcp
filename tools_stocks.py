"""Tools de stock en Bsale."""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from bsale_client import get_client


def _stock_agregado_desde_snapshot(officeid: int | None) -> dict[str, Any]:
    """El mismo agregado, desde stock_actual, en UNA consulta y sin tope.

    Declara frescura (updated_at mas viejo de las filas leidas) y si la
    ultima corrida de stock termino completa, porque un agregado sobre media
    foto es peor que ninguno.
    """
    from sqlalchemy import and_, func, select, text
    from db import session as db_session, stock_actual

    ultima = None
    with db_session() as s:
        try:
            ultima = s.execute(text(
                "select valor from sync_estado where clave = 'stock_ultima_corrida'"
            )).scalar()
        except Exception:  # noqa: BLE001
            ultima = None

        where = [stock_actual.c.office_id == officeid] if officeid else []
        agreg = s.execute(
            select(
                stock_actual.c.office_id,
                func.max(stock_actual.c.office_name).label("office_name"),
                func.coalesce(func.sum(stock_actual.c.quantity), 0.0).label("total_units"),
                func.count().label("sku_count"),
                func.count().filter(stock_actual.c.quantity <= 0).label("out_of_stock"),
                func.count().filter(and_(stock_actual.c.quantity > 0, stock_actual.c.quantity <= 5)).label("low_stock"),
                func.min(stock_actual.c.updated_at).label("mas_viejo"),
            ).where(*where).group_by(stock_actual.c.office_id)
        ).fetchall()

        peores = s.execute(
            select(stock_actual.c.variant_id, stock_actual.c.variant_code,
                   stock_actual.c.office_id, stock_actual.c.office_name,
                   stock_actual.c.quantity)
            .where(*where, stock_actual.c.quantity <= 0)
            .order_by(stock_actual.c.quantity.asc()).limit(50)
        ).fetchall()

    by_office = {
        r.office_id: {
            "office_name": (r.office_name or "").strip(),
            "total_units": float(r.total_units or 0),
            "sku_count": int(r.sku_count),
            "out_of_stock_count": int(r.out_of_stock),
            "low_stock_count": int(r.low_stock),
        }
        for r in agreg
    }
    mas_viejo = min((r.mas_viejo for r in agreg if r.mas_viejo), default=None)
    return {
        "fuente": "stock_actual (Postgres), inventario completo",
        "total_items": sum(o["sku_count"] for o in by_office.values()),
        "truncado": False,
        "actualizado_desde": mas_viejo.isoformat() if mas_viejo else None,
        "ultima_corrida_completa": bool(ultima.get("completo")) if isinstance(ultima, dict) else None,
        "advertencia": (
            None if not isinstance(ultima, dict) or ultima.get("completo")
            else "La ultima corrida de stock NO termino completa: hay filas con el valor de una corrida anterior."
        ),
        "by_office": by_office,
        "low_stock_count": sum(o["low_stock_count"] for o in by_office.values()),
        "out_of_stock_count": sum(o["out_of_stock_count"] for o in by_office.values()),
        "out_of_stock": [
            {"variant_id": r.variant_id, "variant_code": r.variant_code,
             "office_id": r.office_id, "office_name": (r.office_name or "").strip(),
             "quantity": float(r.quantity or 0)}
            for r in peores
        ],
        "nota": "Para UNA variante por sucursal, bsale_stock_variante(code=...) (compacto; stock_live=True para el numero al segundo).",
    }


def _fila_compacta(office_id: int, office_name: str | None, quantity: Any) -> dict[str, Any]:
    return {
        "office_id": int(office_id),
        "office_name": (office_name or "").strip(),
        "quantity": float(quantity or 0),
    }


def _stock_variante_desde_snapshot(variant_id: int | None, code: str | None) -> dict[str, Any]:
    """Las 11 filas de UNA variante desde stock_actual, sin llamar a Bsale.

    Existe porque bsale_listar_stock(variantid=...) devuelve ~7.000 tokens
    por variante: Bsale repite el objeto variant y el objeto office completos
    en cada una de las 11 filas. Para "cuanto hay de esta talla en cada
    tienda" bastan 11 pares (sucursal, cantidad). Es la consulta que hace la
    conciliacion Bsale vs Shopify, y se hace muchas veces.
    """
    from sqlalchemy import select, text
    from db import session as db_session, stock_actual

    with db_session() as s:
        try:
            ultima = s.execute(text(
                "select valor from sync_estado where clave = 'stock_ultima_corrida'"
            )).scalar()
        except Exception:  # noqa: BLE001
            ultima = None
        where = (stock_actual.c.variant_id == variant_id) if variant_id else (stock_actual.c.variant_code == code)
        filas = s.execute(
            select(stock_actual.c.variant_id, stock_actual.c.variant_code,
                   stock_actual.c.office_id, stock_actual.c.office_name,
                   stock_actual.c.quantity, stock_actual.c.updated_at)
            .where(where).order_by(stock_actual.c.office_id)
        ).fetchall()

    if not filas:
        return {
            "fuente": "stock_actual (Postgres)",
            "error": f"sin filas en el snapshot para {'variant_id=' + str(variant_id) if variant_id else 'code=' + str(code)}",
            "nota": "Ausente NO es cero: la variante puede ser nueva o Bsale no la reporta. "
                    "Para el dato en vivo, stock_live=True.",
        }
    mas_viejo = min((f.updated_at for f in filas if f.updated_at), default=None)
    return {
        "fuente": "stock_actual (Postgres), 0 llamadas a Bsale",
        "variant_id": int(filas[0].variant_id),
        "variant_code": filas[0].variant_code,
        "actualizado_desde": mas_viejo.isoformat() if mas_viejo else None,
        "ultima_corrida_completa": bool(ultima.get("completo")) if isinstance(ultima, dict) else None,
        "total_unidades": float(sum(float(f.quantity or 0) for f in filas)),
        "sucursales": [_fila_compacta(f.office_id, f.office_name, f.quantity) for f in filas],
    }


def _stock_variante_en_vivo(variant_id: int | None, code: str | None) -> dict[str, Any]:
    """Mismo formato compacto, leyendo Bsale (1-2 requests). Si viene `code`
    primero resuelve el variant_id."""
    client = get_client()
    if not variant_id:
        v = client.get("/v1/variants.json", params={"code": code, "limit": 1})
        items = v.get("items") or []
        if not items:
            return {"fuente": "Bsale en vivo", "error": f"Bsale no tiene variante con code={code}"}
        variant_id = int(items[0]["id"])
    data = client.get("/v1/stocks.json", params={"variantid": variant_id, "limit": 50, "expand": "[office]"})
    items = data.get("items") or []
    filas = [
        _fila_compacta((i.get("office") or {}).get("id", 0), (i.get("office") or {}).get("name"), i.get("quantity"))
        for i in items
    ]
    filas.sort(key=lambda f: f["office_id"])
    return {
        "fuente": "Bsale en vivo",
        "variant_id": variant_id,
        "variant_code": code,
        "actualizado_desde": datetime.now(timezone.utc).isoformat(),
        "truncado": bool(data.get("count", 0) > len(items)),
        "total_unidades": float(sum(f["quantity"] for f in filas)),
        "sucursales": filas,
    }


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de stock."""

    @mcp.tool()
    def bsale_stock_variante(
        code: str | None = None,
        variant_id: int | None = None,
        stock_live: bool = False,
    ) -> dict[str, Any]:
        """Stock de UNA variante en cada sucursal, en formato compacto.

        Devuelve 11 filas {office_id, office_name, quantity} mas la fecha
        del dato. Por default lee stock_actual (Postgres): 0 llamadas a
        Bsale y ~200 tokens, contra ~7.000 de bsale_listar_stock. Es el
        tool para cruzar contra Shopify (get-inventory-levels) sucursal por
        sucursal.

        stock_live=True lee Bsale (1-2 requests) para el numero al segundo:
        usarlo cuando el snapshot (cada 12 h) no alcanza, por ejemplo para
        medir el retraso de Loadingplay despues de una venta.

        Args:
            code: SKU / codigo de barras de la variante (ej. 724841188979).
            variant_id: id de la variante en Bsale. Uno de los dos es obligatorio.
            stock_live: True = Bsale en vivo; False (default) = snapshot.
        """
        if not code and not variant_id:
            return {"error": "hay que pasar code o variant_id"}
        if stock_live or not os.getenv("DATABASE_URL"):
            return _stock_variante_en_vivo(variant_id, code)
        return _stock_variante_desde_snapshot(variant_id, code)

    @mcp.tool()
    def bsale_listar_stock(
        limit: int = 25,
        offset: int = 0,
        variantid: int | None = None,
        officeid: int | None = None,
        quantity: float | None = None,
    ) -> dict[str, Any]:
        """Lista stock de variantes en Bsale."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "variantid": variantid,
            "officeid": officeid,
            "quantity": quantity,
            "expand": "[variant,office]",
        }
        return client.get("/v1/stocks.json", params=params)

    @mcp.tool()
    def bsale_stock_agregado(
        officeid: int | None = None,
        max_items: int = 40000,
        stock_live: bool = False,
    ) -> dict[str, Any]:
        """Stock agregado por sucursal: unidades, SKUs, quebrados y con poco stock.

        Por default lee stock_actual (Postgres): el inventario COMPLETO
        (240.427 filas) en una consulta, 0 llamadas a la API. Antes leia Bsale
        en vivo con max_items=40000, o sea el 16,6% del stock, y presentaba
        `out_of_stock_count` como un conteo; y la advertencia decia "subir
        max_items", que son 4.800 paginas (~20 min) dentro del proceso que
        sirve /health, contra un corte de 180 s del cliente.

        stock_live=True vuelve al camino en vivo, con su tope. El resultado
        declara siempre fuente, cobertura y frescura.
        """
        if not stock_live and os.getenv("DATABASE_URL"):
            return _stock_agregado_desde_snapshot(officeid)

        client = get_client()
        params = {
            "limit": 50,
            "officeid": officeid,
            "expand": "[variant,office]",
        }
        fetch = client.paginated_fetch("/v1/stocks.json", params=params, max_items=max_items)
        items = fetch["items"]

        by_office: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"office_name": "", "total_units": 0, "sku_count": 0}
        )
        low_stock: list[dict[str, Any]] = []
        out_of_stock: list[dict[str, Any]] = []

        for item in items:
            quantity = float(item.get("quantity", 0) or 0)
            office = item.get("office") or {}
            office_id = office.get("id", 0)
            variant = item.get("variant") or {}

            by_office[office_id]["office_name"] = office.get("name", "Sin nombre")
            by_office[office_id]["total_units"] += quantity
            by_office[office_id]["sku_count"] += 1

            entry = {
                "stock_id": item.get("id"),
                "variant_id": variant.get("id"),
                "variant_code": variant.get("code"),
                "variant_description": variant.get("description"),
                "office_id": office_id,
                "office_name": office.get("name"),
                "quantity": quantity,
            }
            if quantity <= 0:
                # Bsale permite cantidades negativas (sobreventa, ajustes
                # pendientes). Con `== 0` un SKU en -3 caia en "poco stock" en
                # vez de "quebrado", que es al reves de lo que hay que priorizar.
                out_of_stock.append(entry)
            elif quantity <= 5:
                low_stock.append(entry)

        return {
            "fuente": "Bsale en vivo (topado)",
            "total_items": len(items),
            "filas_en_bsale": fetch["total_count"],
            "truncado": fetch["truncated"],
            "advertencia": (
                "RESULTADO PARCIAL: no se leyo todo el stock, los conteos son "
                "menores a la realidad. Llamar sin stock_live (lee el snapshot "
                "completo) o usar bsale_digest('stock_resumen'). Subir max_items "
                "NO sirve: son ~4.800 paginas contra un corte de 180 s."
                if fetch["truncated"] else None
            ),
            "by_office": dict(by_office),
            "low_stock_count": len(low_stock),
            "out_of_stock_count": len(out_of_stock),
            "low_stock": low_stock[:50],
            "out_of_stock": out_of_stock[:50],
        }

    @mcp.tool()
    def bsale_listar_sucursales(
        limit: int = 25,
        offset: int = 0,
        state: int | None = None,
    ) -> dict[str, Any]:
        """Lista sucursales (oficinas) de Bsale."""
        client = get_client()
        params = {
            "limit": min(limit, 50),
            "offset": offset,
            "state": state,
        }
        return client.get("/v1/offices.json", params=params)
