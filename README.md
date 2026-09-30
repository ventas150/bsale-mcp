# MyScrubs Bsale MCP

MCP (Model Context Protocol) server para interactuar con la API de Bsale, ERP/facturación chileno.

Expone tools de **lectura** (productos, stock, ventas, documentos, sucursales) y **escritura** (actualizar stock, crear documentos, actualizar productos) que pueden ser consumidos por agentes de IA conectados vía Cowork o cualquier cliente MCP.

## Capacidades

### Tools de lectura
- `bsale_listar_productos` — lista productos con filtros (categoría, marca, vendor)
- `bsale_obtener_producto` — detalle de un producto específico
- `bsale_listar_stock` — stock por producto y sucursal
- `bsale_stock_por_sucursal` — vista agregada de stock por sucursal
- `bsale_listar_documentos` — facturas, boletas, notas de crédito
- `bsale_obtener_documento` — detalle de un documento (incluye items)
- `bsale_ventas_fast` — venta oficial de un periodo desde el snapshot (Postgres)
- `bsale_listar_sucursales` — sucursales activas
- `bsale_listar_clientes` — clientes (con filtros)
- `bsale_top_productos_fast` — top sellers por periodo desde el snapshot

### Tools de escritura
- `bsale_actualizar_stock` — ajustar cantidad de stock
- `bsale_actualizar_producto` — modificar producto (precio, estado, etc)
- `bsale_crear_documento` — crear factura/boleta

## Stack técnico

- **Lenguaje:** Python 3.11+
- **Framework MCP:** FastMCP 2.x (transport: streamable-http)
- **HTTP client:** httpx
- **Hosting:** Render Standard ($25/mo)
- **Auth a Bsale:** `access_token` en header (env var)

## Quickstart local

```bash
# 1. Clonar
git clone https://github.com/myscrubs/bsale-mcp.git
cd bsale-mcp

# 2. Crear venv
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# o
.venv\Scripts\activate     # Windows

# 3. Instalar deps
pip install -r requirements.in   # en el PC; requirements.txt es el lock de linux que instala Render

# 4. Configurar credentials
cp .env.example .env
# Editar .env y poner tu BSALE_ACCESS_TOKEN

# 5. Correr
python -m src.server
```

El server arranca en `http://localhost:8000/mcp`

## Tests

Dos familias, las dos se corren desde el PC con el venv (`pip install -r requirements-dev.txt`):

- **Unitaria** (`test_venta_oficial.py`): sin red ni base, sesion y cliente falsos.
  `.venv\Scripts\python.exe -m pytest -q`
- **Integracion** (`test_integracion.py`): contra un **Postgres real**. Crea el esquema
  con `init_db()`, siembra 200 documentos y 500 lineas, y corre de verdad
  `snapshot_details` con marca de agua, `ensure_indexes`, la racha del cron,
  `apply_retention`, los digests y los tools `_fast`. Sin `DATABASE_URL_TEST` se
  SALTA entera; se niega a correr contra una URL de Render. Bsale sigue falso.
  `scripts\pg-test.cmd` (solo integracion) o `scripts\pg-test.cmd todo` (las dos).

El Postgres de pruebas del PC es portable (sin instalador ni servicio): binarios
EDB de PostgreSQL 18 en `C:\Users\rolguin\pg18\pgsql`, datos en
`C:\Users\rolguin\pgdata`, puerto **5433**, usuario `postgres` sin clave (trust,
solo localhost), base `bsale_test`. `pg-test.cmd` lo arranca si esta apagado;
para apagarlo: `C:\Users\rolguin\pg18\pgsql\bin\pg_ctl -D C:\Users\rolguin\pgdata stop`.

## Deploy en Render

Ver `DEPLOY.md` para el paso a paso de deployment.

## Conectar en Cowork

1. Cowork → Settings → MCPs → Add Remote MCP
2. URL: `https://bsale-mcp-myscrubs.onrender.com/mcp`
3. Transport: `streamable-http`
4. (La credencial es `MCP_URL_SECRET`, embebida en la ruta: `/mcp/<secreto>`. Sin esa variable el servidor no arranca. Ver DEPLOY.md.)

## Roadmap

- [x] Read tools (productos, stock, documentos, sucursales)
- [x] Write tools (update stock, create document)
- [ ] Webhooks de Bsale (recibir eventos en tiempo real)
- [ ] Cache de productos/clientes (reduce latencia)
- [ ] Agregados pre-calculados (top sellers, ABC analysis)
- [ ] MercadoLibre MCP (siguiente proyecto)

## Seguridad

- El `BSALE_ACCESS_TOKEN` NUNCA está en código, solo en env vars de Render
- HTTPS forzado vía Render
- No se loguea el token en logs ni responses
- Rotar token cada 90 días recomendado

## Owner

- **Build:** Cowork session 26-may-2026 + Roberto Olguín
- **Mantenimiento:** Paul (TBD)
- **Repo:** github.com/myscrubs/bsale-mcp (privado)
