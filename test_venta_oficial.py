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

# Variables que los tests tocan. Se restauran despues de CADA test para que
# ninguno dependa del orden: _reset_flags escribia os.environ y no restauraba,
# asi que test_precios_exigen_allowlist dejaba PRICE=1 hasta que otro lo pisara.
_ENV_QUE_TOCAN_LOS_TESTS = (
    "BSALE_PRICE_WRITES_ENABLED", "BSALE_WRITABLE_PRICE_LISTS",
    "BSALE_STOCK_WRITES_ENABLED", "BSALE_CATALOG_WRITES_ENABLED",
    "BSALE_SKU_WRITES_ENABLED", "BSALE_IVA_PCT", "MCP_URL_SECRET",
    "MCP_AUTH_TOKEN", "DATABASE_URL", "CACHE_DIR", "AUDIT_DIR",
)


@pytest.fixture(autouse=True)
def _entorno_limpio():
    """Cada test arranca sin DATABASE_URL y con las variables de arriba como
    estaban al terminar.

    DATABASE_URL: bsale_client hace load_dotenv() al importar. En una maquina
    con .env (un shell de Render, un dev que copie .env.example), el test de
    rotacion del audit escribia 400 eventos basura en el audit_log de
    PRODUCCION (audit.py va a Postgres primero) y recien despues fallaba. En
    el PC no hay .env, por eso no se vio.
    """
    import sys

    antes = {k: os.environ.get(k) for k in _ENV_QUE_TOCAN_LOS_TESTS}
    os.environ.pop("DATABASE_URL", None)
    # db.DATABASE_URL se lee al importar: si ya esta importado, apagarlo ahi
    # tambien, que es lo que audit._escribir_en_postgres consulta.
    db_mod = sys.modules.get("db")
    db_url_antes = getattr(db_mod, "DATABASE_URL", None) if db_mod else None
    if db_mod is not None:
        db_mod.DATABASE_URL = None
    try:
        yield
    finally:
        for k, v in antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if db_mod is not None:
            db_mod.DATABASE_URL = db_url_antes


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


def test_sin_candado_nada_pasa(monkeypatch):
    """Hasta el 09-sep-2026 este test se llamaba test_sin_candado_todo_pasa y
    afirmaba lo contrario: sin MCP_URL_SECRET ni MCP_AUTH_TOKEN, _auth_ok
    devolvia True y el ERP quedaba escribible desde internet. Las dos
    variables son sync:false en el blueprint. Ahora falla cerrado, y main()
    ni siquiera arranca (ver test_main_no_arranca_sin_credencial)."""
    import server
    _limpiar_candado(monkeypatch)
    assert not server._con_candado()
    assert server._mcp_path() == "/mcp"
    assert server._auth_ok(_Req()) is False
    assert server._auth_ok(_Req(headers={"authorization": "Bearer lo-que-sea"})) is False


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


# ============================================================
# Los queries tienen que COMPILAR, no solo importar
# ============================================================
# bsale_venta_por_sucursal se desplego llamando signed_amount(tabla) en vez de
# signed_amount(columna, columna). Import OK, registro OK, pyflakes OK: el
# TypeError solo aparecio al ejecutar el tool contra Postgres, en produccion.
#
# Compilar el statement a SQL no necesita base ni red y habria reventado en el
# acto. Por eso los queries viven en funciones aparte, fuera del closure del
# tool: para poder compilarlos aca.

def _compilar(stmt):
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def test_los_queries_de_venta_por_sucursal_compilan(monkeypatch):
    """Compilar NO debe tocar la red.

    official_sale_conditions resuelve los ids de nota de venta llamando a
    Bsale. En un test eso reventaba con 401 y tapaba lo que se queria probar,
    asi que aca se fija el fallback conocido (3, 23, 24, 26, 27).
    """
    from datetime import datetime, timezone

    import bsale_client

    monkeypatch.setattr(
        bsale_client, "sales_note_type_ids", lambda: frozenset({3, 23, 24, 26, 27})
    )
    tidb = pytest.importorskip("tools_intelligence_db")
    desde = datetime(2026, 8, 1, tzinfo=timezone.utc)
    hasta = datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc)

    sql_cab = _compilar(tidb._stmt_cabecera_por_sucursal(desde, hasta))
    assert "documents_snapshot" in sql_cab
    assert "office_id" in sql_cab
    # las NC tienen que restar: el CASE de signed_amount
    assert "CASE" in sql_cab.upper()

    sql_det = _compilar(tidb._stmt_unidades_por_sucursal(desde, hasta))
    assert "document_details_snapshot" in sql_det
    assert "quantity" in sql_det
    assert "CASE" in sql_det.upper()
    # y tiene que heredar la regla de venta oficial del documento cabecera
    assert "EXISTS" in sql_det.upper()


def test_rango_utc_cubre_el_dia_completo():
    tidb = pytest.importorskip("tools_intelligence_db")
    desde, hasta = tidb._rango_utc("2026-08-01", "2026-08-31")
    assert desde.day == 1 and desde.hour == 0
    assert hasta.day == 31 and (hasta.hour, hasta.minute) == (23, 59)


# ============================================================
# El cliente no se puede colgar contando errores
# ============================================================
# _bump se llamaba a si misma DENTRO de su propio `with self._stats_lock`.
# threading.Lock no es reentrante: el primer timeout / 429 / 5xx de Bsale
# colgaba el hilo para siempre CON el lock tomado, y de ahi todo hilo que
# tocara _bump se colgaba igual. Con el threadpool de uvicorn acotado, unos
# pocos errores de Bsale dejaban el servicio sin responder, /health incluido.
#
# No lo agarro ningun test porque solo se dispara con Bsale FALLANDO.

def test_bump_no_se_cuelga_y_no_pierde_incrementos():
    import threading

    from bsale_client import BsaleClient

    c = BsaleClient.__new__(BsaleClient)
    c._stats_lock = threading.Lock()
    c._total_requests = 0
    c._total_retries = 0
    c._last_error = None
    c._last_success_ts = None

    def machacar():
        for _ in range(200):
            c._bump("retries")
            c._bump("requests")

    hilos = [threading.Thread(target=machacar) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=10)

    vivos = [h for h in hilos if h.is_alive()]
    assert not vivos, f"{len(vivos)} hilos colgados: _bump volvio a bloquearse"
    # Exacto, no aproximado: si se pierden incrementos el lock no esta sirviendo
    assert c._total_retries == 1600
    assert c._total_requests == 1600
    assert not c._stats_lock.locked()


def test_bump_registra_error_y_lo_limpia_al_exito():
    import threading

    from bsale_client import BsaleClient

    c = BsaleClient.__new__(BsaleClient)
    c._stats_lock = threading.Lock()
    c._total_requests = 0
    c._total_retries = 0
    c._last_error = None
    c._last_success_ts = None

    c._bump("noop", error="RATE_LIMIT")
    assert c._last_error == "RATE_LIMIT"
    c._bump("noop", success=True)
    assert c._last_error is None
    assert c._last_success_ts is not None


# ============================================================
# La zona horaria va sobre now(), no sobre emission_date
# ============================================================
# Bsale entrega emissionDate como medianoche UTC exacta: es una FECHA
# disfrazada de timestamp. Convertirla a America/Santiago la corre un dia
# hacia atras, asi que el filtro "emitido hoy" no calzaba nunca y el digest
# ventas_hoy devolvia $0 todos los dias — con _generated_at fresco, que es
# lo que lo hacia creible.

def test_dia_hoy_no_convierte_emission_date_a_santiago():
    import digests

    sql = digests._dia_hoy()
    assert "emission_date AT TIME ZONE 'UTC'" in sql, (
        "emission_date debe quedar en UTC: es medianoche UTC, no una hora real"
    )
    assert f"emission_date AT TIME ZONE '{digests.TZ_NEGOCIO}'" not in sql, (
        "convertir emission_date a Santiago la corre un dia atras"
    )
    # y el 'hoy' del negocio SI tiene que estar en Santiago
    assert digests.TZ_NEGOCIO in sql
    assert digests._dia_hoy("d").startswith("(d.emission_date")


def test_un_documento_de_hoy_calza_con_hoy_en_chile():
    """Reproduce en Python lo que hace el SQL, para el caso que fallaba.

    Documento emitido hoy -> Bsale lo entrega como medianoche UTC de hoy.
    El filtro tiene que dar True mientras en Chile siga siendo hoy.
    """
    from datetime import datetime, time, timezone
    from zoneinfo import ZoneInfo

    scl = ZoneInfo("America/Santiago")
    hoy_chile = datetime.now(scl).date()

    # como lo guarda snapshot.py: medianoche UTC del dia de emision
    emitido = datetime.combine(hoy_chile, time.min, tzinfo=timezone.utc)

    # lo que hace el SQL corregido
    bien = emitido.astimezone(timezone.utc).date() == hoy_chile
    assert bien, "un documento emitido hoy tiene que contar como de hoy"

    # y lo que hacia el SQL roto
    mal = emitido.astimezone(scl).date() == hoy_chile
    assert not mal, (
        "si esto pasa, el offset de Chile cambio de signo y el test ya no "
        "prueba nada"
    )


# ============================================================
# Nada pesado puede correr dentro del web service
# ============================================================
# El 07-sep-2026 un backfill de 46.738 documentos dejo sin responder /health y
# Render reinicio la instancia. Se topo el backfill a 31 dias, pero
# bsale_snapshot_run_now seguia abierto — y su default, target="all", corre la
# nocturna COMPLETA (stock ~6.000 paginas, ~2 horas). Bastaba llamarlo sin
# argumentos para reproducir el incidente multiplicado.

def _tools_de(modulo):
    """Registra los tools de un modulo en un FastMCP limpio y los devuelve."""
    pytest.importorskip("fastmcp")
    registrados = {}

    class Espia:
        def tool(self, *a, **kw):
            def deco(fn):
                registrados[fn.__name__] = fn
                return fn
            return deco

    modulo.register(Espia())
    return registrados


def test_run_now_rechaza_los_targets_pesados():
    tools = _tools_de(pytest.importorskip("tools_snapshot"))
    run_now = tools["bsale_snapshot_run_now"]

    for target in ("all", "stock", "variants"):
        r = run_now(target=target)
        assert r.get("aplicado") is False, f"target={target} deberia rechazarse"
        assert "health" in str(r).lower()

    # El default era el caso peligroso: target="all" corria la nocturna
    # completa con solo llamar al tool sin argumentos. Ahora el default tiene
    # que ser un target liviano. No se invoca run_now() de verdad aca porque
    # eso pegaria contra Bsale y contra la base.
    import inspect

    default = inspect.signature(run_now).parameters["target"].default
    assert default not in ("all", "stock", "variants"), (
        f"el default de bsale_snapshot_run_now no puede ser pesado: {default}"
    )


def test_run_now_topa_la_ventana_de_documentos():
    tools = _tools_de(pytest.importorskip("tools_snapshot"))
    run_now = tools["bsale_snapshot_run_now"]
    r = run_now(target="documents", days_back=365)
    assert r.get("aplicado") is False
    assert "backfill_rango" in str(r)


def test_backfill_rango_topa_a_31_dias():
    tools = _tools_de(pytest.importorskip("tools_snapshot"))
    bf = tools["bsale_snapshot_backfill_rango"]
    r = bf(date_from="2025-01-01", date_to="2025-04-30")
    assert r.get("aplicado") is False
    assert "31" in str(r)


def test_el_cache_degrada_en_vez_de_tumbar_el_arranque(monkeypatch, tmp_path):
    """cache.py tenia el mismo mkdir sin fallback que ya se arreglo en audit.py.

    Sin fallback: OSError -> BsaleClient.__init__ falla -> todos los tools
    fallan -> /health devuelve 503 -> Render reinicia -> se repite.

    El fallo se simula parcheando mkdir, no pasando una ruta "imposible": la
    primera version de este test usaba /proc/..., que en Linux falla pero en
    Windows resuelve a C:\\proc\\... y se crea sin problema. El test pasaba en
    Render y fallaba en la maquina de Roberto, probando el sistema operativo en
    vez de la logica.
    """
    import pathlib

    import cache as cache_mod

    fallback = tmp_path / "fallback"
    monkeypatch.setattr(cache_mod.FileCache, "_FALLBACK", fallback)

    preferido = tmp_path / "disco-que-no-monto"
    mkdir_real = pathlib.Path.mkdir

    def mkdir_que_falla(self, *a, **kw):
        if self == preferido:
            raise OSError(30, "Read-only file system")
        return mkdir_real(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "mkdir", mkdir_que_falla)

    c = cache_mod.FileCache(cache_dir=str(preferido))
    assert c.cache_dir == fallback, "deberia degradar al fallback, no reventar"
    # y sigue siendo un cache usable, no un objeto a medio construir
    c.set("k", {"v": 1}, 900)
    assert c.get("k") == {"v": 1}
    assert hasattr(c, "_lock") and hasattr(c, "_data")


# ============================================================
# Lote de la auditoria: cada arreglo con su regresion
# ============================================================

def test_redact_censura_en_estructuras_anidadas():
    """Antes solo miraba el primer nivel, y los bodies de escritura son
    anidados. El audit va a stdout -> logs de Render -> Sentry."""
    import audit

    r = audit._redact(
        {"ok": 1, "body": {"token": "x", "items": [{"secret": "y", "n": 2}]}}
    )
    assert r["body"]["token"] == "***REDACTED***"
    assert r["body"]["items"][0]["secret"] == "***REDACTED***"
    assert r["body"]["items"][0]["n"] == 2
    assert r["ok"] == 1


def test_audit_rota_y_no_lee_el_archivo_entero(tmp_path, monkeypatch):
    import audit

    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "writes.jsonl")
    monkeypatch.setattr(audit, "MAX_BYTES", 2000)

    for i in range(400):
        audit.audit_log("POST", f"/v1/x/{i}.json", body={"n": i})

    # rotó: existe el .1 y el activo no crecio sin control
    assert (tmp_path / "writes.jsonl.1").exists()
    assert (tmp_path / "writes.jsonl").stat().st_size <= 2000 * 3

    ev = audit.read_recent(limit=5)
    assert len(ev) <= 5
    assert all("method" in e for e in ev)
    # limite absurdo: no debe reventar ni devolver todo
    assert len(audit.read_recent(limit=-5)) <= 1000


def test_tope_de_rango_rechaza_periodos_largos():
    ta = pytest.importorskip("tools_analytics")

    assert ta._tope_de_rango("2025-01-01", "2025-12-31", 92) is not None
    assert ta._tope_de_rango("2026-08-01", "2026-08-31", 92) is None
    # fechas invertidas
    malo = ta._tope_de_rango("2026-08-31", "2026-08-01", 92)
    assert malo is not None and "anterior" in str(malo)


def test_engine_tiene_timeouts_configurados():
    """Sin timeouts, una base que no responde a nivel de red cuelga el event
    loop de uvicorn y Render reinicia en bucle."""
    import inspect

    import db

    # _solo_codigo: el docstring de get_engine nombra "connect_timeout", asi
    # que sin filtrar el test pasaba con el parametro quitado.
    src = _solo_codigo(inspect.getsource(db.get_engine))
    for esperado in ("connect_timeout", "pool_timeout", "statement_timeout"):
        assert esperado in src, f"falta {esperado} en get_engine"
    # y la creacion tiene que estar bajo lock (era check-then-act)
    assert "with _engine_lock:" in src


def test_health_no_bloquea_el_event_loop():
    """Las llamadas sincronas a la base tienen que ir a un hilo con tope."""
    import inspect

    import server

    src = inspect.getsource(server.health_check)
    assert "_con_limite" in src, "health debe usar el wrapper con timeout"

    # OJO: hay que mirar el CODIGO, no los comentarios. El docstring de
    # health_check nombra las claves que se sacaron para explicar por que se
    # sacaron. Este test conservaba el toggle por linea que _solo_codigo
    # reemplazo por estar roto; ahora usa el mismo helper que el resto.
    codigo = _solo_codigo(src)

    # y no debe filtrar internals
    for prohibido in ("cache_file", "db_error", "escritura_precios"):
        assert prohibido not in codigo, f"/health no debe exponer {prohibido}"


# ============================================================
# Nada que vaya a una columna JSONB puede llevar datetime
# ============================================================
# mapping_audit.before y mapping_audit.after son JSONB. El driver los pasa
# por json.dumps, que no sabe serializar datetime. bsale_mapping_crear metia
# ahi la fila cruda (con created_at/updated_at) y bsale_mapping_actualizar el
# snapshot leido de la base (idem): las dos escrituras al audit reventaban al
# insertar. El tool fallaba entero, no solo el audit, porque van en la misma
# transaccion.

def test_lo_que_va_a_jsonb_no_lleva_datetime():
    import json
    from datetime import datetime, timezone

    tools_mapping = pytest.importorskip("tools_mapping")

    fila = {
        "bsale_code": "ABC-123",
        "confidence": 1.0,
        "created_at": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        "anidado": {"cuando": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        "lista": [datetime(2026, 1, 2, tzinfo=timezone.utc), "texto", 3],
        "nulo": None,
    }

    # la fila cruda es exactamente lo que reventaba
    with pytest.raises(TypeError):
        json.dumps(fila)

    limpia = tools_mapping._jsonable(fila)
    json.dumps(limpia)  # no debe tirar

    assert limpia["created_at"] == "2026-09-08T12:00:00+00:00"
    assert limpia["anidado"]["cuando"] == "2026-01-01T00:00:00+00:00"
    assert limpia["lista"][0] == "2026-01-02T00:00:00+00:00"
    assert limpia["lista"][1] == "texto"
    assert limpia["lista"][2] == 3
    assert limpia["nulo"] is None
    assert limpia["confidence"] == 1.0


def test_los_tools_de_mapping_pasan_todo_por_jsonable():
    """Que el helper exista no sirve si el tool no lo usa."""
    import inspect

    tools_mapping = pytest.importorskip("tools_mapping")
    src = inspect.getsource(tools_mapping.register)

    # ninguna insercion al audit puede pasar un valor crudo
    for crudo in ("after=row,", "before=before,", "after={**before, **updates},"):
        assert crudo not in src, f"{crudo} va crudo a una columna JSONB"

    assert "after=_jsonable(row)" in src
    assert "before=_jsonable(before)" in src


# ============================================================
# La escritura de precios usa el endpoint que existe
# ============================================================
# Bsale no tiene POST de detalles de lista de precio. Su documentacion: "NO
# existe un POST de lista de precio, debido a que las listas de precios
# comparten el total de productos de Bsale. Y solo se puede editar sus
# valores, con el verbo PUT". El codigo posteaba un lote a
# /v1/price_lists/{id}/details.json, que no existe: habria fallado DESPUES de
# consumir el confirm_token.

def test_precios_no_postean_al_endpoint_que_no_existe():
    import inspect

    tools_writes = pytest.importorskip("tools_writes")
    src = inspect.getsource(tools_writes.register)

    assert "/details.json\", json_body" not in src
    assert "client.post(" not in src.split("# PRECIOS")[-1], (
        "la escritura de precios no puede usar POST: Bsale solo documenta PUT"
    )
    assert "details/{detalle_id}.json" in src, "tiene que ir por PUT al detalle"


def test_precios_abortan_si_falta_el_id_del_detalle(monkeypatch):
    """Sin detail_id no hay forma de escribir; no se escribe nada a medias.

    Antes: `src.index("sin_detalle") < src.index("client.put(")` sobre el
    fuente crudo, y el primer "sin_detalle" era un COMENTARIO que esta antes
    de cualquier put. Pasaba con el chequeo movido despues del bucle. Ahora
    se ejecuta: dos variantes, una sin detalle, y ni un PUT."""
    tw = pytest.importorskip("tools_writes")

    class _Cli:
        def __init__(self):
            self.escrituras = []

        def get(self, path, params=None, **k):
            # La 424242 tiene precio pero Bsale no devuelve el id del detalle:
            # sin ese id no hay a que hacerle PUT.
            return {"items": [
                {"id": 111, "variantValue": "19990", "variant": {"id": 117733}},
                {"variantValue": "5000", "variant": {"id": 424242}},
            ]}

        def put(self, path, json_body=None, **k):
            self.escrituras.append((path, json_body))
            return {"id": 1}

    monkeypatch.setenv("BSALE_PRICE_WRITES_ENABLED", "1")
    monkeypatch.setenv("BSALE_WRITABLE_PRICE_LISTS", "5")
    cli = _Cli()
    monkeypatch.setattr(tw, "get_client", lambda: cli)
    m = _McpFalso()
    tw.register(m)
    tool = m.tools["bsale_actualizar_precios_masivo"]

    r = tool(price_list_id=5, updates=[
        {"variant_id": 117733, "new_price": 20500},
        {"variant_id": 424242, "new_price": 10000},   # sin detalle en la lista
    ])
    # _leer_detalles_actuales exige valor E id, asi que la variante sin id no
    # entra al dict y aborta el guardrail de precio actual; el de sin_detalle
    # queda como segunda barrera. Lo que importa: abortar ENTERO, sin tabla y
    # sin un solo PUT, y que el mensaje nombre a la variante.
    assert "bloqueado_por" in r and "424242" in r["bloqueado_por"]
    assert "tabla_de_cambios" not in r, "abortar ENTERO, no armar tabla parcial"
    assert cli.escrituras == []


# ============================================================
# El backfill de detalle de linea tiene que poder terminar
# ============================================================
# Medido el 08-sep-2026: ene-ago 2025 tenia 0% de detalle en 54.562
# documentos y ene-ago 2026 un 58,2%. La causa no era el volumen: el nocturno
# llamaba snapshot_details(only_recent_days=90), asi que nada anterior a 90
# dias se iba a completar NUNCA. Encima habia dos topes encadenados
# (todo[:max_docs] y despues todo[:batch_size]) donde mandaba el menor en
# silencio, y remaining_to_process se calculaba restando el tope, o sea daba
# 0 con 129.000 documentos pendientes.


def _solo_codigo(src: str) -> str:
    """Descarta docstrings y comentarios de un fuente.

    Estos tests buscan patrones prohibidos en el CODIGO, y los
    comentarios de este repo NOMBRAN el patron viejo para explicar por
    que se saco. Sin este filtro el test prueba el comentario en vez del
    codigo: ya paso dos veces (health_check y snapshot_details).

    La primera version era un toggle por linea sobre las comillas triples. Se
    desincronizaba con cualquier string multilinea cuya apertura no estuviera
    al principio de la linea, y a partir de ahi descartaba TODO el resto del
    archivo en silencio: los assert pasaban sobre texto vacio y no probaban
    nada. Verificado el 08-sep-2026 sobre tools_intelligence.py, donde tres
    asserts sobre codigo que SI estaba presente daban falso.

    Ahora se tokeniza y se blanquea por posicion, que es exacto.

    Segunda trampa, encontrada el 09-sep-2026: inspect.getsource de cualquier
    @mcp.tool viene INDENTADO (todos viven dentro de register()), ast.parse
    tiraba IndentationError, el except lo tragaba y los docstrings NO se
    borraban, sin aviso. Un test sobre un tool anidado probaba el docstring.
    Ahora se hace dedent y un fuente que no parsea hace FALLAR el test en vez
    de pasar sobre texto sin filtrar.
    """
    import ast
    import io
    import textwrap
    import tokenize

    src = textwrap.dedent(src)
    lineas = src.splitlines(keepends=True)
    borrar = []

    # Sin try/except: un fuente que no tokeniza o no parsea es un error del
    # test, no algo que se tapa. Antes se tragaba y el filtro no filtraba.
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            borrar.append((tok.start, tok.end))

    arbol = ast.parse(src)
    contenedores = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for n in ast.walk(arbol):
        if not isinstance(n, contenedores):
            continue
        cuerpo = getattr(n, "body", None)
        if (
            cuerpo
            and isinstance(cuerpo[0], ast.Expr)
            and isinstance(cuerpo[0].value, ast.Constant)
            and isinstance(cuerpo[0].value.value, str)
        ):
            c = cuerpo[0].value
            borrar.append(((c.lineno, c.col_offset), (c.end_lineno, c.end_col_offset)))

    # De atras hacia adelante, para que borrar no corra los offsets restantes.
    for (l1, c1), (l2, c2) in sorted(borrar, reverse=True):
        if l1 == l2:
            lineas[l1 - 1] = lineas[l1 - 1][:c1] + lineas[l1 - 1][c2:]
        else:
            lineas[l1 - 1] = lineas[l1 - 1][:c1] + "\n"
            for i in range(l1, l2 - 1):
                lineas[i] = "\n"
            lineas[l2 - 1] = lineas[l2 - 1][c2:]

    return "".join(lineas)


def test_snapshot_details_tiene_un_solo_tope():
    import inspect

    snapshot = pytest.importorskip("snapshot")
    codigo = _solo_codigo(inspect.getsource(snapshot.snapshot_details))

    # el segundo corte encadenado era el bug
    assert "todo[:batch_size]" not in codigo
    assert ".limit(cap)" in codigo, "el tope tiene que ir en el query"
    assert "cap_efectivo" in codigo, "el tope efectivo se declara en el resultado"

    # y lo que queda tiene que ser un count real, no una resta del tope
    assert "len(todo) - batch_size" not in codigo
    assert "pendientes_antes" in codigo


def test_snapshot_details_acepta_ventana_de_fechas():
    import inspect

    snapshot = pytest.importorskip("snapshot")
    params = inspect.signature(snapshot.snapshot_details).parameters
    for esperado in ("date_from", "date_to", "oldest_first"):
        assert esperado in params, f"falta el parametro {esperado}"


def test_dia_utc_es_medianoche_exacta():
    from datetime import datetime, timezone

    snapshot = pytest.importorskip("snapshot")
    d = snapshot._dia_utc("2025-03-31")
    assert d == datetime(2025, 3, 31, 0, 0, tzinfo=timezone.utc)


def test_details_batch_topa_el_lote():
    """Corre dentro del web service: un lote enorme lo deja sin /health."""
    tools = _tools_de(pytest.importorskip("tools_snapshot"))
    batch = tools["bsale_snapshot_details_batch"]
    r = batch(max_docs=50000)
    assert r.get("aplicado") is False
    # OJO: no buscar un numero suelto en el mensaje. El texto nombra
    # 2.500 y 4.000 como los tamanos que se pasaron del timeout del
    # cliente MCP, asi que assert "4.000" in str(r) pasaba aunque el
    # tope real fuera otro. El test probaba la anecdota, no el tope.
    assert "El tope es 2.000" in str(r)

    # y el tope tiene que ser real, no solo texto
    assert batch(max_docs=2001).get("aplicado") is False


# ============================================================
# La cobertura de detalle no puede pasar de 100%
# ============================================================
# El numerador contaba document_id distintos de document_details_snapshot
# filtrando solo por fecha y sucursal, sin la regla de venta oficial; el
# denominador si la aplicaba. Como los pedidos web, las notas de venta y los
# anulados tambien tienen lineas, el numerador incluia documentos que el
# denominador excluye. Medido el 08-sep-2026: 5.208 de 5.128 = 101,6%. Un
# porcentaje sobre 100 delata que se comparan dos poblaciones distintas, y
# hacia parecer completo un periodo al que le faltaba detalle.

def test_cobertura_compara_el_mismo_universo():
    import inspect

    tidb = pytest.importorskip("tools_intelligence_db")
    codigo = _solo_codigo(inspect.getsource(tidb.cobertura_de_detalle))

    # el numerador ya no puede salir de contar la tabla de lineas suelta
    assert "select_from(document_details_snapshot)" not in codigo, (
        "el numerador tiene que contar cabeceras que TIENEN detalle, no lineas"
    )
    # y tiene que aplicar los mismos filtros que el denominador
    assert "and_(*cond_doc, tiene_detalle)" in codigo
    assert "cond_det" not in codigo, "cond_det era el filtro paralelo que no calzaba"


# ============================================================
# Todo tool que salga de la velocity declara su cobertura
# ============================================================
# quiebres, proyeccion de compras, allocation y sobrestockeos salen del
# detalle de linea. Si al periodo le falta detalle, la velocity queda
# subestimada y devuelven MENOS riesgo del que hay, sin decirlo. Una lista
# vacia se nota; un riesgo subestimado no.

def test_los_tools_de_velocity_declaran_cobertura():
    import inspect

    tidb = pytest.importorskip("tools_intelligence_db")
    codigo = _solo_codigo(inspect.getsource(tidb.register))

    # los cuatro returns que salen de vel_rows tienen que traerla
    assert codigo.count('"cobertura_detalle": cobertura_ultimos_dias(') >= 4, (
        "falta declarar la cobertura en algun tool de velocity"
    )
    assert "def cobertura_ultimos_dias" not in codigo, (
        "el helper va a nivel de modulo, no dentro de register()"
    )


# ============================================================
# La retencion tiene que poder terminar, y avisar si no termina
# ============================================================
# Medido el 08-sep-2026: stock_snapshot con 7.535.095 filas creciendo 80.000
# por noche, con una politica que decia 30 dias (~94 fotos). En la MISMA
# corrida variants_snapshot quedaba en exactamente 2 snapshots, o sea que la
# retencion de variantes SI funcionaba. Mismo codigo, unica diferencia el
# tamano de la tabla: el DELETE de stock llevaba adentro un
# "NOT IN (SELECT max(...) GROUP BY ...)" que agrega la tabla entera en cada
# ejecucion, y no cabia en el statement_timeout de 20 s de db.py.

def test_el_stock_es_estado_actual_y_no_serie_de_tiempo():
    """La tabla vieja acumulaba una copia entera del inventario por corrida.

    Llego a 7.653.095 filas. Se reviso quien la leia: digests, sync_incremental
    y el status pedian SOLO la foto mas reciente, y quiebres/proyeccion/
    sobrestockeos leen stock en vivo de Bsale. O sea, historico que nadie
    consultaba, a costa de ~2 horas de nocturno por noche.
    """
    import inspect

    snapshot = pytest.importorskip("snapshot")
    codigo = _solo_codigo(inspect.getsource(snapshot.snapshot_stock))

    assert "pg_insert(stock_actual)" in codigo, "el stock va a la tabla de estado"
    assert "on_conflict_do_update" in codigo, "tiene que pisar, no acumular"
    assert "pg_insert(stock_snapshot)" not in codigo, "la tabla vieja quedo muerta"
    assert '"snapshot_date": snapshot_ts' not in codigo


def test_solo_se_da_de_baja_stock_tras_una_corrida_completa():
    """Borrar lo no tocado tras una corrida a medias borraria stock real."""
    import inspect

    snapshot = pytest.importorskip("snapshot")
    codigo = _solo_codigo(inspect.getsource(snapshot.snapshot_stock))

    # la baja va en la rama del else, no en la de error
    antes_del_else = codigo.split("else:")[0]
    assert "_borrar_stock_no_reportado" not in antes_del_else, (
        "la baja no puede correr cuando la corrida quedo incompleta"
    )
    assert "_borrar_stock_no_reportado" in codigo


def test_ningun_consumidor_lee_la_tabla_vieja():
    """Los que SIRVEN datos leen stock_actual.

    Se exceptuan a proposito los dos que tienen que tocar la tabla vieja: el
    tool de siembra (la lee una vez para migrar) y la retencion (la vacia).
    """
    import inspect

    excepciones = ("bsale_mcp_stock_actual_sembrar", "purge_stock_snapshots")

    for modulo in ("digests", "tools_snapshot", "tools_intelligence_db"):
        mod = pytest.importorskip(modulo)
        src = inspect.getsource(mod)
        for nombre in excepciones:
            if nombre in src:
                # se corta el fuente en el def de la excepcion y se salta
                partes = src.split("def " + nombre)
                src = partes[0] + "".join(p.split("@mcp.tool()", 1)[-1] for p in partes[1:])
        assert "FROM stock_snapshot" not in src, f"{modulo} sigue leyendo la vieja"
        assert "stock_snapshot.c." not in src, f"{modulo} sigue leyendo la vieja"


def test_la_retencion_tiene_su_propio_timeout():
    """El de 20 s protege al web service; un mantenimiento no lo hereda.

    Antes buscaba "_sin_timeout_corto(s)" en el modulo entero y lo encontraba
    en el `def`: pasaba aunque nadie lo llamara. Ahora se exige la LLAMADA
    dentro de cada funcion que borra, con comentarios y docstrings fuera."""
    import inspect

    rt = pytest.importorskip("retention")
    for fn in (rt._borrar_por_lotes, rt.purge_variants_snapshots, rt.purge_stock_snapshots):
        src = _solo_codigo(inspect.getsource(fn))
        assert "_sin_timeout_corto(s)" in src, f"{fn.__name__} no llama _sin_timeout_corto"
    assert "SET LOCAL statement_timeout" in _solo_codigo(inspect.getsource(rt._sin_timeout_corto))


def test_un_fallo_de_retencion_se_ve(monkeypatch):
    """Anidado en un dict que nadie mira, un fallo no marca la corrida.

    Antes: `"hubo_error" in getsource(apply_retention)` sin filtrar, y el
    docstring lo nombraba. Ahora se ejecuta con el purge reventando."""
    import inspect

    rt = pytest.importorskip("retention")

    def revienta(**kw):
        raise RuntimeError("timeout simulado")

    monkeypatch.setattr(rt, "purge_stock_snapshots", revienta)
    monkeypatch.setattr(rt, "purge_variants_snapshots", lambda: 0)
    monkeypatch.setattr(rt, "purge_documents_raw", lambda **kw: {"minimizadas": 0})
    out = rt.apply_retention()
    assert out["hubo_error"] is True and "stock_error" in out

    monkeypatch.setattr(rt, "purge_stock_snapshots", lambda **kw: {"borradas_total": 0})
    out = rt.apply_retention()
    assert out["hubo_error"] is False

    # La corrida real (sync_incremental._run) guarda el fallo en retention_error
    sync = pytest.importorskip("sync_incremental")
    assert 'results["retention_error"]' in _solo_codigo(inspect.getsource(sync._run))


# ============================================================
# Un paso automatico tiene que vivir donde el cron lo ejecuta
# ============================================================
# El cron de Render corre `python sync_incremental.py --modo auto` cada 30
# minutos. NO corre cron_snapshot.py. El 08-sep-2026 se programo el backfill
# historico de detalle dentro de nightly_snapshot(), que ningun cron llama:
# quedo "listo" sin poder ejecutarse nunca. Los 51.000 documentos de 2025 no
# se habrian completado jamas y nadie se habria enterado, porque el paso
# existia y los tests pasaban.

def test_el_backfill_historico_vive_donde_el_cron_lo_ejecuta():
    import inspect

    sync = pytest.importorskip("sync_incremental")
    codigo = _solo_codigo(inspect.getsource(sync._run))

    assert "snapshot_details(" in codigo, (
        "el backfill historico de detalle tiene que estar en sync_incremental.run(), "
        "que es lo que el cron ejecuta de verdad"
    )
    assert "oldest_first=True" in codigo, (
        "sin oldest_first nunca se llega a los periodos viejos"
    )


def test_el_presupuesto_por_corrida_cabe_en_la_cadencia():
    """El cron dispara cada 30 min y la corrida normal dura ~1m30s.

    Un presupuesto grande por corrida alarga cada pasada y arriesga que dos
    corridas se solapen.
    """
    import inspect

    sync = pytest.importorskip("sync_incremental")
    codigo = _solo_codigo(inspect.getsource(sync._run))
    assert 'DETALLE_HISTORICO_POR_CORRIDA", "2000"' in codigo


def test_no_vuelve_el_codigo_muerto_del_nocturno():
    """nightly_snapshot() y cron_snapshot.py se borraron el 09-sep-2026:
    verificado en el dashboard de Render que el cron corre
    `python sync_incremental.py --modo auto`. Cinco tests protegian un
    camino que nadie ejecutaba. Para que nadie lo vuelva a agregar."""
    import os

    snapshot = pytest.importorskip("snapshot")
    assert not hasattr(snapshot, "nightly_snapshot")
    aqui = os.path.dirname(os.path.abspath(__file__))
    assert not os.path.exists(os.path.join(aqui, "cron_snapshot.py"))


# ============================================================
# El audit de escrituras no puede vivir en un disco que no existe
# ============================================================
# Vivia en AUDIT_DIR/writes.jsonl apuntando al disco persistente de Render.
# Ese disco nunca quedo montado (el render.yaml lo declara pero el blueprint
# jamas se sincronizo), asi que caia al fallback /tmp y se borraba en cada
# deploy. Verificado el 08-sep-2026: despues de un deploy, cero eventos.

def test_el_audit_no_depende_de_un_disco():
    import inspect

    audit = pytest.importorskip("audit")
    codigo = _solo_codigo(inspect.getsource(audit.audit_log))
    assert "_escribir_en_postgres(event)" in codigo, (
        "el destino real del audit es Postgres, no un archivo"
    )
    # el archivo queda, pero solo como respaldo
    assert codigo.index("_escribir_en_postgres") < codigo.index("AUDIT_FILE.open")


def test_el_audit_nunca_voltea_una_escritura(monkeypatch):
    """Lo llama cada write hacia Bsale: un problema de logging no puede romperla.

    Antes: `"return False" in src`, que satisfacia el `if not DATABASE_URL:
    return False` aunque el except hiciera raise. Ahora se ejecuta con la
    sesion reventando."""
    audit = pytest.importorskip("audit")

    class SesionRota:
        def __enter__(self):
            raise RuntimeError("postgres caido")

        def __exit__(self, *a):
            return False

    import db
    # _escribir_en_postgres importa DATABASE_URL y session desde db AL LLAMARSE.
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(db, "session", lambda: SesionRota())

    ev = {"ts": 1788913105.0, "method": "POST", "path": "/x", "actor": "t"}
    assert audit._escribir_en_postgres(ev) is False

    # Y con la base "sana" devuelve True: el False de arriba no era el del
    # `if not DATABASE_URL`.
    class SesionSana:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            return None

    monkeypatch.setattr(db, "session", lambda: SesionSana())
    assert audit._escribir_en_postgres(ev) is True


def test_no_poder_leer_el_audit_no_es_lo_mismo_que_no_haber_escrituras():
    """[] dice 'no hubo escrituras'. Si no se pudo leer, hay que decir otra cosa."""
    import inspect

    audit = pytest.importorskip("audit")
    src = inspect.getsource(audit._leer_de_postgres)
    assert "return None" in src, (
        "si la base no responde tiene que devolver None, no lista vacia"
    )
    lectura = _solo_codigo(inspect.getsource(audit.read_recent))
    assert "if desde_db is not None" in lectura, (
        "con 'if desde_db:' una lista vacia legitima caeria al archivo"
    )


# ---------------------------------------------------------------------------
# Stock en paralelo. La corrida serial tardaba mas de una hora (~3.000 paginas
# a ~1,4 s cada una), y en ese rato cualquier deploy la mataba a medio camino.
# Lo delicado no es la velocidad sino el criterio de completitud: de el depende
# si se borran o no las filas que la corrida no toco.
# ---------------------------------------------------------------------------

class _ClienteStockFalso:
    """Emula /v1/stocks.json: devuelve `count` y paginas de `limit` filas."""

    def __init__(self, total, fallar_en=(), vacias=()):
        self.total = total
        self.fallar_en = set(fallar_en)
        self.vacias = set(vacias)
        self.offsets = []

    def get(self, path, params=None, use_cache=True):
        params = params or {}
        off = params["offset"]
        lim = params["limit"]
        self.offsets.append(off)
        if off in self.fallar_en:
            raise RuntimeError("500 simulado de Bsale")
        if off in self.vacias:
            # 200 OK con items vacio. Bsale lo hace bajo carga. NO es un error.
            return {"count": self.total, "items": []}
        items = [
            {
                "quantity": 1.0,
                "variant": {"id": k, "code": "V%d" % k},
                "office": {"id": 1, "name": "Quilicura"},
            }
            for k in range(off, min(off + lim, self.total))
        ]
        return {"count": self.total, "items": items}


class _SesionFalsa:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        self.sink.append(stmt)
        return None


# Lo que snapshot_stock dejo registrado en sync_estado durante el ultimo
# _montar_stock. Va aparte para no cambiarle la firma a la funcion.
REGISTROS_STOCK = []


def _montar_stock(monkeypatch, cliente):
    """Deja snapshot_stock corriendo contra el cliente falso y sin Postgres."""
    snapshot = pytest.importorskip("snapshot")
    escrituras = []
    bajas = []
    REGISTROS_STOCK.clear()
    monkeypatch.setenv("BSALE_STOCK_WORKERS", "4")
    monkeypatch.setattr(snapshot, "get_client", lambda: cliente)
    monkeypatch.setattr(snapshot, "db_session", lambda: _SesionFalsa(escrituras))
    monkeypatch.setattr(
        snapshot,
        "_borrar_stock_no_reportado",
        lambda ts: bajas.append(ts) or 0,
    )
    monkeypatch.setattr(
        snapshot,
        "_registrar_estado",
        lambda clave, valor: REGISTROS_STOCK.append((clave, valor)),
    )
    return snapshot, escrituras, bajas


def test_el_stock_se_baja_en_paralelo_y_no_pagina_a_pagina():
    import inspect

    snapshot = pytest.importorskip("snapshot")
    codigo = _solo_codigo(inspect.getsource(snapshot.snapshot_stock))

    assert "ThreadPoolExecutor" in codigo, "la bajada tiene que ser en paralelo"
    assert "BSALE_STOCK_WORKERS" in codigo, "la concurrencia tiene que ser regulable"
    assert 'primera.get("count")' in codigo, (
        "el plan de la corrida sale de count, no de llegar a una pagina vacia"
    )


def test_una_corrida_completa_de_stock_lee_todo_y_recien_ahi_da_de_baja(monkeypatch):
    cliente = _ClienteStockFalso(total=1000)
    snapshot, escrituras, bajas = _montar_stock(monkeypatch, cliente)

    out = snapshot.snapshot_stock(max_pages=6000)

    assert out["completo"] is True
    assert out["rows"] == 1000
    assert out["filas_vistas"] == 1000
    assert out["count_bsale"] == 1000
    assert out["paginas_previstas"] == 20
    assert "stock_error" not in out
    assert len(bajas) == 1, "solo tras una corrida completa se da de baja"
    # 20 paginas del plan + 1 de cola que vuelve vacia. Nada mas.
    assert sorted(cliente.offsets) == [50 * p for p in range(21)]


def test_una_pagina_que_falla_deja_la_corrida_incompleta_y_no_borra_nada(monkeypatch):
    cliente = _ClienteStockFalso(total=1000, fallar_en={200})
    snapshot, escrituras, bajas = _montar_stock(monkeypatch, cliente)

    out = snapshot.snapshot_stock(max_pages=6000)

    assert out["completo"] is False
    assert "stock_error" in out, "cron_snapshot marca la corrida por la clave *_error"
    assert "200" in out["stock_error"]
    assert bajas == [], "una corrida a medias no puede borrar stock"


def test_una_pagina_vacia_sin_error_no_puede_dar_la_corrida_por_completa(monkeypatch):
    """El modo de falla caro y silencioso.

    Bsale bajo carga contesta 200 con items vacio. Si la completitud se midiera
    solo por "todas las paginas respondieron", esta corrida quedaria completa y
    _borrar_stock_no_reportado borraria 50 filas que SI existen: esas variantes
    aparecerian en cero en la tienda hasta la corrida siguiente. La guardarraya
    compara filas leidas contra count.
    """
    cliente = _ClienteStockFalso(total=1000, vacias={300})
    snapshot, escrituras, bajas = _montar_stock(monkeypatch, cliente)

    out = snapshot.snapshot_stock(max_pages=6000)

    assert out["filas_vistas"] == 950
    assert out["completo"] is False, "faltan 50 filas que Bsale dice tener"
    assert "1000" in out["stock_error"] and "950" in out["stock_error"]
    assert bajas == [], "no se borra nada cuando el conteo no cuadra"


def test_si_bsale_crece_durante_la_corrida_la_cola_lo_alcanza(monkeypatch):
    """count es la foto del arranque. Si entran filas nuevas, no pueden perderse."""
    cliente = _ClienteStockFalso(total=1000)
    snapshot, escrituras, bajas = _montar_stock(monkeypatch, cliente)

    # A partir de la segunda pagina leida, Bsale ya tiene 1.100 filas.
    original = cliente.get
    estado = {"n": 0}

    def get_creciendo(path, params=None, use_cache=True):
        estado["n"] += 1
        if estado["n"] > 1:
            cliente.total = 1100
        return original(path, params=params, use_cache=use_cache)

    monkeypatch.setattr(cliente, "get", get_creciendo)

    out = snapshot.snapshot_stock(max_pages=6000)

    assert out["filas_vistas"] == 1100, "las 100 filas nuevas entran por la cola"
    assert out["paginas_cola"] == 2
    assert out["completo"] is True
    assert len(bajas) == 1


# ---------------------------------------------------------------------------
# "La foto esta fresca" NO es "la foto esta completa".
#
# El 08-sep-2026 se cancelo una corrida de stock a mitad: stock_actual quedo
# con 67.000 de ~150.000 filas y con updated_at de hacia un rato. Como el modo
# auto solo miraba la ANTIGUEDAD de la foto, con STOCK_EVERY_HOURS=12 iba a
# servir medio inventario durante 12 horas, en horario de tienda, sin que nada
# avisara. Ahora la corrida deja registrado si termino, y la siguiente pasada
# la repite si no.
# ---------------------------------------------------------------------------

class _SesionScalar:
    def __init__(self, valor=None, revienta=False):
        self.valor = valor
        self.revienta = revienta

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        if self.revienta:
            raise RuntimeError("base caida")
        return self

    def scalar(self):
        return self.valor


def _completa_con(monkeypatch, **kw):
    db = pytest.importorskip("db")
    sync = pytest.importorskip("sync_incremental")
    monkeypatch.setattr(db, "session", lambda: _SesionScalar(**kw))
    return sync._ultima_corrida_stock_completa()


def test_sin_registro_de_corrida_el_stock_se_considera_incompleto(monkeypatch):
    """Base nueva o corrida que nunca llego a registrar: hay que correr."""
    assert _completa_con(monkeypatch, valor=None) is False


def test_una_corrida_marcada_incompleta_obliga_a_repetir(monkeypatch):
    assert _completa_con(monkeypatch, valor={"completo": False}) is False


def test_una_corrida_completa_no_se_repite(monkeypatch):
    assert _completa_con(monkeypatch, valor={"completo": True}) is True


def test_si_no_se_puede_leer_el_estado_no_se_dispara_el_paso_pesado(monkeypatch):
    """Mismo criterio que _stock_photo_age_hours: ante la duda, no correr."""
    assert _completa_con(monkeypatch, revienta=True) is True


def test_el_modo_auto_mira_completitud_ademas_de_antiguedad():
    import inspect

    sync = pytest.importorskip("sync_incremental")
    codigo = _solo_codigo(inspect.getsource(sync._run))

    assert "_ultima_corrida_stock_completa" in codigo, (
        "auto tiene que repetir la corrida si la anterior quedo a medias"
    )
    assert "_stock_photo_age_hours" in codigo


def test_la_corrida_de_stock_registra_que_quedo_completa(monkeypatch):
    cliente = _ClienteStockFalso(total=1000)
    snapshot, _, _ = _montar_stock(monkeypatch, cliente)

    snapshot.snapshot_stock(max_pages=6000)

    # Dos registros: al ENTRAR (en_curso, completo=False) y al SALIR. Si el
    # proceso muere entre los dos, queda el primero, y la corrida siguiente
    # sabe que tiene que repetir. Antes solo existia el del final.
    assert len(REGISTROS_STOCK) == 2
    clave0, entrada = REGISTROS_STOCK[0]
    assert clave0 == "stock_ultima_corrida"
    assert entrada["completo"] is False and entrada["en_curso"] is True
    clave, valor = REGISTROS_STOCK[-1]
    assert clave == "stock_ultima_corrida"
    assert valor["completo"] is True and valor["en_curso"] is False
    assert valor["filas_vistas"] == 1000
    assert valor["error"] is None


def test_la_corrida_de_stock_registra_que_quedo_incompleta(monkeypatch):
    cliente = _ClienteStockFalso(total=1000, fallar_en={200})
    snapshot, _, _ = _montar_stock(monkeypatch, cliente)

    snapshot.snapshot_stock(max_pages=6000)

    clave, valor = REGISTROS_STOCK[-1]
    assert clave == "stock_ultima_corrida"
    assert valor["completo"] is False and valor["en_curso"] is False
    assert valor["error"], "el registro tiene que decir por que"


def test_el_cron_tambien_crea_su_esquema():
    """init_db vivia solo en server.py, o sea solo en el web service.

    Una tabla nueva en db.py no existia para el cron hasta que el web service
    se reiniciara. Como los helpers que la leen atrapan la excepcion, el
    sintoma habria sido "el paso no hace nada", sin error visible.
    """
    import inspect

    sync = pytest.importorskip("sync_incremental")
    codigo = _solo_codigo(inspect.getsource(sync._run))

    assert "init_db" in codigo


# ===========================================================================
# Auditoria del 08-sep-2026. Un test por arreglo, y para cada uno se verifico
# que falla si se revierte el arreglo (no alcanza con que pase).
# ===========================================================================

class _McpFalso:
    """Captura las funciones que register() decora, para poder llamarlas.

    @mcp.tool() en fastmcp 4.0.3 devuelve la funcion original (verificado), asi
    que basta con quedarse con la referencia.
    """

    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _ClienteEscrituraFalso:
    def __init__(self, stock=None):
        self.llamadas = []
        # stock[(variant_id, office_id)] -> cantidad
        self.stock = stock or {}

    def get(self, path, params=None, **k):
        params = params or {}
        vid, oid = params.get("variantid"), params.get("officeid")
        q = self.stock.get((vid, oid))
        if q is None:
            return {"items": []}
        return {"items": [{
            "quantity": q,
            "variant": {"id": vid},
            "office": {"id": oid},
        }]}

    def post(self, path, json_body=None, **k):
        self.llamadas.append(("POST", path, json_body))
        return {"id": 9999}

    def put(self, path, json_body=None, **k):
        self.llamadas.append(("PUT", path, json_body))
        return {"id": 9999}


def _tools_de_escritura(monkeypatch, stock=None):
    tw = pytest.importorskip("tools_writes")
    cli = _ClienteEscrituraFalso(stock)
    monkeypatch.setattr(tw, "get_client", lambda: cli)
    m = _McpFalso()
    tw.register(m)
    return m.tools, cli


# --------------------------------------------------------------- 1. access_log
def test_el_secreto_de_la_url_no_se_escribe_en_el_log_de_acceso():
    """La autenticacion va en la URL, asi que el access log es una fuga.

    uvicorn 0.52.4 trae Config.access_log=True por defecto (verificado contra
    la libreria instalada). Con el secreto como segmento del path, cada request
    escribia la credencial completa del ERP en stdout -> logs de Render.
    """
    import inspect

    server = pytest.importorskip("server")
    codigo = _solo_codigo(inspect.getsource(server.main))

    assert "access_log=False" in codigo, (
        "sin esto el secreto de /mcp/<secreto> queda en los logs de Render"
    )


# ------------------------------------------- 2. ventana de documentos y HIST_END
def test_el_cron_mira_treinta_dias_no_catorce_ni_dos():
    """La boleta 1280257: emitida el 03-sep, generada el 07-sep (4 dias).
    La boleta 1273799: emitida el 18-jul, generada el 05-ago (18 dias).

    Con 2 dias se perdio la primera ($101.970). Con 14 se perdia la segunda,
    y el comentario que fijaba los 14 citaba justamente ese caso de 18. Como
    hist_end() relee cada mes cerrado UNA sola vez, lo que la ventana no
    alcanza se pierde para siempre. La ventana tiene que cubrir el peor
    desfase visto con margen: 30.
    """
    import inspect
    import re

    sync = pytest.importorskip("sync_incremental")
    codigo = _solo_codigo(inspect.getsource(sync.sync_ventas))

    m = re.search(r"snapshot_documents\(days_back=(\d+)", codigo)
    assert m, "sync_ventas tiene que llamar snapshot_documents(days_back=N)"
    assert int(m.group(1)) >= 30, f"days_back={m.group(1)} no cubre los 18 dias de la 1273799"


def test_el_backfill_historico_no_se_cierra_para_siempre(monkeypatch):
    """HIST_END era la constante "2026-09": al llegar ahi, nunca mas releia nada.

    Ahora es el mes actual, movil. Cuando entra octubre, septiembre vuelve a ser
    anterior al corte y se relee entero, recogiendo lo que se haya emitido con
    fecha retroactiva durante el mes.
    """
    from datetime import datetime, timezone

    sync = pytest.importorskip("sync_incremental")
    assert not hasattr(sync, "HIST_END"), "la constante fija tiene que estar muerta"

    # Con el reloj movido, NO comparando contra el mes de hoy: la constante
    # vieja era "2026-09" y este test se escribio en septiembre de 2026, asi
    # que comparar contra hoy lo hacia pasar con el bug puesto. Probaba el
    # calendario, no el codigo.
    class _RelojFalso:
        @staticmethod
        def now(tz=None):
            return datetime(2027, 4, 15, 12, 0, tzinfo=tz or timezone.utc)

    original = sync.datetime
    sync.datetime = _RelojFalso
    try:
        assert sync.hist_end() == "2027-04"
    finally:
        sync.datetime = original

    assert sync.hist_end() == datetime.now(timezone.utc).strftime("%Y-%m")


# ----------------------------------------------------- 3. errores anidados
def test_un_error_anidado_marca_la_corrida_como_fallida():
    """Ningun paso pone su error en el primer nivel de results.

    snapshot_stock devuelve {"stock": {..., "stock_error": ...}}. El chequeo
    viejo era [k for k in results if k.endswith("_error")], que nunca coincidia:
    una corrida de stock incompleta salia con codigo 0 y Render la pintaba
    verde.
    """
    sync = pytest.importorskip("sync_incremental")

    r = sync.recolectar_errores({"stock": {"completo": False, "stock_error": "faltan filas"}})
    assert r == ["stock.stock_error"]

    r = sync.recolectar_errores({"retention": {"hubo_error": True}})
    assert r == ["retention.hubo_error"]

    # hist_error vive dentro de una LISTA
    r = sync.recolectar_errores({"historico": [{"hist_mes": "2025-03", "hist_error": "truncado"}]})
    assert r == ["historico[0].hist_error"]


def test_lo_que_no_es_error_no_marca_la_corrida():
    sync = pytest.importorskip("sync_incremental")

    assert sync.recolectar_errores({"stock": {"completo": True, "rows": 240427}}) == []
    # las claves de error en None o vacio no cuentan
    assert sync.recolectar_errores({"stock": {"stock_error": None}}) == []
    assert sync.recolectar_errores({"retention": {"hubo_error": False}}) == []
    # retention_warning es a proposito una advertencia, no un error
    assert sync.recolectar_errores({"retention_warning": "algo"}) == []


def test_algunos_documentos_fallidos_no_son_una_falla_pero_ninguno_que_entre_si():
    """Que fallen 3 de 2.000 es normal. Que no entre NINGUNO es sistemico.

    El criterio era `errors >= docs_processed` y estaba mal en las dos
    direcciones, porque en snapshot.py el camino de error hace
    `errors += 1; continue`: los dos contadores son DISJUNTOS, nunca suman el
    lote. Con el token vencido fallan los 400 y docs_processed queda en 0, o
    sea que la guarda vieja (que exigia proc > 0) NO disparaba justo en el caso
    que decia cubrir; y con 1.001 fallos de 2.000 SI disparaba, dejando el cron
    en rojo por una tanda de 429 que la corrida siguiente completa sola.
    """
    sync = pytest.importorskip("sync_incremental")

    # Algunos fallan: normal, no marca.
    assert sync.recolectar_errores({"d": {"docs_processed": 2000, "errors": 3}}) == []
    # Falla la mitad: sigue siendo parcial, NO puede marcar.
    assert sync.recolectar_errores({"d": {"docs_processed": 999, "errors": 1001}}) == []
    # No entro ninguno: eso si es sistemico.
    assert sync.recolectar_errores({"d": {"docs_processed": 0, "errors": 400}}) == ["d.errors"]
    # Lote vacio: no se intento nada, no es un error.
    assert sync.recolectar_errores({"d": {"docs_processed": 0, "errors": 0}}) == []


def test_un_digest_caido_marca_la_corrida():
    """digests.py no usa una clave *_error: mete el error en el VALOR.

    Un matcher que solo mira nombres de clave lo dejaba pasar entero, que es
    el mismo modo de falla que este helper vino a cerrar.
    """
    sync = pytest.importorskip("sync_incremental")

    r = sync.recolectar_errores({"digests": {"ventas_hoy": "error: could not connect"}})
    assert r == ["digests.ventas_hoy"]
    assert sync.recolectar_errores({"digests": {"ventas_hoy": "ok"}}) == []


# ---------------------------------------------------- 5. pools desanidados
def test_el_detalle_no_anida_pools_contra_bsale():
    """4 workers externos x 4 internos = 16 simultaneas. Bsale frena en 6.

    Es lo que explicaba los 242 reintentos, no la concurrencia del pool
    externo, que ya se habia bajado de 6 a 4 sin que el problema cambiara.
    """
    import inspect

    snapshot = pytest.importorskip("snapshot")
    codigo = _solo_codigo(inspect.getsource(snapshot.snapshot_details))

    assert "workers=1" in codigo, (
        "el paginated_fetch de adentro del pool tiene que ir con workers=1"
    )


# --------------------------------------- 6. candados en escrituras de catalogo
def test_las_escrituras_de_catalogo_tienen_kill_switch(monkeypatch):
    """activar/desactivar/actualizar variante y producto no tenian ninguno."""
    tools, cli = _tools_de_escritura(monkeypatch)
    monkeypatch.setenv("BSALE_CATALOG_WRITES_ENABLED", "0")

    for nombre, args in (
        ("bsale_activar_variante", (123,)),
        ("bsale_desactivar_variante", (123,)),
    ):
        r = tools[nombre](*args)
        assert r["aplicado"] is False, nombre
        assert "BLOQUEADA" in r["bloqueado_por"], nombre

    r = tools["bsale_actualizar_variante"](123, code="NUEVO")
    assert r["aplicado"] is False
    assert cli.llamadas == [], "no puede haber tocado Bsale"


def test_cambiar_el_sku_pasa_por_DOS_candados(monkeypatch):
    """El code es la llave con Shopify y Mercado Libre via sku_mapping. Desde
    el 09-sep tiene candado propio: abrir el catalogo para una descripcion no
    abre el SKU."""
    tools, cli = _tools_de_escritura(monkeypatch)
    monkeypatch.setenv("BSALE_CATALOG_WRITES_ENABLED", "1")
    monkeypatch.delenv("BSALE_SKU_WRITES_ENABLED", raising=False)

    r = tools["bsale_actualizar_variante"](123, code="SKU-NUEVO")
    assert r["aplicado"] is False and cli.llamadas == []

    monkeypatch.setenv("BSALE_SKU_WRITES_ENABLED", "1")
    r = tools["bsale_actualizar_variante"](123, code="SKU-NUEVO")
    assert r["aplicado"] is True
    assert cli.llamadas == [("PUT", "/v1/variants/123.json", {"code": "SKU-NUEVO"})]


# ------------------------------------- 6b. validacion de cantidades y costos
def test_no_se_escribe_stock_con_cantidad_invalida(monkeypatch):
    """bsale_crear_traspaso_stock ya validaba; las otras tres no.

    Una cantidad negativa en un "consumo" invierte el sentido de la operacion.
    """
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")

    # CON stock cargado. Antes este test corria sin stock, asi que
    # bsale_ajustar_stock cortaba con "no se pudo leer el stock actual" y el
    # assert pasaba por el motivo equivocado: verificaba una precondicion del
    # mock, no el guardrail. Y ademas afirmaba que ajustar rechaza el 0, que es
    # falso a proposito (0 es un saldo final legitimo).
    tools, cli = _tools_de_escritura(monkeypatch, stock={(1, 1): 10.0})

    for nombre in ("bsale_consumir_stock", "bsale_recepcionar_stock"):
        for mala in (0, -5, float("nan"), float("inf")):
            r = tools[nombre](variant_id=1, office_id=1, quantity=mala)
            assert r["aplicado"] is False, f"{nombre} acepto {mala}"

    # ajustar_stock es un SALDO final: rechaza negativo y NaN, acepta 0.
    for mala in (-5, float("nan"), float("inf")):
        r = tools["bsale_ajustar_stock"](variant_id=1, office_id=1, quantity=mala)
        assert r["aplicado"] is False, f"ajustar acepto {mala}"

    assert cli.llamadas == [], "ninguna llego a Bsale"


def test_no_se_recepciona_con_costo_negativo(monkeypatch):
    """Un costo negativo contamina el costo promedio y de ahi todo el margen."""
    tools, cli = _tools_de_escritura(monkeypatch)
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")

    r = tools["bsale_recepcionar_stock"](variant_id=1, office_id=1, quantity=5, cost=-100)
    assert r["aplicado"] is False
    assert cli.llamadas == []


def test_una_escritura_de_stock_exitosa_dice_que_se_aplico(monkeypatch):
    """El camino de exito devolvia el JSON crudo de Bsale, sin clave "aplicado".

    El camino BLOQUEADO si devolvia {"aplicado": False}. Un llamador que
    escribiera el chequeo obvio -- if not r.get("aplicado"): reintentar --
    reintentaba sobre una escritura EXITOSA. Sin idempotencia en Bsale, eso es
    un ajuste de stock aplicado dos veces.
    """
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 5.0})
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")

    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12, cost=9500)
    assert r["aplicado"] is True
    assert len(cli.llamadas) == 1


def test_subir_stock_sin_costo_no_escribe(monkeypatch):
    """Subir stock se aplica como RECEPCION, y una recepcion sin cost entra a 0.

    Eso arrastra el costo promedio de la variante en Bsale hacia abajo de forma
    permanente: Bsale no recalcula hacia atras. Un scrub de $9.500 con 20
    unidades, ajustado +10 a costo 0, queda con costo promedio $6.333 y el
    margen de esa variante queda mal para siempre. Bajar stock no lo necesita.
    """
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")

    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 5.0})
    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12)
    assert r["aplicado"] is False
    assert "costo 0" in r["bloqueado_por"]
    assert cli.llamadas == [], "no puede haber tocado Bsale"

    # Bajar stock si puede ir sin costo.
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 20.0})
    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12)
    assert r["aplicado"] is True
    assert cli.llamadas[0][1] == "/v1/stocks/consumptions.json"


def test_el_objetivo_queda_en_la_nota_para_poder_reconstruirlo(monkeypatch):
    """El audit log guarda el body. Sin esto queda "recepcion de 7" y no se
    puede reconstruir que la orden fue "dejalo en 12"."""
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 5.0})

    tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12, cost=9500)
    nota = cli.llamadas[0][2]["note"]
    assert "objetivo 12" in nota and "antes 5" in nota


def test_ajustar_stock_no_usa_el_endpoint_que_no_esta_documentado(monkeypatch):
    """La documentacion de Bsale lista DOS endpoints de escritura de stock.

    receptions y consumptions. adjustments.json no aparece, igual que el POST de
    lista de precios que tampoco existia. Verificado el 08-sep-2026 en dos
    fuentes.
    """
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 5.0})
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")

    tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12, cost=9500)
    paths = [c[1] for c in cli.llamadas]
    assert "/v1/stocks/adjustments.json" not in paths
    assert paths == ["/v1/stocks/receptions.json"]


def test_ajustar_stock_deja_el_valor_final_pedido_no_lo_suma(monkeypatch):
    """quantity es el SALDO que debe quedar, no la cantidad a mover.

    El docstring prometia "cantidad final" y el codigo mandaba ese numero como
    delta al endpoint. Con stock 5 y quantity 12, el stock quedaba en 17.
    """
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")

    # Falta: hay que SUMAR 7.
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 5.0})
    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12, cost=9500)
    assert r["stock_antes"] == 5.0 and r["objetivo"] == 12.0
    assert r["delta_aplicado"] == 7.0
    assert cli.llamadas[0][1] == "/v1/stocks/receptions.json"
    assert cli.llamadas[0][2]["details"][0]["quantity"] == 7.0
    assert cli.llamadas[0][2]["details"][0]["cost"] == 9500.0

    # Sobra: hay que RESTAR 8.
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 20.0})
    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12)
    assert r["delta_aplicado"] == -8.0
    assert cli.llamadas[0][1] == "/v1/stocks/consumptions.json"
    assert cli.llamadas[0][2]["details"][0]["quantity"] == 8.0


def test_ajustar_stock_dos_veces_no_mueve_nada_la_segunda(monkeypatch):
    """Idempotencia. Bsale no expone claves de idempotencia.

    Un timeout del cliente MCP sobre una escritura que Bsale SI aplico deja al
    llamador sin saber que paso. Si reintenta y el ajuste fuera un delta, el
    stock queda con el doble. Calculando la diferencia contra el stock actual,
    el segundo intento es un no-op.
    """
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 12.0})

    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12)
    assert r["aplicado"] is False
    assert r["sin_cambios"] is True
    assert cli.llamadas == [], "no puede haber escrito nada"


def test_ajustar_stock_no_escribe_si_no_pudo_leer_el_actual(monkeypatch):
    """Sin el valor actual no hay como calcular el ajuste. Falla cerrada."""
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")
    tools, cli = _tools_de_escritura(monkeypatch, stock={})

    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=12)
    assert r["aplicado"] is False
    assert "no se escribio nada" in r["motivo"].lower()
    assert cli.llamadas == []


def test_ajustar_stock_a_cero_es_valido(monkeypatch):
    """0 es un saldo final legitimo, aunque sea una cantidad de movimiento invalida."""
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")
    tools, cli = _tools_de_escritura(monkeypatch, stock={(7, 2): 4.0})

    r = tools["bsale_ajustar_stock"](variant_id=7, office_id=2, quantity=0)
    assert r["aplicado"] is True
    assert r["delta_aplicado"] == -4.0
    assert cli.llamadas[0][1] == "/v1/stocks/consumptions.json"


def test_la_escritura_de_catalogo_esta_apagada_por_default(monkeypatch):
    """Cambiar un SKU rompe el sync con Shopify y Mercado Libre sin dar error.

    El kill-switch de stock viene encendido porque es operativo y se usa. Este
    no: activar, desactivar y renombrar variantes o productos es excepcional, y
    ahora Andrea tambien tiene acceso al conector.
    """
    monkeypatch.delenv("BSALE_CATALOG_WRITES_ENABLED", raising=False)
    tools, cli = _tools_de_escritura(monkeypatch)

    r = tools["bsale_actualizar_variante"](123, code="SKU-NUEVO")
    assert r["aplicado"] is False
    assert cli.llamadas == []


# -------------------------------------------------- 7. precio actual en cero
def test_una_variante_en_cero_no_puede_recibir_cualquier_precio():
    """`if antes:` dejaba pasar el 0 porque es falsy.

    delta_pct quedaba None, excede_umbral en False, y el filtro siguiente era
    `is None`, que 0.0 no cumple: el tope del 5% no se aplicaba a nada.
    """
    g = pytest.importorskip("guardrails")

    with pytest.raises(g.GuardrailError) as e:
        g.validate_price_updates([{"variant_id": 1, "new_price": 99000}], current={1: 0.0})
    # "no se escribio nada" esta en TODOS los GuardrailError: no distinguia
    # nada. Lo que distingue es que el motivo sea el precio actual en cero, y
    # que un precio actual normal con el mismo delta NO aborte por eso.
    assert "sin_precio_actual" in str(e.value) or "precio actual" in str(e.value).lower()
    assert "umbral" not in str(e.value).lower(), "tiene que abortar por el 0, no por el 5%"


def test_precio_actual_negativo_tambien_cuenta_como_sin_precio():
    """El filtro es `<= 0`, no `is None` ni `not antes`: -1 tambien aborta."""
    g = pytest.importorskip("guardrails")
    with pytest.raises(g.GuardrailError):
        g.validate_price_updates([{"variant_id": 1, "new_price": 10000}], current={1: -1.0})


def test_el_tope_de_delta_sigue_funcionando_con_precio_normal():
    g = pytest.importorskip("guardrails")

    tabla = g.validate_price_updates(
        [{"variant_id": 1, "new_price": 10300}], current={1: 10000}, max_delta_pct=5.0
    )
    assert tabla[0]["delta_pct"] == 3.0
    with pytest.raises(g.GuardrailError):
        g.validate_price_updates(
            [{"variant_id": 1, "new_price": 20000}], current={1: 10000}, max_delta_pct=5.0
        )


# ===========================================================================
# Correcciones de cifras de la auditoria (frente "correctitud del dinero").
# ===========================================================================

def test_ninguna_fecha_se_convierte_con_la_zona_local_del_proceso():
    """fromtimestamp sin tz usa la zona local: es convertir emission_date.

    emissionDate es medianoche UTC exacta. Hoy Render corre en UTC y sale bien
    por accidente; con TZ=America/Santiago todos los dias se corren uno hacia
    atras y la venta del lunes aparece como del domingo.
    """
    import ast
    import inspect

    for mod in ("tools_analytics", "tools_intelligence_db", "snapshot", "digests"):
        m = pytest.importorskip(mod)
        arbol = ast.parse(inspect.getsource(m))
        for n in ast.walk(arbol):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "fromtimestamp"
            ):
                kw = {k.arg for k in n.keywords}
                assert "tz" in kw, (
                    f"{mod}: fromtimestamp sin tz en la linea {n.lineno}"
                )


def test_el_desglose_por_tipo_cuenta_igual_que_el_resto_de_la_respuesta():
    """by_office y by_day excluyen las NC del conteo; by_document_type no.

    Sumar los count de by_document_type daba documentos_de_venta +
    notas_de_credito: dentro de la MISMA respuesta habia dos totales de
    documentos que no cerraban entre si.
    """
    import inspect

    ts = pytest.importorskip("tools_snapshot")
    codigo = _solo_codigo(inspect.getsource(ts))

    # Acotado al SELECT del desglose por tipo. El otro func.count() del archivo
    # esta en la consulta de "excluidos", donde contar TODO si es lo correcto:
    # un assert sobre el modulo entero lo agarraria como falso positivo.
    lineas = [l.strip() for l in codigo.splitlines() if l.strip()]
    i = lineas.index("d.document_type_name,")
    assert lineas[i + 1] == 'n_venta.label("docs"),', (
        "el conteo por tipo tiene que usar n_venta, igual que by_office y by_day; "
        f"hoy usa: {lineas[i + 1]}"
    )


def test_el_sobrestockeo_dice_a_que_precio_esta_valorizado():
    """No es capital inmovilizado: es a cuanto se venderia ese stock.

    avg_price sale de total_amount de la linea, que es el bruto CON IVA que se
    le cobro al cliente. Para un scrub que se vende a $29.990 y cuesta $9.500,
    llamarle "capital" sobrestima 3,2 veces la plata que hay puesta.
    """
    import inspect

    tid = pytest.importorskip("tools_intelligence_db")
    codigo = _solo_codigo(inspect.getsource(tid))

    assert '"capital_tied_clp"' not in codigo
    assert '"capital_inmovilizado_en_los_revisados_clp"' not in codigo
    assert '"valorizado_a_precio_venta_clp"' in codigo
    assert '"nota_valorizacion"' in codigo


def test_el_detalle_de_precio_se_verifica_contra_la_variante(monkeypatch):
    """detalle_id es el destino del PUT: tomarlo a ciegas escribe sobre otra.

    Si Bsale ignorara el filtro `variantid`, items[0] seria la primera fila del
    listado COMPLETO de la lista de precios. Ni sin_precio_actual ni
    sin_detalle lo notarian, porque la clave del dict es vid pase lo que pase.
    Falla cerrada: si no se puede confirmar la variante, el detalle no entra y
    el guardrail aborta antes de escribir.
    """
    tw = pytest.importorskip("tools_writes")

    class _CliPrecios:
        def __init__(self, items):
            self.items = items
            self.escrituras = []

        def get(self, path, params=None, **k):
            return {"items": self.items}

        def put(self, path, json_body=None, **k):
            self.escrituras.append((path, json_body))
            return {"id": 1}

    monkeypatch.setenv("BSALE_PRICE_WRITES_ENABLED", "1")
    monkeypatch.setenv("BSALE_WRITABLE_PRICE_LISTS", "5")

    def _precios(items):
        cli = _CliPrecios(items)
        m = _McpFalso()
        monkeypatch.setattr(tw, "get_client", lambda: cli)
        tw.register(m)
        return m.tools["bsale_actualizar_precios_masivo"], cli

    # Caso malo: Bsale IGNORA el filtro y devuelve el detalle de OTRA variante.
    tool, cli = _precios([{"id": 111, "variantValue": "19990", "variant": {"id": 999}}])
    r = tool(price_list_id=5, updates=[{"variant_id": 117733, "new_price": 20990}])
    # OJO con la asercion: un dry_run EXITOSO tambien devuelve aplicado=False,
    # asi que comprobar eso no distingue nada. Lo que distingue es que aca no
    # hay tabla: el guardrail abortó antes de armarla.
    assert "bloqueado_por" in r, "tenia que abortar, no armar una tabla"
    assert r.get("dry_run") is not True
    assert "tabla_de_cambios" not in r
    assert cli.escrituras == []

    # Caso bueno: el detalle corresponde, el dry_run arma la tabla.
    tool, cli = _precios([{"id": 111, "variantValue": "19990", "variant": {"id": 117733}}])
    r = tool(price_list_id=5, updates=[{"variant_id": 117733, "new_price": 20500}])
    assert r["dry_run"] is True
    assert r["tabla_de_cambios"][0]["precio_actual"] == 19990.0
    assert r["confirm_token"]
    assert cli.escrituras == [], "un dry_run no escribe"


def test_el_precio_de_la_lista_es_neto_y_la_tabla_lo_declara(monkeypatch):
    """variantValue es el precio NETO, sin IVA, y nada lo decia.

    Medido contra produccion el 09-sep-2026: la variante 117733 en la lista 12
    (OUTLET) vale 25.201,6806722689, y 25.201,68 x 1,19 = 29.990 exacto, que es
    el precio de vitrina.

    Importa porque los precios de MyScrubs se hablan CON IVA. Pedirle al
    conector "dejalo en $32.990" escribe 32.990 como neto y la vitrina queda en
    $39.258: 19% de error en un precio, en silencio.
    """
    g = pytest.importorskip("guardrails")
    tw = pytest.importorskip("tools_writes")

    assert g.con_iva(25201.6806722689) == 29990.0
    assert g.con_iva(32990) == 39258.0

    class _CliPrecios:
        def get(self, path, params=None, **k):
            return {"items": [
                {"id": 111, "variantValue": "25201.6806722689",
                 "variant": {"id": 117733}},
            ]}

    monkeypatch.setenv("BSALE_PRICE_WRITES_ENABLED", "1")
    monkeypatch.setenv("BSALE_WRITABLE_PRICE_LISTS", "12")
    monkeypatch.setattr(tw, "get_client", lambda: _CliPrecios())
    m = _McpFalso()
    tw.register(m)

    r = m.tools["bsale_actualizar_precios_masivo"](
        price_list_id=12, updates=[{"variant_id": 117733, "new_price": 25202.6806722689}]
    )
    fila = r["tabla_de_cambios"][0]
    assert fila["precio_actual_con_iva"] == 29990.0
    assert fila["precio_nuevo_con_iva"] == 29991.0
    assert "NETOS" in r["nota_precios"]


def test_el_iva_es_configurable(monkeypatch):
    """Por si cambia la tasa. Hoy 19%."""
    g = pytest.importorskip("guardrails")

    monkeypatch.setenv("BSALE_IVA_PCT", "0")
    assert g.con_iva(1000) == 1000.0
    monkeypatch.setenv("BSALE_IVA_PCT", "19")
    assert g.con_iva(1000) == 1190.0


def test_la_escritura_de_stock_esta_apagada_por_default(monkeypatch):
    """Las cuatro escrituras de stock nunca corrieron contra la API real.

    El camino read-then-delta supone que `quantity` en receptions/consumptions
    es la cantidad a mover y no un saldo final. Esa pregunta esta abierta con
    Bsale. Si el supuesto estuviera al reves el inventario queda mal en las 11
    sucursales y Bsale no tiene idempotencia para deshacerlo, asi que el default
    tiene que ser apagado, igual que los precios y el catalogo.
    """
    g = pytest.importorskip("guardrails")
    monkeypatch.delenv("BSALE_STOCK_WRITES_ENABLED", raising=False)
    assert g.stock_writes_enabled() is False

    with pytest.raises(g.GuardrailError):
        g.guard_stock_write()


def test_las_cuatro_escrituras_de_stock_respetan_el_candado(monkeypatch):
    """Que el default sea 0 no sirve si algun tool no consulta el candado."""
    monkeypatch.delenv("BSALE_STOCK_WRITES_ENABLED", raising=False)
    tools, cli = _tools_de_escritura(monkeypatch, stock={(1, 1): 10.0})

    r = tools["bsale_recepcionar_stock"](variant_id=1, office_id=1, quantity=5, cost=9500)
    assert r["aplicado"] is False
    r = tools["bsale_consumir_stock"](variant_id=1, office_id=1, quantity=5)
    assert r["aplicado"] is False
    r = tools["bsale_ajustar_stock"](variant_id=1, office_id=1, quantity=12, cost=9500)
    assert r["aplicado"] is False
    r = tools["bsale_crear_traspaso_stock"](
        variant_id=1, office_origin_id=1, office_destination_id=2, quantity=5
    )
    assert r["aplicado"] is False

    assert cli.llamadas == [], "ninguna escritura de stock puede llegar a Bsale"


# ===========================================================================
# Segunda auditoria, 09-sep-2026 — Lote 1
# ===========================================================================

def test_render_yaml_no_reabre_ningun_candado():
    """render.yaml declaraba BSALE_STOCK_WRITES_ENABLED: "1". El codigo habia
    pasado a "0" el mismo dia, con dos tests que lo fijaban, pero ningun test
    miraba el yaml, que es lo que Render aplica. Sincronizar el blueprint
    (que es lo que DEPLOY.md manda para montar el disco) reabria las cuatro
    escrituras de stock por un camino que nadie relaciona con escritura."""
    import os
    import re

    aqui = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(aqui, "render.yaml"), encoding="utf-8") as fh:
        yaml_src = fh.read()

    for var in ("BSALE_PRICE_WRITES_ENABLED", "BSALE_STOCK_WRITES_ENABLED",
                "BSALE_CATALOG_WRITES_ENABLED"):
        m = re.search(r"key:\s*" + var + r"\s*\n\s*value:\s*\"?(\w+)\"?", yaml_src)
        assert m, f"{var} tiene que estar declarada en render.yaml, en 0"
        assert m.group(1) == "0", f"render.yaml declara {var}={m.group(1)}: sincronizar el blueprint reabre el candado"


class _SesionStockFalsa:
    """Emula db.session() para _stock_de_variantes: devuelve filas de stock_actual."""

    def __init__(self, filas):
        self.filas = filas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        from datetime import datetime, timezone
        Row = type("Row", (), {})
        out = []
        for vid, oid, q in self.filas:
            r = Row()
            r.variant_id, r.office_id, r.quantity = vid, oid, q
            r.office_name, r.updated_at = f"suc{oid}", datetime(2026, 9, 8, tzinfo=timezone.utc)
            out.append(r)
        return out


def test_una_variante_ausente_del_snapshot_no_es_stock_cero(monkeypatch):
    """Bug del commit 680d19c: snap.get(vid, {}) daba {} y sum({}) es 0.0,
    indistinguible de un cero real. Con la foto a medias, el briefing
    inventaba quiebres y la proyeccion inventaba compras. El camino viejo
    (una llamada a la API por variante) saltaba la variante ante un error.
    Ahora la ausente NO esta en el dict y va declarada en stock_meta."""
    tdb = pytest.importorskip("tools_intelligence_db")
    monkeypatch.setattr(tdb, "db_session", lambda: _SesionStockFalsa([(7, 1, 5.0), (7, 2, 0.0)]))

    snap, meta = tdb._stock_de_variantes([7, 99])

    assert 7 in snap and snap[7][1]["stock"] == 5.0
    assert 99 not in snap, "la ausente no puede aparecer como {} (= stock 0)"
    assert meta["variantes_sin_dato"] == [99]
    assert meta["variantes_con_fila"] == 1
    assert meta["actualizado_desde"].startswith("2026-09-08")


def test_los_tres_tools_saltan_la_variante_sin_dato():
    """Que el helper la excluya no sirve si el tool hace snap.get(vid, {})."""
    import inspect

    tdb = pytest.importorskip("tools_intelligence_db")
    src = _solo_codigo(inspect.getsource(tdb.register))
    # Los tres sitios que interpretan 0 como "compra ya"
    assert src.count("vid not in snap") >= 2, "quiebres y proyeccion tienen que saltar la ausente"
    assert "vid not in snap_briefing" in src, "el briefing tiene que saltar la ausente"
    assert "snap_briefing.get(vid, {})" not in src, "el briefing no puede tratar la ausente como {}"


def test_stock_meta_tiene_la_misma_forma_por_los_dos_caminos(monkeypatch):
    """Tres formas distintas de stock_meta segun el camino: un consumidor que
    lea variantes_pedidas reventaba con lista vacia, y el camino live -el que
    existe para cuando la frescura importa- no declaraba ninguna frescura."""
    tdb = pytest.importorskip("tools_intelligence_db")
    monkeypatch.setattr(tdb, "db_session", lambda: _SesionStockFalsa([]))

    _, vacia = tdb._stock_de_variantes([])
    _, snapshot = tdb._stock_de_variantes([1])
    live = tdb._meta_stock_live([1, 2])

    claves = {"fuente", "variantes_pedidas", "variantes_con_fila", "variantes_sin_dato",
              "actualizado_desde", "nota"}
    for m in (vacia, snapshot, live):
        assert claves <= set(m), f"faltan claves: {claves - set(m)}"
    assert live["actualizado_desde"] is not None, "el camino live tiene que declarar frescura"


def test_recepcionar_sin_costo_se_bloquea(monkeypatch):
    """Una recepcion sin cost entra a costo 0 y arrastra el costo promedio para
    siempre. ajustar_stock ya lo exigia; recepcionar lo dejaba pasar."""
    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")
    tools, cli = _tools_de_escritura(monkeypatch, stock={(1, 1): 10.0})

    r = tools["bsale_recepcionar_stock"](variant_id=1, office_id=1, quantity=5)
    assert r["aplicado"] is False and "costo 0" in r["bloqueado_por"]
    assert cli.llamadas == []

    r = tools["bsale_recepcionar_stock"](variant_id=1, office_id=1, quantity=5, cost=9500)
    assert r["aplicado"] is True
    assert cli.llamadas[0][2]["details"][0]["cost"] == 9500.0


def test_el_traspaso_recepciona_con_costo_o_no_recepciona(monkeypatch):
    """El traspaso hacia consumo + recepcion SIN costo en destino: la operacion
    rutinaria de las 11 tiendas contaminaba el costo promedio en cada
    movimiento. Ahora lee el promedio de Bsale, y si no puede, se bloquea."""
    import tools_writes as tw

    monkeypatch.setenv("BSALE_STOCK_WRITES_ENABLED", "1")
    tools, cli = _tools_de_escritura(monkeypatch, stock={(1, 1): 10.0})

    # Sin costo y sin poder leerlo -> bloqueado, nada escrito.
    monkeypatch.setattr(tw, "_costo_promedio_de", lambda c, v: None)
    r = tools["bsale_crear_traspaso_stock"](variant_id=1, office_origin_id=1,
                                           office_destination_id=2, quantity=3)
    assert r["aplicado"] is False and "costo 0" in r["bloqueado_por"]
    assert cli.llamadas == []

    # Con costo leido de Bsale -> dos POST y la recepcion lo lleva.
    monkeypatch.setattr(tw, "_costo_promedio_de", lambda c, v: 8200.0)
    r = tools["bsale_crear_traspaso_stock"](variant_id=1, office_origin_id=1,
                                           office_destination_id=2, quantity=3)
    paths = [c[1] for c in cli.llamadas]
    assert paths == ["/v1/stocks/consumptions.json", "/v1/stocks/receptions.json"]
    assert cli.llamadas[1][2]["details"][0]["cost"] == 8200.0

    # NaN e infinito: antes pasaban (NaN <= 0 es False).
    for mala in (float("nan"), float("inf")):
        cli.llamadas.clear()
        r = tools["bsale_crear_traspaso_stock"](variant_id=1, office_origin_id=1,
                                               office_destination_id=2, quantity=mala)
        assert r["aplicado"] is False and cli.llamadas == []


def test_validar_costo_rechaza_infinito():
    """`c != c or c < 0` dejaba pasar inf: inf < 0 es False."""
    g = pytest.importorskip("guardrails")
    for mala in (float("inf"), float("-inf"), float("nan"), -1):
        with pytest.raises(g.GuardrailError):
            g.validar_costo(mala)
    assert g.validar_costo(0) == 0.0 and g.validar_costo("9500") == 9500.0


def test_paginado_declara_truncado_si_bsale_devuelve_menos_de_lo_que_dice():
    """truncated se calculaba solo como total_count > max_items. Una pagina
    del medio vacia -documentado: "Bsale bajo carga devuelve 200 con items
    vacio, sin error"- pasaba como completa, y este fetch alimenta
    snapshot_documents: el hueco quedaba en Postgres con cara de total."""
    c = _client_con_paginas(total=396)
    get_real = c.get

    def get_con_hueco(path, params=None, use_cache=False):  # noqa: ANN001
        r = get_real(path, params=params, use_cache=use_cache)
        if int((params or {}).get("offset", 0)) == 100:
            return {"count": 396, "items": []}  # pagina 3 vacia
        return r

    c.get = get_con_hueco  # type: ignore[method-assign]
    r = c.paginated_fetch("/v1/documents.json", params={"limit": 50}, max_items=40000)
    assert r["fetched"] == 346
    assert r["truncated"] is True, "faltan 50 documentos y decia truncado: False"
    assert r["faltantes"] == 50


def test_paginado_declara_truncado_si_la_primera_pagina_viene_corta():
    c = _client_con_paginas(total=396)

    def get_corta(path, params=None, use_cache=False):  # noqa: ANN001
        return {"count": 396, "items": [{"id": i} for i in range(30)]}

    c.get = get_corta  # type: ignore[method-assign]
    r = c.paginated_fetch("/v1/documents.json", params={"limit": 50}, max_items=40000)
    assert r["truncated"] is True and r["faltantes"] == 366


def test_el_backfill_historico_no_bota_el_flag_de_truncado():
    """backfill.py seguia en paginated_get, que devuelve solo ["items"]. Es el
    bug de los 4.101 documentos de marzo-2025, en el archivo que escribe el
    historico. Lo mismo snapshot_variants, del que depende el filtro de
    servicios."""
    import inspect

    bf = pytest.importorskip("backfill")
    sn = pytest.importorskip("snapshot")
    for fn in (bf.backfill_documents, sn.snapshot_variants):
        src = _solo_codigo(inspect.getsource(fn))
        assert "paginated_get(" not in src, f"{fn.__name__} sigue botando el flag de truncado"
        assert "paginated_fetch(" in src
        assert "truncated" in src, f"{fn.__name__} tiene que MIRAR el flag, no solo obtenerlo"


def test_lookback_days_cero_no_revienta():
    """ZeroDivisionError en cinco tools con lookback_days=0, que el schema
    acepta. El agente veia un 500 sin causa."""
    import inspect

    tdb = pytest.importorskip("tools_intelligence_db")
    src_db = _solo_codigo(inspect.getsource(tdb.register))
    assert src_db.count("if lookback_days <= 0:") >= 4


def test_solo_codigo_funciona_sobre_un_tool_anidado():
    """inspect.getsource de un @mcp.tool viene indentado; ast.parse tiraba
    IndentationError y el except lo tragaba: los docstrings no se borraban.
    "adjustments.json" solo esta en el docstring de bsale_ajustar_stock."""
    import inspect

    tw = pytest.importorskip("tools_writes")
    src_register = inspect.getsource(tw.register)
    i = src_register.index("    def bsale_ajustar_stock")
    j = src_register.index("    @mcp.tool()", i)
    fuente_tool = src_register[i:j]  # viene con 4 espacios de indentacion
    assert "adjustments.json" in fuente_tool  # esta en el docstring
    assert "adjustments.json" not in _solo_codigo(fuente_tool), (
        "_solo_codigo tiene que borrar el docstring aunque el fuente venga indentado"
    )


# ===========================================================================
# Segunda auditoria, 09-sep-2026 — Lote 2: batch y base
# ===========================================================================

def test_la_corrida_de_stock_registra_su_estado_al_entrar():
    """El estado "completo" se escribia solo al FINAL. Un proceso muerto a mitad
    dejaba el completo:true de la corrida anterior, y la siguiente no repetia:
    medio inventario servido 12 horas. Ahora se escribe al entrar, tras la
    pagina 0, con completo:False y en_curso:True."""
    import inspect

    sn = pytest.importorskip("snapshot")
    src = _solo_codigo(inspect.getsource(sn.snapshot_stock))
    primera = src.index('_registrar_estado("stock_ultima_corrida"')
    ultima = src.rindex('_registrar_estado("stock_ultima_corrida"')
    assert primera != ultima, "tiene que registrarse dos veces: al entrar y al salir"
    # La primera va ANTES del pool de descarga y dice en_curso.
    assert primera < src.index("ThreadPoolExecutor(max_workers=workers)")
    assert '"en_curso": True' in src[primera:primera + 400]
    assert '"en_curso": False' in src[ultima:ultima + 400]


def test_la_retencion_commitea_por_lote(monkeypatch):
    """Una sola transaccion para 153 lotes: cualquier corte era rollback total
    y cero progreso. Ahora cada lote abre su sesion y commitea al salir."""
    import retention as rt

    sesiones_abiertas = []

    class Sesion:
        def __init__(self):
            self.ejecutados = []

        def __enter__(self):
            sesiones_abiertas.append(self)
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt, params=None):
            self.ejecutados.append((str(stmt), params))
            # 3 lotes llenos y uno corto
            n = 50000 if len(sesiones_abiertas) <= 3 else 7
            return type("R", (), {"rowcount": n})()

    monkeypatch.setattr(rt, "db_session", lambda: Sesion())
    borradas, pendientes = rt._borrar_por_lotes("TRUE", {}, 50000)

    assert borradas == 150007 and pendientes is False
    assert len(sesiones_abiertas) == 4, "una sesion (= una transaccion) por lote"
    for s in sesiones_abiertas:
        assert any("statement_timeout" in e[0] for e in s.ejecutados), "cada lote con su SET LOCAL"


def test_la_retencion_respeta_max_lotes(monkeypatch):
    import retention as rt

    class Sesion:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt, params=None):
            return type("R", (), {"rowcount": 50000})()

    monkeypatch.setattr(rt, "db_session", lambda: Sesion())
    borradas, pendientes = rt._borrar_por_lotes("TRUE", {}, 50000, max_lotes=3)
    assert borradas == 150000 and pendientes is True


def test_el_cron_toma_un_candado_y_se_salta_si_esta_ocupado(monkeypatch):
    """Sin candado, una corrida incompleta se auto-solapaba cada 30 min:
    4+4 = 8 conexiones contra el umbral de 6 de Bsale."""
    sync = pytest.importorskip("sync_incremental")

    corrio = []
    monkeypatch.setattr(sync, "_run", lambda modo: corrio.append(modo) or 0)

    monkeypatch.setattr(sync, "_tomar_candado_de_corrida", lambda: (None, "ocupado"))
    assert sync.run("auto") == 0
    assert corrio == [], "con el candado tomado por otra corrida, esta no corre"

    soltados = []
    monkeypatch.setattr(sync, "_tomar_candado_de_corrida", lambda: ("CONN", "ok"))
    monkeypatch.setattr(sync, "_soltar_candado_de_corrida", lambda c: soltados.append(c))
    assert sync.run("auto") == 0
    assert corrio == ["auto"] and soltados == ["CONN"]

    # Sin poder consultar Postgres se sigue SIN candado, no se convierte el
    # cron en un no-op permanente.
    monkeypatch.setattr(sync, "_tomar_candado_de_corrida", lambda: (None, "error"))
    assert sync.run("auto") == 0
    assert corrio == ["auto", "auto"]


def test_el_candado_es_un_advisory_lock_de_sesion():
    import inspect

    sync = pytest.importorskip("sync_incremental")
    src = _solo_codigo(inspect.getsource(sync._tomar_candado_de_corrida))
    assert "pg_try_advisory_lock" in src, "tiene que ser try_: nunca esperar al otro"
    assert "pg_advisory_xact_lock" not in src, "de sesion, no de transaccion"


def test_un_documento_sin_lineas_queda_marcado_como_procesado():
    """Devolver [] lo dejaba como candidato eterno; con oldest_first, 2.000 de
    esos en la cabeza de la cola = el backfill nunca avanza."""
    import inspect

    sn = pytest.importorskip("snapshot")
    src = _solo_codigo(inspect.getsource(sn.snapshot_details))
    assert "_centinela(cand, -1)" in src, "documento sin lineas -> centinela -1"
    assert "_centinela(cand, -2)" in src, "documento truncado -> centinela -2, no candidato eterno"
    assert "docs_sin_lineas" in src and "docs_con_mas_de_2000_lineas" in src


def test_el_backfill_de_detalle_corta_por_pendientes_y_no_por_la_tanda():
    import inspect

    bf = pytest.importorskip("backfill")
    src = _solo_codigo(inspect.getsource(bf.backfill_details))
    assert 'get("remaining_to_process", 0) == 0' in src
    assert "raise RuntimeError" in src, "una tanda entera fallida no puede ser un backfill exitoso"


def test_la_retencion_si_marca_la_corrida_y_el_codigo_lo_dice():
    """Comentario y codigo decian lo contrario. Se decidio por el codigo."""
    import inspect

    sync = pytest.importorskip("sync_incremental")
    src = inspect.getsource(sync._run)
    assert "NO marcan la corrida" not in src
    assert 'results["retention_error"]' in _solo_codigo(src)
    assert 'results["retention_warning"]' not in _solo_codigo(src)


def test_el_status_expone_la_ultima_corrida_de_stock():
    import inspect

    ts = pytest.importorskip("tools_snapshot")
    src = _solo_codigo(inspect.getsource(ts.register))
    i = src.index("def bsale_snapshot_status")
    j = src.index("def bsale_ventas_fast")
    cuerpo = src[i:j]
    assert "stock_ultima_corrida" in cuerpo
    assert '"ultima_corrida": ultima_corrida_stock' in cuerpo


def test_retencion_run_se_rehusa_con_stock_actual_vacia():
    import inspect

    ts = pytest.importorskip("tools_snapshot")
    src = _solo_codigo(inspect.getsource(ts.register))
    i = src.index("def bsale_mcp_retencion_run")
    j = src.index("def bsale_snapshot_status")
    cuerpo = src[i:j]
    assert "SELECT count(*) FROM stock_actual" in cuerpo
    assert "max_lotes: int = 4" in cuerpo


# ===========================================================================
# Segunda auditoria, 09-sep-2026 — Lote 3: seguridad y configuracion
# ===========================================================================

def test_main_no_arranca_sin_credencial(monkeypatch):
    """Falla VISIBLE en el deploy en vez de servicio abierto en silencio."""
    import server

    _limpiar_candado(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 1


def test_el_secreto_de_la_url_se_compara_en_tiempo_constante():
    import inspect

    import server

    src = _solo_codigo(inspect.getsource(server.BearerAuthMiddleware.dispatch))
    assert "secrets.compare_digest(partes[2]" in src
    assert "path == esperado" not in src and "startswith(esperado" not in src


def test_el_middleware_rechaza_cuerpos_gigantes(monkeypatch):
    import asyncio

    import server

    monkeypatch.setenv("MCP_URL_SECRET", "abc123")
    mw = server.BearerAuthMiddleware(app=None)
    llamados = []

    async def call_next(req):  # noqa: ANN001
        llamados.append(req)
        return "OK"

    grande = _Req(headers={"content-length": str(server.MAX_REQUEST_BYTES + 1)}, path="/mcp/abc123")
    resp = asyncio.run(mw.dispatch(grande, call_next))
    assert getattr(resp, "status_code", None) == 413 and llamados == []

    normal = _Req(headers={"content-length": "512"}, path="/mcp/abc123")
    assert asyncio.run(mw.dispatch(normal, call_next)) == "OK"

    # Y con el secreto equivocado en la URL, 401, no 200.
    malo = _Req(headers={}, path="/mcp/abc124")
    resp = asyncio.run(mw.dispatch(malo, call_next))
    assert getattr(resp, "status_code", None) == 401


def test_uvicorn_limita_la_concurrencia():
    import inspect

    import server

    assert "limit_concurrency=" in _solo_codigo(inspect.getsource(server.main))


def test_sentry_no_manda_la_url_con_el_secreto():
    """La integracion ASGI adjunta request.url a cada evento, y aca la URL es
    la credencial. access_log=False cerro uvicorn; esta es la misma puerta un
    piso mas arriba."""
    import inspect

    import server

    src = inspect.getsource(server)
    i = src.index("sentry_sdk.init(")
    bloque = src[i:i + 600]
    assert "before_send=_sin_secreto_en_la_url" in bloque
    assert "send_default_pii=False" in bloque
    # El regex que usa, sobre una URL real
    import re
    assert "S3CR3T0" not in re.sub(r"/mcp/[^/?#\s]+", "/mcp/<redacted>",
                                   "https://x.onrender.com/mcp/S3CR3T0/tools?x=1")
    assert 're.sub(r"/mcp/[^/?#\\s]+"' in src


def test_redact_censura_por_subcadena_y_por_valor():
    """La lista vieja era de 11 nombres exactos: 'accessToken' (camelCase de
    Bsale) pasaba en claro. Y un token dentro de una note tambien."""
    from audit import _redact

    out = _redact({
        "accessToken": "abc", "Access-Token": "abc", "refresh_token": "abc",
        "x-api-key": "abc", "clientSecret": "abc", "bearer": "abc",
        "variantId": 117733, "quantity": 5, "note": "recepcion normal",
        "details": [{"cost": 9500, "webhookSignature": "zzz"}],
        "pegado": "token=abcdefghijklmnop123456",
        "suelto": "dGhpcy1sb29rcy1saWtlLWEtdG9rZW4tMTIzNDU2",
    })
    for k in ("accessToken", "Access-Token", "refresh_token", "x-api-key", "clientSecret", "bearer"):
        assert out[k] == "***REDACTED***", k
    assert out["details"][0]["webhookSignature"] == "***REDACTED***"
    assert out["pegado"] == "***REDACTED***" and out["suelto"] == "***REDACTED***"
    # Lo inocente queda
    assert out["variantId"] == 117733 and out["quantity"] == 5
    assert out["note"] == "recepcion normal" and out["details"][0]["cost"] == 9500


def test_get_client_y_get_cache_tienen_lock():
    import inspect

    import bsale_client
    import cache

    for fn, lock in ((bsale_client.get_client, "_client_lock"), (cache.get_cache, "_cache_lock")):
        src = _solo_codigo(inspect.getsource(fn))
        assert f"with {lock}:" in src, f"{fn.__name__} sigue siendo check-then-act sin lock"


def test_la_cache_persiste_de_forma_atomica(tmp_path, monkeypatch):
    import cache as c

    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    fc = c.FileCache()
    fc.set("k", {"v": 1}, ttl_seconds=60)
    assert (tmp_path / "cache.json").exists()
    assert not list(tmp_path.glob("*.tmp")), "no puede quedar un .tmp huerfano"
    import inspect
    assert "os.replace(" in _solo_codigo(inspect.getsource(c.FileCache._persist))


def test_las_transitivas_que_sostienen_el_candado_estan_pinneadas():
    """fastmcp-slim[server] declara starlette>=1.0.1 sin techo, y server.py
    importa starlette directamente para el unico middleware de auth."""
    import os

    aqui = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(aqui, "requirements.txt"), encoding="utf-8") as fh:
        req = fh.read()
    for pkg in ("starlette==", "mcp==", "sse-starlette==", "anyio==", "h11=="):
        assert pkg in req, f"{pkg} tiene que estar pinneada"
    assert "apscheduler" not in req.lower().split("# apscheduler")[0], "dependencia muerta"


def test_los_docs_no_ensenan_a_dejar_el_servidor_abierto():
    import os

    aqui = os.path.dirname(os.path.abspath(__file__))
    for nombre in ("DEPLOY.md", "README.md"):
        with open(os.path.join(aqui, nombre), encoding="utf-8") as fh:
            txt = fh.read()
        assert "no requiere auth" not in txt.lower(), nombre
        assert "MCP_URL_SECRET" in txt, f"{nombre} tiene que explicar la credencial"


# ===========================================================================
# Segunda auditoria, 09-sep-2026 — Lote 4: contrato hacia el agente
# ===========================================================================

def _cuerpo_de(modulo, nombre_tool, siguiente):
    import inspect
    src = _solo_codigo(inspect.getsource(modulo.register))
    i = src.index(f"def {nombre_tool}(")
    j = src.index(f"def {siguiente}(", i) if siguiente else len(src)
    return src[i:j]


def test_los_montos_declaran_que_son_brutos_con_iva():
    """"Neto" significaba dos cosas: NC restadas (ranking_fast) y sin IVA
    (ventas_fast). Un agente reportaba bruto como neto: 19% de error."""
    tdb = pytest.importorskip("tools_intelligence_db")
    ts = pytest.importorskip("tools_snapshot")

    assert '"unidad_montos": "CLP bruto con IVA' in _cuerpo_de(tdb, "bsale_ranking_sucursales_fast", "bsale_segmentacion_clientes_rfm_fast")
    assert '"unidad_montos": "CLP bruto con IVA' in _cuerpo_de(tdb, "bsale_briefing_diario", "bsale_top_productos_fast")
    cuerpo_vf = _cuerpo_de(ts, "bsale_ventas_fast", "bsale_conciliacion_venta")
    assert '"venta_oficial_sin_iva"' in cuerpo_vf and '"venta_oficial_neta"' not in cuerpo_vf


def test_ventas_fast_no_promete_guias_y_suma_excluidos_con_signo():
    ts = pytest.importorskip("tools_snapshot")
    cuerpo = _cuerpo_de(ts, "bsale_ventas_fast", "bsale_conciliacion_venta")
    assert "guias de despacho +" not in cuerpo
    assert "func.sum(d.total_amount)" not in cuerpo, "excluidos tiene que usar signed_amount"


def test_el_briefing_usa_una_sola_zona_horaria():
    tdb = pytest.importorskip("tools_intelligence_db")
    cuerpo = _cuerpo_de(tdb, "bsale_briefing_diario", "bsale_top_productos_fast")
    assert "hoy_cl = now.astimezone(ZoneInfo(TZ_NEGOCIO)).date()" in cuerpo
    assert "week_ago = hoy_cl - timedelta" in cuerpo
    assert '"fecha_briefing": hoy_cl.isoformat()' in cuerpo
    assert "now.date()" not in cuerpo, "quedo una fecha en UTC"
    assert '"cobertura_detalle"' in cuerpo


def test_sin_iva_es_la_inversa_de_con_iva(monkeypatch):
    g = pytest.importorskip("guardrails")
    monkeypatch.setenv("BSALE_IVA_PCT", "19")
    assert round(g.sin_iva(29990), 6) == round(25201.6806722689, 6)
    assert g.con_iva(g.sin_iva(32990)) == 32990.0
    monkeypatch.setenv("BSALE_IVA_PCT", "0")
    assert g.sin_iva(1000) == 1000.0


def test_precio_variante_declara_que_new_price_es_neto():
    import inspect

    tw = pytest.importorskip("tools_writes")
    src = inspect.getsource(tw.register)
    i = src.index("def bsale_actualizar_precio_variante")
    j = src.index("def bsale_activar_variante", i)
    assert "NETO" in src[i:j] and "sin_iva" in src[i:j]


def test_parametros_muertos_y_cifras_contradictorias_fuera():
    import inspect

    tdb = pytest.importorskip("tools_intelligence_db")
    ts = pytest.importorskip("tools_snapshot")
    tm = pytest.importorskip("tools_mapping")
    sob = _cuerpo_de(tdb, "bsale_sobrestockeos_detectados", "bsale_ranking_sucursales_fast")
    assert "if top_check > 200:" in sob
    assert "capital_tied" not in inspect.getsource(tdb.register).split("def bsale_sobrestockeos_detectados")[1].split("def bsale_ranking")[0].split('"""')[1]
    src_ts = inspect.getsource(ts.register)
    run_now = src_ts[src_ts.index("def bsale_snapshot_run_now"):src_ts.index("def bsale_snapshot_backfill_rango")]
    assert "~2 horas" not in run_now and "~10 min" not in run_now, "dos cifras distintas para la misma corrida"
    assert "max_docs<=4000" not in src_ts
    assert "not_implemented" not in inspect.getsource(tm)


def test_stock_agregado_lee_el_snapshot_por_default(monkeypatch):
    import tools_stocks as tst

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    llamado = []
    monkeypatch.setattr(tst, "_stock_agregado_desde_snapshot", lambda oid: llamado.append(oid) or {"fuente": "snap"})
    tools = _tools_de(tst)
    r = tools["bsale_stock_agregado"](officeid=3)
    assert r["fuente"] == "snap" and llamado == [3]


def test_el_sku_tiene_candado_propio(monkeypatch):
    """Cuando el catalogo se abra para editar una descripcion, cambiar el
    SKU no puede colarse por la misma ventana."""
    import tools_writes as tw

    monkeypatch.setenv("BSALE_CATALOG_WRITES_ENABLED", "1")
    monkeypatch.delenv("BSALE_SKU_WRITES_ENABLED", raising=False)
    tools, cli = _tools_de_escritura(monkeypatch, stock={})

    r = tools["bsale_actualizar_variante"](variant_id=1, code="NUEVO-SKU")
    assert r["aplicado"] is False and "SKU" in r["bloqueado_por"]
    assert cli.llamadas == []

    r = tools["bsale_actualizar_variante"](variant_id=1, description="solo descripcion")
    assert r["aplicado"] is True

    monkeypatch.setenv("BSALE_SKU_WRITES_ENABLED", "1")
    r = tools["bsale_actualizar_variante"](variant_id=1, code="NUEVO-SKU")
    assert r["aplicado"] is True
    assert tw._flag_env("BSALE_SKU_WRITES_ENABLED") is True


def test_el_digest_de_ventas_declara_cobertura_de_detalle():
    import inspect

    dg = pytest.importorskip("digests")
    src = _solo_codigo(inspect.getsource(dg.build_ventas_periodo))
    assert '"cobertura_detalle"' in src and "con_detalle" in src


# ===========================================================================
# Segunda auditoria, 09-sep-2026 — Lote 5: tests de COMPORTAMIENTO para los
# tools que deciden compras (tools_intelligence_db.py estaba al 7,3%: ningun
# tool se ejecutaba en ningun test).
# ===========================================================================

class _Fila:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _SesionVelocity:
    """db.session() falso: devuelve las mismas filas de velocity a cualquier select."""

    def __init__(self, filas):
        self.filas = filas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        filas = self.filas

        class R:
            def fetchall(self_inner):
                return filas

            def first(self_inner):
                return filas[0] if filas else None

            def scalar(self_inner):
                return None

        return R()


def _tools_db_con(monkeypatch, vel_rows, snap, meta=None):
    tdb = pytest.importorskip("tools_intelligence_db")
    import bsale_client as _bc
    # official_sale_conditions() importa y llama bsale_client.sales_note_type_ids()
    # AL LLAMARSE, y eso va a la API de Bsale (la primera corrida de este
    # test pego un 401 real). Nunca red desde un test.
    monkeypatch.setattr(_bc, "sales_note_type_ids", lambda: frozenset({3, 23, 24, 26, 27}))
    monkeypatch.setattr(tdb, "db_session", lambda: _SesionVelocity(vel_rows))
    monkeypatch.setattr(tdb, "cobertura_ultimos_dias", lambda *a, **k: {"pct": 100})
    meta = meta or {"fuente": "fake", "variantes_pedidas": len(vel_rows),
                    "variantes_con_fila": len(snap),
                    "variantes_sin_dato": [r.variant_id for r in vel_rows if r.variant_id not in snap],
                    "actualizado_desde": "2026-09-08T18:43:14+00:00", "nota": ""}
    monkeypatch.setattr(tdb, "_stock_de_variantes", lambda vids, office_id=None: (snap, meta))
    return _tools_de(tdb)


def test_proyeccion_de_compras_calcula_y_salta_la_variante_sin_dato(monkeypatch):
    """Variante 7: 90 u en 90 dias = 1/dia, stock 10, cobertura 45 dias ->
    pedir 35. Variante 99: sin fila en el snapshot -> NO aparece con orden
    inventada (bug #2 de la segunda auditoria)."""
    vel = [_Fila(variant_id=7, total_qty=90.0, code="A", desc="a"),
           _Fila(variant_id=99, total_qty=45.0, code="B", desc="b")]
    snap = {7: {1: {"office_name": "s1", "stock": 4.0}, 2: {"office_name": "s2", "stock": 6.0}}}
    tools = _tools_db_con(monkeypatch, vel, snap)

    r = tools["bsale_proyeccion_compras_fast"](target_coverage_days=45, lookback_days=90)

    assert r["checked_variants"] == 2
    recs = {x["variant_id"]: x for x in r["recommendations"]}
    assert 99 not in recs, "sin dato NO es stock 0"
    assert recs[7]["stock_total"] == 10.0
    assert recs[7]["velocity_per_day"] == 1.0
    assert recs[7]["order_qty_suggested"] == 35.0
    assert r["stock_meta"]["variantes_sin_dato"] == [99]
    assert r["source"].startswith("snapshot")


def test_quiebres_proyectados_ordena_por_dias_y_respeta_el_horizonte(monkeypatch):
    vel = [_Fila(variant_id=1, total_qty=30.0, code="x", desc=""),   # 1/dia
           _Fila(variant_id=2, total_qty=30.0, code="y", desc=""),   # 1/dia
           _Fila(variant_id=3, total_qty=30.0, code="z", desc="")]   # 1/dia
    snap = {1: {1: {"office_name": "", "stock": 3.0}},     # 3 dias -> critico
            2: {1: {"office_name": "", "stock": 40.0}},    # 40 dias -> fuera del horizonte 14
            3: {1: {"office_name": "", "stock": 10.0}}}    # 10 dias -> bajo
    tools = _tools_db_con(monkeypatch, vel, snap)

    r = tools["bsale_quiebres_proyectados_fast"](days_horizon=14, lookback_days=30)

    ids = [x["variant_id"] for x in r["risks"]]
    assert ids == [1, 3], "ordenado por dias hasta quiebre y sin la que tiene 40 dias"
    assert r["risks"][0]["category"] == "critico" and r["risks"][1]["category"] == "bajo"
    assert r["risks"][0]["stock_by_office"] == {1: 3.0}


def test_lookback_cero_devuelve_motivo_y_no_revienta(monkeypatch):
    tools = _tools_db_con(monkeypatch, [], {})
    for nombre in ("bsale_quiebres_proyectados_fast", "bsale_proyeccion_compras_fast",
                   "bsale_sobrestockeos_detectados"):
        r = tools[nombre](lookback_days=0)
        assert r.get("aplicado") is False and "lookback_days" in r["motivo"], nombre


def test_sobrestockeos_ignora_stock_cero_y_valoriza_a_precio_de_venta(monkeypatch):
    vel = [_Fila(variant_id=5, total_qty=3.0, total_revenue=30000.0, code="q", desc=""),  # 0.1/dia
           _Fila(variant_id=6, total_qty=3.0, total_revenue=30000.0, code="w", desc="")]
    snap = {5: {1: {"office_name": "s1", "stock": 100.0}},   # 1000 dias de cobertura
            6: {1: {"office_name": "s1", "stock": 0.0}}}     # cero: no es sobrestock
    tools = _tools_db_con(monkeypatch, vel, snap)

    r = tools["bsale_sobrestockeos_detectados"](min_coverage_days=180, lookback_days=30, min_velocity=0.05)

    ids = [x["variant_id"] for x in r["sobrestockeos"]]
    assert ids == [5]
    s = r["sobrestockeos"][0]
    assert s["coverage_days"] == 1000.0
    # valorizado a precio de venta: 100 unidades x (30000/3 por unidad) = 1.000.000
    assert s["valorizado_a_precio_venta_clp"] == 1_000_000.0
    assert "PRECIO DE VENTA" in r["nota_valorizacion"]


def test_sobrestockeos_rechaza_top_check_mayor_a_200(monkeypatch):
    tools = _tools_db_con(monkeypatch, [], {})
    r = tools["bsale_sobrestockeos_detectados"](top_check=201)
    assert r["aplicado"] is False and "200" in r["motivo"]


# ===========================================================================
# Retencion de documents_snapshot.raw (09-sep-2026, Ley 21.719)
# ===========================================================================
# raw guardaba la ficha completa del cliente de cada documento (nombre, RUT,
# correo, telefono, direccion) sin politica. Pasados RAW_RETENTION_MONTHS
# meses se minimiza a los campos de negocio; la fila y las columnas tipadas
# quedan, asi que la venta oficial no cambia.

_PII_EN_RAW = (
    "firstName", "lastName", "email", "phone", "code", "company", "address",
    "municipality", "city", "activity", "ted", "token", "urlPdf",
    "urlPublicView", "urlPublicViewOriginal", "urlPdfOriginal", "urlXml",
    "urlTimbre", "note", "responseMsgSii", "messageBodyFormat", "facebook",
    "twitter",
)


def test_el_raw_minimizado_no_conserva_datos_del_cliente():
    import retention as rt

    sql = rt._sql_raw_minimo()
    for k in _PII_EN_RAW:
        assert f"'{k}'" not in sql, f"{k} sobrevive en el raw minimizado"
    assert sql.startswith("jsonb_strip_nulls(jsonb_build_object(")
    # del cliente queda solo el id (ya tipado) y dos flags que no identifican
    assert "'client', jsonb_build_object('id', raw->'client'->'id'" in sql
    for k in ("totalAmount", "netAmount", "emissionDate", "number", "state"):
        assert f"'{k}', raw->'{k}'" in sql
    # con CAST: sin el, psycopg no infiere el tipo dentro de jsonb_build_object
    # y Postgres tira IndeterminateDatatype (la primera corrida real fallo asi)
    assert f"'{rt.RAW_MARCA}', jsonb_build_object('minimizado', CAST(:ts AS text), 'meses', CAST(:meses AS integer))" in sql
    # el SQL sale de las tuplas: la lista que se audita es la que se aplica
    for k in rt.RAW_CAMPOS_QUE_QUEDAN:
        assert f"'{k}', raw->'{k}'" in sql
    for padre, hijos in rt.RAW_SUBCAMPOS_QUE_QUEDAN.items():
        for h in hijos:
            assert f"'{h}', raw->'{padre}'->'{h}'" in sql
    todos = set(rt.RAW_CAMPOS_QUE_QUEDAN) | {
        h for hs in rt.RAW_SUBCAMPOS_QUE_QUEDAN.values() for h in hs
    }
    assert not (todos & set(_PII_EN_RAW))


def test_el_corte_de_raw_es_al_dia_1_de_hace_n_meses():
    """Al dia 1 para calzar con hist_end(), que relee cada mes cerrado una vez."""
    from datetime import date

    import retention as rt

    assert rt.raw_corte(date(2026, 9, 9), 6) == date(2026, 3, 1)
    assert rt.raw_corte(date(2026, 3, 15), 6) == date(2025, 9, 1)
    assert rt.raw_corte(date(2026, 1, 1), 12) == date(2025, 1, 1)
    assert rt.raw_corte(date(2026, 2, 28), 1) == date(2026, 1, 1)
    assert rt.raw_corte(date(2026, 9, 9), 0) is None, "0 meses = apagado"
    assert rt.raw_corte(date(2026, 9, 9)) == rt.raw_corte(date(2026, 9, 9), rt.RAW_RETENTION_MONTHS)


def test_el_predicado_de_raw_y_su_indice_parcial_son_la_misma_expresion():
    """Si difieren, el planner no usa el indice y cada corrida del cron vuelve
    a recorrer 160k filas de JSONB para no encontrar nada."""
    import retention as rt

    marca = f"NOT (raw ? '{rt.RAW_MARCA}')"
    assert marca in rt.RAW_PREDICADO and marca in rt.RAW_INDICE_SQL
    assert "emission_date < :corte" in rt.RAW_PREDICADO
    assert rt.RAW_INDICE_SQL.startswith("CREATE INDEX IF NOT EXISTS")


def test_la_minimizacion_de_raw_es_un_update_no_un_delete():
    import inspect

    import retention as rt

    src = _solo_codigo(inspect.getsource(rt._minimizar_raw_por_lotes))
    assert "UPDATE documents_snapshot" in src and "SET raw =" in src
    assert "DELETE" not in src
    todo = _solo_codigo(inspect.getsource(rt))
    assert "DELETE FROM documents_snapshot" not in todo
    assert "DELETE FROM document_details_snapshot" not in todo


class _SesionRaw:
    """Sesion falsa: registra cada transaccion y responde por tipo de sentencia."""

    abiertas: list = []

    def __init__(self, rowcounts, count=None):
        self._rowcounts = rowcounts
        self._count = count
        self.ejecutados = []

    def __enter__(self):
        _SesionRaw.abiertas.append(self)
        if len(_SesionRaw.abiertas) > 50:
            raise AssertionError("bucle sin salida: mas de 50 transacciones")
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        s = str(stmt)
        self.ejecutados.append((s, params))
        if "count(*)" in s:
            return type("R", (), {"scalar_one": lambda self_: self._count})()
        if s.startswith("UPDATE") or "UPDATE documents_snapshot" in s:
            n = self._rowcounts.pop(0) if self._rowcounts else 0
            return type("R", (), {"rowcount": n})()
        return type("R", (), {"rowcount": 0})()


def test_la_minimizacion_de_raw_commitea_por_lote(monkeypatch):
    from datetime import date

    import retention as rt

    _SesionRaw.abiertas = []
    filas = [5000, 5000, 7]  # dos lotes llenos y uno corto
    monkeypatch.setattr(rt, "db_session", lambda: _SesionRaw(filas))

    minimizadas, pendientes = rt._minimizar_raw_por_lotes(date(2026, 3, 1), 5000)

    assert (minimizadas, pendientes) == (10007, False)
    assert len(_SesionRaw.abiertas) == 3, "una sesion (= una transaccion) por lote"
    for s in _SesionRaw.abiertas:
        assert any("statement_timeout" in e[0] for e in s.ejecutados)
        upd = [e for e in s.ejecutados if "UPDATE documents_snapshot" in e[0]]
        assert len(upd) == 1
        sql, params = upd[0]
        assert rt.RAW_PREDICADO in sql and "LIMIT :batch" in sql
        assert params["corte"] == date(2026, 3, 1) and params["batch"] == 5000
        assert params["meses"] == rt.RAW_RETENTION_MONTHS


def test_la_minimizacion_de_raw_respeta_max_lotes(monkeypatch):
    from datetime import date

    import retention as rt

    _SesionRaw.abiertas = []
    monkeypatch.setattr(rt, "db_session", lambda: _SesionRaw([5000] * 40))
    minimizadas, pendientes = rt._minimizar_raw_por_lotes(date(2026, 3, 1), 5000, max_lotes=2)
    assert (minimizadas, pendientes) == (10000, True)
    assert len(_SesionRaw.abiertas) == 2


def test_purge_documents_raw_apagado_no_toca_la_base(monkeypatch):
    import retention as rt

    def no_deberia(*a, **k):
        raise AssertionError("con RAW_RETENTION_MONTHS=0 no se abre ninguna sesion")

    monkeypatch.setattr(rt, "RAW_RETENTION_MONTHS", 0)
    monkeypatch.setattr(rt, "db_session", no_deberia)
    out = rt.purge_documents_raw()
    assert out["aplicado"] is False and "apagada" in out["motivo"]


def test_purge_documents_raw_crea_el_indice_cuenta_y_minimiza(monkeypatch):
    import retention as rt

    monkeypatch.setattr(rt, "RAW_RETENTION_MONTHS", 6)

    # Sin pendientes: crea el indice, cuenta, y NO ejecuta ningun UPDATE.
    _SesionRaw.abiertas = []
    monkeypatch.setattr(rt, "db_session", lambda: _SesionRaw([], count=0))
    out = rt.purge_documents_raw()
    assert out["pendientes_antes"] == 0 and out["minimizadas"] == 0
    assert out["quedan_pendientes"] is False and out["meses"] == 6
    assert "corte_emission_date" in out
    todo = [e[0] for s in _SesionRaw.abiertas for e in s.ejecutados]
    assert any("CREATE INDEX IF NOT EXISTS ix_documents_raw_pendiente" in e for e in todo)
    assert not any("UPDATE documents_snapshot" in e for e in todo)

    # Con pendientes: minimiza y declara cuantas quedan.
    _SesionRaw.abiertas = []
    monkeypatch.setattr(rt, "db_session", lambda: _SesionRaw([5000, 5000], count=12345))
    out = rt.purge_documents_raw(max_lotes=2)
    assert out["minimizadas"] == 10000 and out["quedan_pendientes"] is True
    assert out["pendientes_despues"] == 2345


def test_apply_retention_incluye_raw_y_su_fallo_marca_la_corrida(monkeypatch):
    import retention as rt

    monkeypatch.setattr(rt, "purge_stock_snapshots", lambda **kw: {"borradas_total": 0})
    monkeypatch.setattr(rt, "purge_variants_snapshots", lambda: 0)

    def revienta(**kw):
        raise RuntimeError("timeout simulado")

    monkeypatch.setattr(rt, "purge_documents_raw", revienta)
    out = rt.apply_retention(max_lotes=3)
    assert out["hubo_error"] is True and "documents_raw_error" in out

    visto = {}
    monkeypatch.setattr(rt, "purge_documents_raw", lambda **kw: visto.update(kw) or {"minimizadas": 1})
    out = rt.apply_retention(max_lotes=3)
    assert out["hubo_error"] is False and out["documents_raw"] == {"minimizadas": 1}
    assert visto == {"max_lotes": 3}, "el tope de lotes del cron llega hasta raw"


# ===========================================================================
# bsale_ventas_fast y bsale_conciliacion_venta SE EJECUTAN (09-sep-2026)
# ===========================================================================
# Son los dos tools que mas se miran y hasta hoy ningun test los corria. Se
# ejecutan con una sesion falsa que responde por FORMA de consulta (la SQL
# compilada en dialecto Postgres) y un cliente falso: sin red, sin base.
# official_sale_conditions() llama a la API (sales_note_type_ids): se
# monkeypatchea SIEMPRE.

_NOTAS_DE_VENTA = frozenset({3, 23, 24, 26, 27})


def _compilar_con_params(stmt):
    from sqlalchemy.dialects import postgresql

    c = stmt.compile(dialect=postgresql.dialect())
    return str(c), dict(c.params)


class _SesionVentas:
    """Responde a cada select de bsale_ventas_fast por su forma."""

    def __init__(self, total, by_office=(), by_doctype=(), by_day=(), excluidos=None, docs=()):
        self.total, self.by_office, self.by_doctype = total, list(by_office), list(by_doctype)
        self.by_day, self.excluidos, self.docs = list(by_day), excluidos, list(docs)
        self.consultas = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        sql, p = _compilar_con_params(stmt)
        self.consultas.append((sql, p))
        if "date_trunc" in sql:
            filas, uno = self.by_day, None
        elif "GROUP BY documents_snapshot.office_id" in sql:
            filas, uno = self.by_office, None
        elif "GROUP BY documents_snapshot.document_type_id" in sql:
            filas, uno = self.by_doctype, None
        elif "NOT (" in sql:
            filas, uno = None, self.excluidos
        elif "ORDER BY documents_snapshot.emission_date DESC" in sql:
            filas, uno = self.docs, None
        else:
            filas, uno = None, self.total

        class R:
            def all(self_):
                return filas

            def one(self_):
                return uno

        return R()


def _ventas_fast_con(monkeypatch, sesion, lag=2.0):
    ts = pytest.importorskip("tools_snapshot")
    import bsale_client as _bc
    import db as _db

    monkeypatch.setattr(_bc, "sales_note_type_ids", lambda: _NOTAS_DE_VENTA)
    monkeypatch.setattr(ts, "db_session", lambda: sesion)
    monkeypatch.setattr(_db, "snapshot_lag_hours", lambda: lag)
    return _tools_de(ts)["bsale_ventas_fast"]


def test_ventas_fast_arma_la_respuesta_desde_el_snapshot(monkeypatch):
    from datetime import datetime, timezone

    sesion = _SesionVentas(
        total=_Fila(docs=10, nc=2, total=1_000_000.0, neto=840_336.0),
        by_office=[_Fila(office_id=1, office_name="E-Commerce ", docs=7, nc=1, total=700_000.0),
                   _Fila(office_id=3, office_name="Dos Caracoles", docs=3, nc=1, total=300_000.0)],
        by_doctype=[_Fila(document_type_id=1, document_type_name="BOLETA ELECTRÓNICA T", docs=9, total=900_000.0),
                    _Fila(document_type_id=9, document_type_name="NOTA DE CRÉDITO ELECTRÓNICA T", docs=0, total=-50_000.0)],
        by_day=[_Fila(day=datetime(2026, 8, 1, tzinfo=timezone.utc), docs=10, total=1_000_000.0)],
        excluidos=_Fila(docs=4, total=-12_345.0),
    )
    fast = _ventas_fast_con(monkeypatch, sesion, lag=2.0)

    r = fast(start_date="2026-08-01", end_date="2026-08-31")

    assert r["source"] == "snapshot" and r["snapshot_advertencia"] is None
    assert r["documentos_de_venta"] == 10 and r["notas_de_credito"] == 2
    assert r["venta_oficial"] == 1_000_000.0 and r["venta_oficial_sin_iva"] == 840_336.0
    assert r["ticket_promedio"] == 100_000, "total / documentos de venta (sin NC)"
    assert r["by_office"][0] == {"office_id": 1, "office_name": "E-Commerce", "count": 7,
                                "notas_de_credito": 1, "amount": 700_000.0}
    assert r["by_document_type"][1]["amount"] == -50_000.0
    assert r["by_day"] == [{"day": "2026-08-01", "count": 10, "amount": 1_000_000.0}]
    assert r["excluidos"]["documentos"] == 4 and r["excluidos"]["monto_con_signo"] == -12_345.0
    assert "guias no estan en el snapshot" in r["excluidos"]["detalle"]
    assert r["documentos"] == [] and "incluir_documentos=True" in r["nota_documentos"]
    assert "BRUTO" in r["unidad"]["venta_oficial"] and "sin IVA" in r["unidad"]["venta_oficial_sin_iva"]

    # Lo que le pidio a la base: 5 consultas (total, sucursal, tipo, dia, excluidos)
    assert len(sesion.consultas) == 5
    sql_total, p_total = sesion.consultas[0]
    assert "FILTER (WHERE documents_snapshot.document_type_use !=" in sql_total, "cuenta sin NC"
    assert "FILTER (WHERE documents_snapshot.document_type_use =" in sql_total, "cuenta las NC aparte"
    assert "THEN -documents_snapshot.total_amount" in sql_total, "la NC resta"
    assert "THEN -documents_snapshot.net_amount" in sql_total
    valores = set(v for v in p_total.values() if not isinstance(v, (list, tuple)))
    assert datetime(2026, 8, 1, tzinfo=timezone.utc) in valores
    assert datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc) in valores, "el fin incluye el dia entero"
    for sql, _ in sesion.consultas:
        assert "documents_snapshot.document_type_use !=" in sql, "excluye guias"
        assert "documents_snapshot.document_type_id NOT IN" in sql, "excluye notas de venta"
        assert "documents_snapshot.state =" in sql, "excluye anulados"
        assert "documents_snapshot.emission_date BETWEEN" in sql
        assert "documents_snapshot.office_id =" not in sql, "sin office_id no filtra sucursal"
    assert "NOT (" in sesion.consultas[4][0], "excluidos = la negacion de la regla"
    ids_nv = [v for v in p_total.values() if isinstance(v, (list, tuple))]
    assert ids_nv and set(ids_nv[0]) == set(_NOTAS_DE_VENTA), "los tipos de nota de venta vienen del monkeypatch"


def test_ventas_fast_filtra_sucursal_lista_documentos_y_avisa_atraso(monkeypatch):
    from datetime import datetime, timezone

    sesion = _SesionVentas(
        total=_Fila(docs=0, nc=0, total=0.0, neto=0.0),
        excluidos=_Fila(docs=0, total=0.0),
        docs=[_Fila(document_id=9050, emission_date=datetime(2026, 8, 2, tzinfo=timezone.utc),
                    office_id=3, office_name="Dos Caracoles", document_type_name="NC ",
                    client_id=77, signed_total=-50.0, total_amount=50.0, net_amount=42.0)],
    )
    fast = _ventas_fast_con(monkeypatch, sesion, lag=30.5)

    r = fast(date_from="2026-08-01", date_to="2026-08-31", office_id=3,
             incluir_documentos=True, limit=5)

    assert r["period"] == {"start": "2026-08-01", "end": "2026-08-31"}, "date_from/date_to son alias"
    assert r["office_id"] == 3
    assert r["ticket_promedio"] is None, "sin documentos no se divide por cero"
    assert "30.5h" in r["snapshot_advertencia"]
    assert r["nota_documentos"] is None
    assert r["documentos"] == [{
        "document_id": 9050, "emission_date": "2026-08-02T00:00:00+00:00", "office_id": 3,
        "office_name": "Dos Caracoles", "document_type_name": "NC", "client_id": 77,
        "amount_signed": -50.0, "total_amount": 50.0, "net_amount": 42.0,
    }]
    assert len(sesion.consultas) == 6
    for sql, p in sesion.consultas:
        assert "documents_snapshot.office_id =" in sql and 3 in p.values()
    sql_docs, p_docs = sesion.consultas[5]
    assert "LIMIT" in sql_docs and 5 in p_docs.values()


def test_ventas_fast_sin_fechas_no_abre_sesion(monkeypatch):
    ts = pytest.importorskip("tools_snapshot")

    def no_deberia():
        raise AssertionError("sin fechas no se consulta nada")

    monkeypatch.setattr(ts, "db_session", no_deberia)
    fast = _tools_de(ts)["bsale_ventas_fast"]
    assert "error" in fast()
    assert "error" in fast(start_date="2026-08-01")


class _ClienteConciliacion:
    def __init__(self, docs, truncated=False, total_count=None):
        self.docs, self.truncated = docs, truncated
        self.total_count = len(docs) if total_count is None else total_count
        self.llamadas = []

    def paginated_fetch(self, path, params=None, max_items=None, **k):
        self.llamadas.append((path, dict(params or {}), max_items))
        return {"items": self.docs, "truncated": self.truncated,
                "total_count": self.total_count, "fetched": len(self.docs)}


class _SesionConciliacion:
    def __init__(self, filas):
        self.filas, self.consultas = filas, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        self.consultas.append(_compilar_con_params(stmt))
        filas = self.filas

        class R:
            def all(self_):
                return filas

        return R()


def _conciliacion_con(monkeypatch, docs_vivo, filas_snap, **kw):
    ts = pytest.importorskip("tools_snapshot")
    import bsale_client as _bc

    cliente = _ClienteConciliacion(docs_vivo, **kw)
    sesion = _SesionConciliacion(filas_snap)
    monkeypatch.setattr(_bc, "sales_note_type_ids", lambda: _NOTAS_DE_VENTA)
    monkeypatch.setattr(_bc, "get_client", lambda: cliente)
    monkeypatch.setattr(ts, "db_session", lambda: sesion)
    return _tools_de(ts)["bsale_conciliacion_venta"], cliente, sesion


def test_conciliacion_explica_la_brecha_documento_a_documento(monkeypatch):
    from datetime import datetime, timezone

    # Vivo: boleta 100, factura 200, NC 50 (resta), boleta 150 que el snapshot
    # no tiene; y tres que la regla deja fuera: guia, nota de venta, anulada.
    vivo = [BOLETA, FACTURA, NOTA_CREDITO, _doc(1, 0, 0, 150),
            GUIA, _doc(3, 0, 1, 300), _doc(1, 0, 0, 400, state=1)]
    # Snapshot: los tres comunes (la factura con OTRO monto) y uno que sobra.
    snap = [_Fila(document_id=1100, monto=100.0), _Fila(document_id=6200, monto=260.0),
            _Fila(document_id=9050, monto=-50.0), _Fila(document_id=7777, monto=70.0)]
    conc, cliente, sesion = _conciliacion_con(monkeypatch, vivo, snap, total_count=7)

    r = conc(start_date="2026-08-01", end_date="2026-08-31")

    assert r["venta_oficial_vivo"] == 400, "100 + 200 - 50 + 150; guia, nota de venta y anulada fuera"
    assert r["venta_oficial_snapshot"] == 380 and r["diferencia"] == -20
    assert r["diferencia_pct"] == -5.0
    assert r["documentos_vivo"] == 4 and r["documentos_snapshot"] == 4
    assert r["brecha"]["faltan_en_snapshot"] == {"count": 1, "monto": 150, "ejemplos": [1150]}
    assert r["brecha"]["sobran_en_snapshot"] == {"count": 1, "monto": 70.0, "ejemplos": [7777]}
    assert r["brecha"]["monto_distinto"] == {"count": 1, "ejemplos": [{"document_id": 6200, "vivo": 200, "snapshot": 260.0}]}
    assert r["truncado_lado_vivo"] is False and r["documentos_en_bsale"] == 7

    # Lo que le pidio a Bsale: rango en epoch, vigentes, tope por defecto
    path, params, max_items = cliente.llamadas[0]
    assert path == "/v1/documents.json" and max_items == 40000
    ini = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    fin = int(datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    assert params["emissiondaterange"] == f"{ini},{fin}"
    assert params["state"] == 0 and params["officeid"] is None
    assert "document_type" in params["expand"]
    # Lo que le pidio al snapshot: la misma regla de venta oficial
    sql, p = sesion.consultas[0]
    assert "THEN -documents_snapshot.total_amount" in sql
    assert "documents_snapshot.document_type_use !=" in sql
    assert "documents_snapshot.document_type_id NOT IN" in sql
    assert "documents_snapshot.state =" in sql
    assert "documents_snapshot.office_id =" not in sql


def test_conciliacion_pasa_sucursal_tope_y_truncado(monkeypatch):
    conc, cliente, sesion = _conciliacion_con(monkeypatch, [BOLETA], [], truncated=True, total_count=99)

    r = conc(start_date="2026-08-01", end_date="2026-08-31", office_id=3, max_documents=10)

    assert r["truncado_lado_vivo"] is True, "un lado vivo cortado no puede pasar por completo"
    assert r["documentos_en_bsale"] == 99 and r["office_id"] == 3
    assert r["brecha"]["faltan_en_snapshot"]["ejemplos"] == [1100]
    assert r["diferencia_pct"] == -100.0
    _, params, max_items = cliente.llamadas[0]
    assert params["officeid"] == 3 and max_items == 10
    sql, p = sesion.consultas[0]
    assert "documents_snapshot.office_id =" in sql and 3 in p.values()


def test_conciliacion_rechaza_mas_de_92_dias_sin_tocar_bsale(monkeypatch):
    ts = pytest.importorskip("tools_snapshot")
    import bsale_client as _bc

    def no_deberia():
        raise AssertionError("con el rango rechazado no se llama a Bsale")

    monkeypatch.setattr(_bc, "get_client", no_deberia)
    monkeypatch.setattr(ts, "db_session", no_deberia)
    conc = _tools_de(ts)["bsale_conciliacion_venta"]
    r = conc(start_date="2026-01-01", end_date="2026-04-30")
    assert r.get("aplicado") is False and "92" in str(r)
    assert "error" in conc(start_date="2026-08-31", end_date="2026-08-01")


def test_conciliacion_sin_venta_en_vivo_no_divide_por_cero(monkeypatch):
    conc, _, _ = _conciliacion_con(monkeypatch, [], [_Fila(document_id=1, monto=10.0)])
    r = conc(start_date="2026-08-01", end_date="2026-08-31")
    assert r["diferencia_pct"] is None and r["diferencia"] == 10.0


# ===========================================================================
# Los tools "en vivo" con par _fast se RETIRARON (09-sep-2026)
# ===========================================================================
# Mientras coexistian, vivo y fast respondian distinto a la misma pregunta
# (#14 de la segunda auditoria) y el vivo pagaba cuota compartida de la API.
# Revisado antes de borrar: ninguna tarea programada, skill ni KPI de Notion
# los nombraba (los KPIs apuntan a los _fast). Este test impide que vuelvan.

_RETIRADOS = (
    "bsale_ventas_por_periodo", "bsale_top_productos", "bsale_quiebres_proyectados",
    "bsale_sugerencia_allocation", "bsale_proyeccion_compras",
    "bsale_ranking_sucursales", "bsale_segmentacion_clientes_rfm",
)


def test_los_tools_en_vivo_retirados_no_vuelven():
    import importlib.util
    import inspect

    assert importlib.util.find_spec("tools_intelligence") is None, "tools_intelligence.py se borro entero"

    import server as sv

    assert "tools_intelligence" not in _solo_codigo(inspect.getsource(sv)).replace("tools_intelligence_db", "")

    registrados = {}
    for mod in ("tools_analytics", "tools_snapshot", "tools_intelligence_db", "tools_documents"):
        registrados.update(_tools_de(pytest.importorskip(mod)))
    for nombre in _RETIRADOS:
        assert nombre not in registrados, f"{nombre} volvio a registrarse"
    # lo que reemplaza a cada uno sigue ahi
    for nombre in ("bsale_ventas_fast", "bsale_top_productos_fast", "bsale_quiebres_proyectados_fast",
                   "bsale_sugerencia_allocation_fast", "bsale_proyeccion_compras_fast",
                   "bsale_ranking_sucursales_fast", "bsale_segmentacion_clientes_rfm_fast",
                   "bsale_comparativo_meses"):
        assert nombre in registrados, f"falta {nombre}"
    # y ningun docstring o mensaje manda al agente a un tool que ya no existe
    for mod in ("tools_analytics", "tools_snapshot", "tools_intelligence_db", "tools_documents", "digests"):
        src = inspect.getsource(pytest.importorskip(mod))
        for nombre in _RETIRADOS:
            for m in __import__("re").finditer(nombre + r"(?!_fast)\b", src):
                linea = src[src.rfind("\n", 0, m.start()) + 1: src.find("\n", m.end())]
                assert "retir" in linea.lower(), f"{mod} nombra {nombre}: {linea.strip()[:80]}"

# ===========================================================================
# Lockfile: requirements.txt es el lock compilado de requirements.in
# ===========================================================================

def _pins(texto):
    import re

    return dict(re.findall(r"^([A-Za-z0-9_.\-\[\]]+)==([0-9][^\s\\;]*)", texto, re.M))


def test_requirements_txt_es_el_lock_de_requirements_in():
    """Render instala requirements.txt. Si alguien vuelve a editarlo a mano o
    lo reemplaza por la lista corta, las ~50 transitivas vuelven a flotar."""
    import os

    aqui = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(aqui, "requirements.in"), encoding="utf-8") as fh:
        entrada = fh.read()
    with open(os.path.join(aqui, "requirements.txt"), encoding="utf-8") as fh:
        lock = fh.read()

    directos = _pins(entrada)
    bloqueados = _pins(lock)
    assert len(directos) >= 15, "requirements.in perdio los directos o las transitivas del candado"
    for pkg, ver in directos.items():
        base = pkg.split("[")[0].lower().replace("_", "-")
        assert bloqueados.get(base) == ver, f"{pkg}=={ver} de requirements.in no esta igual en el lock"
    assert len(bloqueados) > 60, "el lock tiene que traer las transitivas, no solo los directos"
    assert lock.count("--hash=sha256:") >= len(bloqueados), "cada paquete con hash: pip en modo hash-checking"
    assert "# via" in lock, "no parece un archivo compilado"
    # Universal: lo de Windows va detras de un marcador, nunca a secas. Un lock
    # compilado desde el venv de Windows sin --universal lo traeria sin marcador.
    import re

    for solo_windows in ("pywin32", "colorama", "pywin32-ctypes"):
        for m in re.finditer(rf"^{re.escape(solo_windows)}==[^\n]*", lock, re.M):
            assert "sys_platform == 'win32'" in m.group(0), f"{solo_windows} sin marcador: lock de Windows"
    # y lo que solo necesita 3.11 tiene que estar, porque el web service de
    # Render corre 3.11 (el cron, 3.14): un lock solo-3.14 rompio el build.
    assert re.search(r"^backports-tarfile==[^\n]*python_full_version < '3\.12'", lock, re.M), (
        "falta backports-tarfile con marcador: el lock no es universal y el web service (3.11) no compila"
    )
    assert "apscheduler" not in lock.lower(), "dependencia muerta"


# ===========================================================================
# 09-sep-2026 (tarde) — tools compactos para el cruce con Shopify
# ===========================================================================

class _SesionStock:
    """Responde el select de stock_actual con filas fijas y el sync_estado."""

    def __init__(self, filas, completo=True):
        self.filas, self.completo, self.consultas = filas, completo, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        filas, completo = self.filas, self.completo
        self.consultas.append(str(stmt))

        class R:
            def scalar(self_):
                return {"completo": completo}

            def fetchall(self_):
                return filas

        return R()


class _ClienteStock:
    def __init__(self):
        self.llamadas = []

    def get(self, path, params=None, **k):
        self.llamadas.append((path, dict(params or {})))
        if path == "/v1/variants.json":
            return {"items": [{"id": 9928, "code": params["code"]}]}
        return {"count": 2, "items": [
            {"quantity": 10, "office": {"id": 4, "name": "Outlet "}},
            {"quantity": 237, "office": {"id": 1, "name": "E-Commerce "}},
        ]}


def test_stock_variante_lee_el_snapshot_compacto_y_sin_bsale(monkeypatch):
    from datetime import datetime, timezone
    import tools_stocks as tst
    import db as _db

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    t0 = datetime(2026, 9, 9, 7, 2, tzinfo=timezone.utc)
    sesion = _SesionStock([
        _Fila(variant_id=9928, variant_code="724841188979", office_id=1, office_name="E-Commerce ", quantity=237.0, updated_at=t0),
        _Fila(variant_id=9928, variant_code="724841188979", office_id=4, office_name="Outlet ", quantity=11.0, updated_at=t0),
    ])
    monkeypatch.setattr(_db, "session", lambda: sesion)
    cli = _ClienteStock()
    monkeypatch.setattr(tst, "get_client", lambda: cli)

    r = _tools_de(tst)["bsale_stock_variante"](code="724841188979")

    assert cli.llamadas == [], "el default no puede pegarle a Bsale"
    assert r["variant_id"] == 9928 and r["total_unidades"] == 248.0
    assert r["sucursales"] == [
        {"office_id": 1, "office_name": "E-Commerce", "quantity": 237.0},
        {"office_id": 4, "office_name": "Outlet", "quantity": 11.0},
    ]
    assert r["actualizado_desde"] == t0.isoformat() and r["ultima_corrida_completa"] is True
    assert "stock_actual.variant_code =" in sesion.consultas[-1]


def test_stock_variante_ausente_no_es_cero(monkeypatch):
    import tools_stocks as tst
    import db as _db

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(_db, "session", lambda: _SesionStock([]))
    r = _tools_de(tst)["bsale_stock_variante"](variant_id=424242)
    assert "error" in r and "sucursales" not in r and "total_unidades" not in r
    assert "error" in _tools_de(tst)["bsale_stock_variante"]()


def test_stock_variante_en_vivo_resuelve_el_code_y_compacta(monkeypatch):
    import tools_stocks as tst

    cli = _ClienteStock()
    monkeypatch.setattr(tst, "get_client", lambda: cli)
    r = _tools_de(tst)["bsale_stock_variante"](code="724841188979", stock_live=True)
    assert [p for p, _ in cli.llamadas] == ["/v1/variants.json", "/v1/stocks.json"]
    assert cli.llamadas[1][1]["variantid"] == 9928
    assert r["sucursales"][0] == {"office_id": 1, "office_name": "E-Commerce", "quantity": 237.0}
    assert r["total_unidades"] == 247.0 and r["truncado"] is False


def _doc_pesado(id_, office_id=4, sales_id=None, use=0, type_id=1, gen=1788971408):
    return {
        "id": id_, "number": 612420, "emissionDate": 1788912000, "generationDate": gen,
        "totalAmount": 23992, "netAmount": 20161, "state": 0, "salesId": sales_id,
        "trackingNumber": "abc", "urlPublicView": "https://v", "urlPdf": "https://p",
        "ted": "<TED>" + "x" * 800 + "</TED>", "urlTimbre": "t", "urlXml": "x",
        "urlPublicViewOriginal": "o", "urlPdfOriginal": "po",
        "document_type": {"id": type_id, "name": "BOLETA", "codeSii": "39", "use": use, "isSalesNote": 0,
                          "messageBodyFormat": "<p>" + "y" * 2000 + "</p>", "thermalPrinter": 1},
        "office": {"id": office_id, "name": "Outlet ", "address": "NUEVA DE LYON 45", "email": "o@m.cl"},
        "coin": {"href": "c"}, "user": {"href": "u"}, "priceList": {"href": "p"},
        "references": {"href": "r"}, "details": {"href": "d"},
    }


def test_documentos_compactos_sacan_el_html_y_el_ted_y_conservan_la_regla(monkeypatch):
    import json
    import tools_documents as td

    pesado = _doc_pesado(1280456)
    pesado_nc = _doc_pesado(1, use=1, type_id=9)

    class Cli:
        def get(self, path, params=None, **k):
            return {"count": 2, "items": [pesado, pesado_nc]}

    monkeypatch.setattr(td, "get_client", lambda: Cli())
    tools = _tools_de(td)
    r = tools["bsale_listar_documentos"](start_date="2026-09-09", end_date="2026-09-09")
    d = r["items"][0]
    assert "ted" not in d and "messageBodyFormat" not in d["document_type"] and "coin" not in d
    assert d["salesId"] is None and d["urlPublicView"] == "https://v" and d["monto_firmado"] == 23992
    assert d["office"] == {"id": 4, "name": "Outlet"}
    assert d["document_type"]["use"] == 0 and d["document_type"]["isSalesNote"] == 0
    assert "references" not in d, "un href solo no informa nada"
    assert r["items"][1]["monto_firmado"] == -23992, "la NC se firmo ANTES de compactar"
    assert len(json.dumps(d)) < len(json.dumps(pesado)) / 5

    crudo = tools["bsale_listar_documentos"](start_date="2026-09-09", end_date="2026-09-09", compacto=False)
    assert "ted" in crudo["items"][0] and "messageBodyFormat" in crudo["items"][0]["document_type"]


def test_obtener_documento_compacto_conserva_lineas_y_cliente(monkeypatch):
    import tools_documents as td

    doc = _doc_pesado(1280457)
    doc["details"] = {"href": "d", "items": [{"id": 1, "quantity": 1, "variant": {"id": 9928}}]}
    doc["client"] = {"id": 95925, "firstName": "R"}

    class Cli:
        def get(self, path, params=None, **k):
            return doc

    monkeypatch.setattr(td, "get_client", lambda: Cli())
    r = _tools_de(td)["bsale_obtener_documento"](document_id=1280457)
    assert r["details"]["items"][0]["variant"]["id"] == 9928 and r["client"]["id"] == 95925
    assert "ted" not in r and "messageBodyFormat" not in r["document_type"]


class _ClienteCruce:
    def __init__(self, docs, truncated=False):
        self.docs, self.truncated, self.llamadas = docs, truncated, []

    def paginated_fetch(self, path, params=None, max_items=None, **k):
        self.llamadas.append((path, dict(params or {}), max_items))
        return {"items": self.docs, "truncated": self.truncated, "total_count": len(self.docs), "fetched": len(self.docs)}


def _pedido(nombre, *locs):
    return {"id": "gid://shopify/Order/1", "name": nombre, "createdAt": "x",
            "fulfillmentOrders": {"nodes": [
                {"id": "gid://shopify/FulfillmentOrder/1",
                 "assignedLocation": {"location": {"id": f"gid://shopify/Location/{l}", "name": n}}}
                for l, n in locs]}}


def test_cruce_boletas_vs_shopify_cuadra_por_sucursal(monkeypatch):
    import tools_cruce_shopify as tc

    # Bsale: 2 boletas web en E-Commerce, 1 web en Outlet, 1 de caja en Outlet, 1 NC
    docs = [_doc_pesado(1, office_id=1, sales_id="73540330"), _doc_pesado(2, office_id=1, sales_id="73540331"),
            _doc_pesado(3, office_id=4, sales_id="73540332"), _doc_pesado(4, office_id=4),
            _doc_pesado(5, office_id=4, use=1, type_id=9), _doc_pesado(6, office_id=4, use=2, type_id=8)]
    cli = _ClienteCruce(docs)
    monkeypatch.setattr(tc, "get_client", lambda: cli)
    # Shopify: 2 FO en Bodega, 1 en Outlet, 1 en una ubicacion desconocida
    pedidos = [_pedido("#1", (104335016258, "Myscrubs Bodega")), _pedido("#2", (104335016258, "Myscrubs Bodega"), (117047886146, "Outlet")),
               _pedido("#3", (999, "Rara"))]
    monkeypatch.setenv("SHOPIFY_SHOP", "x.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", "shpat")
    vistos = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"orders": {"nodes": pedidos, "pageInfo": {"hasNextPage": False}}}}

    def post(url, json=None, headers=None, timeout=None):
        vistos.append((url, json["variables"], headers))
        return Resp()

    monkeypatch.setattr(tc.httpx, "post", post)

    r = _tools_de(tc)["bsale_boletas_vs_shopify"](fecha="2026-09-09")

    assert r["cruce_hecho"] is True and r["cuadra"] is True
    por = {f["office_id"]: f for f in r["por_sucursal"]}
    assert por[1]["boletas_con_salesId"] == 2 and por[1]["fo_shopify"] == 2 and por[1]["diferencia"] == 0
    assert por[4]["boletas_con_salesId"] == 1 and por[4]["boletas_sin_salesId"] == 1 and por[4]["diferencia"] == 0
    assert por[4]["notas_de_credito"] == 1, "la NC no entra en la comparacion pero se declara"
    assert r["shopify"]["fo_sin_mapear"] == [{"pedido": "#3", "location_id": 999, "location": "Rara"}]
    assert r["bsale"]["venta_oficial"] == 5, "la guia queda fuera"
    # El query a Shopify cubre el dia de Chile (UTC-3 en septiembre) y solo pagados
    url, variables, headers = vistos[0]
    q = variables["q"]
    assert "created_at:>='2026-09-09T03:00:00Z'" in q and "created_at:<'2026-09-10T03:00:00Z'" in q
    assert "financial_status:paid" in q
    assert url.startswith("https://x.myshopify.com/admin/api/") and headers["X-Shopify-Access-Token"] == "shpat"
    # A Bsale le pidio el dia en epoch, vigentes, con tope
    path, params, max_items = cli.llamadas[0]
    assert path == "/v1/documents.json" and params["state"] == 0 and max_items == 2000


def test_cruce_sin_token_entrega_solo_bsale_y_no_inventa_ceros(monkeypatch):
    import tools_cruce_shopify as tc

    monkeypatch.delenv("SHOPIFY_SHOP", raising=False)
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(tc, "get_client", lambda: _ClienteCruce([_doc_pesado(1, office_id=1, sales_id="1")]))
    r = _tools_de(tc)["bsale_boletas_vs_shopify"](fecha="2026-09-09")
    assert r["cruce_hecho"] is False and r["cuadra"] is None and "SHOPIFY" in r["motivo_sin_cruce"]
    fila = r["por_sucursal"][0]
    assert fila["boletas_con_salesId"] == 1 and fila["fo_shopify"] is None and fila["diferencia"] is None


def test_cruce_detecta_la_boleta_que_falta(monkeypatch):
    import tools_cruce_shopify as tc

    monkeypatch.setattr(tc, "get_client", lambda: _ClienteCruce([_doc_pesado(1, office_id=1, sales_id="1")]))
    monkeypatch.setattr(tc, "fulfillment_orders_shopify", lambda fecha: {
        "disponible": True, "pedidos_pagados": 2, "fulfillment_orders": 2, "truncado": False,
        "fo_por_office": {1: 2}, "fo_sin_mapear": []})
    r = _tools_de(tc)["bsale_boletas_vs_shopify"](fecha="2026-09-09", incluir_documentos=True)
    assert r["cuadra"] is False
    assert r["por_sucursal"][0]["diferencia"] == -1
    assert r["por_sucursal"][0]["documentos"][0]["generado"] == "13:30:08", "hora de Chile, para ver si es reciente"
