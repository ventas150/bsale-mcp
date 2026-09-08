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

    src = inspect.getsource(db.get_engine)
    for esperado in ("connect_timeout", "pool_timeout", "statement_timeout"):
        assert esperado in src, f"falta {esperado} en get_engine"
    # y la creacion tiene que estar bajo lock (era check-then-act)
    assert "_engine_lock" in src


def test_health_no_bloquea_el_event_loop():
    """Las llamadas sincronas a la base tienen que ir a un hilo con tope."""
    import inspect

    import server

    src = inspect.getsource(server.health_check)
    assert "_con_limite" in src, "health debe usar el wrapper con timeout"

    # OJO: hay que mirar el CODIGO, no los comentarios. El docstring de
    # health_check nombra las claves que se sacaron para explicar por que se
    # sacaron; si se escanea el fuente crudo, el propio comentario hace fallar
    # el test. Se descartan lineas de comentario y el docstring.
    lineas = []
    en_docstring = False
    for linea in src.splitlines():
        limpia = linea.strip()
        if limpia.startswith('"""') or limpia.startswith("'''"):
            comillas = limpia[:3]
            # docstring de una sola linea
            if len(limpia) > 3 and limpia.endswith(comillas):
                continue
            en_docstring = not en_docstring
            continue
        if en_docstring or limpia.startswith("#"):
            continue
        lineas.append(linea)
    codigo = "\n".join(lineas)

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


def test_precios_abortan_si_falta_el_id_del_detalle():
    """Sin detail_id no hay forma de escribir; no se escribe nada a medias."""
    import inspect

    tools_writes = pytest.importorskip("tools_writes")
    src = inspect.getsource(tools_writes.register)

    assert "variantes_sin_detalle" in src
    # el chequeo tiene que estar ANTES del bucle que escribe
    assert src.index("sin_detalle") < src.index("client.put(")


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

def test_el_nocturno_cierra_el_hueco_historico():
    import inspect

    snapshot = pytest.importorskip("snapshot")
    src = inspect.getsource(snapshot.nightly_snapshot)

    assert "oldest_first=True" in src, (
        "sin oldest_first el backfill se queda masticando lo reciente y nunca "
        "llega a los periodos viejos"
    )
    assert "details_historico" in src


def _solo_codigo(src: str) -> str:
    """Descarta docstrings y comentarios de un fuente.

    Estos tests buscan patrones prohibidos en el CODIGO, y los
    comentarios de este repo NOMBRAN el patron viejo para explicar por
    que se saco. Sin este filtro el test prueba el comentario en vez del
    codigo: ya paso dos veces (health_check y snapshot_details).
    """
    lineas = []
    en_docstring = False
    for linea in src.splitlines():
        limpia = linea.strip()
        if limpia.startswith(chr(34) * 3) or limpia.startswith(chr(39) * 3):
            comillas = limpia[:3]
            if len(limpia) > 3 and limpia.endswith(comillas):
                continue
            en_docstring = not en_docstring
            continue
        if en_docstring or limpia.startswith('#'):
            continue
        lineas.append(linea)
    return chr(10).join(lineas)


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
    assert "4.000" in str(r)
