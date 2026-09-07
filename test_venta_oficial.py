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
