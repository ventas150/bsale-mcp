"""Layer Postgres para snapshots y mapping SKU cross-source.

Se activa solo si DATABASE_URL esta seteado (Render Postgres).
Sin DB, todo el resto del MCP sigue funcionando — esto es opcional.

Tablas:
- documents_snapshot: snapshot diario de documents (ventas)
- variants_snapshot: snapshot diario de variantes
- stock_snapshot: snapshot diario de stock por sucursal
- sku_mapping: cross-source mapping Bsale <-> Shopify <-> ML
- mapping_audit: log de cambios al mapping
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    case,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

# ============================
# Schema
# ============================

metadata = MetaData()

# NOTA: PK = document_id (una fila por documento). El snapshot hace upsert
# (on_conflict_do_update). snapshot_date queda como columna informativa.
# document_type_use: 0=venta, 1=nota de credito, 2=guia.
documents_snapshot = Table(
    "documents_snapshot",
    metadata,
    Column("document_id", Integer, primary_key=True),
    Column("snapshot_date", DateTime(timezone=True)),
    Column("emission_date", DateTime(timezone=True), index=True),
    Column("office_id", Integer, index=True),
    Column("office_name", String(200)),
    Column("document_type_id", Integer),
    Column("document_type_name", String(100)),
    Column("document_type_use", Integer, index=True),
    Column("client_id", Integer, nullable=True),
    Column("total_amount", Float),
    Column("net_amount", Float),
    Column("tax_amount", Float),
    Column("state", Integer),
    Column("raw", JSONB),
)

variants_snapshot = Table(
    "variants_snapshot",
    metadata,
    Column("snapshot_date", DateTime(timezone=True), primary_key=True),
    Column("variant_id", Integer, primary_key=True),
    Column("product_id", Integer),
    Column("code", String(100)),
    Column("barcode", String(100)),
    Column("description", String(500)),
    Column("state", Integer),
    Column("raw", JSONB),
)

stock_snapshot = Table(
    "stock_snapshot",
    metadata,
    Column("snapshot_date", DateTime(timezone=True), primary_key=True),
    Column("variant_id", Integer, primary_key=True),
    Column("office_id", Integer, primary_key=True),
    Column("quantity", Float),
    Column("variant_code", String(100)),
    Column("office_name", String(200)),
)

# Line items de cada documento (1 row por linea de doc).
# Esta tabla es la que permite calcular velocity, top sellers, allocation, etc.
document_details_snapshot = Table(
    "document_details_snapshot",
    metadata,
    Column("document_id", Integer, primary_key=True),
    Column("line_id", Integer, primary_key=True),
    Column("variant_id", Integer, index=True, nullable=True),
    Column("variant_code", String(100), index=True, nullable=True),
    Column("variant_description", String(500), nullable=True),
    Column("office_id", Integer, index=True, nullable=True),
    Column("emission_date", DateTime(timezone=True), index=True),
    Column("document_type_use", Integer, index=True),  # 0=venta, 1=NC, 2=guia
    Column("quantity", Float),
    Column("net_amount", Float),
    Column("total_amount", Float),
    Column("fetched_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
)

sku_mapping = Table(
    "sku_mapping",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("bsale_variant_id", Integer, index=True, nullable=True),
    Column("bsale_code", String(100), index=True, nullable=True),
    Column("shopify_variant_id", String(50), index=True, nullable=True),
    Column("shopify_sku", String(100), index=True, nullable=True),
    Column("shopify_product_id", String(50), nullable=True),
    Column("ml_item_id", String(50), index=True, nullable=True),
    Column("ml_variation_id", String(50), nullable=True),
    Column("notes", Text, nullable=True),
    Column("confidence", Float, default=1.0),
    Column("source", String(50), default="manual"),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
    Column("updated_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
)

mapping_audit = Table(
    "mapping_audit",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
    Column("mapping_id", Integer),
    Column("action", String(20)),  # created, updated, deleted
    Column("before", JSONB, nullable=True),
    Column("after", JSONB, nullable=True),
    Column("actor", String(100)),
)


# ============================
# Helpers de agregacion
# ============================

def official_sale_conditions(tbl, sales_note_ids=None) -> list:
    """Condiciones para quedarse solo con VENTA OFICIAL en una tabla snapshot.

    Venta oficial = Boletas + Facturas + Notas de Debito - Notas de Credito.
    Excluye: guias de despacho (use=2), notas de venta / pedidos web /
    cotizaciones (document_type_id en la lista de isSalesNote) y anulados
    (state != 0).

    Degrada con elegancia: si la tabla no tiene la columna (por ejemplo
    document_details_snapshot no guarda document_type_id ni state), esa
    condicion simplemente no se aplica. Los tools que dependan de eso deben
    declararlo en su respuesta.
    """
    from sqlalchemy import or_

    conds = []
    cols = tbl.c
    if "document_type_use" in cols:
        conds.append(cols.document_type_use != 2)
    if "document_type_id" in cols:
        if sales_note_ids is None:
            from bsale_client import sales_note_type_ids

            sales_note_ids = sales_note_type_ids()
        ids = list(sales_note_ids)
        if ids:
            conds.append(
                or_(cols.document_type_id.is_(None), cols.document_type_id.notin_(ids))
            )
    if "state" in cols:
        conds.append(or_(cols.state.is_(None), cols.state == 0))
    return conds


def official_sale_supported(tbl) -> dict:
    """Que partes de la regla de venta oficial puede aplicar esta tabla."""
    cols = tbl.c
    return {
        "excluye_guias": "document_type_use" in cols,
        "excluye_notas_de_venta": "document_type_id" in cols,
        "excluye_anulados": "state" in cols,
    }


def signed_amount(amount_col, use_col):
    """Monto con notas de credito (use=1) en negativo; el resto suma.

    Usar en todos los SUM de venta para obtener el neto correcto.
    """
    return case((use_col == 1, -amount_col), else_=amount_col)


# ============================
# Engine
# ============================

_engine: Engine | None = None
_SessionMaker = None


def get_engine() -> Engine:
    """Lazy init del engine."""
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL no esta configurado")
        # Render Postgres viene con postgres:// pero SQLAlchemy quiere postgresql://
        url = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        _engine = create_engine(
            url,
            pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
            pool_pre_ping=True,
        )
    return _engine


def get_session_maker():
    """Devuelve el sessionmaker."""
    global _SessionMaker
    if _SessionMaker is None:
        _SessionMaker = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionMaker


@contextmanager
def session() -> Iterator[Session]:
    """Context manager de session con commit/rollback."""
    sm = get_session_maker()
    s = sm()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    """Crea las tablas si no existen. Idempotente."""
    if not DATABASE_URL:
        logger.info("init_db skipped: DATABASE_URL no configurado")
        return
    engine = get_engine()
    metadata.create_all(engine)
    logger.info("DB schema inicializado")


def db_health() -> dict[str, Any]:
    """Diagnostico de la DB para healthcheck."""
    if not DATABASE_URL:
        return {"status": "not_configured"}
    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1 as ok"))
            ok = result.scalar() == 1
        return {"status": "ok" if ok else "degraded", "reachable": ok}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)[:200]}


def snapshot_lag_hours() -> float | None:
    """Horas desde el ultimo snapshot de documents. None si no hay data/DB.

    Sirve para que /health marque 'degraded' si el snapshot quedo viejo.
    """
    if not DATABASE_URL:
        return None
    try:
        engine = get_engine()
        with engine.connect() as conn:
            ts = conn.execute(
                text("SELECT max(snapshot_date) FROM documents_snapshot")
            ).scalar()
        if not ts:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None
