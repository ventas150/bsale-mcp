"""Capa 'lenguaje LLM': resúmenes JSON pre-calculados.

Se regeneran al final de cada sync (sync_incremental.py / nightly).
El agente los lee al instante via el tool bsale_digest(), sin correr SQL pesado.

Tabla destino: llm_digests (digest_key TEXT PK, data JSONB, generated_at TIMESTAMPTZ).
El esquema se autoprovisiona (ensure_schema): no requiere correr migración a mano.

Convenciones de negocio (mismas que el resto del MCP):
  - document_type_use: 0=venta, 1=nota de crédito, 2=guía.
  - Las guías (use=2) ya están excluidas del snapshot de documentos.
  - Ventas con signo: nota de crédito (use=1) resta.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from db import session as db_session

logger = logging.getLogger(__name__)

_schema_ready = False

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS llm_digests (
        digest_key    TEXT PRIMARY KEY,
        data          JSONB NOT NULL,
        generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_stock_snapshot_date ON stock_snapshot (snapshot_date)",
)


def ensure_schema() -> None:
    """Crea la tabla de digests (idempotente). Se ejecuta una vez por proceso."""
    global _schema_ready
    if _schema_ready:
        return
    with db_session() as s:
        for stmt in _DDL:
            try:
                s.execute(text(stmt))
            except Exception as e:  # noqa: BLE001
                logger.warning("ensure_schema: %s", e)
    _schema_ready = True


# ============================
# Persistencia de digests
# ============================

def _save_digest(key: str, data: dict[str, Any]) -> None:
    """Upsert de un digest por su key."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    with db_session() as s:
        s.execute(
            text(
                """
                INSERT INTO llm_digests (digest_key, data, generated_at)
                VALUES (:k, CAST(:d AS jsonb), now())
                ON CONFLICT (digest_key)
                DO UPDATE SET data = EXCLUDED.data,
                              generated_at = EXCLUDED.generated_at
                """
            ),
            {"k": key, "d": payload},
        )


def get_digest(key: str) -> dict[str, Any] | None:
    """Lee un digest (lo usa el tool). Devuelve None si no existe."""
    ensure_schema()
    with db_session() as s:
        row = s.execute(
            text("SELECT data, generated_at FROM llm_digests WHERE digest_key = :k"),
            {"k": key},
        ).first()
    if not row:
        return None
    data, generated_at = row
    out = dict(data) if isinstance(data, dict) else data
    if isinstance(out, dict):
        out.setdefault("_generated_at", generated_at.isoformat() if generated_at else None)
    return out


def list_digests() -> list[dict[str, Any]]:
    """Lista los digests disponibles y su frescura."""
    ensure_schema()
    with db_session() as s:
        rows = s.execute(
            text("SELECT digest_key, generated_at FROM llm_digests ORDER BY digest_key")
        ).fetchall()
    return [
        {"digest": k, "generated_at": ts.isoformat() if ts else None}
        for k, ts in rows
    ]


# ============================
# Cálculo de cada digest
# ============================

def build_ventas_hoy() -> dict[str, Any]:
    """Ventas de hoy: por sucursal + top 10 SKU. Montos con signo (NC resta)."""
    with db_session() as s:
        por_sucursal = s.execute(text(
            """
            SELECT office_id,
                   max(office_name)                                              AS sucursal,
                   count(*) FILTER (WHERE document_type_use <> 1)                AS docs,
                   sum(CASE WHEN document_type_use = 1 THEN -total_amount
                            ELSE total_amount END)                               AS total,
                   sum(CASE WHEN document_type_use = 1 THEN -net_amount
                            ELSE net_amount END)                                 AS neto
            FROM documents_snapshot
            WHERE emission_date::date = current_date
              AND document_type_use <> 2
            GROUP BY office_id
            ORDER BY total DESC NULLS LAST
            """
        )).fetchall()

        top_sku = s.execute(text(
            """
            SELECT variant_code,
                   max(variant_description)                          AS descripcion,
                   sum(CASE WHEN document_type_use = 1 THEN -quantity
                            ELSE quantity END)                       AS unidades,
                   sum(CASE WHEN document_type_use = 1 THEN -total_amount
                            ELSE total_amount END)                   AS ingresos
            FROM document_details_snapshot
            WHERE emission_date::date = current_date
              AND document_type_use <> 2
            GROUP BY variant_code
            ORDER BY unidades DESC NULLS LAST
            LIMIT 10
            """
        )).fetchall()

    return {
        "fecha": datetime.now(timezone.utc).date().isoformat(),
        "por_sucursal": [
            {
                "office_id": r.office_id,
                "sucursal": r.sucursal,
                "documentos": int(r.docs or 0),
                "total": float(r.total or 0),
                "neto": float(r.neto or 0),
            }
            for r in por_sucursal
        ],
        "total_dia": float(sum((r.total or 0) for r in por_sucursal)),
        "top_skus": [
            {
                "sku": r.variant_code,
                "descripcion": r.descripcion,
                "unidades": float(r.unidades or 0),
                "ingresos": float(r.ingresos or 0),
            }
            for r in top_sku
        ],
    }


def build_ventas_periodo(dias: int) -> dict[str, Any]:
    """Ventas de los últimos N días: total + top 20 productos."""
    with db_session() as s:
        tot = s.execute(text(
            """
            SELECT count(*) FILTER (WHERE document_type_use <> 1) AS docs,
                   sum(CASE WHEN document_type_use = 1 THEN -total_amount
                            ELSE total_amount END)                AS total
            FROM documents_snapshot
            WHERE emission_date >= now() - make_interval(days => :d)
              AND document_type_use <> 2
            """
        ), {"d": dias}).first()

        top = s.execute(text(
            """
            SELECT variant_code,
                   max(variant_description)                        AS descripcion,
                   sum(CASE WHEN document_type_use = 1 THEN -quantity
                            ELSE quantity END)                     AS unidades,
                   sum(CASE WHEN document_type_use = 1 THEN -total_amount
                            ELSE total_amount END)                 AS ingresos
            FROM document_details_snapshot
            WHERE emission_date >= now() - make_interval(days => :d)
              AND document_type_use <> 2
            GROUP BY variant_code
            ORDER BY ingresos DESC NULLS LAST
            LIMIT 20
            """
        ), {"d": dias}).fetchall()

    return {
        "ventana_dias": dias,
        "documentos": int((tot.docs if tot else 0) or 0),
        "total": float((tot.total if tot else 0) or 0),
        "top_productos": [
            {
                "sku": r.variant_code,
                "descripcion": r.descripcion,
                "unidades": float(r.unidades or 0),
                "ingresos": float(r.ingresos or 0),
            }
            for r in top
        ],
    }


def build_stock_resumen(umbral_bajo: float = 3) -> dict[str, Any]:
    """Resumen del stock actual: por sucursal, quiebres y bajo stock.

    Usa la foto más reciente (max snapshot_date), sin depender de una vista.
    """
    with db_session() as s:
        por_sucursal = s.execute(text(
            """
            WITH latest AS (SELECT max(snapshot_date) AS d FROM stock_snapshot)
            SELECT s.office_id,
                   max(s.office_name)                          AS sucursal,
                   count(*)                                    AS skus,
                   sum(s.quantity)                             AS unidades,
                   count(*) FILTER (WHERE s.quantity <= 0)     AS quiebres
            FROM stock_snapshot s, latest
            WHERE s.snapshot_date = latest.d
            GROUP BY s.office_id
            ORDER BY sucursal
            """
        )).fetchall()

        quiebres = s.execute(text(
            """
            WITH latest AS (SELECT max(snapshot_date) AS d FROM stock_snapshot)
            SELECT s.variant_code, s.office_name, s.quantity
            FROM stock_snapshot s, latest
            WHERE s.snapshot_date = latest.d
              AND s.quantity <= :u
            ORDER BY s.quantity ASC
            LIMIT 20
            """
        ), {"u": umbral_bajo}).fetchall()

        foto = s.execute(text("SELECT max(snapshot_date) FROM stock_snapshot")).scalar()

    return {
        "foto_stock": foto.isoformat() if foto else None,
        "por_sucursal": [
            {
                "office_id": r.office_id,
                "sucursal": r.sucursal,
                "skus": int(r.skus or 0),
                "unidades": float(r.unidades or 0),
                "quiebres": int(r.quiebres or 0),
            }
            for r in por_sucursal
        ],
        "top_bajo_stock": [
            {"sku": r.variant_code, "sucursal": r.office_name, "cantidad": float(r.quantity or 0)}
            for r in quiebres
        ],
    }


# ============================
# Orquestación
# ============================

def build_all() -> dict[str, Any]:
    """Regenera todos los digests. Se llama al final de cada sync."""
    ensure_schema()
    results: dict[str, Any] = {}
    for key, fn in (
        ("ventas_hoy", build_ventas_hoy),
        ("ventas_30d", lambda: build_ventas_periodo(30)),
        ("ventas_90d", lambda: build_ventas_periodo(90)),
        ("stock_resumen", build_stock_resumen),
    ):
        try:
            data = fn()
            _save_digest(key, data)
            results[key] = "ok"
        except Exception as e:  # noqa: BLE001
            logger.error("Error generando digest %s: %s", key, e)
            results[key] = f"error: {e}"
    return results


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
    print(build_all())
