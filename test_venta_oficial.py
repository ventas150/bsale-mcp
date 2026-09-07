"""Regresiones de la venta oficial y del paginado.

Cubren los cuatro bugs encontrados el 07-sep-2026:
  1. Notas de credito sumadas en positivo en bsale_ventas_fast.
  2. Totales calculados sobre la pagina devuelta y no sobre el periodo.
  3. total_documents contando guias que despues se excluian del desglose.
  4. Paginado que cortaba en max_pages y devolvia el parcial sin avisar.

No necesitan red ni base de datos.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("BSALE_ACCESS_TOKEN", "test-token")

from bsale_client import (  # noqa: E402
    BsaleClient,
    doc_revenue_signed,
    is_official_sale,
    is_sales_note,
)


def _doc(type_id, use, is_sales_note_flag, amount, state=0):
    return {
        "id": type_id * 1000 + amount,
        "totalAmount": amount,
        "state": state,
        "document_type": {"id": type_id, "use": use, "isSalesNote": is_sales_note_flag},
    }


BOLETA = _doc(1, 0, 0, 100)
FACTURA = _doc(6, 0, 0, 200)
NOTA_CREDITO = _doc(9, 1, 0, 50)
NOTA_DEBITO = _doc(18, 4, 0, 10)
GUIA = _doc(8, 2, 0, 999)
NOTA_VENTA = _doc(3, 0, 1, 777)
PEDIDO_WEB = _doc(26, 0, 1, 555)
ANULADA = _doc(1, 0, 0, 300, state=1)


def test_nota_de_credito_resta():
    assert doc_revenue_signed(NOTA_CREDITO) == -50
    assert doc_revenue_signed(BOLETA) == 100


def test_notas_de_venta_no_son_venta():
    assert is_sales_note(NOTA_VENTA)
    assert is_sales_note(PEDIDO_WEB)
    assert not is_sales_note(BOLETA)
    assert not is_official_sale(NOTA_VENTA)
    assert not is_official_sale(PEDIDO_WEB)


def test_guias_y_anulados_fuera():
    assert not is_official_sale(GUIA)
    assert not is_official_sale(ANULADA)


def test_venta_oficial_es_bol_fac_nd_menos_nc():
    universo = [BOLETA, FACTURA, NOTA_CREDITO, NOTA_DEBITO, GUIA, NOTA_VENTA, PEDIDO_WEB, ANULADA]
    total = sum(doc_revenue_signed(d) for d in universo if is_official_sale(d))
    assert total == 100 + 200 - 50 + 10  # 260


def _client_con_paginas(total: int, limit: int = 50) -> BsaleClient:
    c = BsaleClient()
    calls = []

    def fake_get(path, params=None, use_cache=False):  # noqa: ANN001
        params = params or {}
        off = int(params.get("offset", 0))
        lim = int(params.get("limit", limit))
        calls.append(off)
        items = [{"id": i} for i in range(off, min(off + lim, total))]
        return {"count": total, "items": items}

    c.get = fake_get  # type: ignore[method-assign]
    c._calls = calls  # type: ignore[attr-defined]
    return c


def test_paginado_trae_todo_y_no_miente():
    c = _client_con_paginas(total=396)
    r = c.paginated_fetch("/v1/documents.json", params={"limit": 50}, max_items=40000)
    assert r["total_count"] == 396
    assert r["fetched"] == 396
    assert len(r["items"]) == 396
    assert r["truncated"] is False
    assert [d["id"] for d in r["items"]] == list(range(396))  # orden preservado


def test_paginado_declara_truncado():
    c = _client_con_paginas(total=6000)
    r = c.paginated_fetch("/v1/documents.json", params={"limit": 50}, max_items=2500)
    assert r["total_count"] == 6000
    assert r["fetched"] == 2500
    assert r["truncated"] is True, "un total parcial SIEMPRE tiene que declararse"


def test_paginado_una_sola_pagina():
    c = _client_con_paginas(total=12)
    r = c.paginated_fetch("/v1/documents.json", params={"limit": 50}, max_items=40000)
    assert r["fetched"] == 12 and r["pages"] == 1 and r["truncated"] is False


def test_paginated_get_sigue_funcionando():
    c = _client_con_paginas(total=130)
    items = c.paginated_get("/v1/documents.json", params={"limit": 50}, max_pages=2)
    assert len(items) == 100  # 2 paginas x 50, compatibilidad con el comportamiento viejo


# --- Candados de escritura (guardrails.py) ----------------------------------
import guardrails  # noqa: E402


def _reset_flags(price="0", listas=""):
    os.environ["BSALE_PRICE_WRITES_ENABLED"] = price
    os.environ["BSALE_WRITABLE_PRICE_LISTS"] = listas


def test_precios_bloqueados_por_default():
    _reset_flags()
    try:
        guardrails.guard_price_write(6)
    except guardrails.GuardrailError as e:
        assert "DESHABILITADA" in str(e)
        return
    raise AssertionError("La escritura de precios NO debe estar permitida por default")


def test_precios_exigen_allowlist():
    _reset_flags(price="1", listas="")
    try:
        guardrails.guard_price_write(6)
    except guardrails.GuardrailError as e:
        assert "allowlist" in str(e).lower() or "BSALE_WRITABLE" in str(e)
        return
    raise AssertionError("Sin allowlist no se debe poder escribir")


def test_precios_rechazan_lista_no_declarada():
    _reset_flags(price="1", listas="6")
    guardrails.guard_price_write(6)  # permitida
    try:
        guardrails.guard_price_write(1)  # otra lista
    except guardrails.GuardrailError:
        _reset_flags()
        return
    raise AssertionError("Una lista fuera de la allowlist debe rechazarse")


def test_precio_cero_o_negativo_aborta_todo():
    for malo in (0, -100):
        try:
            guardrails.validate_price_updates(
                [{"variant_id": 1, "new_price": 10000}, {"variant_id": 2, "new_price": malo}],
                current={1: 10000, 2: 9000},
            )
        except guardrails.GuardrailError as e:
            assert "no se escribio nada" in str(e).lower()
            continue
        raise AssertionError(f"precio {malo} debe abortar la operacion completa")


def test_bajada_grande_aborta():
    try:
        guardrails.validate_price_updates(
            [{"variant_id": 1, "new_price": 5000}], current={1: 10000}, max_delta_pct=5.0
        )
    except guardrails.GuardrailError as e:
        assert "superan" in str(e)
        return
    raise AssertionError("una bajada de 50% debe abortar con el umbral en 5%")


def test_sin_precio_anterior_no_se_escribe():
    try:
        guardrails.validate_price_updates([{"variant_id": 99, "new_price": 10000}], current={})
    except guardrails.GuardrailError as e:
        assert "revertir" in str(e)
        return
    raise AssertionError("sin precio anterior no hay rollback posible: debe abortar")


def test_confirm_token_es_de_un_solo_uso_y_atado_al_payload():
    payload = {"price_list_id": 6, "tabla": [{"variant_id": 1, "precio_nuevo": 10000}]}
    tok = guardrails.issue_confirm_token(payload)
    guardrails.consume_confirm_token(tok, payload)  # primera vez: pasa
    try:
        guardrails.consume_confirm_token(tok, payload)  # segunda: no
    except guardrails.GuardrailError:
        pass
    else:
        raise AssertionError("el confirm_token debe ser de un solo uso")

    tok2 = guardrails.issue_confirm_token(payload)
    otro = {"price_list_id": 6, "tabla": [{"variant_id": 1, "precio_nuevo": 999}]}
    try:
        guardrails.consume_confirm_token(tok2, otro)
    except guardrails.GuardrailError as e:
        assert "no corresponde" in str(e)
        return
    raise AssertionError("el token no debe validar un payload distinto al que lo genero")


def test_tope_de_cantidad_de_cambios():
    muchos = [{"variant_id": i, "new_price": 1000} for i in range(60)]
    try:
        guardrails.validate_price_updates(muchos, current={i: 1000 for i in range(60)})
    except guardrails.GuardrailError as e:
        assert "tope" in str(e)
        return
    raise AssertionError("60 cambios en una llamada debe superar el tope de 50")


# ============================================================
# Candado por URL (el conector de Cowork no soporta headers)
# ============================================================

class _Req:
    def __init__(self, headers=None, path="/mcp"):
        self.headers = headers or {}
        self.url = type("U", (), {"path": path})()


def _limpiar_candado(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_URL_SECRET", raising=False)


def test_sin_candado_todo_pasa(monkeypatch):
    import server
    _limpiar_candado(monkeypatch)
    assert not server._con_candado()
    assert server._mcp_path() == "/mcp"
    assert server._auth_ok(_Req())


def test_url_secreta_cambia_la_ruta_del_mcp(monkeypatch):
    import server
    _limpiar_candado(monkeypatch)
    monkeypatch.setenv("MCP_URL_SECRET", "abc123")
    assert server._mcp_path() == "/mcp/abc123"
    assert server._con_candado()


def test_el_secreto_de_la_url_sirve_como_bearer(monkeypatch):
    """Asi Roberto administra UNA variable y /audit sigue siendo alcanzable."""
    import server
    _limpiar_candado(monkeypatch)
    monkeypatch.setenv("MCP_URL_SECRET", "abc123")
    assert server._auth_ok(_Req({"authorization": "Bearer abc123"}))
    assert not server._auth_ok(_Req({"authorization": "Bearer otro"}))
    assert not server._auth_ok(_Req())


def test_ambas_credenciales_conviven(monkeypatch):
    import server
    _limpiar_candado(monkeypatch)
    monkeypatch.setenv("MCP_URL_SECRET", "por-url")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "por-header")
    assert server._auth_ok(_Req({"authorization": "Bearer por-url"}))
    assert server._auth_ok(_Req({"authorization": "Bearer por-header"}))
    assert not server._auth_ok(_Req({"authorization": "Bearer nada"}))


def test_health_nunca_se_bloquea(monkeypatch):
    """Render usa /health para el deploy: si lo cerramos, hace rollback."""
    import asyncio
    import server
    _limpiar_candado(monkeypatch)
    monkeypatch.setenv("MCP_URL_SECRET", "abc123")
    mw = server.BearerAuthMiddleware(app=None)

    async def _next(req):
        return "PASO"

    assert asyncio.run(mw.dispatch(_Req(path="/health"), _next)) == "PASO"


def test_la_ruta_secreta_del_mcp_pasa_sin_header(monkeypatch):
    import asyncio
    import server
    _limpiar_candado(monkeypatch)
    monkeypatch.setenv("MCP_URL_SECRET", "abc123")
    mw = server.BearerAuthMiddleware(app=None)

    async def _next(req):
        return "PASO"

    assert asyncio.run(mw.dispatch(_Req(path="/mcp/abc123"), _next)) == "PASO"
    assert asyncio.run(mw.dispatch(_Req(path="/mcp/abc123/messages"), _next)) == "PASO"


def test_la_ruta_vieja_y_una_adivinada_no_pasan(monkeypatch):
    """El punto entero: /mcp a secas y /mcp/<otro> tienen que quedar fuera."""
    import asyncio
    import server
    _limpiar_candado(monkeypatch)
    monkeypatch.setenv("MCP_URL_SECRET", "abc123")
    mw = server.BearerAuthMiddleware(app=None)

    async def _next(req):
        return "PASO"

    for ruta in ("/mcp", "/mcp/adivinado", "/audit"):
        r = asyncio.run(mw.dispatch(_Req(path=ruta), _next))
        assert r != "PASO", f"{ruta} no deberia pasar sin credencial"
        assert getattr(r, "status_code", None) == 401


def test_health_no_filtra_el_secreto(monkeypatch):
    import server
    _limpiar_candado(monkeypatch)
    monkeypatch.setenv("MCP_URL_SECRET", "secreto-que-no-debe-salir")
    assert "secreto-que-no-debe-salir" not in server._describir_auth()
    assert server._describir_auth() == "url-secreta"


# ============================================================
# Red de seguridad contra el bug del 07-sep-2026
# ============================================================
# `bsale_listar_documentos` se desplego usando cinco nombres que nunca se
# importaron. No exploto al importar el modulo ni al registrar los tools:
# un nombre faltante dentro de una funcion solo falla cuando la funcion se
# LLAMA. El smoke test de ese dia solo importaba y listaba, asi que paso
# limpio y el tool quedo caido 8 horas en produccion.
#
# pyflakes lo detecta en dos segundos. Este test existe para que ese chequeo
# no dependa de que alguien se acuerde de correrlo.

def test_no_hay_nombres_indefinidos_en_el_repo():
    import pathlib
    pyflakes_api = pytest.importorskip("pyflakes.api")
    from pyflakes import reporter as pyflakes_reporter
    import io

    raiz = pathlib.Path(__file__).parent
    archivos = sorted(
        p for p in raiz.glob("*.py")
        if not p.name.startswith("_")
    )
    assert archivos, "no se encontraron modulos que analizar"

    salida, errores = io.StringIO(), io.StringIO()
    rep = pyflakes_reporter.Reporter(salida, errores)
    for f in archivos:
        pyflakes_api.checkPath(str(f), reporter=rep)

    indefinidos = [
        linea for linea in salida.getvalue().splitlines()
        if "undefined name" in linea
    ]
    assert not indefinidos, (
        "Hay nombres usados sin importar/definir. Esto NO lo agarra un "
        "smoke test de imports:\n  " + "\n  ".join(indefinidos)
    )
