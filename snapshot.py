"""Snapshot nocturno de data Bsale a Postgres.

Llamado por el Render Cron Job (cron_snapshot.py) o manualmente via tool.
Hace upsert idempotente para que correrlo dos veces no rompa nada.

documents_snapshot: una fila por document_id (PK = document_id) -> upsert.
Esto evita el doble conteo que existia con la PK compuesta (snapshot_date, document_id).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bsale_client import get_client, is_sales_doc, iso_to_epoch_range
from db import (
    document_details_snapshot,
    documents_snapshot,
    stock_actual,
    variants_snapshot,
    session as db_session,
)

logger = logging.getLogger(__name__)


def _dia_utc(fecha: str) -> datetime:
    """'YYYY-MM-DD' -> medianoche UTC exacta.

    emission_date en el snapshot es medianoche UTC exacta (es una FECHA
    disfrazada de timestamp), asi que comparar contra medianoche UTC hace que
    date_to sea inclusivo sin sumar un dia.
    """
    return datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _ts_to_dt(ts: Any) -> datetime | None:
    """Convierte timestamp Bsale (segundos epoch) a datetime UTC."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def snapshot_documents(days_back: int = 14, max_pages: int = 600) -> dict[str, Any]:
    """Snapshot de documents emitidos en los ultimos N dias.

    Default 14 dias (para snapshot nocturno). NO bajar a 1: Bsale permite
    fechas de emision retroactivas y un documento generado despues de la
    ventana se pierde para siempre. max_pages=600 = 30.000 documentos, con
    margen para backfills largos sin truncar.
    Para backfill historico, llamar con days_back grande.
    """
    client = get_client()
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days_back)
    snapshot_ts = datetime.now(timezone.utc)

    params = {
        "limit": 50,
        "emissiondaterange": iso_to_epoch_range(start_date.isoformat(), end_date.isoformat()),
        "expand": "[document_type,office,client]",
    }
    # paginated_get tira el flag de truncado y devuelve el parcial como si
    # fuera completo: es el mismo modo de falla que dejo huecos permanentes en
    # el historico. Aca la ventana es corta (14 dias) y hay margen de sobra,
    # pero el tool bsale_snapshot_run_now deja pedir days_back grandes.
    _f = client.paginated_fetch(
        "/v1/documents.json", params=params, max_items=max_pages * 50
    )
    docs = _f["items"]

    rows = []
    for doc in docs:
        # Excluir guias de despacho (use=2) del snapshot tambien
        if not is_sales_doc(doc):
            continue
        office = doc.get("office") or {}
        doctype = doc.get("document_type") or {}
        client_ref = doc.get("client") or {}
        rows.append({
            "snapshot_date": snapshot_ts,
            "document_id": doc.get("id"),
            "emission_date": _ts_to_dt(doc.get("emissionDate")),
            "office_id": office.get("id"),
            "office_name": office.get("name"),
            "document_type_id": doctype.get("id"),
            "document_type_name": doctype.get("name"),
            "document_type_use": doctype.get("use", 0),
            "client_id": client_ref.get("id"),
            "total_amount": float(doc.get("totalAmount", 0) or 0),
            "net_amount": float(doc.get("netAmount", 0) or 0),
            "tax_amount": float(doc.get("taxAmount", 0) or 0),
            "state": doc.get("state"),
            "raw": doc,
        })

    # Dedupe por document_id: ON CONFLICT DO UPDATE no permite afectar la misma
    # fila dos veces en un mismo statement (CardinalityViolation). Bsale a veces
    # devuelve el mismo documento en dos paginas, asi que limpiamos antes del upsert.
    if rows:
        dedup: dict[Any, dict[str, Any]] = {}
        for r in rows:
            dedup[r["document_id"]] = r
        rows = list(dedup.values())

    if rows:
        # Chunked insert (500 por chunk) para evitar SSL EOF en queries enormes
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            with db_session() as s:
                stmt = pg_insert(documents_snapshot).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["document_id"],
                    set_={
                        "snapshot_date": stmt.excluded.snapshot_date,
                        "emission_date": stmt.excluded.emission_date,
                        "office_id": stmt.excluded.office_id,
                        "office_name": stmt.excluded.office_name,
                        "document_type_id": stmt.excluded.document_type_id,
                        "document_type_name": stmt.excluded.document_type_name,
                        "document_type_use": stmt.excluded.document_type_use,
                        "client_id": stmt.excluded.client_id,
                        "total_amount": stmt.excluded.total_amount,
                        "net_amount": stmt.excluded.net_amount,
                        "tax_amount": stmt.excluded.tax_amount,
                        "state": stmt.excluded.state,
                        "raw": stmt.excluded.raw,
                    },
                )
                s.execute(stmt)

    out = {
        "snapshot_ts": snapshot_ts.isoformat(),
        "rows": len(rows),
        "days_back": days_back,
        "documentos_leidos": _f.get("fetched"),
        "documentos_en_bsale": _f.get("total_count"),
        "truncado": bool(_f.get("truncated")),
    }
    if out["truncado"]:
        out["documents_error"] = (
            f"TRUNCADO: se leyeron {_f.get('fetched')} de "
            f"{_f.get('total_count')} documentos. La ventana quedo INCOMPLETA."
        )
    return out


def snapshot_documents_range(
    date_from: str, date_to: str, max_pages: int = 1200
) -> dict[str, Any]:
    """Como snapshot_documents pero para un rango de fechas EXPLICITO [date_from, date_to] (ISO).

    Pensado para backfill historico por tramos. Upsert idempotente con dedupe
    por document_id (re-cargar un mes parcial lo completa).

    Usa paginated_fetch, no paginated_get: este ultimo TIRA el aviso de truncado
    y devuelve la lista parcial como si estuviera completa. Es exactamente el bug
    que dejo el snapshot de 2025 con 4.101 documentos menos en marzo (-45,9% del
    mes) sin que nada lo dijera. Aca el truncado se devuelve al llamador y el
    tool lo declara.

    max_pages por defecto 1200 (60.000 documentos): un mes de MyScrubs son
    ~14.000, asi que un trimestre entra holgado.
    """
    client = get_client()
    snapshot_ts = datetime.now(timezone.utc)

    params = {
        "limit": 50,
        "emissiondaterange": iso_to_epoch_range(date_from, date_to),
        "expand": "[document_type,office,client]",
    }
    fetched = client.paginated_fetch(
        "/v1/documents.json", params=params, max_items=max_pages * 50
    )
    docs = fetched["items"]

    rows = []
    for doc in docs:
        if not is_sales_doc(doc):
            continue
        office = doc.get("office") or {}
        doctype = doc.get("document_type") or {}
        client_ref = doc.get("client") or {}
        rows.append({
            "snapshot_date": snapshot_ts,
            "document_id": doc.get("id"),
            "emission_date": _ts_to_dt(doc.get("emissionDate")),
            "office_id": office.get("id"),
            "office_name": office.get("name"),
            "document_type_id": doctype.get("id"),
            "document_type_name": doctype.get("name"),
            "document_type_use": doctype.get("use", 0),
            "client_id": client_ref.get("id"),
            "total_amount": float(doc.get("totalAmount", 0) or 0),
            "net_amount": float(doc.get("netAmount", 0) or 0),
            "tax_amount": float(doc.get("taxAmount", 0) or 0),
            "state": doc.get("state"),
            "raw": doc,
        })

    # Dedupe por document_id (CardinalityViolation guard) y upsert chunked.
    if rows:
        dedup: dict[Any, dict[str, Any]] = {}
        for r in rows:
            dedup[r["document_id"]] = r
        rows = list(dedup.values())
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            with db_session() as s:
                stmt = pg_insert(documents_snapshot).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["document_id"],
                    set_={
                        "snapshot_date": stmt.excluded.snapshot_date,
                        "emission_date": stmt.excluded.emission_date,
                        "office_id": stmt.excluded.office_id,
                        "office_name": stmt.excluded.office_name,
                        "document_type_id": stmt.excluded.document_type_id,
                        "document_type_name": stmt.excluded.document_type_name,
                        "document_type_use": stmt.excluded.document_type_use,
                        "client_id": stmt.excluded.client_id,
                        "total_amount": stmt.excluded.total_amount,
                        "net_amount": stmt.excluded.net_amount,
                        "tax_amount": stmt.excluded.tax_amount,
                        "state": stmt.excluded.state,
                        "raw": stmt.excluded.raw,
                    },
                )
                s.execute(stmt)

    out = {
        "date_from": date_from,
        "date_to": date_to,
        "rows": len(rows),
        "documentos_leidos": fetched.get("fetched"),
        "documentos_en_bsale": fetched.get("total_count"),
        "truncado": bool(fetched.get("truncated")),
    }
    if out["truncado"]:
        out["advertencia"] = (
            "TRUNCADO: no se leyeron todos los documentos del rango. El snapshot "
            "queda incompleto para este periodo. Repetir por tramos mas cortos o "
            "subir max_pages."
        )
    return out


def snapshot_stock(max_pages: int = 500) -> dict[str, Any]:
    """Snapshot del stock actual por sucursal, bajado en paralelo.

    Antes esto paginaba de a una pagina en serie: ~1,4 s por cada 50 filas.
    Con ~150.000 filas son ~3.000 paginas y mas de una hora de corrida, durante
    la cual el cron esta tomado y cualquier deploy la mata a medio camino.

    Ahora la pagina 0 trae ademas `count`, y con eso se calcula la lista exacta
    de offsets y se bajan con un pool de threads. `count` da tambien un criterio
    de completitud mucho mas fuerte que el anterior ("llegue a una pagina
    vacia"): la corrida es completa solo si se leyeron TODAS las paginas
    previstas y ninguna fallo. Eso importa porque de ese criterio depende si se
    borran o no las filas que la corrida no toco.
    """
    client = get_client()
    snapshot_ts = datetime.now(timezone.utc)

    PAGE = 50
    FLUSH_CADA = 40  # paginas por flush (2.000 filas)
    COLA_MAX = 40    # paginas extra si Bsale crecio durante la corrida
    workers = max(1, int(os.getenv("BSALE_STOCK_WORKERS", "4")))

    seen: set[tuple] = set()
    buffer: list[dict[str, Any]] = []
    total_persisted = 0
    paginas_ok = 0
    error: str | None = None

    def _pagina(offset: int) -> dict[str, Any]:
        return client.get(
            "/v1/stocks.json",
            params={"limit": PAGE, "offset": offset, "expand": "[variant,office]"},
            use_cache=False,
        )

    def _intento(offset: int):
        try:
            return offset, _pagina(offset), None
        except Exception as e:  # noqa: BLE001
            return offset, None, f"{type(e).__name__}: {str(e)[:200]}"

    def _acumular(data: dict[str, Any]) -> int:
        n = 0
        for item in (data.get("items") or []):
            variant = item.get("variant") or {}
            office = item.get("office") or {}
            vid = variant.get("id")
            oid = office.get("id")
            if vid is None or oid is None:
                continue
            key = (vid, oid)
            if key in seen:
                continue
            seen.add(key)
            buffer.append({
                "updated_at": snapshot_ts,
                "variant_id": vid,
                "office_id": oid,
                "quantity": float(item.get("quantity", 0) or 0),
                "variant_code": variant.get("code"),
                "office_name": office.get("name"),
            })
            n += 1
        return n

    def _flush() -> int:
        """Upsert sobre el estado actual.

        Antes era on_conflict_do_nothing sobre (snapshot_date, variant_id,
        office_id), o sea que cada corrida escribia un juego de filas nuevo.
        Ahora es do_update sobre (variant_id, office_id): la fila de cada
        variante en cada sucursal se pisa con el valor nuevo.
        """
        if not buffer:
            return 0
        with db_session() as s:
            stmt = pg_insert(stock_actual).values(buffer)
            stmt = stmt.on_conflict_do_update(
                index_elements=["variant_id", "office_id"],
                set_={
                    "quantity": stmt.excluded.quantity,
                    "variant_code": stmt.excluded.variant_code,
                    "office_name": stmt.excluded.office_name,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            s.execute(stmt)
        n = len(buffer)
        buffer.clear()
        return n

    # Pagina 0 aparte: si esta falla no hay corrida, y su `count` es lo que
    # define el plan del resto.
    _, primera, err0 = _intento(0)
    if err0 is not None:
        logger.error("snapshot_stock no pudo leer la pagina 0: %s", err0)
        return {
            "snapshot_ts": snapshot_ts.isoformat(),
            "rows": 0,
            "paginas_leidas": 0,
            "completo": False,
            "stock_error": err0,
            "advertencia": (
                "No se pudo leer la primera pagina de stock. No se escribio ni "
                "se borro nada: el estado anterior queda intacto."
            ),
        }

    paginas_ok = 1
    _acumular(primera)
    total_bsale = int(primera.get("count") or 0)

    paginas_previstas = max(1, -(-total_bsale // PAGE)) if total_bsale else 1
    tope = min(paginas_previstas, max_pages)
    truncado = paginas_previstas > max_pages
    offsets = [p * PAGE for p in range(1, tope)]

    if offsets:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i in range(0, len(offsets), FLUSH_CADA):
                lote = offsets[i:i + FLUSH_CADA]
                for offset, data, err in pool.map(_intento, lote):
                    if err is not None:
                        error = error or f"offset {offset}: {err}"
                        logger.error(
                            "snapshot_stock fallo en offset %d: %s", offset, err
                        )
                        continue
                    paginas_ok += 1
                    _acumular(data)
                total_persisted += _flush()

    # Cola: `count` es la foto del instante en que arranco la corrida. Si Bsale
    # sumo filas mientras bajabamos quedan fuera del plan, asi que se siguen
    # leyendo de a una hasta que venga una pagina corta o vacia.
    cola = 0
    offset = tope * PAGE
    while error is None and not truncado and cola < COLA_MAX:
        _, data, err = _intento(offset)
        if err is not None:
            error = f"cola offset {offset}: {err}"
            break
        items = data.get("items") or []
        if not items:
            break
        paginas_ok += 1
        cola += 1
        _acumular(data)
        offset += PAGE
        if len(items) < PAGE:
            break

    total_persisted += _flush()

    # Guardarraya. `count` dice cuantas filas deberia haber. Contar paginas no
    # alcanza: Bsale bajo carga devuelve 200 con items vacio, sin error. Con el
    # criterio de "todas las paginas respondieron" esa corrida quedaria
    # COMPLETA, y entonces _borrar_stock_no_reportado borraria las filas de esa
    # pagina, que existen. Un stock viejo se corrige solo en la corrida
    # siguiente; uno borrado se ve como cero en la tienda.
    filas_vistas = len(seen)
    faltante = total_bsale - filas_vistas
    if total_bsale and faltante > 10:
        error = error or (
            f"Bsale reporta {total_bsale} filas de stock y solo se leyeron "
            f"{filas_vistas} ({faltante} sin explicacion)"
        )

    completo = error is None and not truncado and paginas_ok == tope + cola

    out: dict[str, Any] = {
        "snapshot_ts": snapshot_ts.isoformat(),
        "rows": total_persisted,
        "count_bsale": total_bsale,
        "paginas_previstas": paginas_previstas,
        "paginas_leidas": paginas_ok,
        "paginas_cola": cola,
        "filas_vistas": filas_vistas,
        "workers": workers,
        "max_pages_attempted": max_pages,
        "completo": completo,
    }
    if truncado:
        error = error or (
            f"se alcanzo el tope de {max_pages} paginas y Bsale reporta "
            f"{paginas_previstas}"
        )
    if not completo:
        # La clave *_error es la que hace que cron_snapshot marque la corrida
        # como fallida y dispare la notificacion de Render.
        out["stock_error"] = error or "la corrida no leyo todas las paginas previstas"
        out["advertencia"] = (
            "Corrida de stock incompleta: hay variantes con el valor de una "
            "corrida anterior. Mirar updated_at por fila para saber cuales. "
            "NO se borro nada: un valor viejo es mejor que ninguno."
        )
    else:
        # Solo despues de una corrida COMPLETA se puede afirmar que lo que no
        # aparecio ya no existe en Bsale. Si la corrida quedo a medias, borrar
        # lo no tocado eliminaria stock real.
        out["filas_dadas_de_baja"] = _borrar_stock_no_reportado(snapshot_ts)
    return out


def _borrar_stock_no_reportado(corrida_ts) -> int:
    """Borra las filas que esta corrida no toco. Solo tras una corrida completa.

    Con una tabla de estado actual y upsert, una variante que Bsale deja de
    reportar (se dio de baja, quedo sin stock en esa sucursal) mantendria su
    ultimo valor para siempre. updated_at es el que resuelve eso: lo que no se
    escribio en esta corrida ya no existe del otro lado.

    Este es el unico borrado del flujo de stock y es barato: opera sobre una
    tabla de ~150.000 filas, no sobre 7,6 millones.
    """
    try:
        with db_session() as s:
            n = s.execute(
                stock_actual.delete().where(stock_actual.c.updated_at < corrida_ts)
            ).rowcount or 0
        if n:
            logger.info("Stock: %s filas dadas de baja (ya no las reporta Bsale)", n)
        return n
    except Exception as e:  # noqa: BLE001
        logger.error("No se pudo limpiar el stock no reportado: %s", e)
        return 0


def snapshot_variants(max_pages: int = 100) -> dict[str, Any]:
    """Snapshot del catalogo de variantes."""
    client = get_client()
    snapshot_ts = datetime.now(timezone.utc)

    items = client.paginated_get(
        "/v1/variants.json",
        params={"limit": 50},
        max_pages=max_pages,
    )

    rows = []
    seen = set()
    for v in items:
        vid = v.get("id")
        if vid is None or vid in seen:
            continue
        seen.add(vid)
        product = v.get("product") or {}
        rows.append({
            "snapshot_date": snapshot_ts,
            "variant_id": vid,
            "product_id": product.get("id"),
            "code": v.get("code"),
            "barcode": v.get("barCode"),
            "description": v.get("description"),
            "state": v.get("state"),
            "raw": v,
        })

    if rows:
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            with db_session() as s:
                stmt = pg_insert(variants_snapshot).values(chunk)
                stmt = stmt.on_conflict_do_nothing(index_elements=["snapshot_date", "variant_id"])
                s.execute(stmt)

    return {"snapshot_ts": snapshot_ts.isoformat(), "rows": len(rows)}


def snapshot_details(
    batch_size: int | None = None,
    max_docs: int | None = None,
    only_recent_days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    oldest_first: bool = False,
) -> dict[str, Any]:
    """Para docs en documents_snapshot que aun no tienen details, fetch y store.

    Bsale requiere 1 API call por documento para sus details. Esta funcion
    es la mas costosa. Medido el 08-sep-2026: ~8 documentos por segundo.

    OJO con batch_size y max_docs: eran DOS topes distintos aplicados uno
    despues del otro (`todo[:max_docs]` y despues `for cand in todo[:batch_size]`),
    asi que mandaba el mas chico y el otro no hacia nada. Con los defaults
    viejos (batch_size=50, max_docs=500) pedir 500 documentos procesaba 50 y
    no lo decia. Ahora hay UN tope efectivo y va declarado en el resultado.

    Args:
        batch_size: Alias historico de max_docs. Si vienen los dos, manda el menor.
        max_docs: Cuantos documentos procesar en esta llamada. Default 400.
        only_recent_days: Solo docs con emission_date >= now - N dias.
        date_from / date_to: Ventana explicita 'YYYY-MM-DD' (inclusive). Es lo
            que se usa para rellenar un periodo viejo puntual — el nocturno
            corria con only_recent_days=90, asi que ningun documento anterior
            a esa ventana se iba a completar NUNCA por si solo.
        oldest_first: Procesa del mas viejo al mas nuevo. Para que un backfill
            historico avance en vez de quedarse siempre en lo reciente.

    Returns:
        Dict con count de docs procesados, lineas insertadas, errores y el
        tope efectivo que se aplico.
    """
    client = get_client()

    topes = [t for t in (batch_size, max_docs) if t]
    cap = min(topes) if topes else 400

    # 1. Encuentra docs sin details aun. El anti-join va en SQL: la version
    # anterior traia a Python TODOS los document_id con detalle y TODOS los
    # documentos del snapshot para cruzarlos en memoria (159.000 filas y
    # subiendo) solo para quedarse con unos cientos.
    with db_session() as s:
        ya_tienen = select(document_details_snapshot.c.document_id).where(
            document_details_snapshot.c.document_id == documents_snapshot.c.document_id
        )
        filtros = [~ya_tienen.exists()]
        if only_recent_days:
            from datetime import timedelta as _td
            cutoff = datetime.now(timezone.utc) - _td(days=only_recent_days)
            filtros.append(documents_snapshot.c.emission_date >= cutoff)
        if date_from:
            filtros.append(documents_snapshot.c.emission_date >= _dia_utc(date_from))
        if date_to:
            filtros.append(documents_snapshot.c.emission_date <= _dia_utc(date_to))

        orden = (
            documents_snapshot.c.emission_date.asc()
            if oldest_first
            else documents_snapshot.c.emission_date.desc()
        )
        cand_stmt = (
            select(
                documents_snapshot.c.document_id,
                documents_snapshot.c.emission_date,
                documents_snapshot.c.office_id,
                documents_snapshot.c.document_type_use,
            )
            .where(*filtros)
            .order_by(orden)
            .limit(cap)
        )
        todo = s.execute(cand_stmt).fetchall()

        # Cuantos quedan DE VERDAD con estos mismos filtros. La version
        # anterior calculaba `len(todo) - batch_size`, que con el tope aplicado
        # da 0 siempre: el tool informaba "no queda nada" con 129.000
        # documentos pendientes.
        pendientes_antes = s.execute(
            select(func.count()).select_from(documents_snapshot).where(*filtros)
        ).scalar_one()

    rows_inserted = 0
    docs_processed = 0
    errors = 0

    # El tope ya lo aplico el LIMIT del query. Volver a cortar aca con otra
    # variable es justo el bug que se acaba de sacar.
    #
    # Los documentos se piden EN PARALELO. Antes iban de a uno: 1 request,
    # esperar, siguiente. Medido el 08-sep-2026 asi daba ~8 documentos por
    # segundo, o sea 4,5 horas para los 129.000 pendientes, y cada llamada
    # de mas de ~1.400 documentos se pasaba del timeout de 180 s del cliente
    # MCP. La concurrencia la aguanta Bsale (misma que usa paginated_fetch).
    # 4, no 6. Con 6 sostenidos Bsale empieza a frenar: en una corrida de
    # ~8.000 documentos el cliente acumulo 242 reintentos (08-sep-2026).
    # 4 es la concurrencia que ya usa paginated_fetch sin protestas.
    _DETALLE_WORKERS = int(os.getenv("BSALE_DETAIL_WORKERS", "4"))
    n_workers = max(1, min(_DETALLE_WORKERS, 10))

    def _bajar(cand):
        """Devuelve (doc_id, filas, hubo_error). No toca la base: solo red."""
        doc_id = cand.document_id
        try:
            # Paginado: un GET suelto traia solo 50 lineas y, como el documento
            # quedaba marcado como procesado, el resto se perdia para siempre.
            # Las facturas institucionales son justo las de muchas lineas.
            fetch = client.paginated_fetch(
                f"/v1/documents/{doc_id}/details.json",
                params={"limit": 50, "expand": "[variant]"},
                max_items=2000,
            )
            if fetch["truncated"]:
                # no insertar parcial: se reintenta en la proxima corrida
                return doc_id, [], True
            items = fetch["items"] or []
        except Exception:  # noqa: BLE001
            return doc_id, [], True

        filas = []
        for line in items:
            variant = line.get("variant") or {}
            line_id = line.get("id")
            if line_id is None:
                continue
            filas.append({
                "document_id": doc_id,
                "line_id": line_id,
                "variant_id": variant.get("id"),
                "variant_code": variant.get("code"),
                "variant_description": (variant.get("description") or "")[:500],
                "office_id": cand.office_id,
                "emission_date": cand.emission_date,
                "document_type_use": cand.document_type_use or 0,
                "quantity": float(line.get("quantity", 0) or 0),
                "net_amount": float(line.get("netAmount", 0) or 0),
                "total_amount": float(line.get("totalAmount", 0) or 0),
                "fetched_at": datetime.now(timezone.utc),
            })
        return doc_id, filas, False

    pendientes_de_insertar: dict[tuple, dict[str, Any]] = {}

    def _descargar(filas_dict):
        """Inserta lo acumulado. Se llama por tandas para no juntar todo en RAM."""
        nonlocal rows_inserted
        if not filas_dict:
            return
        valores = list(filas_dict.values())
        CHUNK = 500
        for k in range(0, len(valores), CHUNK):
            trozo = valores[k:k + CHUNK]
            with db_session() as s:
                stmt = pg_insert(document_details_snapshot).values(trozo)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["document_id", "line_id"]
                )
                s.execute(stmt)
            rows_inserted += len(trozo)
        filas_dict.clear()

    if todo:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            for _doc_id, filas, hubo_error in pool.map(_bajar, todo):
                if hubo_error:
                    errors += 1
                    continue
                # Dedupe por (document_id, line_id), misma razon que en documentos.
                for fila in filas:
                    pendientes_de_insertar[(fila["document_id"], fila["line_id"])] = fila
                docs_processed += 1
                if len(pendientes_de_insertar) >= 2000:
                    _descargar(pendientes_de_insertar)
        _descargar(pendientes_de_insertar)

    return {
        "docs_processed": docs_processed,
        "lines_inserted": rows_inserted,
        "errors": errors,
        "cap_efectivo": cap,
        "remaining_to_process": max(0, pendientes_antes - docs_processed),
        "candidates_total": pendientes_antes,
    }


def nightly_snapshot() -> dict[str, Any]:
    """Job nocturno completo. OJO: HOY NINGUN CRON LLAMA A ESTA FUNCION.

    El cron de Render `bsale-mcp-snapshot` corre
    `python sync_incremental.py --modo auto` cada 30 minutos, NO
    cron_snapshot.py. Verificado en la configuracion del cron el
    08-sep-2026, despues de programar aca un backfill que nunca iba a
    ejecutarse.

    Se conserva porque sirve para una corrida completa a mano y porque
    cron_snapshot.py la usa si algun dia se agenda. **Antes de agregar un
    paso aca, confirmar en el dashboard de Render que comando corre el
    cron.** Si el paso tiene que correr solo, va en sync_incremental.run().
    """
    logger.info("Iniciando snapshot nocturno")
    results = {}
    try:
        # Ventana de 14 dias, no de 1. Bsale permite generar un documento con
        # fecha de emision retroactiva: la boleta 1273799 tiene emissionDate
        # 18-jul-2026 y generationDate 05-ago-2026, 18 dias despues. Con
        # days_back=1 la corrida del 19-jul buscaba por fecha de emision y ese
        # documento todavia no existia; ninguna corrida posterior vuelve a
        # mirar esa fecha, asi que se perdia para siempre. Verificado el
        # 07-sep-2026: faltaban 126 documentos ($3.889.808) en jun-sep por
        # este motivo. El upsert es por document_id, releer dias ya cargados
        # no duplica nada.
        results["documents"] = snapshot_documents(days_back=14)
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_documents: %s", e)
        results["documents_error"] = str(e)

    try:
        # 6000 paginas = 300.000 filas. Con el default de 500 el nocturno
        # guardaba una foto del 10% que, por ser la mas reciente, GANABA sobre
        # la completa del sync incremental (verificado 07-sep-2026).
        results["stock"] = snapshot_stock(max_pages=6000)
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_stock: %s", e)
        results["stock_error"] = str(e)

    try:
        results["variants"] = snapshot_variants(max_pages=2000)  # 5.000 cortaba el catalogo
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_variants: %s", e)
        results["variants_error"] = str(e)

    # Details para docs recientes. Corre en el Cron Job dedicado (no compite
    # con el web service), asi que el batch puede ser grande.
    try:
        results["details"] = snapshot_details(max_docs=2000, only_recent_days=90)
    except Exception as e:  # noqa: BLE001
        logger.error("Error en snapshot_details: %s", e)
        results["details_error"] = str(e)

    # RETENCION. Va ACA, antes del backfill historico, y no al final.
    #
    # ORDEN DE LOS PASOS: la retencion es corta y critica; el backfill
    # historico es largo y opcional. Tenerla detras del backfill significa que
    # cualquier cosa que mate la corrida a mitad de camino (un redeploy encima
    # del cron, un tope de tiempo, un error de Bsale) deja la retencion sin
    # correr esa noche. Eso fue exactamente lo que paso el 07-sep-2026: se
    # empujaron 7 commits durante la noche, cada push redespliega el cron por
    # autoDeploy, y la corrida de las 01:00 UTC murio a mitad. Un paso barato
    # que arregla una tabla de 7,5 millones de filas no puede quedar detras de
    # 40 minutos de trabajo opcional.
    try:
        from retention import apply_retention

        results["retention"] = apply_retention()
        # Se promueve a clave de primer nivel porque cron_snapshot.py solo
        # mira las de primer nivel que terminan en _error. Anidado, un fallo
        # de retencion no marcaba la corrida como fallida: podia fallar todas
        # las noches sin que llegara un solo correo.
        if results["retention"].get("hubo_error"):
            results["retention_error"] = str(results["retention"])[:300]
    except Exception as e:  # noqa: BLE001
        logger.error("Error en la retencion: %s", e)
        results["retention_error"] = str(e)[:300]

    # Y el hueco historico, del mas viejo al mas nuevo.
    #
    # POR QUE EXISTE ESTE PASO: el de arriba filtra a 90 dias. Todo documento
    # anterior a esa ventana no lo miraba NADIE, nunca. Medido el 08-sep-2026:
    # ene-ago 2025 tenia 0% de detalle de linea en 54.562 documentos, y ene-ago
    # 2026 un 58,2%. O sea que las UNIDADES no se podian comparar ano contra
    # ano, y el tool devolvia lista vacia sin decir por que.
    #
    # Va oldest_first a proposito: con el orden por defecto (mas nuevo
    # primero) el backfill se queda para siempre masticando lo reciente y
    # nunca llega a 2025.
    #
    # Presupuesto: 40.000 por noche. Con el pool en paralelo se midieron 45
    # documentos/segundo en frio y menos con Bsale ya frenando, asi que son
    # entre 15 y 45 minutos. Aca no hay healthcheck que dejar sin responder
    # (es el cron, no el web service) ni timeout de cliente MCP. Con ~129.000
    # pendientes converge en 3 o 4 noches sin tocar la config de Render.
    try:
        results["details_historico"] = snapshot_details(
            max_docs=int(os.getenv("DETAILS_BACKFILL_POR_NOCHE", "40000")),
            oldest_first=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Error en el backfill historico de details: %s", e)
        results["details_historico_error"] = str(e)

    logger.info("Snapshot nocturno completado: %s", results)
    return results
