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

    La primera version era un toggle por linea sobre las comillas triples. Se
    desincronizaba con cualquier string multilinea cuya apertura no estuviera
    al principio de la linea, y a partir de ahi descartaba TODO el resto del
    archivo en silencio: los assert pasaban sobre texto vacio y no probaban
    nada. Verificado el 08-sep-2026 sobre tools_intelligence.py, donde tres
    asserts sobre codigo que SI estaba presente daban falso.

    Ahora se tokeniza y se blanquea por posicion, que es exacto.
    """
    import ast
    import io
    import tokenize

    lineas = src.splitlines(keepends=True)
    borrar = []

    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                borrar.append((tok.start, tok.end))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass

    try:
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
    except SyntaxError:
        pass

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
    """El de 20 s protege al web service; un mantenimiento no lo hereda."""
    import inspect

    retention = pytest.importorskip("retention")
    src = inspect.getsource(retention)
    assert "SET LOCAL statement_timeout" in src
    assert "_sin_timeout_corto(s)" in src


def test_un_fallo_de_retencion_se_ve():
    """Anidado en un dict que nadie mira, un fallo no marca la corrida."""
    import inspect

    retention = pytest.importorskip("retention")
    assert "hubo_error" in inspect.getsource(retention.apply_retention)

    snapshot = pytest.importorskip("snapshot")
    nocturno = _solo_codigo(inspect.getsource(snapshot.nightly_snapshot))
    assert 'results["retention_error"]' in nocturno, (
        "cron_snapshot.py solo mira claves de primer nivel que terminen en _error"
    )


def test_la_retencion_corre_antes_del_backfill_historico():
    """Paso corto y critico no puede ir detras de uno largo y opcional."""
    import inspect

    snapshot = pytest.importorskip("snapshot")
    codigo = _solo_codigo(inspect.getsource(snapshot.nightly_snapshot))
    assert codigo.index("apply_retention") < codigo.index("oldest_first=True"), (
        "si la corrida muere en el backfill, la retencion no corre esa noche"
    )


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
    codigo = _solo_codigo(inspect.getsource(sync.run))

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
    codigo = _solo_codigo(inspect.getsource(sync.run))
    assert 'DETALLE_HISTORICO_POR_CORRIDA", "2000"' in codigo


def test_nightly_snapshot_declara_que_no_lo_corre_el_cron():
    """Para que nadie vuelva a agregar un paso automatico en codigo muerto."""
    import inspect

    snapshot = pytest.importorskip("snapshot")
    doc = inspect.getdoc(snapshot.nightly_snapshot) or ""
    assert "NINGUN CRON" in doc.upper()
    assert "sync_incremental" in doc


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


def test_el_audit_nunca_voltea_una_escritura():
    """Lo llama cada write hacia Bsale: un problema de logging no puede romperla."""
    import inspect

    audit = pytest.importorskip("audit")
    src = inspect.getsource(audit._escribir_en_postgres)
    assert "except Exception" in src
    assert "return False" in src


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
    codigo = _solo_codigo(inspect.getsource(sync.run))

    assert "_ultima_corrida_stock_completa" in codigo, (
        "auto tiene que repetir la corrida si la anterior quedo a medias"
    )
    assert "_stock_photo_age_hours" in codigo


def test_la_corrida_de_stock_registra_que_quedo_completa(monkeypatch):
    cliente = _ClienteStockFalso(total=1000)
    snapshot, _, _ = _montar_stock(monkeypatch, cliente)

    snapshot.snapshot_stock(max_pages=6000)

    assert len(REGISTROS_STOCK) == 1
    clave, valor = REGISTROS_STOCK[0]
    assert clave == "stock_ultima_corrida"
    assert valor["completo"] is True
    assert valor["filas_vistas"] == 1000
    assert valor["error"] is None


def test_la_corrida_de_stock_registra_que_quedo_incompleta(monkeypatch):
    cliente = _ClienteStockFalso(total=1000, fallar_en={200})
    snapshot, _, _ = _montar_stock(monkeypatch, cliente)

    snapshot.snapshot_stock(max_pages=6000)

    clave, valor = REGISTROS_STOCK[0]
    assert clave == "stock_ultima_corrida"
    assert valor["completo"] is False
    assert valor["error"], "el registro tiene que decir por que"


def test_el_cron_tambien_crea_su_esquema():
    """init_db vivia solo en server.py, o sea solo en el web service.

    Una tabla nueva en db.py no existia para el cron hasta que el web service
    se reiniciara. Como los helpers que la leen atrapan la excepcion, el
    sintoma habria sido "el paso no hace nada", sin error visible.
    """
    import inspect

    sync = pytest.importorskip("sync_incremental")
    codigo = _solo_codigo(inspect.getsource(sync.run))

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
def test_el_cron_mira_catorce_dias_no_dos():
    """La boleta 1280257: emitida el 03-sep, generada el 07-sep.

    Con la ventana de 2 dias nunca entro al snapshot ($101.970). El arreglo de
    los 14 dias existia, pero en nightly_snapshot(), que ningun cron corre.
    """
    import inspect

    sync = pytest.importorskip("sync_incremental")
    codigo = _solo_codigo(inspect.getsource(sync.sync_ventas))

    assert "days_back=14" in codigo
    assert "days_back=2" not in codigo


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


def test_el_cron_y_el_sync_usan_el_mismo_criterio():
    import inspect

    sync = pytest.importorskip("sync_incremental")
    cron = pytest.importorskip("cron_snapshot")

    assert "recolectar_errores" in _solo_codigo(inspect.getsource(sync.run))
    assert "recolectar_errores" in _solo_codigo(inspect.getsource(cron.run))


# ------------------------------------------------- 4. cobertura declarada
def test_los_tools_de_decision_ya_no_usan_el_paginado_que_tira_el_truncado():
    """paginated_get devuelve ["items"] y descarta el aviso de truncado.

    bsale_quiebres_proyectados leia 2.500 filas de las 240.427 que tiene Bsale
    (medido el 08-sep-2026): el 1,04%, presentado como si fuera el total.
    """
    import ast
    import inspect

    ti = pytest.importorskip("tools_intelligence")
    # Con ast, no buscando texto: este archivo NOMBRA paginated_get en los
    # comentarios para explicar por que se saco, y _solo_codigo() no alcanza a
    # descartar todos los docstrings anidados. Un test que busca texto sobre
    # este repo se prueba a si mismo.
    arbol = ast.parse(inspect.getsource(ti))
    llamadas = [
        n.func.attr
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "paginated_get" not in llamadas, (
        "paginated_get tira el aviso de truncado; usar _fetch_declarado"
    )
    assert "paginated_fetch" in llamadas


def test_una_lectura_truncada_lo_dice_y_ofrece_el_tool_que_si_cubre():
    ti = pytest.importorskip("tools_intelligence")

    class _Cli:
        def paginated_fetch(self, path, params=None, max_items=0, workers=None):
            return {"items": [{"i": n} for n in range(max_items)],
                    "total_count": 240427, "truncated": True}

    items, cob = ti._fetch_declarado(
        _Cli(), "/v1/stocks.json", {}, 2500, "filas de stock", "usar el _fast",
    )
    assert len(items) == 2500
    assert cob["truncado"] is True
    assert cob["total_en_bsale"] == 240427
    assert cob["pct"] == 1.04
    assert "PISO" in cob["advertencia"]
    assert cob["alternativa"] == "usar el _fast"


def test_una_lectura_completa_no_grita():
    ti = pytest.importorskip("tools_intelligence")

    class _Cli:
        def paginated_fetch(self, path, params=None, max_items=0, workers=None):
            return {"items": [{"i": n} for n in range(120)],
                    "total_count": 120, "truncated": False}

    _items, cob = ti._fetch_declarado(_Cli(), "/x.json", {}, 5000, "cosas")
    assert cob["truncado"] is False
    assert cob["pct"] == 100.0
    assert "advertencia" not in cob


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


def test_cambiar_el_sku_pasa_por_el_candado(monkeypatch):
    """El code es la llave con Shopify y Mercado Libre via sku_mapping."""
    tools, cli = _tools_de_escritura(monkeypatch)
    monkeypatch.setenv("BSALE_CATALOG_WRITES_ENABLED", "1")

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
    assert "no se escribio nada" in str(e.value).lower()


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

def test_el_rfm_en_vivo_aplica_la_regla_de_venta_oficial_completa():
    """Solo sacaba guias, y eso hacia contar los pedidos web dos veces.

    Bsale genera PEDIDO WEB (tipo 26, nota de venta) Y la boleta por la misma
    compra. is_sales_doc solo excluye use=2, asi que las dos entraban: un
    cliente con 3 compras web salia con frequency 6 y el doble de facturacion,
    y cruzaba el umbral de "Champion". La version _fast (SQL) si aplicaba la
    regla completa, o sea que los dos RFM daban distinto para el mismo cliente.
    """
    import inspect

    ti = pytest.importorskip("tools_intelligence")
    codigo = _solo_codigo(inspect.getsource(ti))

    assert "if not is_sales_doc(doc):" not in codigo, (
        "is_sales_doc no alcanza: hay que usar is_official_sale"
    )
    assert "if not is_official_sale(doc):" in codigo


def test_producttypeid_llega_a_la_consulta():
    """Se aceptaba, se documentaba como filtro y se devolvia en la respuesta.

    Pero no entraba en ningun params: preguntar por una marca devolvia el
    catalogo entero rotulado como si fuera de esa marca.
    """
    import inspect

    ti = pytest.importorskip("tools_intelligence")
    codigo = _solo_codigo(inspect.getsource(ti))

    assert '_p_stock["producttypeid"] = producttypeid' in codigo


def test_ninguna_fecha_se_convierte_con_la_zona_local_del_proceso():
    """fromtimestamp sin tz usa la zona local: es convertir emission_date.

    emissionDate es medianoche UTC exacta. Hoy Render corre en UTC y sale bien
    por accidente; con TZ=America/Santiago todos los dias se corren uno hacia
    atras y la venta del lunes aparece como del domingo.
    """
    import ast
    import inspect

    for mod in ("tools_analytics", "tools_intelligence", "snapshot", "digests"):
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
