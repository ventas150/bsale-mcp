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
- `bsale_ventas_por_periodo` — ventas agregadas en rango de fechas
- `bsale_listar_sucursales` — sucursales activas
- `bsale_listar_clientes` — clientes (con filtros)
- `bsale_top_productos` — análisis de top sellers por período

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
pip install -r requirements.txt

# 4. Configurar credentials
cp .env.example .env
# Editar .env y poner tu BSALE_ACCESS_TOKEN

# 5. Correr
python -m src.server
```

El server arranca en `http://localhost:8000/mcp`

## Deploy en Render

Ver `DEPLOY.md` para el paso a paso de deployment.

## Conectar en Cowork

1. Cowork → Settings → MCPs → Add Remote MCP
2. URL: `https://bsale-mcp-myscrubs.onrender.com/mcp`
3. Transport: `streamable-http`
4. (No requiere auth header — el server identifica MyScrubs por el `BSALE_ACCESS_TOKEN` interno)

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
