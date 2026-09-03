# Pricing Cockpit

Front-end del cockpit de pricing (Hygie31 / Ecoceutics): un panel web que lee de
**ClickHouse** y muestra la analítica de pricing, evolución de precios, inventario,
y una pestaña **Explorar** que embebe dashboards de **Metabase**.

- `index.html` — la SPA (worklist de productos, escala de precio, evolución, inventario,
  y el selector de dashboards de Metabase por persona). Tema claro/oscuro con botón.
- `server.py` — servidor Python (stdlib `http.server`) que sirve la página y expone
  `/api/products`, `/api/trends`, `/api/inventory` (leen ClickHouse) y `/api/mb/dashboards`
  (lista dashboards de Metabase y arma el menú "Explorar").

## Requisitos
- **ClickHouse** con la base `price_monitor` (tablas `pricing_cockpit`, `pricing_history`,
  `inventory`, …). Ver el repo `price_monitor` para el docker-compose y el esquema.
- **Metabase** sobre ClickHouse (para la pestaña Explorar).

## Ejecutar
```bash
python server.py
# -> http://localhost:8899
```

## Configuración (variables de entorno, con defaults de demo)
| Variable | Default | Qué es |
|---|---|---|
| `CH_URL` | `http://127.0.0.1:8123` | HTTP de ClickHouse |
| `CH_USER` / `CH_PASS` | `price_monitor` / `changeme_pm` | credenciales ClickHouse |
| `MB_URL` | `http://127.0.0.1:3000` | Metabase (lado servidor) |
| `MB_BROWSER` | `http://localhost:3000` | Metabase (URL para el iframe del navegador) |
| `MB_USER` / `MB_PASS` | admin de demo | login de Metabase para listar dashboards |

> En producción, define estas variables en el entorno y **no** uses los defaults de demo.
