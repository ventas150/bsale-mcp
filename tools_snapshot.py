"""Tools que exponen el snapshot Postgres como MCP tools.

Solo se registran si DATABASE_URL esta seteado (ver server.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, func, select

from db import (
    document_details_snapshot,
    documents_snapshot,
    official_sale_conditions,
    official_sale_supported,
    session as db_session,
    signed_amount,
    stock_snapshot,
    variants_snapshot,
)
# OJO: nightly_snapshot, snapshot_stock y snapshot_variants NO se importan
# aca a proposito. Corren solo en el cron nocturno (cron_snapshot.py), que es
# un proceso aparte. Tenerlos importados en el web service invita a volver a
# llamarlos desde un tool, que es exactamente lo que tumbo el servicio el
# 07-sep-2026.
from snapshot import (
    snapshot_details,
    snapshot_documents,
    snapshot_documents_range,
)


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de snapshot."""

    @mcp.tool()
    def bsale_snapshot_run_now(
        target: str = "documents",
        days_back: int = 1,
    ) -> dict[str, Any]:
        """Corre snapshot ahora mismo (manual). WRITE OP (a DB local).

        Solo acepta los targets LIVIANOS. 'all', 'stock' y 'variants' corren en
        el cron nocturno (cron_snapshot.py), que es un proceso aparte.

        Por que: este tool corre DENTRO del web service, el mismo proceso que
        responde /health. snapshot_stock pagina secuencialmente ~6.000 paginas y
        tarda cerca de 2 horas; 'all' hace ademas variants y details. Con eso el
        healthcheck no responde en 5 segundos, Render marca el servicio caido y
        reinicia la instancia a mitad de la carga. Ya paso el 07-sep-2026 con un
        backfill de 46.738 documentos, que es una fraccion de esto — y 'all' era
        el DEFAULT de este tool, o sea que bastaba llamarlo sin argumentos.

        Args:
            target: 'documents' (por days_back) o 'details' (lote acotado).
            days_back: Ventana para 'documents'. Maximo 31; para tramos viejos
                usar bsale_snapshot_backfill_rango.
        """
        PESADOS = {
            "all": "la corrida nocturna completa (stock + variants + details)",
            "stock": "la foto de stock (~6.000 paginas, ~2 horas)",
            "variants": "el catalogo completo de variantes (~2.000 paginas)",
        }
        if target in PESADOS:
            return {
                "aplicado": False,
                "motivo": (
                    f"'{target}' es {PESADOS[target]} y corre DENTRO del web "
                    "service, que es el mismo proceso que responde /health. "
                    "Lanzarlo desde aca deja sin responder el healthcheck y "
                    "Render reinicia la instancia a mitad de la carga."
                ),
                "donde_corre": "cron_snapshot.py, como Render Cron Job aparte",
                "alternativas": {
                    "documents": "bsale_snapshot_run_now(target='documents', days_back<=31)",
                    "rango_viejo": "bsale_snapshot_backfill_rango(date_from, date_to)",
                    "details": "bsale_snapshot_details_batch(...)",
                },
            }
        if target == "documents":
            if days_back > 31:
                return {
                    "aplicado": False,
                    "motivo": (
                        f"days_back={days_back}. El tope es 31 por la misma razon "
                        "que el backfill: una ventana larga satura el proceso que "
                        "sirve el healthcheck."
                    ),
                    "alternativa": "bsale_snapshot_backfill_rango(date_from, date_to)",
                }
            return snapshot_documents(days_back=days_back)
        if target == "details":
            return snapshot_details(batch_size=100, max_docs=500)
        return {
            "aplicado": False,
            "motivo": f"target '{target}' no reconocido.",
            "validos": ["documents", "details"],
        }

    @mcp.tool()
    def bsale_snapshot_backfill_rango(
        date_from: str,
        date_to: str,
        max_documentos: int = 60000,
    ) -> dict[str, Any]:
        """Rellena el snapshot de documentos para un RANGO de fechas. WRITE OP (a DB local).

        Existe porque bsale_snapshot_run_now solo acepta `days_back`, o sea una
        ventana que siempre termina hoy. Para tapar un hueco viejo habia que
        traerse todo desde ese hueco hasta hoy, que se truncaba antes de llegar.
        Asi quedo el snapshot de marzo-2025 con 4.101 documentos menos (-45,9%
        del mes) y marzo-2026 con 37 menos.

        Es idempotente: hace upsert por document_id, no borra nada. Correrlo dos
        veces sobre el mismo rango no duplica ni pierde datos.

        Despues de correrlo, verificar con bsale_conciliacion_venta sobre el
        mismo rango: tiene que dar diferencia 0.

        Maximo 31 dias por llamada: corre dentro del web service y un tramo
        largo deja sin responder el healthcheck de Render (ver comentario en el
        cuerpo). Para reparar varios meses, una llamada por mes.

        Args:
            date_from: YYYY-MM-DD inicio (inclusive).
            date_to: YYYY-MM-DD fin (inclusive).
            max_documentos: Tope de documentos a leer. Si se alcanza, la
                respuesta lo declara en `truncado` y el rango queda INCOMPLETO.
        """
        # Tope de tramo. El 07-sep-2026 lance este tool sobre 4 meses (46.738
        # documentos) y saturo el proceso: el web service es el MISMO que
        # responde /health, Render no obtuvo respuesta en 5 segundos, marco el
        # servicio caido y reinicio la instancia a mitad del backfill. Tramos de
        # ~10.000 documentos (un mes, o media quincena cargada) pasan limpios.
        #
        # El upsert es idempotente, asi que cortar y reintentar no rompe nada;
        # lo que hay que evitar es tumbar el MCP en horario de operacion.
        from datetime import date

        d1 = date.fromisoformat(date_from)
        d2 = date.fromisoformat(date_to)
        dias = (d2 - d1).days + 1
        if dias > 31:
            return {
                "aplicado": False,
                "motivo": (
                    f"Rango de {dias} dias. El tope es 31 porque este backfill corre "
                    "DENTRO del web service, y un tramo largo deja sin responder el "
                    "healthcheck de Render, que reinicia la instancia a mitad de "
                    "camino. Partirlo en tramos mensuales o quincenales."
                ),
                "sugerencia": "un mes por llamada; si el mes es pesado, dos quincenas",
            }
        if dias > 15:
            aviso = (
                "Tramo de mas de 15 dias: si Render reporta healthcheck fallido, "
                "partirlo en quincenas. El upsert es idempotente, se puede reintentar."
            )
        else:
            aviso = None

        paginas = max(1, int(max_documentos) // 50)
        res = snapshot_documents_range(date_from, date_to, max_pages=paginas)
        if aviso:
            res["nota"] = aviso
        res["idempotente"] = "upsert por document_id; correrlo de nuevo no duplica"
        res["siguiente_paso"] = (
            f"bsale_conciliacion_venta(start_date='{date_from}', "
            f"end_date='{date_to}') para confirmar diferencia 0"
        )
        return res

    @mcp.tool()
    def bsale_snapshot_details_batch(
        batch_size: int | None = None,
        max_docs: int = 1000,
        only_recent_days: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        oldest_first: bool = False,
    ) -> dict[str, Any]:
        """Fetch details (line items) para docs sin details aun. WRITE OP.

        1 API call por documento. Medido el 08-sep-2026: ~8 documentos por
        segundo, o sea que 1.000 documentos son unos 2 minutos. Ese es el tope
        razonable por llamada: corre dentro del web service.

        Para un backfill grande, llamar varias veces hasta que
        remaining_to_process llegue a 0. Ese numero ahora es un COUNT real
        sobre la base; antes se calculaba restando el tope y daba 0 siempre.

        Args:
            batch_size: Alias historico de max_docs. Si vienen los dos, manda el menor.
            max_docs: Documentos por llamada (default 1.000, ~2 minutos).
            only_recent_days: Solo documentos de los ultimos N dias.
            date_from / date_to: Ventana explicita 'YYYY-MM-DD' inclusive. Es la
                forma de rellenar un periodo viejo puntual.
            oldest_first: Del mas viejo al mas nuevo. Necesario para un backfill
                historico: con el orden por defecto nunca se llega a lo viejo.
        """
        if max_docs and max_docs > 4000:
            return {
                "aplicado": False,
                "motivo": (
                    f"max_docs={max_docs}. El tope es 4.000 (~8 minutos) porque "
                    "esto corre DENTRO del web service, el mismo proceso que "
                    "responde /health. Para el hueco historico completo no hace "
                    "falta forzar: el cron nocturno ya lo va cerrando solo."
                ),
                "alternativa": "llamar varias veces con max_docs<=4000",
            }
        return snapshot_details(
            batch_size=batch_size,
            max_docs=max_docs,
            only_recent_days=only_recent_days,
            date_from=date_from,
            date_to=date_to,
            oldest_first=oldest_first,
        )

    @mcp.tool()
    def bsale_snapshot_status() -> dict[str, Any]:
        """Devuelve cuando fue el ultimo snapshot exitoso de cada tabla."""
        with db_session() as s:
            doc_max = s.execute(select(func.max(documents_snapshot.c.snapshot_date))).scalar()
            stock_max = s.execute(select(func.max(stock_snapshot.c.snapshot_date))).scalar()
            var_max = s.execute(select(func.max(variants_snapshot.c.snapshot_date))).scalar()
            det_max = s.execute(select(func.max(document_details_snapshot.c.fetched_at))).scalar()

            doc_count = s.execute(select(func.count()).select_from(documents_snapshot)).scalar()
            stock_count = s.execute(select(func.count()).select_from(stock_snapshot)).scalar()
            var_count = s.execute(select(func.count()).select_from(variants_snapshot)).scalar()
            det_count = s.execute(select(func.count()).select_from(document_details_snapshot)).scalar()
            det_docs_count = s.execute(
                select(func.count(func.distinct(document_details_snapshot.c.document_id)))
            ).scalar()

            # Min y max emission_date en documents_snapshot
            doc_min_emit = s.execute(select(func.min(documents_snapshot.c.emission_date))).scalar()
            doc_max_emit = s.execute(select(func.max(documents_snapshot.c.emission_date))).scalar()

        return {
            "documents": {
                "last_snapshot": doc_max.isoformat() if doc_max else None,
                "total_rows": doc_count,
                "emission_date_min": doc_min_emit.isoformat() if doc_min_emit else None,
                "emission_date_max": doc_max_emit.isoformat() if doc_max_emit else None,
            },
            "details": {
                "last_fetched": det_max.isoformat() if det_max else None,
                "total_line_items": det_count,
                "unique_documents": det_docs_count,
            },
            "stock": {
                "last_snapshot": stock_max.isoformat() if stock_max else None,
                "total_rows": stock_count,
            },
            "variants": {
                "last_snapshot": var_max.isoformat() if var_max else None,
                "total_rows": var_count,
            },
        }

    @mcp.tool()
    def bsale_ventas_fast(
        start_date: str | None = None,
        end_date: str | None = None,
        office_id: int | None = None,
        incluir_documentos: bool = False,
        limit: int = 200,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Venta oficial de un periodo leida del SNAPSHOT (Postgres), no de Bsale.

        Devuelve AGREGADOS por defecto. Los totales se calculan siempre sobre
        el periodo completo en SQL, nunca sobre las filas devueltas: `limit`
        solo recorta la lista de documentos cuando se piden explicitamente.

        Venta oficial = Boletas + Facturas + ND - NC. Excluye guias de
        despacho, notas de venta / pedidos web / cotizaciones y anulados.

        Args:
            start_date: YYYY-MM-DD inicio (alias: date_from).
            end_date: YYYY-MM-DD fin (alias: date_to).
            office_id: Filtra por sucursal.
            incluir_documentos: True para adjuntar el detalle documento a
                documento. Por defecto False: pesa mucho y casi nunca se usa.
            limit: Tope de documentos a listar cuando incluir_documentos=True.
        """
        start_date = start_date or date_from
        end_date = end_date or date_to
        if not start_date or not end_date:
            return {
                "error": "Faltan fechas. Usar start_date y end_date en formato YYYY-MM-DD.",
            }

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        d = documents_snapshot.c
        amt = signed_amount(d.total_amount, d.document_type_use)
        net = signed_amount(d.net_amount, d.document_type_use)
        # count(*) contaba tambien las notas de credito, asi que
        # documentos_de_venta salia inflado y cualquier ticket promedio
        # calculado sobre el quedaba ~14% bajo. Se separan, como ya hacia
        # bsale_venta_por_sucursal.
        n_venta = func.count().filter(d.document_type_use != 1)
        n_nc = func.count().filter(d.document_type_use == 1)

        where = [d.emission_date.between(start_dt, end_dt)]
        where += official_sale_conditions(documents_snapshot)
        if office_id:
            where.append(d.office_id == office_id)
        cond = and_(*where)

        with db_session() as s:
            total_row = s.execute(
                select(
                    n_venta.label("docs"),
                    n_nc.label("nc"),
                    func.coalesce(func.sum(amt), 0.0).label("total"),
                    func.coalesce(func.sum(net), 0.0).label("neto"),
                ).where(cond)
            ).one()

            by_office = [
                {
                    "office_id": r.office_id,
                    "office_name": (r.office_name or "").strip(),
                    "count": r.docs,
                    "notas_de_credito": r.nc,
                    "amount": float(r.total or 0),
                }
                for r in s.execute(
                    select(
                        d.office_id,
                        d.office_name,
                        n_venta.label("docs"),
                        n_nc.label("nc"),
                        func.sum(amt).label("total"),
                    )
                    .where(cond)
                    .group_by(d.office_id, d.office_name)
                    .order_by(desc(func.sum(amt)))
                ).all()
            ]

            by_doctype = [
                {
                    "document_type_id": r.document_type_id,
                    "type_name": (r.document_type_name or "").strip(),
                    "count": r.docs,
                    "amount": float(r.total or 0),
                }
                for r in s.execute(
                    select(
                        d.document_type_id,
                        d.document_type_name,
                        func.count().label("docs"),
                        func.sum(amt).label("total"),
                    )
                    .where(cond)
                    .group_by(d.document_type_id, d.document_type_name)
                ).all()
            ]

            day = func.date_trunc("day", d.emission_date)
            by_day = [
                {
                    "day": r.day.date().isoformat() if r.day else None,
                    "count": r.docs,
                    "amount": float(r.total or 0),
                }
                for r in s.execute(
                    select(
                        day.label("day"),
                        n_venta.label("docs"),
                        func.sum(amt).label("total"),
                    )
                    .where(cond)
                    .group_by(day)
                    .order_by(day)
                ).all()
            ]

            # Lo que se dejo fuera, para que nadie tenga que adivinar el delta
            excl_where = [d.emission_date.between(start_dt, end_dt)]
            if office_id:
                excl_where.append(d.office_id == office_id)
            excluidos = s.execute(
                select(
                    func.count().label("docs"),
                    func.coalesce(func.sum(d.total_amount), 0.0).label("total"),
                ).where(and_(*excl_where, ~and_(*official_sale_conditions(documents_snapshot))))
            ).one()

            documentos = []
            if incluir_documentos:
                rows = s.execute(
                    select(
                        d.document_id,
                        d.emission_date,
                        d.office_id,
                        d.office_name,
                        d.document_type_name,
                        d.client_id,
                        amt.label("signed_total"),
                        d.total_amount,
                        d.net_amount,
                    )
                    .where(cond)
                    .order_by(desc(d.emission_date))
                    .limit(limit)
                ).all()
                documentos = [
                    {
                        "document_id": r.document_id,
                        "emission_date": r.emission_date.isoformat() if r.emission_date else None,
                        "office_id": r.office_id,
                        "office_name": (r.office_name or "").strip(),
                        "document_type_name": (r.document_type_name or "").strip(),
                        "client_id": r.client_id,
                        "amount_signed": float(r.signed_total or 0),
                        "total_amount": float(r.total_amount or 0),
                        "net_amount": float(r.net_amount or 0),
                    }
                    for r in rows
                ]

        # Frescura: un tool que lee el snapshot sin decir hasta cuando llega la
        # carga puede devolver un mes corto con cara de completo si el cron
        # murio. snapshot_lag_hours ya existia y solo la usaba /health.
        try:
            from db import snapshot_lag_hours

            lag = snapshot_lag_hours()
        except Exception:  # noqa: BLE001
            lag = None

        return {
            "source": "snapshot",
            "snapshot_lag_horas": round(lag, 1) if lag is not None else None,
            "snapshot_advertencia": (
                f"El snapshot tiene {round(lag, 1)}h de atraso: el periodo "
                "reciente puede estar incompleto."
                if lag is not None and lag > 26 else None
            ),
            "period": {"start": start_date, "end": end_date},
            "office_id": office_id,
            "regla": "venta oficial = Boletas + Facturas + ND - NC (sin notas de venta, sin guias, sin anulados)",
            "reglas_aplicadas": official_sale_supported(documents_snapshot),
            "documentos_de_venta": total_row.docs,
            "notas_de_credito": total_row.nc,
            "ticket_promedio": (
                round(float(total_row.total or 0) / total_row.docs)
                if total_row.docs else None
            ),
            "venta_oficial": float(total_row.total or 0),
            "venta_oficial_neta": float(total_row.neto or 0),
            "excluidos": {
                "documentos": excluidos.docs,
                "monto_bruto": float(excluidos.total or 0),
                "detalle": "guias de despacho + notas de venta/pedidos web/cotizaciones + anulados",
            },
            "by_office": by_office,
            "by_document_type": by_doctype,
            "by_day": by_day,
            "documentos": documentos,
            "nota_documentos": (
                None
                if incluir_documentos
                else "Agregados solamente. Pasar incluir_documentos=True para el detalle."
            ),
        }

    @mcp.tool()
    def bsale_conciliacion_venta(
        start_date: str,
        end_date: str,
        office_id: int | None = None,
        max_documents: int = 40000,
    ) -> dict[str, Any]:
        """Concilia la venta del SNAPSHOT contra Bsale EN VIVO y explica la brecha.

        Existe para cerrar el KPI "Conciliacion interna Bsale", en rojo desde
        el 22-jul-2026. Devuelve las dos cifras, la diferencia, y el desglose
        de por que difieren (documentos que faltan en el snapshot, documentos
        que sobran, montos distintos).

        Maximo 92 dias por llamada: lee Bsale en vivo dentro del web service,
        que es el mismo proceso que responde el healthcheck.

        Args:
            start_date: YYYY-MM-DD inicio.
            end_date: YYYY-MM-DD fin.
            office_id: Filtrar por sucursal.
            max_documents: Tope de documentos a leer de Bsale en vivo.
        """
        from tools_analytics import _tope_de_rango

        tope = _tope_de_rango(
            start_date, end_date, 92, "conciliar mes a mes",
        )
        if tope:
            return tope
        from bsale_client import doc_revenue_signed, get_client, is_official_sale

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc,
        )

        # --- Lado Bsale en vivo ---
        client = get_client()
        fetch = client.paginated_fetch(
            "/v1/documents.json",
            params={
                "limit": 50,
                "emissiondaterange": f"{int(start_dt.timestamp())},{int(end_dt.timestamp())}",
                "officeid": office_id,
                "state": 0,
                "expand": "[document_type,office]",
            },
            max_items=max_documents,
        )
        vivo = {
            int(d["id"]): doc_revenue_signed(d)
            for d in fetch["items"]
            if d.get("id") is not None and is_official_sale(d)
        }

        # --- Lado snapshot ---
        d = documents_snapshot.c
        amt = signed_amount(d.total_amount, d.document_type_use)
        where = [d.emission_date.between(start_dt, end_dt)]
        where += official_sale_conditions(documents_snapshot)
        if office_id:
            where.append(d.office_id == office_id)

        with db_session() as s:
            snap = {
                int(r.document_id): float(r.monto or 0)
                for r in s.execute(
                    select(d.document_id, amt.label("monto")).where(and_(*where))
                ).all()
            }

        solo_vivo = sorted(set(vivo) - set(snap))
        solo_snap = sorted(set(snap) - set(vivo))
        distintos = [
            {"document_id": k, "vivo": vivo[k], "snapshot": snap[k]}
            for k in (set(vivo) & set(snap))
            if abs(vivo[k] - snap[k]) > 1
        ]

        total_vivo = sum(vivo.values())
        total_snap = sum(snap.values())
        diff = total_snap - total_vivo

        return {
            "period": {"start": start_date, "end": end_date},
            "office_id": office_id,
            "venta_oficial_vivo": total_vivo,
            "venta_oficial_snapshot": total_snap,
            "diferencia": diff,
            "diferencia_pct": round(diff / total_vivo * 100, 2) if total_vivo else None,
            "documentos_vivo": len(vivo),
            "documentos_snapshot": len(snap),
            "brecha": {
                "faltan_en_snapshot": {
                    "count": len(solo_vivo),
                    "monto": sum(vivo[k] for k in solo_vivo),
                    "ejemplos": solo_vivo[:20],
                },
                "sobran_en_snapshot": {
                    "count": len(solo_snap),
                    "monto": sum(snap[k] for k in solo_snap),
                    "ejemplos": solo_snap[:20],
                },
                "monto_distinto": {
                    "count": len(distintos),
                    "ejemplos": distintos[:20],
                },
            },
            "truncado_lado_vivo": fetch["truncated"],
            "documentos_en_bsale": fetch["total_count"],
        }
