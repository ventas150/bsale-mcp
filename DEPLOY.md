# Guia de Deployment — Bsale MCP en Render

Esta guia asume:
- Repo de GitHub ya creado (`bsale-mcp`)
- Archivos del proyecto subidos al repo
- Tienes cuenta en Render

## Paso 1 — Conectar GitHub a Render

1. Login en https://dashboard.render.com
2. Click **"New +"** (arriba a la derecha)
3. Click **"Blueprint"** (no "Web Service" — Blueprint detecta el render.yaml automaticamente)
4. **"Connect a repository"** → autoriza Render para leer tus repos de GitHub
5. Selecciona `bsale-mcp` del listado

Render detecta automaticamente el `render.yaml` y configura el web service.

## Paso 2 — Setear `BSALE_ACCESS_TOKEN` y `MCP_URL_SECRET`

CRITICAL: ninguno de los dos esta en el repo (`.gitignore`). Los dos son `sync: false`
en `render.yaml`: el blueprint NO los provee, hay que escribirlos a mano.

1. En la pantalla de Blueprint config, veras la lista de env vars
2. `BSALE_ACCESS_TOKEN`: el token de la API de Bsale
3. `MCP_URL_SECRET`: un secreto largo y aleatorio (por ejemplo `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   **Sin esta variable el servidor se niega a arrancar.** Da acceso de escritura al ERP de
   produccion; hasta el 09-sep-2026 arrancaba abierto a internet si faltaba.
4. Click **"Apply"** (abajo)

Las demas env vars vienen del `render.yaml`. OJO: los tres candados de escritura
(`BSALE_PRICE_WRITES_ENABLED`, `BSALE_STOCK_WRITES_ENABLED`, `BSALE_CATALOG_WRITES_ENABLED`)
tienen que quedar en `"0"`; hay un test que lo verifica sobre el yaml.

## Paso 3 — Esperar el primer deploy

1. Render arranca el build (~3-5 min)
2. Veras logs en tiempo real:
   - "Installing requirements..."
   - "Starting bsale-mcp-myscrubs on 0.0.0.0:10000"
3. Cuando termine, el service tendra status **"Live"** con un check verde

## Paso 4 — Probar el health endpoint

Abrir en browser:
```
https://bsale-mcp-myscrubs.onrender.com/health
```

Deberia devolver:
```json
{"status": "ok", "service": "bsale-mcp-myscrubs"}
```

(Reemplazar el dominio con el que Render te da — sera del estilo `bsale-mcp-myscrubs-xyz123.onrender.com`)

## Paso 5 — Probar el MCP endpoint

El endpoint MCP esta en `/mcp/<MCP_URL_SECRET>`. El secreto va en la RUTA porque el conector
de Cowork no permite headers. `/mcp` a secas devuelve 401. Para validar que esta corriendo:

```bash
curl -X POST https://bsale-mcp-myscrubs.onrender.com/mcp/<MCP_URL_SECRET> \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list"
  }'
```

Deberia devolver la lista de los ~17 tools de Bsale.

## Paso 6 — Conectar a Cowork

1. Cowork → Settings → MCPs → **Add Remote MCP**
2. **Name:** `bsale-myscrubs`
3. **URL:** `https://bsale-mcp-myscrubs.onrender.com/mcp/<MCP_URL_SECRET>` (el secreto en la ruta)
4. **Transport:** `streamable-http`
5. **Headers:** vacio. La credencial ya viaja en la URL; el conector no soporta headers.
   Si da 401, el secreto de la URL no coincide con `MCP_URL_SECRET` en Render. NUNCA se
   arregla borrando la variable: sin ella el servidor no arranca.
6. Click **Save**

Esperar 5-10 segundos. Si conecta correctamente, Cowork mostrara los 17 tools disponibles.

## Paso 7 — Primera query de prueba

En Cowork, pedir:
```
Lista las primeras 5 productos de Bsale
```

Deberia ejecutar `bsale_listar_productos(limit=5)` y devolver los productos.

## Troubleshooting

### "Application failed to respond"
- Verificar logs en Render Dashboard
- Causa comun: BSALE_ACCESS_TOKEN no configurado o invalido
- Verificar logs por "BsaleAuthError"

### "Connection refused"
- El service esta sleeping (free tier) o no termino de deployar
- Esperar 1-2 min y reintentar
- Standard tier ($25) no duerme

### Cowork no detecta los tools
- Verificar URL termina en `/mcp` (no `/`)
- Verificar transport sea `streamable-http`
- Logs de Render deberian mostrar `Started server` y peticiones SSE entrantes

### Rate limit de Bsale (429)
- Bsale tiene 90 req/min por defecto
- Si Cowork hace muchas queries en paralelo, puede saltar
- Solucion: agregar exponential backoff en bsale_client.py (TODO)

## Actualizar el codigo

Cuando hagamos cambios al codigo:

1. Modificar archivos localmente o via GitHub UI
2. Commit + push a `main`
3. Render detecta el push automaticamente
4. Re-deploys en ~3-5 min
5. Service queda con la nueva version

## Rotar el token de Bsale

Recomendable cada 90 dias:

1. Bsale Admin → Configuracion → Integraciones → API
2. Generar nuevo token
3. Render Dashboard → bsale-mcp-myscrubs → Environment
4. Editar `BSALE_ACCESS_TOKEN` con el nuevo valor
5. Click **Save changes** → Render hace redeploy automatico
6. Eliminar token viejo en Bsale Admin
