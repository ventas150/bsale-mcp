"""Politica de retencion para las tablas de snapshot.

Evita que la base crezca sin limite (la causa del incidente de storage
de agosto 2026: stock_snapshot llego a 13 GB con fotos horarias que
ningun tool consultaba).

POR QUE ESTE ARCHIVO SE REESCRIBIO (08-sep-2026)
------------------------------------------------
La version anterior no funcionaba, y no funcionaba en silencio. Medido:
stock_snapshot tenia 7.535.095 filas creciendo 80.000 por noche, o sea
~94 fotos, con una politica que decia 30 dias. En la MISMA corrida,
variants_snapshot quedaba en exactamente 2 snapshots (44.508 filas), o sea
que purge_variants_snapshots SI funcionaba. Mismo codigo, misma corrida: la
unica diferencia era el tamano de la tabla.

La causa es que el DELETE llevaba adentro

    snapshot_date NOT IN (SELECT max(snapshot_date) FROM stock_snapshot
                          GROUP BY snapshot_date::date)

que obliga a agregar las 7,5 millones de filas enteras en cada ejecucion.
Con el statement_timeout de 20 s que db.py pone en TODAS las conexiones, eso
no alcanza a terminar nunca. La excepcion la atrapaba apply_retention(), la
escribia en un log que nadie lee, y la corrida seguia como si nada.

Dos cambios de fondo:

1. El borrado se parte en dos. El grueso (todo lo anterior al corte diario)
   es un predicado de RANGO sobre snapshot_date, que es la primera columna de
   la PK (snapshot_date, variant_id, office_id): usa el indice y va por lotes
   acotados. Recien despues, sobre una tabla ya chica, se aplica la regla fina
   de "una foto por dia".
2. La retencion corre con su propio statement_timeout. El de 20 s existe para
   que una consulta pesada no ocupe una de las 5 conexiones del web service;
   un mantenimiento nocturno no tiene por que heredarlo.

Reglas:
- stock_snapshot: tabla LEGADA desde el 08-sep-2026 (el stock vive en
  stock_actual). Se VACIA por lotes hasta que quede en cero y despues es un
  no-op. No hay politica de horas ni de dias: la tabla entera sobra.
- variants_snapshot: las ultimas VARIANTS_KEEP_SNAPSHOTS fotos (default 2).
- documents_snapshot.raw: se MINIMIZA pasados RAW_RETENTION_MONTHS meses
  (default 6). La fila se queda (ahi vive el historico de ventas que alimenta
  los comparativos, y usa las columnas tipadas), pero el JSON crudo se reduce
  a los campos de negocio: fuera nombre, RUT, correo, telefono, direccion,
  el TED (que trae el RUT del receptor), el token y las URLs publicas del
  documento. Ver purge_documents_raw. Ley 21.719, vigente desde dic-2026.
- document_details_snapshot: NO se toca (no tiene datos de personas).

Transaccionalidad (09-sep-2026): UNA transaccion POR LOTE. La version anterior
abria la sesion afuera y pasaba la misma sesion a todos los lotes: ~153
vueltas de DELETE ... LIMIT 50000 dentro de una sola transaccion, con el
commit en el __exit__. Cualquier corte (deploy, timeout, solapamiento) era un
ROLLBACK completo y cero progreso, y media hora despues arrancaba de nuevo
desde el principio. El docstring decia "cada vuelta toma un lock corto" y era
falso. Ahora cada lote commitea solo: lo borrado queda borrado.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from db import session as db_session

logger = logging.getLogger(__name__)

VARIANTS_KEEP_SNAPSHOTS = int(os.getenv("VARIANTS_KEEP_SNAPSHOTS", "2"))

# Timeout propio del mantenimiento. El de db.py (20 s) protege al web service.
RETENTION_TIMEOUT_MS = int(os.getenv("RETENTION_STATEMENT_TIMEOUT_MS", "600000"))
# Filas por lote. Acotado para que un lote entre holgado en el timeout y para
# no tomar un lock largo sobre la tabla.
RETENTION_BATCH = int(os.getenv("RETENTION_BATCH_ROWS", "50000"))

# Meses que documents_snapshot.raw conserva los datos del cliente. 0 = apagado
# (no minimiza nada; sirve de kill-switch, no de politica).
RAW_RETENTION_MONTHS = int(os.getenv("RAW_RETENTION_MONTHS", "6"))
# Filas por lote del UPDATE de raw. Es un UPDATE de JSONB (reescribe la fila
# entera, ~10 KB cada una por el messageBodyFormat que Bsale mete en cada
# document_type), no un DELETE por ctid: lote chico.
RAW_RETENTION_BATCH = int(os.getenv("RAW_RETENTION_BATCH_ROWS", "5000"))

# Lo UNICO que sobrevive en raw pasado el plazo. Todo lo que no este aca se
# borra: nombre, RUT (client.code y el RR del TED), correo, telefono,
# direccion/comuna/ciudad (del cliente y del documento), token y URLs
# publicas, giro. Si un tool nuevo necesita algo mas de raw para documentos
# viejos, se agrega aca A PROPOSITO y con el test de PII mirando.
RAW_CAMPOS_QUE_QUEDAN: tuple[str, ...] = (
    "id", "number", "emissionDate", "generationDate", "expirationDate",
    "totalAmount", "netAmount", "taxAmount", "exemptAmount",
    "state", "commercialState", "cancellationStatus", "cancellationDate",
    "informedSii", "salesId",
)
RAW_SUBCAMPOS_QUE_QUEDAN: dict[str, tuple[str, ...]] = {
    "document_type": ("id", "name", "use", "isSalesNote", "codeSii", "isCreditNote"),
    "office": ("id", "name"),
    # client.id ya esta tipado; companyOrPerson e isForeigner no identifican.
    "client": ("id", "companyOrPerson", "isForeigner"),
    "priceList": ("id",),
    "user": ("id",),
    "coin": ("id",),
}
# Marca que deja la minimizacion dentro de raw. El predicado de seleccion la
# usa para no volver a pasar por la misma fila.
RAW_MARCA = "_retencion"


def _sin_timeout_corto(s) -> None:
    """Le da a ESTA transaccion el timeout del mantenimiento.

    SET LOCAL solo dura la transaccion, asi que no afecta al resto del pool.
    """
    s.execute(text(f"SET LOCAL statement_timeout = {RETENTION_TIMEOUT_MS}"))


def _borrar_por_lotes(
    where_sql: str, params: dict[str, Any], batch: int, max_lotes: int | None = None
) -> tuple[int, bool]:
    """Borra en lotes acotados por ctid, UNA TRANSACCION POR LOTE.
    Devuelve (borradas, quedan_pendientes).

    El DELETE ... WHERE ctid IN (SELECT ctid ... LIMIT n) es la forma de acotar
    un borrado masivo en Postgres. Cada vuelta abre su propia sesion, toma su
    propio SET LOCAL statement_timeout y commitea al salir: si la corrida se
    corta en el lote 80, los 79 anteriores ya estan borrados.

    max_lotes acota el tiempo total: desde el cron (ventana de 30 min) y desde
    un tool MCP (corte de 180 s del cliente). None corre hasta terminar.
    """
    borradas = 0
    lotes = 0
    sql = text(
        f"""
        DELETE FROM stock_snapshot
        WHERE ctid IN (
            SELECT ctid FROM stock_snapshot
            WHERE {where_sql}
            LIMIT :batch
        )
        """
    )
    while True:
        with db_session() as s:
            _sin_timeout_corto(s)
            res = s.execute(sql, {**params, "batch": batch})
            n = res.rowcount or 0
        borradas += n
        lotes += 1
        if n < batch:
            return borradas, False
        if max_lotes is not None and lotes >= max_lotes:
            return borradas, True


def purge_stock_snapshots(max_lotes: int | None = None) -> dict[str, Any]:
    """Vacia stock_snapshot, que es una tabla legada.

    El 08-sep-2026 el stock dejo de guardarse como serie de tiempo y paso a
    stock_actual, una fila por (variante, sucursal) con upsert. Se reviso quien
    leia stock_snapshot y TODOS los consumidores pedian solo la foto mas
    reciente; los tools de quiebres, proyeccion y sobrestockeos ni la tocaban
    (leen stock en vivo de Bsale). O sea que sus 7.653.095 filas eran historico
    que nadie consultaba.

    Ya no hay politica de dias ni de horas que aplicar: la tabla entera sobra.
    Esto la vacia por lotes y despues queda como no-op. La tabla se deja
    creada a proposito, para poder volver atras sin una migracion inversa; se
    borra cuando stock_actual lleve unas semanas andando.
    """
    out: dict[str, Any] = {"tabla": "stock_snapshot (legada)"}

    with db_session() as s:
        _sin_timeout_corto(s)
        out["filas_antes"] = s.execute(
            text("SELECT count(*) FROM stock_snapshot")
        ).scalar_one()

    if out["filas_antes"] == 0:
        out["borradas_total"] = 0
        out["quedan_pendientes"] = False
        return out

    # Sin WHERE que dependa de fechas: sobra la tabla completa. Cada lote
    # commitea solo (ver _borrar_por_lotes).
    borradas, pendientes = _borrar_por_lotes("TRUE", {}, RETENTION_BATCH, max_lotes)
    out["borradas_total"] = borradas
    out["quedan_pendientes"] = pendientes
    out["filas_despues"] = out["filas_antes"] - borradas
    return out


def purge_variants_snapshots() -> int:
    """Conserva solo las ultimas N fotos del catalogo. Devuelve filas borradas.

    Esta si funcionaba: variants_snapshot es chica (44.508 filas) y el mismo
    patron de subquery le sale barato. Se deja como estaba, con el timeout
    largo por consistencia.
    """
    sql = text(
        """
        DELETE FROM variants_snapshot
        WHERE snapshot_date NOT IN (
          SELECT snapshot_date FROM (
            SELECT DISTINCT snapshot_date
            FROM variants_snapshot
            ORDER BY snapshot_date DESC
            LIMIT :keep
          ) k
        )
        """
    )
    with db_session() as s:
        _sin_timeout_corto(s)
        res = s.execute(sql, {"keep": VARIANTS_KEEP_SNAPSHOTS})
        return res.rowcount or 0


# ============================
# documents_snapshot.raw
# ============================

def _restar_meses(d: date, meses: int) -> date:
    """d menos N meses calendario, al dia 1 del mes resultante.

    Al dia 1 a proposito: el corte es "todo lo emitido antes del mes M", que
    es facil de explicar y de auditar, y calza con el hist_end() del cron que
    relee cada mes cerrado UNA vez, el 1 del siguiente. Un corte a mitad de
    mes minimizaria filas que esa relectura vuelve a hidratar.
    """
    y, m = d.year, d.month - meses
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def raw_corte(hoy: date | None = None, meses: int | None = None) -> date | None:
    """Fecha de emision desde la cual raw se conserva completo. None = apagado."""
    meses = RAW_RETENTION_MONTHS if meses is None else meses
    if meses <= 0:
        return None
    hoy = hoy or datetime.now(timezone.utc).date()
    return _restar_meses(hoy, meses)


def _sql_raw_minimo() -> str:
    """Expresion jsonb con SOLO los campos de RAW_CAMPOS_QUE_QUEDAN.

    Se construye desde las tuplas, no a mano: asi el test que prohibe PII mira
    la misma lista que produce el SQL. jsonb_strip_nulls saca las claves que el
    documento no traia (un cliente ausente queda como {} y no como nulls).
    """
    partes = [f"'{k}', raw->'{k}'" for k in RAW_CAMPOS_QUE_QUEDAN]
    for padre, hijos in RAW_SUBCAMPOS_QUE_QUEDAN.items():
        sub = ", ".join(f"'{h}', raw->'{padre}'->'{h}'" for h in hijos)
        partes.append(f"'{padre}', jsonb_build_object({sub})")
    # CAST explicito: dentro de jsonb_build_object psycopg no puede inferir el
    # tipo del parametro y Postgres responde IndeterminateDatatype (paso en la
    # primera corrida real, 09-sep 02:00). La sesion falsa de los tests no lo
    # ve: esto solo lo prueba Postgres.
    partes.append(
        f"'{RAW_MARCA}', jsonb_build_object("
        "'minimizado', CAST(:ts AS text), 'meses', CAST(:meses AS integer))"
    )
    return "jsonb_strip_nulls(jsonb_build_object(" + ", ".join(partes) + "))"


# Predicado de "todavia tiene el raw completo y ya vencio". Va en el UPDATE y
# en el indice parcial: tiene que ser la MISMA expresion para que el planner
# use el indice. El indice se vacia solo a medida que las filas se minimizan,
# asi que la vuelta del cron sobre una tabla ya procesada cuesta cero.
RAW_PREDICADO = f"emission_date < :corte AND NOT (raw ? '{RAW_MARCA}')"
RAW_INDICE_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_documents_raw_pendiente "
    "ON documents_snapshot (emission_date) "
    f"WHERE NOT (raw ? '{RAW_MARCA}')"
)


def _minimizar_raw_por_lotes(
    corte: date, batch: int, max_lotes: int | None = None
) -> tuple[int, bool]:
    """UPDATE por lotes, UNA TRANSACCION POR LOTE (igual que _borrar_por_lotes).
    Devuelve (minimizadas, quedan_pendientes)."""
    sql = text(
        f"""
        UPDATE documents_snapshot
        SET raw = {_sql_raw_minimo()}
        WHERE document_id IN (
            SELECT document_id FROM documents_snapshot
            WHERE {RAW_PREDICADO}
            LIMIT :batch
        )
        """
    )
    ts = datetime.now(timezone.utc).isoformat()
    params = {"corte": corte, "batch": batch, "ts": ts, "meses": RAW_RETENTION_MONTHS}
    minimizadas = 0
    lotes = 0
    while True:
        with db_session() as s:
            _sin_timeout_corto(s)
            res = s.execute(sql, params)
            n = res.rowcount or 0
        minimizadas += n
        lotes += 1
        if n < batch:
            return minimizadas, False
        if max_lotes is not None and lotes >= max_lotes:
            return minimizadas, True


def purge_documents_raw(max_lotes: int | None = None) -> dict[str, Any]:
    """Minimiza documents_snapshot.raw para lo emitido antes del corte.

    NO borra filas: la venta oficial, los comparativos y la conciliacion leen
    las columnas tipadas (emission_date, office_id, document_type_*, montos,
    state) y siguen iguales. Lo que se va es el JSON crudo con la ficha del
    cliente que Bsale expande en cada documento (nombre, RUT, correo,
    telefono, direccion), el TED (trae el RUT del receptor), el token y las
    URLs publicas del PDF. Queda lo que esta en RAW_CAMPOS_QUE_QUEDAN.

    Idempotente y re-aplicable: la marca `_retencion` dentro de raw evita
    repasar filas. Si un backfill manual (bsale_snapshot_backfill_rango,
    backfill.py) vuelve a hidratar el raw de un rango viejo, la siguiente
    corrida del cron lo minimiza de nuevo: la politica se sostiene sola.

    Efecto conocido: bsale_segmentacion_clientes_rfm_fast saca nombre y
    empresa de raw->client; para clientes cuya ultima compra es anterior al
    corte vienen en null. El client_id sigue, y la ficha se resuelve en vivo
    con bsale_obtener_cliente cuando haga falta contactar a alguien.
    """
    out: dict[str, Any] = {"tabla": "documents_snapshot.raw", "meses": RAW_RETENTION_MONTHS}
    corte = raw_corte()
    if corte is None:
        out["aplicado"] = False
        out["motivo"] = "RAW_RETENTION_MONTHS=0: minimizacion apagada"
        return out
    out["corte_emission_date"] = corte.isoformat()

    with db_session() as s:
        _sin_timeout_corto(s)
        s.execute(text(RAW_INDICE_SQL))
        out["pendientes_antes"] = s.execute(
            text(f"SELECT count(*) FROM documents_snapshot WHERE {RAW_PREDICADO}"),
            {"corte": corte},
        ).scalar_one()

    if out["pendientes_antes"] == 0:
        out["minimizadas"] = 0
        out["quedan_pendientes"] = False
        return out

    minimizadas, pendientes = _minimizar_raw_por_lotes(corte, RAW_RETENTION_BATCH, max_lotes)
    out["minimizadas"] = minimizadas
    out["quedan_pendientes"] = pendientes
    out["pendientes_despues"] = out["pendientes_antes"] - minimizadas
    return out


def apply_retention(max_lotes: int | None = None) -> dict[str, Any]:
    """Aplica toda la politica. Nunca lanza, pero SI declara si algo fallo.

    OJO: la version anterior tambien atrapaba las excepciones, pero las dejaba
    en una clave anidada que cron_snapshot.py no miraba (solo revisa claves de
    primer nivel que terminen en _error). Resultado: la retencion podia fallar
    todas las noches durante meses sin que el cron marcara la corrida como
    fallida ni llegara un correo. Ahora se expone `hubo_error` para que el
    llamador no tenga que adivinar.
    """
    out: dict[str, Any] = {}
    try:
        out["stock"] = purge_stock_snapshots(max_lotes=max_lotes)
    except Exception as e:  # noqa: BLE001
        logger.error("Error en purge_stock_snapshots: %s", e)
        out["stock_error"] = str(e)[:300]
    try:
        out["variants_rows_deleted"] = purge_variants_snapshots()
    except Exception as e:  # noqa: BLE001
        logger.error("Error en purge_variants_snapshots: %s", e)
        out["variants_error"] = str(e)[:300]
    try:
        out["documents_raw"] = purge_documents_raw(max_lotes=max_lotes)
    except Exception as e:  # noqa: BLE001
        logger.error("Error en purge_documents_raw: %s", e)
        out["documents_raw_error"] = str(e)[:300]

    out["hubo_error"] = any(k.endswith("_error") for k in out)
    logger.info("Retencion aplicada: %s", out)
    return out
