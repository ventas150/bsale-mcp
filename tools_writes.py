"""Tools de escritura en Bsale.

Todas las operaciones aqui pasan por audit log automaticamente
(via bsale_client.post/put/delete).

Use con cuidado: estos tools modifican data real en Bsale.
"""
from __future__ import annotations

import os
from typing import Any

from bsale_client import get_client
from guardrails import (
    GuardrailError,
    con_iva,
    guard_price_write,
    guard_stock_write,
    guard_variant_write,
    issue_confirm_token,
    consume_confirm_token,
    validar_cantidad,
    validar_costo,
    validate_price_updates,
)


def _stock_actual_de(client, variant_id: int, office_id: int):
    """Stock de una variante en una sucursal. None si no se pudo leer.

    Verifica que la fila devuelta sea la pedida en vez de tomar items[0] a
    ciegas: si Bsale ignorara un filtro que no reconoce, items[0] seria la
    primera fila del listado COMPLETO y se ajustaria contra el stock de otra
    variante.
    """
    try:
        data = client.get(
            "/v1/stocks.json",
            params={"variantid": variant_id, "officeid": office_id, "limit": 50,
                    "expand": "[variant,office]"},
            use_cache=False,
        )
    except Exception:  # noqa: BLE001
        return None
    for item in (data.get("items") or []):
        v = (item.get("variant") or {}).get("id")
        o = (item.get("office") or {}).get("id")
        try:
            if v is not None and o is not None and int(v) == variant_id and int(o) == office_id:
                return float(item.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
    return None


def _flag_env(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _costo_promedio_de(client, variant_id: int):
    """Costo promedio de una variante segun Bsale. None si no se pudo leer.

    Bsale expone GET /v1/variants/{id}/costs.json con `averageCost` (y
    `lastCost`). Si el endpoint cambia, no responde o trae 0, devolvemos None
    y el llamador BLOQUEA en vez de recepcionar a costo 0: es preferible no
    mover stock a contaminar el costo promedio para siempre.
    """
    try:
        data = client.get(f"/v1/variants/{int(variant_id)}/costs.json", use_cache=False)
    except Exception:  # noqa: BLE001
        return None
    for clave in ("averageCost", "average_cost", "lastCost", "last_cost"):
        v = data.get(clave) if isinstance(data, dict) else None
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def register(mcp) -> None:  # noqa: ANN001
    """Registra tools de escritura."""

    # ============================
    # STOCK
    # ============================

    @mcp.tool()
    def bsale_ajustar_stock(
        variant_id: int,
        office_id: int,
        quantity: float,
        note: str = "Ajuste via MCP",
        cost: float | None = None,
    ) -> dict[str, Any]:
        """Deja el stock de una variante en una sucursal EN UN VALOR FINAL.

        WRITE OPERATION. Pasa por audit log.

        Antes esto posteaba a /v1/stocks/adjustments.json. Ese endpoint NO
        aparece en la documentacion de Bsale: la documentacion oficial lista
        DOS endpoints de escritura de stock, receptions y consumptions, y
        ninguno mas (verificado el 08-sep-2026 en dos fuentes). Es el mismo
        caso del POST de lista de precios, que tampoco existia.

        Ademas el docstring prometia "cantidad final" mientras el comentario de
        bsale_client advertia que un reintento podia "consumir el stock dos
        veces", cosa que solo tiene sentido si quantity es un delta. Las dos
        cosas no podian ser ciertas. La documentacion dice que en receptions y
        consumptions quantity es la cantidad A SUMAR o A RESTAR, no el saldo.

        Ahora se lee el stock actual, se calcula la diferencia y se aplica con
        el endpoint documentado que corresponda. Tres consecuencias:

          1. No depende de un endpoint no documentado.
          2. La semantica es de verdad la que dice el nombre: valor final.
          3. Es IDEMPOTENTE. Correrlo dos veces no mueve nada la segunda vez,
             porque la diferencia ya es cero. Bsale no expone claves de
             idempotencia, asi que esta es la unica forma de que un reintento
             por timeout no aplique el ajuste dos veces.

        Args:
            variant_id: ID de la variante (SKU).
            office_id: ID de la sucursal.
            quantity: Cantidad FINAL que debe quedar en stock. 0 es valido.
            note: Nota explicativa (queda en historial Bsale).
            cost: Costo unitario. OBLIGATORIO cuando el ajuste SUMA stock.

        Sobre `cost`: subir el stock se aplica como una recepcion, y una
        recepcion sin costo entra a costo 0 y arrastra el costo promedio de la
        variante hacia abajo EN BSALE, de forma permanente. Un scrub que cuesta
        $9.500 con 20 unidades, ajustado +10 a costo 0, queda con costo
        promedio $6.333 y todos los reportes de margen de esa variante quedan
        mal para siempre: Bsale no recalcula hacia atras. Por eso, si el ajuste
        suma y no viene costo, se aborta. Bajar stock no lo necesita.

        LIMITE CONOCIDO: entre el GET del stock actual y el POST no hay
        transaccion. Si se vende una unidad en el medio, el saldo final no es
        el pedido. La idempotencia vale para un reintento inmediato sobre
        stock quieto, no frente a movimientos concurrentes.
        """
        try:
            guard_stock_write()
            objetivo = validar_cantidad(quantity, "quantity", permitir_cero=True)
            if cost is not None:
                cost = validar_costo(cost)
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

        client = get_client()
        actual = _stock_actual_de(client, variant_id, office_id)
        if actual is None:
            return {
                "aplicado": False,
                "motivo": (
                    f"No se pudo leer el stock actual de la variante {variant_id} "
                    f"en la sucursal {office_id}. Sin el valor actual no hay como "
                    "calcular el ajuste: no se escribio nada."
                ),
            }

        delta = round(objetivo - actual, 4)
        if abs(delta) < 1e-9:
            return {
                "aplicado": False,
                "sin_cambios": True,
                "stock_actual": actual,
                "objetivo": objetivo,
                "detalle": "El stock ya esta en el valor pedido. No se escribio nada.",
            }

        detalle: dict[str, Any] = {"variantId": variant_id, "quantity": abs(delta)}
        if delta > 0:
            if cost is None:
                return {
                    "aplicado": False,
                    "bloqueado_por": (
                        f"Subir el stock de {actual} a {objetivo} se aplica como una "
                        "recepcion, y una recepcion sin `cost` entra a costo 0: "
                        "arrastra el costo promedio de la variante en Bsale hacia "
                        "abajo de forma permanente y deja todos los reportes de "
                        "margen mal. Pasar `cost` con el costo unitario real. No se "
                        "escribio nada."
                    ),
                    "stock_actual": actual,
                    "objetivo": objetivo,
                    "delta_necesario": delta,
                }
            detalle["cost"] = cost
            path, movimiento = "/v1/stocks/receptions.json", "recepcion"
        else:
            path, movimiento = "/v1/stocks/consumptions.json", "consumo"

        body = {
            "officeId": office_id,
            # El objetivo va en la nota: el audit log guarda el body, y sin esto
            # queda registrado "recepcion de 7" sin que se pueda reconstruir que
            # la orden fue "dejalo en 12".
            "note": f"{note} [objetivo {objetivo}, antes {actual}]",
            "details": [detalle],
        }
        # "aplicado": True explicito. Antes el camino de exito devolvia el JSON
        # crudo de Bsale, que no trae esa clave, mientras el camino BLOQUEADO si
        # devolvia {"aplicado": False}. Un llamador que escribiera el chequeo
        # obvio -- if not r.get("aplicado"): reintentar -- reintentaba sobre una
        # escritura EXITOSA.
        return {
            "aplicado": True,
            "movimiento": movimiento,
            "stock_antes": actual,
            "objetivo": objetivo,
            "delta_aplicado": delta,
            "bsale": client.post(path, json_body=body),
        }

    @mcp.tool()
    def bsale_consumir_stock(
        variant_id: int,
        office_id: int,
        quantity: float,
        note: str = "Consumo via MCP",
    ) -> dict[str, Any]:
        """Reduce stock de una variante (consumo). WRITE OPERATION.

        Util para reflejar ventas externas, mermas, regalos, etc.
        """
        try:
            guard_stock_write()
            quantity = validar_cantidad(quantity)
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

        client = get_client()
        body = {
            "officeId": office_id,
            "note": note,
            "details": [{"variantId": variant_id, "quantity": quantity}],
        }
        return {"aplicado": True, "bsale": client.post("/v1/stocks/consumptions.json", json_body=body)}

    @mcp.tool()
    def bsale_recepcionar_stock(
        variant_id: int,
        office_id: int,
        quantity: float,
        cost: float | None = None,
        note: str = "Recepcion via MCP",
    ) -> dict[str, Any]:
        """Recepciona stock (entrada). WRITE OPERATION.

        Util para reflejar compras a proveedor, devoluciones de clientes, etc.

        `cost` es OBLIGATORIO. Una recepcion sin costo entra a costo 0 y
        arrastra el costo promedio de la variante en Bsale hacia abajo de forma
        permanente: Bsale no recalcula hacia atras, y todo reporte de margen
        posterior queda mal. bsale_ajustar_stock ya lo exigia; este tool lo
        dejaba pasar por la puerta de al lado.
        """
        try:
            guard_stock_write()
            quantity = validar_cantidad(quantity)
            if cost is None:
                raise GuardrailError(
                    "Una recepcion sin `cost` entra a costo 0 y arrastra el costo "
                    "promedio de la variante en Bsale hacia abajo para siempre. "
                    "Pasar el costo unitario (neto) de la mercaderia que entra. "
                    "No se escribio nada."
                )
            cost = validar_costo(cost)
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

        client = get_client()
        detail: dict[str, Any] = {"variantId": variant_id, "quantity": quantity, "cost": cost}
        body = {
            "officeId": office_id,
            "note": note,
            "details": [detail],
        }
        return {"aplicado": True, "bsale": client.post("/v1/stocks/receptions.json", json_body=body)}

    @mcp.tool()
    def bsale_crear_traspaso_stock(
        variant_id: int,
        office_origin_id: int,
        office_destination_id: int,
        quantity: float,
        note: str = "Traspaso via MCP",
        cost: float | None = None,
    ) -> dict[str, Any]:
        """Traspasa stock entre sucursales. WRITE OPERATION.

        Son DOS movimientos, NO atomicos: consumo en origen y recepcion en
        destino. Si la recepcion falla se intenta compensar en origen y el
        resultado lo dice; leer `accion_requerida` si `aplicado` es False.

        `cost` es el costo unitario (neto) con que entra a destino. Si no se
        pasa, se lee el costo promedio de la variante en Bsale; si eso no se
        puede, el traspaso se BLOQUEA. Nunca se recepciona sin costo: entra a
        costo 0 y arrastra el costo promedio de la variante hacia abajo para
        siempre, que es el daño que bsale_ajustar_stock ya se negaba a hacer.

        Args:
            variant_id: SKU a mover.
            office_origin_id: Sucursal de origen.
            office_destination_id: Sucursal de destino.
            quantity: Unidades a mover.
            note: Nota que queda en historial.
            cost: Costo unitario neto. Opcional si Bsale entrega el promedio.

        Returns:
            Dict con resultado de consumo y recepcion.
        """
        client = get_client()
        try:
            guard_stock_write()
            quantity = validar_cantidad(quantity)
            if cost is None:
                cost = _costo_promedio_de(client, variant_id)
            if cost is None:
                raise GuardrailError(
                    "No se pudo leer el costo promedio de la variante en Bsale y "
                    "no se paso `cost`. Un traspaso sin costo recepciona a costo 0 "
                    "en destino y contamina el costo promedio para siempre. Pasar "
                    "`cost` explicito. No se escribio nada."
                )
            cost = validar_costo(cost)
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}

        if office_origin_id == office_destination_id:
            return {
                "aplicado": False,
                "bloqueado_por": "Origen y destino son la misma sucursal; el traspaso no hace nada.",
            }

        # 1. Consumo en origen
        consumption_body = {
            "officeId": office_origin_id,
            "note": f"[Traspaso] {note} -> office {office_destination_id}",
            "details": [{"variantId": variant_id, "quantity": quantity}],
        }
        consumption = client.post("/v1/stocks/consumptions.json", json_body=consumption_body)

        # 2. Recepcion en destino.
        # Si esta falla y dejamos que la excepcion suba, las unidades ya salieron de
        # origen y nunca entraron a destino: stock evaporado, sin rastro del consumo
        # que si se hizo. Por eso se captura y se intenta compensar.
        reception_body = {
            "officeId": office_destination_id,
            "note": f"[Traspaso] {note} <- office {office_origin_id}",
            "details": [{"variantId": variant_id, "quantity": quantity, "cost": cost}],
        }
        try:
            reception = client.post("/v1/stocks/receptions.json", json_body=reception_body)
        except Exception as e:  # noqa: BLE001
            compensacion = None
            compensacion_error = None
            try:
                compensacion = client.post(
                    "/v1/stocks/receptions.json",
                    json_body={
                        "officeId": office_origin_id,
                        "note": f"[Traspaso REVERTIDO] fallo la recepcion en {office_destination_id}",
                        "details": [{"variantId": variant_id, "quantity": quantity, "cost": cost}],
                    },
                )
            except Exception as e2:  # noqa: BLE001
                compensacion_error = str(e2)
            return {
                "aplicado": False,
                "error_recepcion": str(e),
                "consumo_si_se_hizo": consumption,
                "compensacion_en_origen": compensacion,
                "compensacion_error": compensacion_error,
                "accion_requerida": (
                    "El consumo en origen SI se ejecuto. La compensacion se intento y "
                    "su resultado esta arriba. Si compensacion_error no es null, hay "
                    f"{quantity} unidades de la variante {variant_id} fuera de inventario: "
                    "hay que reingresarlas a mano en la sucursal de origen."
                ),
            }

        return {
            "aplicado": True,
            "variant_id": variant_id,
            "from_office": office_origin_id,
            "to_office": office_destination_id,
            "quantity": quantity,
            "consumption": consumption,
            "reception": reception,
        }

    # ============================
    # PRECIOS
    # ============================

    def _leer_detalles_actuales(
        client, price_list_id: int, variant_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Lee el detalle vigente de cada variante: su id y su precio.

        El `id` del detalle no es un lujo: es la unica forma documentada de
        escribir un precio en Bsale (PUT sobre el detalle). Ver el comentario
        de bsale_actualizar_precios_masivo.
        """
        detalles: dict[int, dict[str, Any]] = {}
        for vid in variant_ids:
            try:
                data = client.get(
                    f"/v1/price_lists/{price_list_id}/details.json",
                    params={"variantid": vid, "limit": 50, "expand": "[variant]"},
                    use_cache=False,
                )
                for item in (data.get("items") or []):
                    # Verificar que el detalle es el de ESTA variante, en vez
                    # de tomar items[0] a ciegas. detalle_id es el destino del
                    # PUT: si Bsale ignorara el filtro `variantid`, items[0]
                    # seria la primera fila del listado COMPLETO de la lista de
                    # precios y se escribiria el precio nuevo SOBRE OTRA
                    # VARIANTE. Ni sin_precio_actual ni sin_detalle lo notarian,
                    # porque la clave del dict es vid pase lo que pase.
                    #
                    # Falla cerrada a proposito: si no se puede confirmar la
                    # variante, el detalle no entra, y el guardrail aborta la
                    # corrida entera antes de escribir nada.
                    v = (item.get("variant") or {}).get("id")
                    if v is None or int(v) != int(vid):
                        continue
                    valor = item.get("variantValue")
                    detalle_id = item.get("id")
                    if valor is not None and detalle_id is not None:
                        detalles[int(vid)] = {
                            "detail_id": int(detalle_id),
                            "precio": float(valor),
                        }
                    break
            except Exception:  # noqa: BLE001
                continue  # queda fuera -> el guardrail aborta
        return detalles

    def _leer_precios_actuales(client, price_list_id: int, variant_ids: list[int]) -> dict[int, float]:
        """Precio vigente por variante. Sin esto no hay rollback."""
        return {
            vid: d["precio"]
            for vid, d in _leer_detalles_actuales(client, price_list_id, variant_ids).items()
        }

    @mcp.tool()
    def bsale_actualizar_precios_masivo(
        price_list_id: int,
        updates: list[dict[str, Any]],
        dry_run: bool = True,
        confirm_token: str | None = None,
        max_delta_pct: float = 5.0,
    ) -> dict[str, Any]:
        """Cambia precios en una lista de precios. ESCRITURA CON CANDADO.

        Regla permanente de MyScrubs: los precios no los cambia un agente. Este
        tool esta deshabilitado por default (BSALE_PRICE_WRITES_ENABLED=0) y
        exige, ademas, que la lista este en la allowlist, un dry_run previo y un
        confirm_token de un solo uso.

        Flujo obligatorio:
          1. Llamar con dry_run=True (default). Devuelve la tabla de cambios con
             precio actual, precio nuevo y delta%, mas un confirm_token.
          2. Roberto revisa esa tabla.
          3. Volver a llamar con dry_run=False y ese confirm_token.

        Args:
            price_list_id: ID de la lista de precios (tiene que estar en la allowlist).
            updates: Lista de dicts con las claves `variant_id` y `new_price`.
                OJO: `new_price` es el precio NETO, SIN IVA, que es lo que
                guarda Bsale en variantValue. Para dejar un producto a $29.990
                en vitrina hay que mandar 25.201,68, no 29.990. La tabla de
                cambios devuelve las dos cifras para poder revisarlo.
            dry_run: True (default) solo simula y devuelve la tabla de cambios.
            confirm_token: El token que devolvio el dry_run. Obligatorio para escribir.
            max_delta_pct: Tope de variacion permitida por variante. Sobre eso, aborta.

        Returns:
            En dry_run, la tabla de cambios y el confirm_token. En escritura, el
            resultado de Bsale mas la tabla de lo aplicado (con los precios previos,
            que son los que permiten revertir).
        """
        client = get_client()
        try:
            guard_price_write(price_list_id)
            variant_ids = []
            for u in updates or []:
                if isinstance(u, dict) and u.get("variant_id") is not None:
                    try:
                        variant_ids.append(int(u["variant_id"]))
                    except (TypeError, ValueError):
                        pass
            detalles = _leer_detalles_actuales(client, price_list_id, variant_ids)
            actuales = {vid: d["precio"] for vid, d in detalles.items()}
            tabla = validate_price_updates(
                updates, current=actuales, max_delta_pct=max_delta_pct
            )
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e), "cambios": 0}

        # El precio de vitrina al lado del neto. Sin esto, la tabla que
        # Roberto aprueba esta en una moneda distinta a la que el piensa: los
        # precios de MyScrubs se hablan CON IVA ($29.990) y Bsale los guarda
        # SIN IVA (25.201,68). Aprobar 32.990 "para dejarlo en 32.990" dejaria
        # la vitrina en $39.258.
        for fila in tabla:
            fila["precio_actual_con_iva"] = (
                con_iva(fila["precio_actual"]) if fila.get("precio_actual") else None
            )
            fila["precio_nuevo_con_iva"] = con_iva(fila["precio_nuevo"])

        payload = {"price_list_id": price_list_id, "tabla": tabla}
        if dry_run:
            return {
                "aplicado": False,
                "dry_run": True,
                "price_list_id": price_list_id,
                "cambios": len(tabla),
                "nota_precios": (
                    "Los precios de la lista son NETOS (sin IVA), que es lo que "
                    "guarda Bsale. Las columnas *_con_iva son el precio de "
                    "vitrina, referencial: los productos exentos no llevan IVA."
                ),
                "tabla_de_cambios": tabla,
                "confirm_token": issue_confirm_token(payload),
                "siguiente_paso": (
                    "Roberto revisa la tabla. Si aprueba, repetir la MISMA llamada con "
                    "dry_run=False y este confirm_token."
                ),
            }

        try:
            consume_confirm_token(confirm_token, payload)
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e), "cambios": 0}

        # OJO: aca habia un POST a /v1/price_lists/{id}/details.json con un
        # arreglo "details". ESE ENDPOINT NO EXISTE. La documentacion de Bsale
        # es explicita: "NO existe un POST de lista de precio, debido a que las
        # listas de precios comparten el total de productos de Bsale. Y solo se
        # puede editar sus valores, con el verbo PUT". Lo unico documentado es
        # PUT /v1/price_lists/{id}/details/{detailId}.json, de a un detalle.
        # El POST habria fallado DESPUES de consumir el confirm_token, o sea
        # con el candado ya gastado y sin haber escrito nada.
        # Por eso hace falta el id del detalle, no el de la variante.
        sin_detalle = [
            f["variant_id"] for f in tabla if int(f["variant_id"]) not in detalles
        ]
        if sin_detalle:
            return {
                "aplicado": False,
                "bloqueado_por": (
                    "No se pudo resolver el id del detalle en la lista para "
                    f"{len(sin_detalle)} variante(s). Sin ese id no hay forma "
                    "documentada de escribir el precio, y no se escribe nada "
                    "a medias."
                ),
                "variantes_sin_detalle": sin_detalle,
                "cambios": 0,
            }

        aplicados: list[dict[str, Any]] = []
        fallidos: list[dict[str, Any]] = []
        for f in tabla:
            vid = int(f["variant_id"])
            detalle_id = detalles[vid]["detail_id"]
            try:
                client.put(
                    f"/v1/price_lists/{price_list_id}/details/{detalle_id}.json",
                    json_body={"id": detalle_id, "variantValue": f["precio_nuevo"]},
                )
                aplicados.append(f)
            except Exception as e:  # noqa: BLE001
                fallidos.append({**f, "error": str(e)})

        return {
            # Se escribe de a una variante, asi que una corrida puede quedar a
            # medias. Se declara en vez de disfrazarlo de exito.
            "aplicado": bool(aplicados) and not fallidos,
            "parcial": bool(aplicados) and bool(fallidos),
            "price_list_id": price_list_id,
            "cambios": len(aplicados),
            "fallidos": fallidos,
            "tabla_aplicada": aplicados,
            "para_revertir": [
                {"variant_id": f["variant_id"], "new_price": f["precio_actual"]}
                for f in aplicados
            ],
        }

    @mcp.tool()
    def bsale_actualizar_precio_variante(
        variant_id: int,
        price_list_id: int,
        new_price: float,
        dry_run: bool = True,
        confirm_token: str | None = None,
        max_delta_pct: float = 5.0,
    ) -> dict[str, Any]:
        """Cambia el precio de UNA variante. ESCRITURA CON CANDADO.

        Mismos candados que bsale_actualizar_precios_masivo: kill-switch, allowlist
        de listas, dry_run por default y confirm_token. Ver ese tool para el flujo.
        
        UNIDAD: `new_price` es el precio NETO (sin IVA), que es lo que guarda
        Bsale en variantValue. Para dejar un producto en $32.990 de vitrina,
        new_price = guardrails.sin_iva(32990) = 27.722,69. Mandar 32.990 lo
        deja en $39.258 en la tienda. La tabla del dry_run trae las dos
        columnas (precio_*_con_iva) para verificarlo antes de confirmar.
        """
        return bsale_actualizar_precios_masivo(
            price_list_id=price_list_id,
            updates=[{"variant_id": variant_id, "new_price": new_price}],
            dry_run=dry_run,
            confirm_token=confirm_token,
            max_delta_pct=max_delta_pct,
        )

    # ============================
    # PRODUCTOS
    # ============================

    @mcp.tool()
    def bsale_activar_variante(variant_id: int) -> dict[str, Any]:
        """Activa una variante (state=0). WRITE OPERATION."""
        try:
            guard_variant_write(f"activar variante {variant_id}")
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}
        client = get_client()
        return {"aplicado": True, "bsale": client.put(
            f"/v1/variants/{variant_id}.json", json_body={"state": 0})}

    @mcp.tool()
    def bsale_desactivar_variante(variant_id: int) -> dict[str, Any]:
        """Desactiva una variante (state=1). WRITE OPERATION.

        Una variante desactivada desaparece del catalogo activo en las 9
        tiendas y en cualquier integracion que filtre por state.
        """
        try:
            guard_variant_write(f"desactivar variante {variant_id}")
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}
        client = get_client()
        return {"aplicado": True, "bsale": client.put(
            f"/v1/variants/{variant_id}.json", json_body={"state": 1})}

    @mcp.tool()
    def bsale_actualizar_variante(
        variant_id: int,
        code: str | None = None,
        barcode: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Actualiza campos de una variante. WRITE OPERATION.

        Solo se actualizan los campos que se pasan.

        OJO CON `code`: es el SKU, y es la llave que une Bsale con Shopify y
        con Mercado Libre en la tabla sku_mapping. Cambiarlo deja el mapping
        huerfano en silencio y el sync de los tres canales deja de encontrar la
        variante. No lanza ningun error: simplemente dejan de coincidir.
        """
        try:
            guard_variant_write(f"actualizar variante {variant_id}")
            if code is not None:
                # Candado PROPIO para el SKU, aparte del de catalogo. Cuando
                # BSALE_CATALOG_WRITES_ENABLED se abra para editar una
                # descripcion, este tool no puede aprovechar la ventana para
                # romper el sku_mapping de los tres canales.
                if not _flag_env("BSALE_SKU_WRITES_ENABLED"):
                    raise GuardrailError(
                        "Cambiar `code` (el SKU) esta BLOQUEADO por politica "
                        "(BSALE_SKU_WRITES_ENABLED=0), aunque el catalogo este "
                        "abierto: rompe el sku_mapping con Shopify y Mercado "
                        "Libre sin dar error. Para un cambio puntual se habilita "
                        "esa variable, se aplica y se apaga. No se escribio nada."
                    )
        except GuardrailError as e:
            return {"aplicado": False, "bloqueado_por": str(e)}
        client = get_client()
        body: dict[str, Any] = {}
        if code is not None:
            body["code"] = code
        if barcode is not None:
            body["barCode"] = barcode
        if description is not None:
            body["description"] = description

        if not body:
            return {"error": "Debe especificar al menos un campo a actualizar"}

        return {"aplicado": True, "bsale": client.put(
            f"/v1/variants/{variant_id}.json", json_body=body)}
