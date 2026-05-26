"""Tools de mapping SKU cross-source: Bsale <-> Shopify <-> MercadoLibre.

Permite que un agente busque la misma variante en los 3 sistemas para:
- Allocation (qué stock va a web vs tiendas)
- Reconciliacion (ventas Shopify vs facturas Bsale)
- Pricing strategy (mismo SKU en 3 canales con precios distintos)

Tabla `sku_mapping` en Postgres es la fuente de verdad.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from db import mapping_audit, session as db_session, sku_mapping


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de mapping."""

    @mcp.tool()
    def bsale_mapping_buscar(
        bsale_code: str | None = None,
        bsale_variant_id: int | None = None,
        shopify_sku: str | None = None,
        shopify_variant_id: str | None = None,
        ml_item_id: str | None = None,
    ) -> dict[str, Any]:
        """Busca un mapping por cualquier identificador. Devuelve los 3 IDs cross-source.

        Pasa al menos uno de los args. Si pasa varios, busca matches en cualquiera.
        """
        with db_session() as s:
            conditions = []
            if bsale_code:
                conditions.append(sku_mapping.c.bsale_code == bsale_code)
            if bsale_variant_id:
                conditions.append(sku_mapping.c.bsale_variant_id == bsale_variant_id)
            if shopify_sku:
                conditions.append(sku_mapping.c.shopify_sku == shopify_sku)
            if shopify_variant_id:
                conditions.append(sku_mapping.c.shopify_variant_id == shopify_variant_id)
            if ml_item_id:
                conditions.append(sku_mapping.c.ml_item_id == ml_item_id)

            if not conditions:
                return {"error": "Pasa al menos un identificador para buscar"}

            stmt = select(sku_mapping).where(or_(*conditions))
            rows = s.execute(stmt).fetchall()

        results = [dict(r._mapping) for r in rows]
        return {"count": len(results), "matches": results}

    @mcp.tool()
    def bsale_mapping_crear(
        bsale_code: str | None = None,
        bsale_variant_id: int | None = None,
        shopify_sku: str | None = None,
        shopify_variant_id: str | None = None,
        shopify_product_id: str | None = None,
        ml_item_id: str | None = None,
        ml_variation_id: str | None = None,
        notes: str | None = None,
        confidence: float = 1.0,
        source: str = "manual",
    ) -> dict[str, Any]:
        """Crea un mapping cross-source nuevo. WRITE OP (a DB local).

        confidence: 0-1, indica que tan seguro estamos del match
        (1.0 = match exacto manual, 0.7 = match automatico por SKU).

        source: 'manual', 'auto_sku_match', 'auto_barcode_match', 'csv_import'.
        """
        row = {
            "bsale_code": bsale_code,
            "bsale_variant_id": bsale_variant_id,
            "shopify_sku": shopify_sku,
            "shopify_variant_id": shopify_variant_id,
            "shopify_product_id": shopify_product_id,
            "ml_item_id": ml_item_id,
            "ml_variation_id": ml_variation_id,
            "notes": notes,
            "confidence": confidence,
            "source": source,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        with db_session() as s:
            result = s.execute(sku_mapping.insert().values(row).returning(sku_mapping.c.id))
            new_id = result.scalar()

            # Audit
            s.execute(mapping_audit.insert().values(
                mapping_id=new_id,
                action="created",
                after=row,
                actor="mcp",
            ))

        return {"mapping_id": new_id, "status": "created", "row": row}

    @mcp.tool()
    def bsale_mapping_actualizar(
        mapping_id: int,
        shopify_sku: str | None = None,
        shopify_variant_id: str | None = None,
        ml_item_id: str | None = None,
        ml_variation_id: str | None = None,
        notes: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """Actualiza un mapping existente. WRITE OP (a DB local)."""
        updates: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if shopify_sku is not None:
            updates["shopify_sku"] = shopify_sku
        if shopify_variant_id is not None:
            updates["shopify_variant_id"] = shopify_variant_id
        if ml_item_id is not None:
            updates["ml_item_id"] = ml_item_id
        if ml_variation_id is not None:
            updates["ml_variation_id"] = ml_variation_id
        if notes is not None:
            updates["notes"] = notes
        if confidence is not None:
            updates["confidence"] = confidence

        with db_session() as s:
            # Snapshot before para audit
            before_row = s.execute(
                select(sku_mapping).where(sku_mapping.c.id == mapping_id)
            ).first()
            if not before_row:
                return {"error": f"Mapping {mapping_id} no existe"}
            before = dict(before_row._mapping)

            s.execute(
                sku_mapping.update().where(sku_mapping.c.id == mapping_id).values(**updates)
            )

            s.execute(mapping_audit.insert().values(
                mapping_id=mapping_id,
                action="updated",
                before=before,
                after={**before, **updates},
                actor="mcp",
            ))

        return {"mapping_id": mapping_id, "status": "updated", "updates": updates}

    @mcp.tool()
    def bsale_mapping_auto_match_sku() -> dict[str, Any]:
        """Intenta auto-matchear variantes Bsale con SKUs Shopify por `code`.

        Para funcionar necesita que el snapshot de variantes Bsale este corrido,
        y que tengas un import previo de SKUs Shopify (TODO: futuro tool).

        Por ahora devuelve un placeholder. Se implementa cuando Shopify MCP
        este conectado y podamos hacer cross-join real.
        """
        return {
            "status": "not_implemented",
            "reason": "Requiere Shopify connector con SKU list",
            "next_step": "Conectar Shopify MCP y armar shopify_skus_snapshot table",
        }

    @mcp.tool()
    def bsale_mapping_listar(
        limit: int = 50,
        only_complete: bool = False,
    ) -> dict[str, Any]:
        """Lista mappings existentes.

        Args:
            limit: Max rows a retornar.
            only_complete: Si True, solo devuelve mappings con los 3 IDs (Bsale + Shopify + ML).
        """
        with db_session() as s:
            stmt = select(sku_mapping)
            if only_complete:
                stmt = stmt.where(
                    sku_mapping.c.bsale_variant_id.isnot(None),
                    sku_mapping.c.shopify_variant_id.isnot(None),
                    sku_mapping.c.ml_item_id.isnot(None),
                )
            stmt = stmt.limit(limit)
            rows = s.execute(stmt).fetchall()

        return {
            "count": len(rows),
            "mappings": [dict(r._mapping) for r in rows],
        }
