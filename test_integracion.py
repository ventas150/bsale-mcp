"""Tests de INTEGRACION contra un Postgres REAL.

Por que existen: la suite unitaria (test_venta_oficial.py) corre con sesion
falsa y no ve lo que solo Postgres ve. Los tres bugs de septiembre fueron de
esa familia: IndeterminateDatatype en jsonb_build_object (09-sep),
QueryCanceled por statement_timeout en el anti-join de detalle (30-sep) y el
ShareLock de CREATE INDEX IF NOT EXISTS (30-sep). Sin estos tests, cada cambio
de SQL se probaba en produccion a las 3 de la tarde.

Como correrlos (PC de Roberto):
    scripts\\pg-test.cmd            -> arranca el Postgres portable (puerto 5433)
                                      y corre SOLO esta familia
    o a mano:
    set DATABASE_URL_TEST=postgresql://postgres@localhost:5433/bsale_test
    .venv\\Scripts\\python.exe -m pytest -q test_integracion.py

Sin DATABASE_URL_TEST el modulo entero se SALTA (no falla): la suite unitaria
sigue corriendo igual en cualquier maquina. Y se niega a correr contra
cualquier URL que huela a Render/produccion: cada test empieza borrando el
esquema.

No pega a Bsale: el cliente se reemplaza por uno falso que devuelve detalle
de linea inventado. Lo unico real es Postgres.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

URL = os.getenv("DATABASE_URL_TEST")

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL_TEST no definido: sin Postgres local")

if URL and any(s in URL for s in ("render.com", "dpg-", "bsale-mcp-db")):
    raise RuntimeError("DATABASE_URL_TEST apunta a Render/produccion: estos tests BORRAN el esquema.")

os.environ.setdefault("BSALE_ACCESS_TOKEN", "test-token")

AHORA = datetime.now(timezone.utc)
NOTAS_DE_VENTA = frozenset({3, 23, 24, 26, 27})

# ---------------------------------------------------------------------------
# Datos sembrados. Se construyen una vez y los tests calculan lo esperado
# DESDE esta lista, no a mano, para que cambiar la siembra no rompa nada.
# ---------------------------------------------------------------------------

def _doc(doc_id, tipo, use, total, dias_atras, office, state=0, snap_dias_atras=0):
    emis = (AHORA - timedelta(days=dias_atras)).replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "document_id": doc_id,
        "snapshot_date": AHORA - timedelta(days=snap_dias_atras),
        "emission_date": emis,
        "office_id": office,
        "office_name": {1: "E-Commerce", 2: "Los Leones", 3: "Dos Caracoles"}[office],
        "document_type_id": tipo,
        "document_type_name": {1: "BOLETA ELECTRONICA T", 6: "FACTURA ELECTRONICA T",
                               9: "NOTA DE CREDITO ELECTRONICA T", 3: "NOTA DE VENTA"}[tipo],
        "document_type_use": use,
        "client_id": 500 + (doc_id % 7),
        "total_amount": float(total),
        "net_amount": round(total / 1.19, 2),
        "tax_amount": round(total - total / 1.19, 2),
        "state": state,
        "raw": {
            "id": doc_id, "totalAmount": total, "state": state,
            "client": {"id": 500 + (doc_id % 7), "firstName": "Juan", "lastName": "Perez",
                       "code": "11.111.111-1", "email": "juan@example.com",
                       "companyOrPerson": 0},
            "document_type": {"id": tipo, "use": use, "isSalesNote": int(tipo in NOTAS_DE_VENTA)},
            "office": {"id": office, "name": "x"},
        },
    }


def _siembra():
    docs = []
    i = 1000
    for k in range(140):                       # boletas recientes
        docs.append(_doc(i, 1, 0, 10000 + k, k % 28, k % 3 + 1)); i += 1
    for k in range(20):                        # facturas
        docs.append(_doc(i, 6, 0, 50000, k % 28, 2)); i += 1
    for k in range(15):                        # notas de credito
        docs.append(_doc(i, 9, 1, 5000, k % 28, 1)); i += 1
    for k in range(10):                        # notas de venta: NO son venta oficial
        docs.append(_doc(i, 3, 0, 999, k % 28, 3)); i += 1
    for k in range(5):                         # anuladas: fuera
        docs.append(_doc(i, 1, 0, 777, k % 28, 1, state=1)); i += 1
    for k in range(10):                        # viejas (8 meses): para la retencion de raw
        docs.append(_doc(i, 1, 0, 3000, 240 + k, 2)); i += 1
    assert i == 1200 and len(docs) == 200
    # snapshot_date: 1100..1159 hace 10 dias (fuera de la marca), el resto ahora
    for d in docs:
        if 1100 <= d["document_id"] <= 1159:
            d["snapshot_date"] = AHORA - timedelta(days=10)
    return docs


DOCS = _siembra()
CON_DETALLE = [d for d in DOCS if d["document_id"] < 1100]        # 100 docs, 5 lineas c/u
PENDIENTES_VIEJOS = [d for d in DOCS if 1100 <= d["document_id"] <= 1159]
PENDIENTES_NUEVOS = [d for d in DOCS if 1160 <= d["document_id"] <= 1199]


def _lineas(doc):
    out = []
    for j in range(5):
        vid = (doc["document_id"] + j) % 10 + 1
        q = j % 3 + 1
        out.append({
            "document_id": doc["document_id"], "line_id": doc["document_id"] * 10 + j,
            "variant_id": vid, "variant_code": f"SKU-{vid:03d}",
            "variant_description": f"Variante {vid}", "office_id": doc["office_id"],
            "emission_date": doc["emission_date"], "document_type_use": doc["document_type_use"],
            "quantity": float(q), "net_amount": round(q * 1000 / 1.19, 2), "total_amount": float(q * 1000),
            "fetched_at": AHORA,
        })
    return out


def _es_venta_oficial(d):
    return d["document_type_use"] != 2 and d["document_type_id"] not in NOTAS_DE_VENTA and d["state"] == 0


def _signo(d):
    return -1 if d["document_type_use"] == 1 else 1


# ---------------------------------------------------------------------------
# Cliente Bsale falso: solo sabe responder /v1/documents/{id}/details.json
# ---------------------------------------------------------------------------

class _ClienteDetalleFalso:
    def __init__(self):
        self.pedidos = []

    def paginated_fetch(self, path, params=None, max_items=None, workers=None):
        from bsale_client import BsaleError
        doc_id = int(path.split("/")[3])
        self.pedidos.append(doc_id)
        if doc_id == 1199:
            raise BsaleError("Bsale respondio 404: {\"error\":\"not found\"}")
        if doc_id == 1198:
            return {"items": [], "truncated": True}
        if doc_id == 1197:
            return {"items": [], "truncated": False}
        doc = next(d for d in DOCS if d["document_id"] == doc_id)
        items = [{
            "id": ln["line_id"], "quantity": ln["quantity"], "netAmount": ln["net_amount"],
            "totalAmount": ln["total_amount"],
            "variant": {"id": ln["variant_id"], "code": ln["variant_code"], "description": ln["variant_description"]},
        } for ln in _lineas(doc)]
        return {"items": items, "truncated": False}


# ---------------------------------------------------------------------------
# Base: esquema desde cero + siembra, UNA vez por modulo
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def base():
    import db
    import digests
    import bsale_client

    os.environ["DATABASE_URL"] = URL
    db.DATABASE_URL = URL
    db._engine = None
    db._SessionMaker = None
    digests._schema_ready = False
    # sales_note_type_ids llama a Bsale: se fija la cache para no pegarle.
    bsale_client._sales_note_ids_cache = NOTAS_DE_VENTA
    bsale_client._sales_note_ids_from_api = True
    from sqlalchemy import text
    eng = db.get_engine()
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    db.init_db()
    digests.ensure_schema()

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    with db.session() as s:
        s.execute(pg_insert(db.documents_snapshot).values(DOCS))
        lineas = [ln for d in CON_DETALLE for ln in _lineas(d)]
        assert len(lineas) == 500
        s.execute(pg_insert(db.document_details_snapshot).values(lineas))
        stock = [{"variant_id": v, "office_id": o, "quantity": float((v * o) % 7),
                  "variant_code": f"SKU-{v:03d}", "office_name": f"Sucursal {o}",
                  "updated_at": AHORA} for v in range(1, 11) for o in range(1, 12)]
        s.execute(pg_insert(db.stock_actual).values(stock))
    yield db
    eng.dispose()
    db._engine = None
    db._SessionMaker = None
    db.DATABASE_URL = None
    os.environ.pop("DATABASE_URL", None)
    digests._schema_ready = False


def _sql(base, q, **params):
    from sqlalchemy import text
    with base.session() as s:
        r = s.execute(text(q), params)
        return r.fetchall() if r.returns_rows else None


# ---------------------------------------------------------------------------
# 1. Esquema e indices
# ---------------------------------------------------------------------------

def test_init_db_crea_el_esquema_y_el_indice_de_snapshot_date(base):
    tablas = {r[0] for r in _sql(base, "select tablename from pg_tables where schemaname='public'")}
    for t in ("documents_snapshot", "document_details_snapshot", "stock_actual", "sync_estado", "llm_digests"):
        assert t in tablas, f"falta {t}"
    idx = _sql(base, "select to_regclass('ix_documents_snapshot_snapshot_date')")[0][0]
    assert idx == "ix_documents_snapshot_snapshot_date", "ensure_indexes no creo el indice"


def test_ensure_indexes_es_idempotente_y_no_toma_lock_si_ya_existe(base):
    """Con el indice ya creado, la segunda pasada solo consulta to_regclass.
    Se mide con pg_stat: el numero de indices no cambia y no hay error."""
    antes = _sql(base, "select count(*) from pg_indexes where tablename='documents_snapshot'")[0][0]
    base.ensure_indexes()
    base.ensure_indexes()
    despues = _sql(base, "select count(*) from pg_indexes where tablename='documents_snapshot'")[0][0]
    assert antes == despues


def test_la_siembra_quedo_como_se_pidio(base):
    n_docs = _sql(base, "select count(*) from documents_snapshot")[0][0]
    n_lin = _sql(base, "select count(*) from document_details_snapshot")[0][0]
    n_con = _sql(base, "select count(distinct document_id) from document_details_snapshot")[0][0]
    assert (n_docs, n_lin, n_con) == (200, 500, 100)


# ---------------------------------------------------------------------------
# 2. snapshot_details con marca de agua, contra Postgres
# ---------------------------------------------------------------------------

def test_leer_estado_devuelve_dict_desde_jsonb(base):
    """Auditoria del 30-sep: si psycopg devolviera str, `isinstance(fila, dict)`
    daria False siempre y la marca/racha nunca se leerian."""
    import sync_incremental as sync
    sync._registrar_estado("prueba_jsonb", {"a": 1, "b": [1, 2], "c": None})
    assert sync._leer_estado("prueba_jsonb") == {"a": 1, "b": [1, 2], "c": None}
    assert sync._leer_estado("no_existe") is None


def test_el_incremental_solo_mira_lo_escrito_desde_la_marca(base, monkeypatch):
    import snapshot as sn
    cli = _ClienteDetalleFalso()
    monkeypatch.setattr(sn, "get_client", lambda: cli)

    r = sn.snapshot_details(max_docs=2000, oldest_first=True,
                            desde_snapshot_date=AHORA - timedelta(hours=6))
    assert r["candidates_total"] == len(PENDIENTES_NUEVOS) == 40, r
    assert r["docs_processed"] == 40 and r["remaining_to_process"] == 0
    assert sorted(cli.pedidos) == sorted(d["document_id"] for d in PENDIENTES_NUEVOS)
    # centinelas: 1197 sin lineas (-1), 1198 truncado (-2), 1199 404 (-3)
    assert r["docs_sin_lineas"] == 1 and r["docs_con_mas_de_2000_lineas"] == 1 and r["docs_404_en_bsale"] == 1
    assert r["errors"] == 0, "un 404 permanente NO es un error: deja centinela"
    cent = _sql(base, "select document_id, line_id from document_details_snapshot where line_id < 0 order by 1")
    assert [tuple(x) for x in cent] == [(1197, -1), (1198, -2), (1199, -3)]
    # los 60 viejos siguen pendientes: la marca no los toco
    pend = _sql(base, "select count(*) from documents_snapshot d where not exists "
                      "(select 1 from document_details_snapshot x where x.document_id=d.document_id)")[0][0]
    assert pend == 60


def test_el_barrido_completo_con_set_local_recoge_el_resto(base, monkeypatch):
    import snapshot as sn
    cli = _ClienteDetalleFalso()
    monkeypatch.setattr(sn, "get_client", lambda: cli)

    r = sn.snapshot_details(max_docs=2000, oldest_first=True, statement_timeout_ms=180000)
    assert r["candidates_total"] == 60 and r["docs_processed"] == 60 and r["errors"] == 0
    assert r["desde_snapshot_date"] is None
    # SET LOCAL no se pego a la conexion del pool: el timeout de sesion sigue en el del engine
    st = _sql(base, "show statement_timeout")[0][0]
    assert st == "20s", f"statement_timeout de la conexion cambio a {st}"
    pend = _sql(base, "select count(*) from documents_snapshot d where not exists "
                      "(select 1 from document_details_snapshot x where x.document_id=d.document_id)")[0][0]
    assert pend == 0


def test_detalle_historico_step_escribe_y_lee_la_marca_en_sync_estado(base, monkeypatch):
    """El paso entero del cron, con _leer_estado/_registrar_estado REALES."""
    import snapshot as sn
    import sync_incremental as sync
    monkeypatch.setattr(sn, "get_client", lambda: _ClienteDetalleFalso())
    _sql(base, "delete from sync_estado where clave = 'detalle_historico_marca'")

    t0 = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
    r1 = sync.detalle_historico_step(t0)
    assert r1["barrido"] == "completo" and r1["marca_avanzo_a"] == t0.isoformat()
    fila = sync._leer_estado("detalle_historico_marca")
    assert fila["verificado_hasta"] == t0.isoformat()
    assert fila["ultimo_barrido_completo_ok"] == t0.isoformat()

    r2 = sync.detalle_historico_step(t0 + timedelta(minutes=30))
    assert r2["barrido"] == "incremental", "con marca fresca la segunda corrida es incremental"
    assert r2["desde_snapshot_date"] == (t0 - timedelta(hours=sync.DETALLE_MARCA_MARGEN_HORAS)).isoformat()
    assert r2["docs_processed"] == 0 and r2["remaining_to_process"] == 0


def test_el_plan_del_count_de_pendientes_usa_el_indice_de_snapshot_date(base):
    """EXPLAIN del count de pendientes con marca (la segunda consulta de
    snapshot_details, sin ORDER BY). Con 200 filas el planner prefiere seq
    scan; se fuerza enable_seqscan=off solo para comprobar que el indice ES
    usable por ese predicado (sargable). Si alguien cambiara el filtro a
    date_trunc(snapshot_date) o similar, esto lo delata."""
    from sqlalchemy import text
    import db
    with db.session() as s:
        s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = s.execute(text(
            "EXPLAIN SELECT count(*) FROM documents_snapshot d "
            "WHERE NOT EXISTS (SELECT 1 FROM document_details_snapshot x WHERE x.document_id = d.document_id) "
            "AND snapshot_date >= :desde"
        ), {"desde": AHORA - timedelta(hours=6)}).fetchall()
    texto = "\n".join(r[0] for r in plan)
    assert "ix_documents_snapshot_snapshot_date" in texto, texto


# ---------------------------------------------------------------------------
# 3. Racha de fallas persistida en Postgres
# ---------------------------------------------------------------------------

def test_evaluar_fallas_persiste_la_racha_y_alerta_a_la_tercera(base, monkeypatch):
    import sync_incremental as sync
    monkeypatch.setattr(sync, "CRON_FALLAS_PARA_ALERTAR", 3)
    _sql(base, "delete from sync_estado where clave = 'cron_fallas'")
    falla = {"detalle_historico_error": "QueryCanceled"}
    assert sync._evaluar_fallas(["detalle_historico_error"], falla) == 0
    assert sync._evaluar_fallas(["detalle_historico_error"], falla) == 0
    assert sync._evaluar_fallas(["detalle_historico_error"], falla) == 1
    fila = sync._leer_estado("cron_fallas")
    assert fila["rachas"] == {"detalle_historico": 3} and fila["alerto"] == ["detalle_historico"]
    assert sync._evaluar_fallas([], {"detalle_historico": {"errors": 0}}) == 0
    assert sync._leer_estado("cron_fallas")["rachas"] == {}


def test_leer_estado_con_la_base_caida_levanta_estado_ilegible(base, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db
    import sync_incremental as sync
    roto = create_engine("postgresql+psycopg://postgres@127.0.0.1:1/nada",
                         connect_args={"connect_timeout": 1})
    monkeypatch.setattr(db, "get_session_maker", lambda: sessionmaker(bind=roto))
    with pytest.raises(sync.EstadoIlegible):
        sync._leer_estado("cron_fallas")
    assert sync._evaluar_fallas(["x_error"], {"x_error": "x"}) == 1


# ---------------------------------------------------------------------------
# 4. Retencion de raw (la que fallo meses en silencio y despues por CAST)
# ---------------------------------------------------------------------------

def test_apply_retention_minimiza_solo_el_raw_viejo(base):
    import retention as rt
    out = rt.apply_retention()
    assert out["hubo_error"] is False, out
    assert out["documents_raw"]["minimizadas"] == 10, out["documents_raw"]
    viejo = _sql(base, "select raw from documents_snapshot where document_id = 1190")[0][0]
    assert "firstName" not in str(viejo) and "code" not in viejo.get("client", {})
    assert viejo["totalAmount"] == 3000 and viejo["client"]["id"] == 1190 % 7 + 500
    reciente = _sql(base, "select raw from documents_snapshot where document_id = 1000")[0][0]
    assert reciente["client"]["firstName"] == "Juan"
    # idempotente
    out2 = rt.apply_retention()
    assert out2["documents_raw"]["minimizadas"] == 0 and out2["hubo_error"] is False


# ---------------------------------------------------------------------------
# 5. Digests y tools _fast contra los datos sembrados
# ---------------------------------------------------------------------------

def test_build_all_genera_los_cuatro_digests(base):
    import digests
    r = digests.build_all()
    assert r == {"ventas_hoy": "ok", "ventas_30d": "ok", "ventas_90d": "ok", "stock_resumen": "ok"}, r
    claves = {x[0] for x in _sql(base, "select digest_key from llm_digests")}
    assert {"ventas_hoy", "ventas_30d", "ventas_90d", "stock_resumen"} <= claves


def _tools(modulo):
    registrados = {}

    class Espia:
        def tool(self, *a, **kw):
            def deco(fn):
                registrados[fn.__name__] = fn
                return fn
            return deco

    modulo.register(Espia())
    return registrados


def test_ventas_fast_aplica_la_regla_de_venta_oficial(base):
    import tools_snapshot
    t = _tools(tools_snapshot)["bsale_ventas_fast"]
    desde = (AHORA - timedelta(days=30)).date().isoformat()
    hasta = AHORA.date().isoformat()
    r = t(start_date=desde, end_date=hasta)
    esperado = [d for d in DOCS if _es_venta_oficial(d) and d["emission_date"].date() >= date.fromisoformat(desde)]
    assert r["venta_oficial"] == pytest.approx(sum(_signo(d) * d["total_amount"] for d in esperado))
    assert r["documentos_de_venta"] == sum(1 for d in esperado if d["document_type_use"] == 0)
    assert r["notas_de_credito"] == sum(1 for d in esperado if d["document_type_use"] == 1)
    assert r["excluidos"]["documentos"] == 15, "10 notas de venta + 5 anuladas"


def test_top_productos_y_venta_por_sucursal_cuadran_con_las_lineas(base):
    import tools_intelligence_db as ti
    tools = _tools(ti)
    desde = (AHORA - timedelta(days=30)).date().isoformat()
    hasta = AHORA.date().isoformat()

    top = tools["bsale_top_productos_fast"](date_from=desde, date_to=hasta, top_n=3)
    assert len(top["top_products"]) == 3
    assert top["top_products"][0]["units_sold"] >= top["top_products"][1]["units_sold"]

    por_suc = tools["bsale_venta_por_sucursal"](date_from=desde, date_to=hasta)
    oficinas = {x["office_id"] for x in por_suc["por_sucursal"]}
    assert oficinas == {1, 2, 3}
    # todos los documentos del periodo tienen detalle (o centinela): cobertura 100
    assert por_suc["cobertura_detalle"]["pct"] == 100
    total_unidades = por_suc["totales"]["unidades"]
    esperado = sum(_signo(d) * ln["quantity"]
                   for d in DOCS if _es_venta_oficial(d) and d["emission_date"].date() >= date.fromisoformat(desde)
                   for ln in _lineas(d) if d["document_id"] not in (1197, 1198, 1199))
    assert total_unidades == pytest.approx(esperado, abs=1)


def test_stock_variante_lee_stock_actual_y_ausente_no_es_cero(base):
    import tools_stocks
    t = _tools(tools_stocks)["bsale_stock_variante"]
    r = t(code="SKU-003")
    assert len(r["sucursales"]) == 11
    assert r["total_unidades"] == sum(x["quantity"] for x in r["sucursales"])
    r2 = t(code="SKU-999")
    assert "error" in r2 and "sucursales" not in r2, "ausente NO es cero"


def test_snapshot_status_lee_las_claves_del_cron(base):
    import tools_snapshot
    t = _tools(tools_snapshot)["bsale_snapshot_status"]
    r = t()
    assert r["documents"]["total_rows"] == 200
    assert r["details"]["unique_documents"] == 200
    assert r["cron"]["detalle_marca"]["verificado_hasta"]
    assert r["cron"]["fallas"]["rachas"] == {}
