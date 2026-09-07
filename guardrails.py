"""Candados para las operaciones de escritura.

Regla permanente de MyScrubs: ningun agente cambia precios en Bsale, ni para
subir ni para bajar, sin aprobacion explicita de Roberto fuera de la corrida.
Hasta el 07-sep-2026 esa regla vivia solo en los prompts; aca pasa a ser codigo.

Tres capas, de afuera hacia adentro:
  1. Kill-switch por variable de entorno (BSALE_PRICE_WRITES_ENABLED).
  2. Allowlist de listas de precio escribibles (BSALE_WRITABLE_PRICE_LISTS).
  3. dry_run por default + confirm_token de un solo uso + guardrail de magnitud.

Cualquiera de las tres que no se cumpla aborta la operacion COMPLETA. Nunca
parcial: una escritura de precios a medias es peor que ninguna.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any

# Ventana de vida de un confirm_token. Corta a proposito: el token es para
# confirmar lo que se acaba de ver, no para guardarlo y usarlo manana.
CONFIRM_TTL_SECONDS = 300

_tokens: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


class GuardrailError(Exception):
    """La operacion fue bloqueada por un candado. No se escribio nada."""


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def price_writes_enabled() -> bool:
    """Kill-switch global de escritura de precios. Apagado por default."""
    return _flag("BSALE_PRICE_WRITES_ENABLED", "0")


def stock_writes_enabled() -> bool:
    """Kill-switch de escritura de stock. Encendido por default (es operativo)."""
    return _flag("BSALE_STOCK_WRITES_ENABLED", "1")


def writable_price_lists() -> set[int]:
    """Listas de precio en las que se permite escribir. Vacio = ninguna."""
    raw = os.getenv("BSALE_WRITABLE_PRICE_LISTS", "").strip()
    if not raw:
        return set()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _fingerprint(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def issue_confirm_token(payload: Any) -> str:
    """Emite un token atado a ESTE payload exacto. Un solo uso, 5 minutos."""
    token = secrets.token_urlsafe(12)
    now = time.time()
    with _lock:
        # limpieza oportunista de los vencidos
        for t, (_, exp) in list(_tokens.items()):
            if exp < now:
                _tokens.pop(t, None)
        _tokens[token] = (_fingerprint(payload), now + CONFIRM_TTL_SECONDS)
    return token


def consume_confirm_token(token: str | None, payload: Any) -> None:
    """Valida y quema el token. Lanza GuardrailError si no calza."""
    if not token:
        raise GuardrailError(
            "Falta confirm_token. Corre primero con dry_run=True, revisa la tabla "
            "de cambios y vuelve a llamar con el token que devuelve."
        )
    with _lock:
        entry = _tokens.pop(token, None)
    if entry is None:
        raise GuardrailError("confirm_token invalido o ya usado. Repite el dry_run.")
    fingerprint, expires = entry
    if time.time() > expires:
        raise GuardrailError(
            f"confirm_token vencido (dura {CONFIRM_TTL_SECONDS}s). Repite el dry_run."
        )
    if fingerprint != _fingerprint(payload):
        raise GuardrailError(
            "El confirm_token no corresponde a estos cambios. Cambio algo entre el "
            "dry_run y la confirmacion: repite el dry_run y revisa la tabla nueva."
        )


def validate_price_updates(
    updates: list[dict[str, Any]],
    current: dict[int, float] | None = None,
    max_delta_pct: float = 5.0,
    max_items: int = 50,
) -> list[dict[str, Any]]:
    """Valida una lista de cambios de precio y devuelve la tabla de cambios.

    Lanza GuardrailError ante el primer problema estructural. Los precios que
    superan el delta permitido se devuelven marcados; el llamador decide.
    """
    if not updates:
        raise GuardrailError("La lista de cambios viene vacia.")
    if len(updates) > max_items:
        raise GuardrailError(
            f"{len(updates)} cambios de precio en una sola llamada supera el tope de "
            f"{max_items}. Partelo en lotes y revisa cada uno."
        )

    current = current or {}
    tabla = []
    problemas = []
    for i, u in enumerate(updates):
        if not isinstance(u, dict):
            problemas.append(f"item {i}: se esperaba un dict con variant_id y new_price")
            continue
        vid = u.get("variant_id")
        price = u.get("new_price")
        if vid is None or price is None:
            problemas.append(
                f"item {i}: faltan claves. Se esperan exactamente 'variant_id' y "
                f"'new_price'; llegaron {sorted(u.keys())}"
            )
            continue
        try:
            vid = int(vid)
            price = float(price)
        except (TypeError, ValueError):
            problemas.append(f"item {i}: variant_id o new_price no son numericos ({u!r})")
            continue
        if price <= 0:
            problemas.append(f"variante {vid}: precio {price} <= 0. Nunca se escribe.")
            continue

        antes = current.get(vid)
        delta_pct = None
        if antes:
            delta_pct = round((price - antes) / antes * 100, 2)
        fila = {
            "variant_id": vid,
            "precio_actual": antes,
            "precio_nuevo": price,
            "delta_pct": delta_pct,
            "excede_umbral": bool(delta_pct is not None and abs(delta_pct) > max_delta_pct),
        }
        tabla.append(fila)

    if problemas:
        raise GuardrailError("No se escribio nada. Problemas: " + " | ".join(problemas))

    excedidos = [f for f in tabla if f["excede_umbral"]]
    if excedidos:
        detalle = ", ".join(
            f"variante {f['variant_id']}: {f['delta_pct']}%" for f in excedidos[:10]
        )
        raise GuardrailError(
            f"{len(excedidos)} cambio(s) superan el {max_delta_pct}% permitido ({detalle}). "
            f"No se escribio nada. Si el cambio es intencional, sube max_delta_pct "
            f"explicitamente en la llamada."
        )

    sin_precio_actual = [f["variant_id"] for f in tabla if f["precio_actual"] is None]
    if sin_precio_actual:
        raise GuardrailError(
            f"No se pudo leer el precio actual de {len(sin_precio_actual)} variante(s) "
            f"({sin_precio_actual[:10]}). Sin precio anterior no hay como revertir: "
            f"no se escribio nada."
        )
    return tabla


def guard_price_write(price_list_id: int) -> None:
    """Candados 1 y 2. Lanza GuardrailError si la escritura no esta permitida."""
    if not price_writes_enabled():
        raise GuardrailError(
            "Escritura de precios DESHABILITADA por politica (BSALE_PRICE_WRITES_ENABLED=0). "
            "Regla de MyScrubs: los precios no los cambia un agente. Si Roberto autoriza "
            "un cambio puntual, se habilita la variable en Render, se aplica y se vuelve a apagar."
        )
    permitidas = writable_price_lists()
    if not permitidas:
        raise GuardrailError(
            "No hay ninguna lista de precios habilitada para escritura "
            "(BSALE_WRITABLE_PRICE_LISTS vacia). Declara explicitamente cual."
        )
    if int(price_list_id) not in permitidas:
        raise GuardrailError(
            f"La lista de precios {price_list_id} no esta en la allowlist "
            f"(permitidas: {sorted(permitidas)}). Escribir en la lista equivocada "
            f"—mayorista, costos— no da error en Bsale: por eso el candado."
        )


def guard_stock_write() -> None:
    if not stock_writes_enabled():
        raise GuardrailError(
            "Escritura de stock DESHABILITADA por politica (BSALE_STOCK_WRITES_ENABLED=0)."
        )
