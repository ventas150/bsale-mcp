"""Versiones SQL-powered de las intelligence tools.

Estas leen del snapshot Postgres en vez de paginar Bsale en vivo.
~100x mas rapidas. Requieren DATABASE_URL.

Se registran solo si DATABASE_URL esta configurado (ver server.py).

Sobreescriben las versiones de tools_intelligence.py si esta cargado.
Decision: registrar nombres DISTINTOS con sufijo _fast para que ambas coexistan
y el agente pueda elegir. Si las _fast funcionan, las antiguas se pueden retirar.

Correctitud (P0):
- documents_snapshot tiene PK = document_id (una fila por documento), asi que
  los SUM ya no doble-cuentan.
- Las notas de credito (document_type_use=1) se RESTAN via signed_amount().
- Las guias (use=2) se excluyen.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import and_, desc, exists, func, not_, select, text

from bsale_client import get_client
from digests import TZ_NEGOCIO
from db import (
    document_details_snapshot,
    documents_snapshot,
    session as db_session,
    official_sale_conditions,
    signed_amount,
    stock_actual,
    variants_snapshot,
)


def _stock_de_variantes(
    vids: list[int], office_id: int | None = None
) -> tuple[dict[int, dict[int, dict[str, Any]]], dict[str, Any]]:
    """Stock por variante y sucursal, leido de stock_actual en UNA consulta.

    Reemplaza el patron de pedir /v1/stocks.json?variantid=X dentro de un bucle:
    con top_velocity_check=100 eso eran 100 llamadas a la API por invocacion, y
    la cuota de Bsale es COMPARTIDA con Loadingplay y con la app de documentos.
    Un barrido que aca se ve gratis alla aparece como 429.

    stock_actual tiene el inventario entero (240.427 filas al 09-sep-2026) con
    PK (variant_id, office_id), asi que una sola consulta responde por todas las
    variantes. El costo en API es CERO.

    Devuelve tambien la frescura: quien use este stock tiene que poder decir de
    cuando es. La regla de la casa es que un numero que no declara su cobertura
    es un numero que miente, y aca la cobertura es temporal.
    """
    # Una variante que NO esta en la tabla NO es una variante con stock 0.
    # Con la foto a medias (corrida cancelada, variante creada despues de la
    # ultima foto) el camino viejo la saltaba; devolverla como 0 hace que el
    # briefing invente quiebres y la proyeccion invente compras. Por eso el
    # dict solo trae las que tienen fila, y las ausentes van declaradas en
    # stock_meta["variantes_sin_dato"]: el llamador las salta.
    meta: dict[str, Any] = {
        "fuente": "stock_actual (Postgres)",
        "variantes_pedidas": len(vids),
        "variantes_con_fila": 0,
        "variantes_sin_dato": [],
        "actualizado_desde": None,
        "nota": (
            "Stock leido del snapshot, no de Bsale en vivo: 0 llamadas a la API "
            "en vez de una por variante. La cuota de Bsale es compartida con "
            "Loadingplay y la app de documentos. Las variantes sin fila en el "
            "snapshot se SALTAN, no se cuentan como 0. Para el numero al "
            "segundo, pasar stock_live=True."
        ),
    }
    if not vids:
        return {}, meta

    stmt = select(
        stock_actual.c.variant_id,
        stock_actual.c.office_id,
        stock_actual.c.quantity,
        stock_actual.c.office_name,
        stock_actual.c.updated_at,
    ).where(stock_actual.c.variant_id.in_(vids))
    if office_id:
        stmt = stmt.where(stock_actual.c.office_id == office_id)

    por_variante: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    mas_viejo = None
    with db_session() as s:
        for row in s.execute(stmt):
            por_variante[row.variant_id][row.office_id] = {
                "office_name": row.office_name,
                "stock": float(row.quantity or 0),
            }
            if row.updated_at is not None and (mas_viejo is None or row.updated_at < mas_viejo):
                mas_viejo = row.updated_at

    meta["variantes_con_fila"] = len(por_variante)
    meta["variantes_sin_dato"] = [v for v in vids if v not in por_variante]
    meta["actualizado_desde"] = mas_viejo.isoformat() if mas_viejo else None
    return dict(por_variante), meta


def _meta_stock_live(vids: list[int]) -> dict[str, Any]:
    """stock_meta con la MISMA forma que el camino snapshot, para que un
    consumidor no reviente segun el camino. La frescura es 'ahora'."""
    return {
        "fuente": "Bsale en vivo",
        "variantes_pedidas": len(vids),
        "variantes_con_fila": None,
        "variantes_sin_dato": [],
        "actualizado_desde": datetime.now(timezone.utc).isoformat(),
        "llamadas_api": len(vids),
        "nota": "Una llamada a la API por variante, contra la cuota compartida.",
    }


def _service_variant_ids_subquery():
    """Subquery: variant_ids que son servicios (unlimitedStock=1) -> EXCLUIR de quiebres.

    Bsale marca servicios con unlimitedStock=1 (ej. bordado, servicios intangibles).
    Esos no deben aparecer en quiebres/proyeccion porque no se quiebran.
    """
    return (
        select(variants_snapshot.c.variant_id)
        .where(text("(raw ->> 'unlimitedStock') = '1'"))
        .scalar_subquery()
    )


def _coverage_category(days: float) -> str:
    if days < 7:
        return "critico"
    if days < 14:
        return "bajo"
    if days < 30:
        return "ok"
    if days < 60:
        return "alto"
    return "sobrestockeo"


def _detalle_de_venta_oficial():
    """El detalle hereda las reglas de venta oficial de su documento cabecera.

    `document_details_snapshot` no guarda `document_type_id` ni `state`, asi que
    el filtro `document_type_use != 2` que habia aca era letra muerta: las guias
    de despacho nunca entran al snapshot (snapshot.py ya las descarta), pero SI
    entraban las notas de venta, los pedidos web, las cotizaciones y los
    documentos anulados. Como un pedido web genera PRIMERO un "PEDIDO WEB" y
    DESPUES la boleta, y ambos tienen lineas, **cada unidad vendida por el canal
    web se contaba dos veces**: velocity inflada, quiebres anticipados y
    proyeccion de compras sobre-pedida. Verificado el 07-sep-2026 contra el
    export de Bsale (el GORRO 2506 daba 291 unidades contra 286 reales, y los
    primeros lugares del ranking eran servicios y glosas).

    Un EXISTS correlacionado por clave primaria contra las ~152k filas de
    cabecera es despreciable frente al costo de agregar las lineas.
    """
    return exists().where(
        and_(
            documents_snapshot.c.document_id == document_details_snapshot.c.document_id,
            *official_sale_conditions(documents_snapshot),
        )
    )


def cobertura_de_detalle(desde, hasta, office_id=None) -> dict[str, Any]:
    """Que porcentaje de los documentos del periodo tiene detalle de linea.

    document_details_snapshot se llena documento por documento y NO cubre todo
    el historico: 2025 completo tiene cero lineas. Sin esto, un tool que agrega
    lineas devuelve lista VACIA para un periodo sin detalle, que se lee como
    "no se vendio nada" en vez de "no tengo el dato". Verificado el
    07-sep-2026: top_productos_fast devolvia [] para marzo-2025, un mes de
    $521 millones.
    """
    d = documents_snapshot.c
    det = document_details_snapshot.c
    cond_doc = [d.emission_date.between(desde, hasta), *official_sale_conditions(documents_snapshot)]
    if office_id:
        cond_doc.append(d.office_id == office_id)

    # Los dos lados tienen que contar el MISMO universo. El numerador contaba
    # los document_id distintos de document_details_snapshot filtrando solo por
    # fecha y sucursal, sin la regla de venta oficial, mientras el denominador
    # si la aplicaba. Como los pedidos web, las notas de venta y los anulados
    # tambien tienen lineas, el numerador incluia documentos que el
    # denominador excluye: el 08-sep-2026 los ultimos 30 dias daban 5.208 de
    # 5.128, o sea 101,6% de cobertura. Un porcentaje sobre 100 es la senal de
    # que se estan comparando dos poblaciones distintas — y hacia parecer
    # completo un periodo al que le faltaba detalle.
    tiene_detalle = exists().where(
        and_(
            det.document_id == d.document_id,
            det.emission_date.between(desde, hasta),
        )
    )
    with db_session() as s:
        total = s.execute(
            select(func.count()).select_from(documents_snapshot).where(and_(*cond_doc))
        ).scalar() or 0
        con_det = s.execute(
            select(func.count())
            .select_from(documents_snapshot)
            .where(and_(*cond_doc, tiene_detalle))
        ).scalar() or 0
    pct = round(100 * con_det / total, 1) if total else 0.0
    out = {
        "documentos_del_periodo": total,
        "documentos_con_detalle": con_det,
        "pct": pct,
    }
    if pct < 99:
        out["advertencia"] = (
            f"Solo el {pct}% de los documentos del periodo tiene detalle de "
            "linea cargado. Las UNIDADES estan subestimadas y NO sirven para "
            "comparar con otro periodo. Los PESOS a nivel de documento si son "
            "completos. Para corregir: bsale_snapshot_details_batch."
        )
    return out


def cobertura_ultimos_dias(dias: int, office_id: int | None = None) -> dict:
    """Cobertura de detalle de los ultimos N dias, para los tools de velocity.

    quiebres, proyeccion de compras, allocation y sobrestockeos salen TODOS de
    la velocity, y la velocity sale del detalle de linea. Si al periodo le
    falta detalle, la velocity queda subestimada y estos tools devuelven menos
    riesgo del que hay, callados. Es el mismo modo de falla que tenia
    top_productos_fast devolviendo [] para marzo-2025, pero peor: una lista
    vacia se nota, un riesgo subestimado no.
    """
    hasta = datetime.now(timezone.utc)
    desde = hasta - timedelta(days=dias)
    return cobertura_de_detalle(desde, hasta, office_id)


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools SQL-powered."""

    register_venta_por_sucursal(mcp)

    # ============================
    # 1. QUIEBRES PROYECTADOS (FAST)
    # ============================

    @mcp.tool()
    def bsale_quiebres_proyectados_fast(
        days_horizon: int = 14,
        lookback_days: int = 30,
        office_id: int | None = None,
        min_velocity: float = 0.5,
        top_velocity_check: int = 100,
        stock_live: bool = False,
    ) -> dict[str, Any]:
        """Quiebres proyectados. Velocity SQL + stock del snapshot.

        Pasos:
        1. SQL: top N variantes por velocity en lookback period (de snapshot completo)
        2. SQL: stock de esas variantes, en UNA consulta a stock_actual
        3. Computa dias_hasta_quiebre y filtra por horizon

        Antes el paso 2 era una llamada a la API POR VARIANTE: con el default de
        100, cien llamadas por invocacion. La cuota de Bsale es compartida con
        Loadingplay y con la app de documentos, asi que ese barrido aparecia como
        429 en las otras integraciones. Ahora son 0 llamadas.

        Args:
            days_horizon: Horizonte de prediccion (default 14d).
            lookback_days: Ventana de velocity (default 30d).
            office_id: Filtra por sucursal. None = totales.
            min_velocity: Velocity minima (units/d) para considerar.
            top_velocity_check: Cuantas variantes top-velocity revisar.
            stock_live: True pide el stock a Bsale al segundo, una llamada por
                variante. Solo para decisiones que no toleran la frescura del
                snapshot, que el resultado declara en stock_meta.
        """
        now = datetime.now(timezone.utc)
        if lookback_days <= 0:
            return {"aplicado": False,
                    "motivo": "lookback_days tiene que ser mayor que 0 "
                              f"(recibido {lookback_days})."}
        lookback_cutoff = now - timedelta(days=lookback_days)

        # velocity firmada: las notas de credito (use=1) restan unidades
        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )

        # 1. Top variantes por velocity (snapshot SQL, instantaneo)
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(qty).label("total_qty"),
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    _detalle_de_venta_oficial(),
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id)
            if office_id:
                vel_stmt = vel_stmt.where(document_details_snapshot.c.office_id == office_id)

            # Excluir servicios (unlimitedStock=1)
            vel_stmt = vel_stmt.where(
                not_(document_details_snapshot.c.variant_id.in_(_service_variant_ids_subquery()))
            )
            vel_stmt = vel_stmt.having(
                func.sum(qty) >= min_velocity * lookback_days
            ).order_by(desc("total_qty")).limit(top_velocity_check)

            vel_rows = s.execute(vel_stmt).fetchall()

        # 2. Stock. Del snapshot en UNA consulta, o en vivo si lo piden explicito.
        vids = [r.variant_id for r in vel_rows]
        snap: dict[int, dict[int, dict[str, Any]]] = {}
        if stock_live:
            stock_meta = _meta_stock_live(vids)
        else:
            snap, stock_meta = _stock_de_variantes(vids, office_id)

        client = get_client() if stock_live else None
        risks = []
        for r in vel_rows:
            vid = r.variant_id
            if not stock_live and vid not in snap:
                continue  # sin dato NO es cero: va declarado en stock_meta
            if stock_live:
                try:
                    stock_params = {"variantid": vid, "limit": 50, "expand": "[office]"}
                    if office_id:
                        stock_params["officeid"] = office_id
                    stock_data = client.get("/v1/stocks.json", params=stock_params, use_cache=False)
                    stock_items = stock_data.get("items", []) or []
                except Exception:  # noqa: BLE001
                    continue

                stock_by_office = {}
                stock_total = 0.0
                for item in stock_items:
                    office = item.get("office") or {}
                    oid = office.get("id")
                    if oid is None:
                        continue
                    qv = float(item.get("quantity", 0) or 0)
                    stock_by_office[oid] = qv
                    stock_total += qv
            else:
                stock_by_office = {
                    oid: d["stock"] for oid, d in snap.get(vid, {}).items()
                }
                stock_total = sum(stock_by_office.values())

            vtot = float(r.total_qty or 0)
            vpd = vtot / lookback_days
            days = stock_total / vpd if vpd > 0 else 9999

            if days <= days_horizon:
                risks.append({
                    "variant_id": vid,
                    "code": r.code,
                    "description": r.desc,
                    "stock_total": stock_total,
                    "stock_by_office": stock_by_office,
                    "velocity_per_day": round(vpd, 2),
                    "lookback_units": round(vtot, 0),
                    "days_until_stockout": round(days, 1),
                    "category": _coverage_category(days),
                })

        risks.sort(key=lambda x: x["days_until_stockout"])

        return {
            "source": ("hybrid (velocity:snapshot, stock:live)" if stock_live
                       else "snapshot (velocity y stock)"),
            "stock_meta": stock_meta,
            "horizon_days": days_horizon,
            "lookback_days": lookback_days,
            "office_id": office_id,
            "min_velocity": min_velocity,
            "checked_variants": len(vel_rows),
            "cobertura_detalle": cobertura_ultimos_dias(lookback_days, office_id),
            "en_riesgo_en_los_revisados": len(risks),
            "risks": risks,
        }

    # ============================
    # 2. ALLOCATION SUGERIDA (FAST)
    # ============================

    @mcp.tool()
    def bsale_sugerencia_allocation_fast(
        variant_id: int,
        lookback_days: int = 60,
    ) -> dict[str, Any]:
        """Sugerencia de allocation. Hibrido: stock LIVE de Bsale + velocity desde snapshot.

        Es ~10x mas rapido que la version full-live (1 API call a Bsale en vez de paginar
        miles de docs). Stock siempre actualizado, velocity precalculada.
        """
        now = datetime.now(timezone.utc)
        if lookback_days <= 0:
            return {"aplicado": False,
                    "motivo": "lookback_days tiene que ser mayor que 0 "
                              f"(recibido {lookback_days})."}
        lookback_cutoff = now - timedelta(days=lookback_days)

        # 1. Stock LIVE de Bsale (1 API call - rapido y siempre actualizado)
        client = get_client()
        stock_data = client.get(
            "/v1/stocks.json",
            params={"variantid": variant_id, "limit": 50, "expand": "[variant,office]"},
            use_cache=False,
        )
        stock_items = stock_data.get("items", []) or []

        # 2. Velocity desde snapshot (rapido SQL), firmada (NC restan)
        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.office_id,
                func.sum(qty).label("total_qty"),
            ).where(
                and_(
                    document_details_snapshot.c.variant_id == variant_id,
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    _detalle_de_venta_oficial(),
                )
            ).group_by(document_details_snapshot.c.office_id)

            vel_rows = s.execute(vel_stmt).fetchall()

        velocity_by_office = {r.office_id: float(r.total_qty or 0) for r in vel_rows}

        rows = []
        for item in stock_items:
            office = item.get("office") or {}
            oid = office.get("id")
            if oid is None:
                continue
            stock = float(item.get("quantity", 0) or 0)
            v_total = velocity_by_office.get(oid, 0)
            vpd = v_total / lookback_days if lookback_days > 0 else 0
            cov = stock / vpd if vpd > 0 else (9999 if stock > 0 else 0)
            rows.append({
                "office_id": oid,
                "office_name": office.get("name"),
                "stock": stock,
                "velocity_per_day": round(vpd, 2),
                "coverage_days": round(cov, 1),
                "category": _coverage_category(cov),
            })
        rows.sort(key=lambda x: x["coverage_days"])

        # Sugerencias de traspaso
        suggestions = []
        sobre = [r for r in rows if r["coverage_days"] > 60 and r["stock"] > 5]
        quiebre = [r for r in rows if r["coverage_days"] < 14 and r["velocity_per_day"] > 0]
        for q in quiebre:
            target = q["velocity_per_day"] * 30
            need = max(0, target - q["stock"])
            for so in sobre:
                if need <= 0:
                    break
                excess = max(0, so["stock"] - so["velocity_per_day"] * 30)
                give = min(excess, need)
                if give > 0:
                    suggestions.append({
                        "from_office_id": so["office_id"],
                        "from_office_name": so["office_name"],
                        "to_office_id": q["office_id"],
                        "to_office_name": q["office_name"],
                        "suggested_qty": round(give, 0),
                        "reason": f"{q['office_name']} {q['coverage_days']}d cobertura, {so['office_name']} {so['coverage_days']}d",
                    })
                    so["stock"] -= give
                    need -= give

        return {
            "source": "snapshot",
            "variant_id": variant_id,
            "lookback_days": lookback_days,
            "cobertura_detalle": cobertura_ultimos_dias(lookback_days),
            "current_state": rows,
            "suggestions": suggestions,
        }

    # ============================
    # 3. PROYECCION DE COMPRAS (FAST)
    # ============================

    @mcp.tool()
    def bsale_proyeccion_compras_fast(
        target_coverage_days: int = 45,
        lookback_days: int = 90,
        min_velocity: float = 0.5,
        top_velocity_check: int = 100,
        stock_live: bool = False,
    ) -> dict[str, Any]:
        """Proyeccion de compras. Velocity SQL + stock del snapshot.

        Calcula compra sugerida = max(0, velocity_per_day * target_coverage_days - stock_total).
        Solo recorre las top_velocity_check variantes por venta historica.

        El stock sale de stock_actual en UNA consulta. Antes era una llamada a la
        API por variante -- cien con el default -- contra una cuota compartida con
        Loadingplay y la app de documentos. stock_live=True vuelve al camino en
        vivo; el resultado declara siempre cual se uso y de cuando es el dato.
        """
        now = datetime.now(timezone.utc)
        if lookback_days <= 0:
            return {"aplicado": False,
                    "motivo": "lookback_days tiene que ser mayor que 0 "
                              f"(recibido {lookback_days})."}
        lookback_cutoff = now - timedelta(days=lookback_days)

        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )

        # 1. Top variantes por velocity
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(qty).label("total_qty"),
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    _detalle_de_venta_oficial(),
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).having(
                func.sum(qty) >= min_velocity * lookback_days
            ).order_by(desc("total_qty")).limit(top_velocity_check)
            # Excluir servicios (unlimitedStock=1)
            vel_stmt = vel_stmt.where(
                not_(document_details_snapshot.c.variant_id.in_(_service_variant_ids_subquery()))
            )

            vel_rows = s.execute(vel_stmt).fetchall()

        # 2. Stock: del snapshot en UNA consulta, o en vivo si lo piden explicito.
        vids = [r.variant_id for r in vel_rows]
        snap: dict[int, dict[int, dict[str, Any]]] = {}
        if stock_live:
            stock_meta = _meta_stock_live(vids)
        else:
            snap, stock_meta = _stock_de_variantes(vids)

        client = get_client() if stock_live else None
        recs = []
        for r in vel_rows:
            vid = r.variant_id
            if not stock_live and vid not in snap:
                continue  # sin dato NO es cero: seria una compra inventada
            if stock_live:
                try:
                    stock_data = client.get(
                        "/v1/stocks.json",
                        params={"variantid": vid, "limit": 50},
                        use_cache=False,
                    )
                    stock_items = stock_data.get("items", []) or []
                except Exception:  # noqa: BLE001
                    continue
                stock_total = sum(float(item.get("quantity", 0) or 0) for item in stock_items)
            else:
                stock_total = sum(d["stock"] for d in snap.get(vid, {}).values())

            vtot = float(r.total_qty or 0)
            vpd = vtot / lookback_days
            target = vpd * target_coverage_days
            order_qty = max(0, target - stock_total)

            if order_qty <= 0:
                continue
            recs.append({
                "variant_id": vid,
                "code": r.code,
                "description": r.desc,
                "stock_total": stock_total,
                "velocity_per_day": round(vpd, 2),
                "target_stock": round(target, 0),
                "order_qty_suggested": round(order_qty, 0),
                "current_coverage_days": round(stock_total / vpd, 1) if vpd > 0 else 9999,
            })
        recs.sort(key=lambda x: x["current_coverage_days"])

        return {
            "source": ("hybrid (velocity:snapshot, stock:live)" if stock_live
                       else "snapshot (velocity y stock)"),
            "stock_meta": stock_meta,
            "target_coverage_days": target_coverage_days,
            "lookback_days": lookback_days,
            "min_velocity": min_velocity,
            "checked_variants": len(vel_rows),
            "cobertura_detalle": cobertura_ultimos_dias(lookback_days),
            "recomendaciones_en_los_revisados": len(recs),
            "recommendations": recs,
        }

    # ============================
    # 3b. SOBRESTOCKEOS DETECTADOS (FAST)
    # ============================

    @mcp.tool()
    def bsale_sobrestockeos_detectados(
        min_coverage_days: int = 180,
        lookback_days: int = 30,
        min_velocity: float = 0.05,
        top_check: int = 150,
        stock_live: bool = False,
    ) -> dict[str, Any]:
        """Detecta SKUs sobrestockeados (cobertura > N dias). Inversa de quiebres.

        Identifica capital muerto en bodega. Velocity, precio y stock salen todos
        del snapshot; el stock, en UNA consulta a stock_actual. Antes eran hasta
        150 llamadas a la API por invocacion contra una cuota compartida con
        Loadingplay y la app de documentos.

        Args:
            min_coverage_days: Umbral. Default 180 = 6 meses de stock = sobrestock.
            lookback_days: Ventana de velocity (default 30d).
            min_velocity: Velocity minima (units/d) para incluir. <esto = stock muerto, no sobrestockeo.
            top_check: Cuantas variantes top-velocity revisar. Tope 200 (se
                rechaza por encima, no se recorta en silencio).
            stock_live: True pide el stock a Bsale, una llamada por variante. Para
                un sobrestockeo de 6 meses la frescura del snapshot sobra.

        Returns:
            Lista de SKUs sobrestockeados con valorizado_a_precio_venta_clp
            (a PRECIO DE VENTA con IVA, no a costo: ver nota_valorizacion).
        """
        if top_check > 200:
            return {"aplicado": False,
                    "motivo": f"top_check={top_check}: el tope es 200 (con stock_live=True son 200 llamadas a la API)."}
        now = datetime.now(timezone.utc)
        if lookback_days <= 0:
            return {"aplicado": False,
                    "motivo": "lookback_days tiene que ser mayor que 0 "
                              f"(recibido {lookback_days})."}
        lookback_cutoff = now - timedelta(days=lookback_days)

        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        rev = signed_amount(
            document_details_snapshot.c.total_amount,
            document_details_snapshot.c.document_type_use,
        )

        # 1. Top variantes por velocity con precio promedio (SQL puro)
        with db_session() as s:
            vel_stmt = select(
                document_details_snapshot.c.variant_id,
                func.sum(qty).label("total_qty"),
                func.sum(rev).label("total_revenue"),
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback_cutoff,
                    _detalle_de_venta_oficial(),
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).having(
                func.sum(qty) >= min_velocity * lookback_days
            ).order_by(desc("total_qty")).limit(top_check)

            # Excluir servicios (unlimitedStock=1)
            vel_stmt = vel_stmt.where(
                not_(document_details_snapshot.c.variant_id.in_(_service_variant_ids_subquery()))
            )

            vel_rows = s.execute(vel_stmt).fetchall()

        # 2. Stock: del snapshot en UNA consulta, o en vivo si lo piden explicito.
        vids = [r.variant_id for r in vel_rows]
        snap: dict[int, dict[int, dict[str, Any]]] = {}
        if stock_live:
            stock_meta = _meta_stock_live(vids)
        else:
            snap, stock_meta = _stock_de_variantes(vids)

        client = get_client() if stock_live else None
        sobrestockeos = []
        for r in vel_rows:
            vid = r.variant_id
            stock_by_office: dict[int, dict[str, Any]] = {}
            stock_total = 0.0

            if stock_live:
                try:
                    stock_data = client.get(
                        "/v1/stocks.json",
                        params={"variantid": vid, "limit": 50, "expand": "[office]"},
                        use_cache=False,
                    )
                    stock_items = stock_data.get("items", []) or []
                except Exception:  # noqa: BLE001
                    continue

                for item in stock_items:
                    office = item.get("office") or {}
                    oid = office.get("id")
                    if oid is None:
                        continue
                    qv = float(item.get("quantity", 0) or 0)
                    if qv > 0:
                        stock_by_office[oid] = {
                            "office_name": office.get("name"),
                            "stock": qv,
                        }
                        stock_total += qv
            else:
                for oid, d in snap.get(vid, {}).items():
                    if d["stock"] > 0:
                        stock_by_office[oid] = dict(d)
                        stock_total += d["stock"]

            if stock_total == 0:
                continue

            vtot = float(r.total_qty or 0)
            vpd = vtot / lookback_days
            coverage = stock_total / vpd if vpd > 0 else 9999

            if coverage < min_coverage_days:
                continue

            # OJO: avg_price sale de total_amount de la linea, que es el
            # BRUTO CON IVA que se le cobro al cliente, no el costo. O sea que
            # esto NO es "capital inmovilizado": es a cuanto se venderia ese
            # stock. Para un scrub que se vende a $29.990 y cuesta $9.500, la
            # cifra sobrestima el capital real 3,2 veces. El costo no esta en
            # el snapshot, asi que no se puede calcular aca; lo que si se puede
            # es no llamarle capital a un precio de venta.
            avg_price = float(r.total_revenue or 0) / vtot if vtot > 0 else 0
            capital_tied = stock_total * avg_price

            # Detectar concentracion en una sola sucursal (>70% en una office)
            largest_office_stock = max((o["stock"] for o in stock_by_office.values()), default=0)
            concentration_pct = (largest_office_stock / stock_total * 100) if stock_total else 0
            largest_office = next(
                (o for o in stock_by_office.values() if o["stock"] == largest_office_stock),
                None,
            )

            sobrestockeos.append({
                "variant_id": vid,
                "code": r.code,
                "description": r.desc,
                "stock_total": stock_total,
                "stock_by_office": stock_by_office,
                "velocity_per_day": round(vpd, 2),
                "lookback_units_sold": round(vtot, 0),
                "coverage_days": round(coverage, 0),
                "avg_price": round(avg_price, 0),
                "valorizado_a_precio_venta_clp": round(capital_tied, 0),
                "concentration_pct": round(concentration_pct, 1),
                "concentrated_in": largest_office["office_name"] if largest_office else None,
            })

        # Ordenar por capital_tied descendente (donde hay mas plata muerta)
        sobrestockeos.sort(key=lambda x: x["valorizado_a_precio_venta_clp"], reverse=True)

        total_valorizado = sum(s["valorizado_a_precio_venta_clp"] for s in sobrestockeos)
        return {
            "source": ("hybrid (velocity:snapshot, stock:live)" if stock_live
                       else "snapshot (velocity y stock)"),
            "stock_meta": stock_meta,
            "min_coverage_days": min_coverage_days,
            "lookback_days": lookback_days,
            "checked_variants": len(vel_rows),
            "cobertura_detalle": cobertura_ultimos_dias(lookback_days),
            "total_sobrestockeos": len(sobrestockeos),
            "valorizado_a_precio_venta_en_los_revisados_clp": total_valorizado,
            "nota_valorizacion": (
                "Valorizado a PRECIO DE VENTA CON IVA, no a costo: es a cuanto "
                "se venderia ese stock, no la plata que hay puesta en el. El "
                "costo no esta en el snapshot. Para capital real, multiplicar "
                "por el margen inverso."
            ),
            "nota_cobertura": "Calculado solo sobre las variantes revisadas (ver checked_variants), no sobre todo el catalogo.",
            "sobrestockeos": sobrestockeos,
        }

    # ============================
    # 4. RANKING SUCURSALES (FAST)
    # ============================

    @mcp.tool()
    def bsale_ranking_sucursales_fast(
        days_back: int = 30,
    ) -> dict[str, Any]:
        """Ranking sucursales desde snapshot. Lectura SQL pura, sub-segundo.

        Montos en CLP BRUTO con IVA (totalAmount), con las notas de credito
        RESTADAS. Ojo: eso no es "neto" (neto = sin IVA; para eso,
        bsale_ventas_fast.venta_oficial_sin_iva). Notas de credito (use=1)
        restan; guias (use=2) excluidas; sin doble conteo (PK = document_id).

        Ventana: exactamente `days_back` dias de emision, hoy INCLUIDO. Misma
        ventana que bsale_ranking_sucursales (vivo). El corte va a medianoche
        UTC de hace days_back-1 dias, porque emission_date es medianoche UTC
        exacta: cortar con now() - N dias dejaba fuera el dia mas viejo segun
        la hora a la que se llamara.
        """
        now = datetime.now(timezone.utc)
        cutoff = datetime.combine(now.date() - timedelta(days=days_back - 1),
                                  datetime.min.time()).replace(tzinfo=timezone.utc)

        amt = signed_amount(
            documents_snapshot.c.total_amount,
            documents_snapshot.c.document_type_use,
        )

        with db_session() as s:
            stmt = select(
                documents_snapshot.c.office_id,
                func.max(documents_snapshot.c.office_name).label("office_name"),
                func.sum(amt).label("revenue"),
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("doc_count"),
                # sin el filter, avg promediaba tambien las NC (negativas) y
                # min_ticket era siempre la nota de credito mas grande
                func.avg(amt).filter(documents_snapshot.c.document_type_use != 1).label("avg_ticket"),
                func.max(amt).filter(documents_snapshot.c.document_type_use != 1).label("max_ticket"),
                func.min(amt).filter(documents_snapshot.c.document_type_use != 1).label("min_ticket"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date >= cutoff,
                    *official_sale_conditions(documents_snapshot),
                )
            ).group_by(documents_snapshot.c.office_id)

            rows = s.execute(stmt).fetchall()

        ranking = []
        total_rev = 0.0
        for r in rows:
            rev = float(r.revenue or 0)
            total_rev += rev
            ranking.append({
                "office_id": r.office_id,
                "office_name": r.office_name,
                "revenue": rev,
                "doc_count": r.doc_count,
                "avg_ticket": float(r.avg_ticket or 0),
                "max_ticket": float(r.max_ticket or 0),
                "min_ticket": float(r.min_ticket or 0),
            })

        ranking.sort(key=lambda x: x["revenue"], reverse=True)
        for r in ranking:
            r["share_pct"] = round(r["revenue"] / total_rev * 100, 2) if total_rev else 0

        return {
            "source": "snapshot",
            "period_days": days_back,
            "ventana": {"desde": cutoff.date().isoformat(), "hasta": now.date().isoformat(), "hoy_incluido": True},
            "unidad_montos": "CLP bruto con IVA (totalAmount), NC restadas",
            "total_revenue": total_rev,
            "ranking": ranking,
        }

    # ============================
    # 5. SEGMENTACION CLIENTES RFM (FAST)
    # ============================

    @mcp.tool()
    def bsale_segmentacion_clientes_rfm_fast(
        days_back: int = 365,
    ) -> dict[str, Any]:
        """RFM desde snapshot. Lectura SQL pura, segundos."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days_back)

        amt = signed_amount(
            documents_snapshot.c.total_amount,
            documents_snapshot.c.document_type_use,
        )

        with db_session() as s:
            stmt = select(
                documents_snapshot.c.client_id,
                func.max(documents_snapshot.c.emission_date).label("last_purchase"),
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("frequency"),
                func.sum(amt).label("monetary"),
                func.max(documents_snapshot.c.raw["client"]["firstName"].astext).label("first_name"),
                func.max(documents_snapshot.c.raw["client"]["lastName"].astext).label("last_name"),
                func.max(documents_snapshot.c.raw["client"]["company"].astext).label("company"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date >= cutoff,
                    documents_snapshot.c.client_id.isnot(None),
                    *official_sale_conditions(documents_snapshot),
                )
            ).group_by(documents_snapshot.c.client_id)

            rows = s.execute(stmt).fetchall()

        # Categorize
        segments: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            cid = r.client_id
            last = r.last_purchase
            if not last:
                continue
            days_since = (now - last).days
            freq = r.frequency
            mon = float(r.monetary or 0)

            if days_since <= 30 and freq >= 3:
                seg = "Champions"
            elif days_since <= 60 and freq >= 2:
                seg = "Loyal"
            elif days_since <= 30 and freq == 1:
                seg = "New"
            elif days_since <= 90 and freq >= 1:
                seg = "Promising"
            elif days_since > 180 and freq >= 2:
                seg = "At Risk"
            elif days_since > 365:
                seg = "Lost"
            else:
                seg = "Other"

            name_parts = [r.first_name or "", r.last_name or ""]
            name = " ".join(p for p in name_parts if p).strip() or r.company or f"Cliente {cid}"

            segments[seg].append({
                "client_id": cid,
                "name": name,
                "days_since_last": days_since,
                "frequency": freq,
                "monetary": round(mon, 0),
            })

        summary = {seg: len(c) for seg, c in segments.items()}
        return {
            "source": "snapshot",
            "period_days": days_back,
            "total_clients_analyzed": sum(summary.values()),
            "summary_by_segment": summary,
            "top_champions": sorted(segments.get("Champions", []), key=lambda c: c["monetary"], reverse=True)[:20],
            "at_risk_top": sorted(segments.get("At Risk", []), key=lambda c: c["monetary"], reverse=True)[:20],
        }

    # ============================
    # 6. BRIEFING DIARIO (PURE SQL + HYBRID)
    # ============================

    @mcp.tool()
    def bsale_briefing_diario(
        lookback_days: int = 7,
    ) -> dict[str, Any]:
        """Briefing matinal: ventas de ayer + ranking de sucursales + top
        productos de la semana + quiebres criticos + compras urgentes.

        Todo del snapshot, cero llamadas a la API. Fechas en hora de Chile.
        Montos en CLP BRUTO con IVA (totalAmount), notas de credito restadas.
        No incluye RFM. Tipico runtime: 1-3 s.

        Args:
            lookback_days: Ventana para top_sellers y ranking (default 7d).
        """
        now = datetime.now(timezone.utc)
        # "Ayer" es ayer EN CHILE, no en UTC. Entre las 21:00 y la medianoche
        # de Santiago ya es el dia siguiente en UTC, asi que el briefing
        # mostraba las ventas de HOY (parciales) rotuladas como las de ayer.
        # Mismo criterio que digests.TZ_NEGOCIO.
        # Un solo "hoy" para todo el briefing. Antes yesterday iba en hora de
        # Chile y week_ago/fecha_briefing en UTC: a las 22:00 de Santiago el
        # rotulo decia MANANA y la ventana de 7 dias arrancaba un dia corrido.
        hoy_cl = now.astimezone(ZoneInfo(TZ_NEGOCIO)).date()
        yesterday = hoy_cl - timedelta(days=1)
        week_ago = hoy_cl - timedelta(days=lookback_days)
        lookback30_cutoff = datetime.combine(hoy_cl - timedelta(days=30), datetime.min.time()).replace(tzinfo=timezone.utc)

        amt = signed_amount(
            documents_snapshot.c.total_amount,
            documents_snapshot.c.document_type_use,
        )
        det_qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        det_rev = signed_amount(
            document_details_snapshot.c.total_amount,
            document_details_snapshot.c.document_type_use,
        )

        with db_session() as s:
            # 1. Ventas ayer
            yesterday_dt_start = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=timezone.utc)
            yesterday_dt_end = datetime.combine(yesterday, datetime.max.time()).replace(tzinfo=timezone.utc)

            yest = s.execute(select(
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("docs"),
                func.sum(amt).label("revenue"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date.between(yesterday_dt_start, yesterday_dt_end),
                    *official_sale_conditions(documents_snapshot),
                )
            )).first()

            # 2. Ranking sucursales ultimos N dias
            week_dt = datetime.combine(week_ago, datetime.min.time()).replace(tzinfo=timezone.utc)
            ranking = s.execute(select(
                documents_snapshot.c.office_id,
                func.max(documents_snapshot.c.office_name).label("name"),
                func.sum(amt).label("rev"),
                func.count().filter(documents_snapshot.c.document_type_use != 1).label("docs"),
            ).where(
                and_(
                    documents_snapshot.c.emission_date >= week_dt,
                    *official_sale_conditions(documents_snapshot),
                )
            ).group_by(documents_snapshot.c.office_id).order_by(desc("rev")).limit(5)).fetchall()

            # 3. Top 5 productos vendidos ultima semana
            top_prod = s.execute(select(
                document_details_snapshot.c.variant_id,
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.sum(det_qty).label("units"),
                func.sum(det_rev).label("revenue"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= week_dt,
                    _detalle_de_venta_oficial(),
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).order_by(desc("units")).limit(5)).fetchall()

            # 4. Top 5 variantes por velocity 30d (para chequeo de quiebres live)
            top_vel = s.execute(select(
                document_details_snapshot.c.variant_id,
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.sum(det_qty).label("units"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date >= lookback30_cutoff,
                    _detalle_de_venta_oficial(),
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id).order_by(desc("units")).limit(30)).fetchall()

            # Filtra servicios (unlimitedStock=1) - mismo session
            svc_rows = s.execute(
                select(variants_snapshot.c.variant_id).where(text("(raw ->> 'unlimitedStock') = '1'"))
            ).fetchall()
            service_ids = {r.variant_id for r in svc_rows}

            top_vel = [r for r in top_vel if r.variant_id not in service_ids]

        # 5. Stock del snapshot para top 30 velocity -> quiebres + proyeccion.
        #
        # Esto eran 30 llamadas a la API cada vez que corria el briefing, que es
        # el tool que mas se invoca. La cuota de Bsale es compartida con
        # Loadingplay y con la app de documentos: 30 lecturas gratis aca son 30
        # menos alla. Con stock_actual es UNA consulta a Postgres.
        snap_briefing, stock_meta = _stock_de_variantes([r.variant_id for r in top_vel])
        quiebres_criticos = []
        compras_urgentes = []
        for r in top_vel:
            vid = r.variant_id
            if vid not in snap_briefing:
                continue  # sin dato NO es cero: seria un quiebre inventado
            vtot = float(r.units or 0)
            vpd = vtot / 30
            stock_total = sum(d["stock"] for d in snap_briefing[vid].values())

            days_to_stockout = stock_total / vpd if vpd > 0 else 9999
            if days_to_stockout <= 14:
                quiebres_criticos.append({
                    "variant_id": vid,
                    "code": r.code,
                    "stock": stock_total,
                    "vel_per_day": round(vpd, 2),
                    "days_until_stockout": round(days_to_stockout, 1),
                })
            target_45d = vpd * 45
            if stock_total < target_45d:
                compras_urgentes.append({
                    "variant_id": vid,
                    "code": r.code,
                    "stock": stock_total,
                    "vel_per_day": round(vpd, 2),
                    "order_qty": round(target_45d - stock_total, 0),
                    "current_coverage": round(days_to_stockout, 1),
                })

        quiebres_criticos.sort(key=lambda x: x["days_until_stockout"])
        compras_urgentes.sort(key=lambda x: x["current_coverage"])

        return {
            "fecha_briefing": hoy_cl.isoformat(),
            "zona_horaria": TZ_NEGOCIO,
            "unidad_montos": "CLP bruto con IVA (totalAmount), NC restadas",
            "cobertura_detalle": cobertura_ultimos_dias(lookback_days),
            "ventas_ayer": {
                "fecha": yesterday.isoformat(),
                "documentos": yest.docs if yest else 0,
                "revenue": float(yest.revenue or 0) if yest else 0,
            },
            "ranking_sucursales_semana": [
                {"office_id": r.office_id, "name": r.name, "revenue": float(r.rev or 0), "docs": r.docs}
                for r in ranking
            ],
            "top_productos_semana": [
                {"variant_id": r.variant_id, "code": r.code, "units": float(r.units or 0), "revenue": float(r.revenue or 0)}
                for r in top_prod
            ],
            "quiebres_criticos_proximos_14d": quiebres_criticos[:10],
            "compras_urgentes_top_velocity": compras_urgentes[:10],
            "stock_meta": stock_meta,
        }

    # ============================
    # 7. TOP PRODUCTOS (FAST)
    # ============================

    @mcp.tool()
    def bsale_top_productos_fast(
        date_from: str,
        date_to: str,
        top_n: int = 20,
        office_id: int | None = None,
    ) -> dict[str, Any]:
        """Top productos desde snapshot. SQL puro, sub-segundo."""
        start_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        qty = signed_amount(
            document_details_snapshot.c.quantity,
            document_details_snapshot.c.document_type_use,
        )
        rev = signed_amount(
            document_details_snapshot.c.total_amount,
            document_details_snapshot.c.document_type_use,
        )

        with db_session() as s:
            stmt = select(
                document_details_snapshot.c.variant_id,
                func.max(document_details_snapshot.c.variant_code).label("code"),
                func.max(document_details_snapshot.c.variant_description).label("desc"),
                func.sum(qty).label("units"),
                func.sum(rev).label("revenue"),
            ).where(
                and_(
                    document_details_snapshot.c.emission_date.between(start_dt, end_dt),
                    _detalle_de_venta_oficial(),
                    document_details_snapshot.c.variant_id.isnot(None),
                )
            ).group_by(document_details_snapshot.c.variant_id)

            if office_id:
                stmt = stmt.where(document_details_snapshot.c.office_id == office_id)

            stmt = stmt.order_by(desc("units")).limit(top_n)
            rows = s.execute(stmt).fetchall()

        top = [{
            "variant_id": r.variant_id,
            "code": r.code,
            "description": r.desc,
            "units_sold": float(r.units or 0),
            "revenue": float(r.revenue or 0),
        } for r in rows]

        return {
            "source": "snapshot",
            "period": {"from": date_from, "to": date_to},
            "office_id": office_id,
            "cobertura_detalle": cobertura_de_detalle(start_dt, end_dt, office_id),
            "top_products": top,
        }


def _rango_utc(date_from: str, date_to: str):
    """[desde 00:00, hasta 23:59:59] en UTC a partir de dos fechas ISO."""
    desde = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    hasta = datetime.fromisoformat(date_to).replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    return desde, hasta


def _stmt_cabecera_por_sucursal(desde, hasta):
    """SELECT de pesos y documentos por sucursal. Extraido para poder compilarlo
    en un test sin base: la primera version llamaba signed_amount(tabla) en vez
    de signed_amount(columna, columna) y el TypeError solo aparecio al ejecutar
    el tool contra Postgres."""
    d = documents_snapshot.c
    return (
        select(
            d.office_id,
            func.max(d.office_name).label("sucursal"),
            func.sum(
                signed_amount(d.total_amount, d.document_type_use)
            ).label("venta"),
            func.count().filter(d.document_type_use != 1).label("docs"),
            func.count().filter(d.document_type_use == 1).label("nc"),
        )
        .where(
            and_(
                d.emission_date >= desde,
                d.emission_date <= hasta,
                *official_sale_conditions(documents_snapshot),
            )
        )
        .group_by(d.office_id)
    )


def _stmt_unidades_por_sucursal(desde, hasta):
    """SELECT de unidades por sucursal, desde el detalle de linea."""
    det = document_details_snapshot.c
    return (
        select(
            det.office_id,
            func.sum(
                signed_amount(det.quantity, det.document_type_use)
            ).label("unidades"),
            func.count(func.distinct(det.document_id)).label("docs_con_detalle"),
        )
        .where(
            and_(
                det.emission_date >= desde,
                det.emission_date <= hasta,
                _detalle_de_venta_oficial(),
            )
        )
        .group_by(det.office_id)
    )


def register_venta_por_sucursal(mcp) -> None:  # noqa: ANN001
    """Registra bsale_venta_por_sucursal. Se llama desde register()."""

    @mcp.tool()
    def bsale_venta_por_sucursal(
        date_from: str,
        date_to: str,
    ) -> dict[str, Any]:
        """Venta por sucursal en PESOS y UNIDADES para un periodo. SQL sobre snapshot.

        Venta oficial = Boletas + Facturas + ND - NC. Sin guias, sin notas de
        venta / pedidos web / cotizaciones, sin anulados.

        Dos advertencias que vienen en la respuesta y hay que leer:

        1. `unidades` sale de document_details_snapshot, que se llena documento
           por documento y NO cubre todo el historico. La respuesta trae
           `cobertura_detalle_pct`: si no es ~100, las unidades estan
           SUBESTIMADAS y no sirven para comparar periodos. Con 0% no hay
           detalle en absoluto (es el caso de 2025 completo).
        2. `documentos` cuenta solo documentos de venta; las notas de credito
           van aparte en `notas_de_credito`. Asi el ticket promedio no queda
           diluido, que es lo que pasaba al dividir por el total de documentos.

        Args:
            date_from: YYYY-MM-DD inicio (inclusive).
            date_to: YYYY-MM-DD fin (inclusive).
        """
        desde, hasta = _rango_utc(date_from, date_to)

        with db_session() as s:
            cab = s.execute(_stmt_cabecera_por_sucursal(desde, hasta)).fetchall()
            filas_det = s.execute(_stmt_unidades_por_sucursal(desde, hasta)).fetchall()

        unidades = {r.office_id: float(r.unidades or 0) for r in filas_det}
        con_det = {r.office_id: int(r.docs_con_detalle or 0) for r in filas_det}

        salida = []
        for r in cab:
            docs = int(r.docs or 0)
            nc = int(r.nc or 0)
            venta = float(r.venta or 0)
            cubiertos = con_det.get(r.office_id, 0)
            total_docs = docs + nc
            cob = round(100 * cubiertos / total_docs, 1) if total_docs else 0.0
            u = unidades.get(r.office_id)
            salida.append({
                "office_id": r.office_id,
                "sucursal": (r.sucursal or "").strip(),
                "venta": round(venta),
                "documentos": docs,
                "notas_de_credito": nc,
                "ticket_promedio": round(venta / docs) if docs else None,
                "unidades": round(u) if u is not None and cob > 0 else None,
                "cobertura_detalle_pct": cob,
            })
        salida.sort(key=lambda x: x["venta"], reverse=True)

        tot_docs = sum(x["documentos"] for x in salida)
        tot_nc = sum(x["notas_de_credito"] for x in salida)
        tot_cub = sum(con_det.values())
        cob_global = round(100 * tot_cub / (tot_docs + tot_nc), 1) if (tot_docs + tot_nc) else 0.0
        hay_unidades = cob_global > 0

        out: dict[str, Any] = {
            "source": "snapshot",
            "period": {"from": date_from, "to": date_to},
            "regla": "venta oficial = Boletas + Facturas + ND - NC",
            "por_sucursal": salida,
            "totales": {
                "venta": sum(x["venta"] for x in salida),
                "documentos": tot_docs,
                "notas_de_credito": tot_nc,
                "unidades": (
                    sum(x["unidades"] or 0 for x in salida) if hay_unidades else None
                ),
            },
            "cobertura_detalle": {
                "documentos_del_periodo": tot_docs + tot_nc,
                "documentos_con_detalle": tot_cub,
                "pct": cob_global,
            },
        }
        if cob_global < 99:
            out["cobertura_detalle"]["advertencia"] = (
                f"Solo el {cob_global}% de los documentos del periodo tiene detalle "
                "de linea cargado. Las UNIDADES estan subestimadas y NO sirven para "
                "comparar con otro periodo. Los PESOS si son completos (salen de la "
                "cabecera). Para corregir: bsale_snapshot_details_batch."
            )
        out["nota_pesos"] = (
            "Los pesos salen de documents_snapshot. Si el snapshot tiene huecos en "
            "el periodo tambien lo estaran: confirmar con bsale_conciliacion_venta."
        )
        return out
