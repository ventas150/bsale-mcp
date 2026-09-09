"""Sync incremental frecuente (Render Cron Job).

A diferencia del nocturno (cron_snapshot.py -> nightly_snapshot), este corre
seguido para mantener venta y stock casi en vivo, y regenera la capa LLM.

Modos (flag --modo):
  ventas  -> documentos del día + detalle reciente + digests   (barato)
  stock   -> foto completa de stock + digests                  (pesado)
  full    -> ventas + stock + digests
  auto    -> RECOMENDADO para correr cada 30 min en un solo cron:
               * ventas + detalle + digests  -> SIEMPRE
               * stock                        -> 1 vez por hora (top of hour)
               * variantes (catálogo)         -> 1 vez al día (~05:xx UTC)
             Así, con schedule */30 * * * *, obtienes ventas c/30min,
             stock c/hora y catálogo diario, sin reventar el rate limit.

Uso:
  python sync_incremental.py --modo auto    # para el cron */30 * * * *
  python sync_incremental.py --modo ventas
  python sync_incremental.py --modo stock

Reanudable e idempotente: todos los snapshots hacen upsert / on_conflict.
Sale con código !=0 si algún paso falla (para que Render marque la corrida).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("sync_incremental")

# Hora UTC en la que el modo auto refresca el catálogo de variantes (1x/día).
VARIANTS_HOUR_UTC = 5

# La foto completa de stock via API tarda ~2h (235k registros a 50/pagina).
# Correrla cada hora bloqueaba todas las corridas del cron (incidente ago-2026):
# ahora solo corre si la ultima foto tiene mas de STOCK_EVERY_HOURS horas, y
# SIEMPRE al final de la corrida para no retrasar ventas/backfill/digests.
STOCK_EVERY_HOURS = float(os.getenv("STOCK_EVERY_HOURS", "12"))


def _stock_photo_age_hours() -> float:
    """Horas desde la ultima foto de stock. Infinito si no hay ninguna."""
    from sqlalchemy import text
    from db import session as db_session
    try:
        with db_session() as s:
            ts = s.execute(text("select max(updated_at) from stock_actual")).scalar()
        if not ts:
            return float("inf")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception as e:  # noqa: BLE001
        logger.warning("_stock_photo_age_hours: %s", e)
        return 0.0  # ante la duda, no correr el paso pesado


def _ultima_corrida_stock_completa() -> bool:
    """True si la ultima corrida de stock termino de leer todo Bsale.

    Sin esto, una corrida cortada a mitad deja stock_actual con la mitad de las
    filas y con updated_at reciente: _stock_photo_age_hours la ve fresca y el
    modo auto no la vuelve a correr en 12 horas. Paso el 08-sep-2026: una
    corrida cancelada dejo 67.000 de ~150.000 filas.

    Ante un error de lectura devuelve True (no forzar el paso pesado), igual
    criterio que _stock_photo_age_hours.
    """
    from sqlalchemy import text
    from db import session as db_session
    try:
        with db_session() as s:
            fila = s.execute(text(
                "select valor from sync_estado where clave = 'stock_ultima_corrida'"
            )).scalar()
        if not fila:
            # Nunca se registro una corrida: no se puede afirmar que este
            # completa, asi que se corre.
            return False
        return bool(fila.get("completo"))
    except Exception as e:  # noqa: BLE001
        logger.warning("_ultima_corrida_stock_completa: %s", e)
        return True


def _variants_empty() -> bool:
    """True si el catalogo de variantes aun no se carga en esta base."""
    from sqlalchemy import text
    from db import session as db_session
    try:
        with db_session() as s:
            row = s.execute(text("select 1 from variants_snapshot limit 1")).first()
        return row is None
    except Exception:  # noqa: BLE001
        return False

# Backfill histórico de documentos por tramos (hasta HIST_MESES_POR_CORRIDA
# meses por corrida :30 del cron). Recorre desde HIST_START hasta HIST_END
# (exclusivo). Tras el incidente de storage ago-2026 la base se reconstruye
# desde cero, por lo que el rango cubre hasta hoy.
HIST_START = "2024-12"
HIST_MESES_POR_CORRIDA = 4


def hist_end() -> str:
    """Mes actual: el backfill recorre hasta el mes anterior, inclusive.

    Antes esto era la constante "2026-09". Al llegar ahi el paso devolvia
    {"hist": "completo"} PARA SIEMPRE y ningun mes posterior se volvia a leer
    jamas. Combinado con la ventana corta de documentos, cualquier documento
    con fecha de emision retroactiva que se escapara de esa ventana quedaba
    fuera del snapshot de forma permanente.

    Movil, el cursor vuelve a moverse cuando cambia el mes: al entrar octubre,
    septiembre pasa a ser < hist_end() y se relee entero, recogiendo todo lo
    que se haya emitido con fecha retroactiva durante el mes.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _month_bounds(ym: str):
    y, m = int(ym[:4]), int(ym[5:7])
    start = "%04d-%02d-01" % (y, m)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    nxt = "%04d-%02d-01" % (ny, nm)
    return start, nxt, "%04d-%02d" % (ny, nm)


def _hist_cursor_get() -> str:
    from sqlalchemy import text
    from db import session as db_session
    with db_session() as s:
        row = s.execute(text(
            "select data->>'cursor' from llm_digests where digest_key='hist_backfill_cursor'"
        )).first()
    return row[0] if row and row[0] else HIST_START


def _hist_cursor_set(ym: str) -> None:
    import json as _json
    from sqlalchemy import text
    from db import session as db_session
    with db_session() as s:
        s.execute(text(
            "insert into llm_digests (digest_key, data, generated_at) "
            "values ('hist_backfill_cursor', cast(:d as jsonb), now()) "
            "on conflict (digest_key) do update set data=excluded.data, generated_at=excluded.generated_at"
        ), {"d": _json.dumps({"cursor": ym})})


def recolectar_errores(obj: Any, _ruta: str = "", _prof: int = 0) -> list[str]:
    """Todas las senales de error de una corrida, a CUALQUIER nivel del dict.

    El chequeo era `[k for k in results if k.endswith("_error")]`, o sea solo
    el primer nivel. Pero ningun paso pone su error ahi: snapshot_stock
    devuelve {"stock": {..., "stock_error": ...}}, apply_retention devuelve
    {"retention": {..., "hubo_error": True}}, y el backfill historico deja
    "hist_error" dentro de una LISTA. Ninguno llegaba al chequeo.

    Consecuencia medida el 08-sep-2026: una corrida de stock que se detecta a
    si misma como incompleta escribe stock_error, no borra nada (bien) y
    despues sale con codigo 0. Render la marca verde y no llega ninguna
    notificacion. Es el mismo modo de falla por el que la retencion fallo cada
    30 minutos durante meses sin que nadie se enterara.

    `errors` (el contador de snapshot_details) NO cuenta por si solo: que
    fallen algunos documentos de un lote de 2.000 es normal. La falla sistemica
    (token vencido, Bsale caido) es que NINGUNO haya entrado.

    La primera version de este chequeo decia `errors >= docs_processed`, y
    estaba mal en las dos direcciones. En snapshot.py el camino de error hace
    `errors += 1; continue`, o sea que docs_processed NO se incrementa: los dos
    contadores son DISJUNTOS. Consecuencias medidas:

      - Fallan los 400 documentos del lote -> docs_processed = 0 -> la guarda
        exigia `proc > 0` -> NO disparaba. Justo el caso que decia cubrir.
      - Fallan 1.001 de 2.000 -> errors >= docs_processed -> SI disparaba, y
        una tanda de 429 transitorios que la corrida siguiente completa sola
        dejaba el cron en rojo.

    El criterio correcto es `errors > 0 and docs_processed == 0`: se intento y
    no entro ninguno.
    """
    hallazgos: list[str] = []
    if _prof > 6:
        return hallazgos
    if isinstance(obj, str) and obj.startswith("error:"):
        # digests.py no usa una clave *_error: mete el error en el VALOR
        # ({"ventas_hoy": "error: could not connect"}). Un matcher que solo
        # mira nombres de clave lo dejaba pasar entero, que es el mismo modo
        # de falla que este helper vino a cerrar.
        return [_ruta or "<raiz>"]
    if isinstance(obj, dict):
        errs, proc = obj.get("errors"), obj.get("docs_processed")
        if (
            isinstance(errs, int)
            and isinstance(proc, int)
            and errs > 0
            and proc == 0
        ):
            hallazgos.append(("%s.errors" % _ruta) if _ruta else "errors")
        for k, v in obj.items():
            ruta = ("%s.%s" % (_ruta, k)) if _ruta else str(k)
            if isinstance(k, str) and v and (k.endswith("_error") or k == "hubo_error"):
                hallazgos.append(ruta)
                continue
            hallazgos.extend(recolectar_errores(v, ruta, _prof + 1))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hallazgos.extend(recolectar_errores(v, "%s[%d]" % (_ruta, i), _prof + 1))
    return hallazgos


def backfill_historico_step() -> dict[str, Any]:
    """Carga UN mes histórico de documentos y avanza el cursor. Idempotente."""
    cur = _hist_cursor_get()
    if cur >= hist_end():
        return {"hist": "al dia", "cursor": cur}
    from snapshot import snapshot_documents_range
    start, nxt, nxt_ym = _month_bounds(cur)
    # 200 paginas eran 10.000 documentos, y los meses de MyScrubs llegan a
    # 14.400 (marzo-2025). snapshot_documents_range SI declara el truncado; el
    # que lo ignoraba era este llamador, que avanzaba el cursor igual. Como
    # hist_end() cierra el recorrido, el mes quedaba con un hueco PERMANENTE que
    # nadie volvia a mirar: asi se perdieron 4.101 documentos de marzo-2025
    # ($239,6 millones, el 45,9% del mes).
    res = snapshot_documents_range(start, nxt, max_pages=1200)
    if res.get("truncado"):
        # No avanzar: dejar el cursor donde esta para reintentar este mes.
        return {
            "hist_mes": cur,
            "rows": res.get("rows"),
            "next_cursor": cur,
            "hist_error": (
                f"{cur} quedo TRUNCADO ({res.get('documentos_leidos')} de "
                f"{res.get('documentos_en_bsale')} documentos). El cursor NO "
                "avanza: se reintenta el mismo mes en la proxima corrida."
            ),
        }
    _hist_cursor_set(nxt_ym)
    return {"hist_mes": cur, "rows": res.get("rows"), "next_cursor": nxt_ym}


def sync_ventas() -> dict[str, Any]:
    """Documentos recientes + detalle reciente. Barato (usa emissiondaterange)."""
    from snapshot import snapshot_documents, snapshot_details

    out: dict[str, Any] = {}
    # days_back=14, NO 2. Bsale permite emitir con fecha retroactiva: la
    # boleta 1280257 tiene emissionDate 03-sep-2026 y generationDate
    # 07-sep-2026, cuatro dias despues. Con la ventana de 2 dias nunca entro al
    # snapshot, y como el backfill historico ya se habia declarado completo,
    # ninguna corrida iba a volver a mirar el 3 de septiembre: $101.970
    # perdidos de forma permanente. Verificado con bsale_conciliacion_venta
    # sobre 1-7 sep (1.174 documentos en Bsale, 1.173 en el snapshot).
    #
    # El docstring de snapshot_documents ya decia "NO bajar a 1"; el arreglo de
    # los 14 dias se habia aplicado a nightly_snapshot(), que ningun cron corre.
    # El upsert es por document_id, asi que releer dias ya cargados no duplica.
    #
    # Y 30, NO 14: la boleta 1273799 tiene emissionDate 18-jul y
    # generationDate 05-ago, DIECIOCHO dias de desfase. Con 14 quedaba fuera,
    # y como hist_end() relee cada mes cerrado una sola vez (el 1 del
    # siguiente), julio no se volvia a mirar jamas. 30 dias son ~7.200
    # documentos a ~1 s por pagina de 50: ~2,5 min mas por corrida, contra
    # perder plata para siempre. max_pages sube en proporcion.
    out["documents"] = snapshot_documents(days_back=30, max_pages=1000)
    # Ventana de 90 dias: cada corrida procesa hasta 400 documentos sin detalle,
    # asi el cron va completando el backlog historico de ~90 dias por si solo
    # (de lo mas reciente a lo mas viejo). Cuando esta al dia, solo mantiene lo nuevo.
    out["details"] = snapshot_details(batch_size=400, max_docs=100000, only_recent_days=90)
    return out


def sync_stock() -> dict[str, Any]:
    """Foto completa del stock actual. Pesado: ~235k registros (sin filtro incremental)."""
    from snapshot import snapshot_stock

    return {"stock": snapshot_stock(max_pages=6000)}


def sync_variantes() -> dict[str, Any]:
    """Catálogo completo de variantes (pesado; correr 1x/día)."""
    from snapshot import snapshot_variants

    return {"variants": snapshot_variants(max_pages=2000)}


# Clave del advisory lock de Postgres que evita dos corridas del cron a la vez.
# Un bigint cualquiera, fijo. Lo unico que importa es que sea el mismo en todos
# los procesos que corren este sync.
CRON_LOCK_KEY = 76187337


def _tomar_candado_de_corrida():
    """Toma el advisory lock de sesion. Devuelve la conexion que lo sostiene, o
    None si otra corrida lo tiene.

    No habia ningun candado. En operacion normal las corridas no se solapaban
    porque do_stock salia False, pero apenas una corrida de stock terminaba
    INCOMPLETA, do_stock quedaba True para todas las siguientes: a los 30
    minutos arrancaba otra con la anterior en vuelo, 4+4 = 8 conexiones
    contra el umbral de 6 de Bsale, las dos acumulaban 429, las dos terminaban
    incompletas, y el bucle se reforzaba solo. Ademas _borrar_stock_no_reportado
    solo es correcto con una corrida a la vez.

    Lock de SESION (no de transaccion): se suelta solo cuando el proceso muere,
    que es exactamente lo que hace falta cuando un deploy mata el cron.

    Devuelve (conexion, "ok") con el lock tomado; (None, "ocupado") si otra
    corrida lo tiene; (None, "error") si no se pudo ni preguntar. Los dos
    ultimos se tratan distinto: "ocupado" se salta en silencio, "error" sigue
    sin candado y lo dice, porque un fallo de conexion no puede convertir el
    cron en un no-op permanente sin que nadie se entere.
    """
    from sqlalchemy import text
    from db import get_engine
    try:
        conn = get_engine().connect()
    except Exception as e:  # noqa: BLE001
        logger.warning("No se pudo abrir conexion para el candado: %s", e)
        return None, "error"
    try:
        got = conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": CRON_LOCK_KEY}).scalar()
    except Exception as e:  # noqa: BLE001
        logger.warning("pg_try_advisory_lock fallo: %s", e)
        conn.close()
        return None, "error"
    if not got:
        conn.close()
        return None, "ocupado"
    return conn, "ok"


def _soltar_candado_de_corrida(conn) -> None:
    from sqlalchemy import text
    try:
        conn.execute(text("select pg_advisory_unlock(:k)"), {"k": CRON_LOCK_KEY})
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def run(modo: str) -> int:
    candado, estado = _tomar_candado_de_corrida()
    if estado == "ocupado":
        # Otra corrida esta en vuelo. No es un error: es la razon de ser del
        # candado. Salir en 0 para que Render no lo pinte de rojo.
        logger.warning("Hay otra corrida del sync en vuelo (advisory lock %s tomado). "
                       "Esta corrida se salta.", CRON_LOCK_KEY)
        return 0
    if estado == "error":
        logger.warning("Sin candado de corrida (no se pudo consultar Postgres). Se sigue.")
    try:
        return _run(modo)
    finally:
        if candado is not None:
            _soltar_candado_de_corrida(candado)


def _run(modo: str) -> int:
    results: dict[str, Any] = {}
    now = datetime.now(timezone.utc)

    # El cron nunca creaba tablas: init_db() solo corria en server.py, o sea en
    # el web service. Agregar una tabla a db.py quedaba dependiendo de que el
    # web service se reiniciara primero, y mientras tanto el paso que la usa
    # fallaba en silencio (los helpers atrapan la excepcion). create_all es
    # idempotente y barato, asi que el cron tambien se asegura su esquema.
    try:
        from db import init_db
        init_db()
    except Exception as e:  # noqa: BLE001
        logger.warning("init_db en el cron fallo: %s", e)

    # Asegura el esquema de digests ANTES de todo: el cursor del backfill
    # historico vive en llm_digests y en una base recien creada aun no existe.
    try:
        from digests import ensure_schema
        ensure_schema()
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_schema fallo: %s", e)

    do_ventas = modo in ("ventas", "full", "auto")
    # stock: en full/stock siempre; en auto solo si la foto esta vieja (ver arriba)
    # Dos condiciones, no una. "Fresca" no implica "completa": una corrida
    # cortada deja la tabla a medias con updated_at reciente.
    do_stock = modo in ("stock", "full") or (
        modo == "auto"
        and (
            _stock_photo_age_hours() >= STOCK_EVERY_HOURS
            or not _ultima_corrida_stock_completa()
        )
    )
    # variantes: 1 vez al día, o si el catalogo aun no existe en esta base
    do_variants = modo == "auto" and (
        (now.hour == VARIANTS_HOUR_UTC and now.minute < 15) or _variants_empty()
    )
    # backfill histórico de documentos: siempre que quede pendiente (no-op al completar)
    do_hist = modo == "auto"

    if do_ventas:
        try:
            results.update(sync_ventas())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_ventas: %s", e)
            results["ventas_error"] = str(e)

    if do_hist:
        try:
            pasos = []
            for _ in range(HIST_MESES_POR_CORRIDA):
                paso = backfill_historico_step()
                pasos.append(paso)
                if paso.get("hist") == "al dia":
                    break
            results["historico"] = pasos
        except Exception as e:  # noqa: BLE001
            logger.error("Error en backfill_historico_step: %s", e)
            results["hist_error"] = str(e)

    if do_variants:
        try:
            results.update(sync_variantes())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_variantes: %s", e)
            results["variants_error"] = str(e)

    # Detalle de linea del hueco historico, del mas viejo al mas nuevo.
    #
    # OJO CON DONDE VIVE ESTE PASO. El cron de Render corre
    # `python sync_incremental.py --modo auto`, NO cron_snapshot.py. La
    # primera version de este backfill quedo dentro de nightly_snapshot(),
    # que ningun cron ejecuta: habria estado "listo" sin correr nunca.
    # Verificado el 08-sep-2026 leyendo la configuracion del cron en Render.
    #
    # POR QUE HACE FALTA: el paso de detalle de sync_ventas mira solo lo
    # reciente. Medido el 08-sep-2026: ene-ago 2025 tenia 0% de detalle de
    # linea en 54.562 documentos y ene-ago 2026 un 58,2%, asi que las
    # UNIDADES no se podian comparar ano contra ano.
    #
    # El presupuesto es chico A PROPOSITO: este cron corre cada 30 minutos y
    # la corrida normal dura ~1m30s. 2.000 documentos son ~45 s a los 45
    # doc/s medidos. Con 48 corridas al dia son ~96.000 documentos diarios,
    # o sea que los ~51.000 pendientes se cierran en menos de un dia sin
    # alargar ninguna corrida ni arriesgar solapamiento.
    try:
        from snapshot import snapshot_details

        results["detalle_historico"] = snapshot_details(
            max_docs=int(os.getenv("DETALLE_HISTORICO_POR_CORRIDA", "2000")),
            oldest_first=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Error en el backfill historico de detalle: %s", e)
        results["detalle_historico_error"] = str(e)

    # Regenerar la capa LLM (antes del stock, que es el paso lento).
    try:
        from digests import build_all
        results["digests"] = build_all()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en digests: %s", e)
        results["digests_error"] = str(e)

    # Retencion: purga fotos viejas de stock/variantes para que la DB no
    # crezca sin limite (incidente storage ago-2026). Corre siempre, con tope
    # de lotes para no comerse la ventana de 30 minutos.
    #
    # Un fallo de retencion SI marca la corrida: antes el comentario decia que
    # no y el codigo hacia que si (apply_retention devuelve hubo_error y
    # recolectar_errores lo matchea a cualquier profundidad). Se decidio por
    # el codigo: una retencion que falla es una base que crece sin limite, y
    # eso ya fue un incidente. Por eso la clave es *_error y no *_warning.
    try:
        from retention import apply_retention
        results["retention"] = apply_retention(max_lotes=int(os.getenv("RETENTION_MAX_LOTES", "20")))
    except Exception as e:  # noqa: BLE001
        logger.error("Error en retention: %s", e)
        results["retention_error"] = str(e)

    # Stock AL FINAL: es el paso lento (~2h por la API de Bsale) y no debe
    # bloquear ventas, backfill ni digests si la corrida se corta.
    if do_stock:
        try:
            results.update(sync_stock())
        except Exception as e:  # noqa: BLE001
            logger.error("Error en sync_stock: %s", e)
            results["stock_error"] = str(e)

    logger.info("Sync (%s) terminado [stock=%s variants=%s]: %s",
                modo, do_stock, do_variants, results)

    failed = recolectar_errores(results)
    if failed:
        logger.error("Pasos con error: %s", failed)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync incremental Bsale -> Postgres + digests")
    parser.add_argument(
        "--modo",
        choices=["ventas", "stock", "full", "auto"],
        default="auto",
        help="auto (recomendado para cron */30), ventas, stock, o full.",
    )
    args = parser.parse_args()
    return run(args.modo)


if __name__ == "__main__":
    sys.exit(main())
