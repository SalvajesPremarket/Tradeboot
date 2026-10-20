# Tradeboot

Servicio independiente de datos de mercado y diagnóstico para Tradeboot. No importa ni reutiliza el motor de TradeScanner.

## Estado actual

- API HTTP de solo lectura conectada a los endpoints REST de datos de mercado de eToro.
- Endpoint de diagnóstico WebSocket que autentica, solicita snapshots y cierra la conexión; todavía no es un consumidor continuo de streaming.
- Indicadores de antigüedad, estado de mercado y señales de datos retrasados cuando eToro los proporciona.
- No envía órdenes reales ni ejecuta operaciones. El trading automático permanece deshabilitado.
- Las listas locales del módulo base están en memoria y no persisten entre reinicios.
- La calidad de cotización no se puede confirmar completamente cuando el proveedor omite metadatos.

## Despliegue

- Servicio: `tradeboot-api` en Render.
- URL: https://tradeboot-api.onrender.com
- Rama: `main`; el despliegue automático está deshabilitado, así que los cambios requieren un despliegue manual.

## Endpoints

- `GET /health` — comprobación de salud.
- `GET /` — resumen del servicio.
- `GET /status` y `GET /watchlist` — estado local de solo lectura.
- `GET /etoro/watchlist` — watchlist de eToro.
- `GET /etoro/instrument-search?symbol=AAPL` — búsqueda de instrumento.
- `GET /etoro/instrument-rates?symbol=AAPL` — snapshot REST de una cotización.
- `GET /etoro/instrument-rates?symbols=AAPL,MSFT,NVDA` — snapshot REST de hasta cinco símbolos.
- `GET /etoro/instrument-stream?symbols=AAPL,MSFT,NVDA` — diagnóstico WebSocket de snapshots puntuales.
- `GET /etoro/demo/portfolio` — consulta de cartera demo.

## Campos de calidad

Las respuestas de tasas incluyen `quote_time`, `quote_age_seconds`, `quote_is_fresh`, `market_open`, `delayed_quote`, `quote_is_usable` y `quote_quality_status`. Las respuestas multi-símbolo también incluyen `all_quotes_fresh` y `all_quotes_usable`.

`quote_is_usable` es una evaluación conservadora del snapshot, no una garantía para trading. No activa órdenes ni sustituye validaciones de riesgo. Cuando el proveedor no informa si una cotización está retrasada o si el mercado está abierto, la respuesta indica que la calidad no está confirmada.

## Configuración

Render requiere `ETORO_API_KEY` y `ETORO_USER_KEY` como variables de entorno secretas. No incluirlas en código, commits ni registros. `ETORO_MAX_QUOTE_AGE_SECONDS` permite configurar el umbral de antigüedad; por defecto son 300 segundos.

## Ejecutar localmente

Requiere Python 3.10 o posterior.

```bash
pip install -r requirements.txt
python app.py
```

La API escucha en `http://localhost:10000` por defecto; puede cambiarse con `PORT`.

## Pendiente antes de considerar listo para producción

1. Ejecutar pruebas automatizadas y comprobar los endpoints desplegados.
2. Validar snapshots durante mercado abierto y cerrado, y verificar cómo reporta eToro cotizaciones retrasadas.
3. Implementar un consumidor WebSocket continuo si se requiere actualización en tiempo real.
4. Definir persistencia, monitoreo, límites de riesgo y requisitos de operación antes de diseñar cualquier ejecución de trading.

**Seguridad:** Tradeboot permanece en modo de solo lectura. No habilitar trading automático a partir de las pruebas de datos de mercado.
