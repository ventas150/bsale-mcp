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

documents_snapshot = Table(
    "documents_snapshot",
    metadata,
    Column("snapshot_date", DateTime(timezone=True), primary_key=True),
    Column("document_id", Integer, primary_key=True),
    Column("emission_date", DateTime(timezone=True)),
    Column("office_id", Integer),
    Column("office_name", String(200)),
    Column("document_type_id", Integer),
    Column("document_type_name", String(100)),
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
