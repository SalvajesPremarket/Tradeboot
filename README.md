# Tradeboot

Servicio independiente de datos de mercado y diagnóstico para Tradeboot. No importa ni reutiliza el motor de TradeScanner.

## Estado actual

- API HTTP de solo lectura conectada a los endpoints REST de datos de mercado de eToro.
- Dashboard que actualiza snapshots puntuales de hasta cinco símbolos cada 30 segundos, evalúa fórmulas y muestra el historial persistente de paper trading.
- Endpoint de diagnóstico WebSocket que autentica, solicita snapshots y cierra la conexión; todavía no es un consumidor continuo de streaming.
- Indicadores de antigüedad, estado de mercado y señales de datos retrasados cuando eToro los proporciona.
- No envía órdenes reales ni ejecuta operaciones. El trading automático permanece deshabilitado.
- Las estrategias se guardan localmente en el navegador salvo que se configure el almacenamiento compartido con `TRADEBOOT_STRATEGY_ADMIN_TOKEN` y la base de datos correspondiente.
- La calidad de cotización no se puede confirmar completamente cuando el proveedor omite metadatos.

## Despliegue

- Servicio: `tradeboot-api` en Render.
- URL: https://tradeboot-api.onrender.com
- Rama: `main`; el despliegue automático está activado en modo `On Commit`.
- Los cambios enviados a `main` inician un despliegue automático en Render.

## Schwab (preparación de integración de solo lectura)

- `SCHWAB_CLIENT_ID` / `SCHWAB_CLIENT_SECRET` (también acepta `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET`), `SCHWAB_REDIRECT_URI` y `SCHWAB_TOKEN_ENCRYPTION_KEY` deben configurarse en Render.
- `GET /schwab/authorize` inicia OAuth y valida la configuración antes de redirigir al portal de Schwab.
- `GET /schwab/callback` valida `state`, intercambia el código y guarda tokens cifrados en PostgreSQL.
- `GET /schwab/status` informa el estado sin mostrar secretos.
- `GET /schwab/quotes?symbols=AAPL,MSFT` consulta cotizaciones de Market Data API; no envía órdenes.
- El motor paper consulta historial Schwab de velas de 1 minuto y velas diarias completas para los cuatro motores revisados. Solo procesa velas de minuto cerradas y nunca inventa datos ausentes.
- Solo se permite activar una de las cuatro estrategias Python a la vez. Las señales, stops, entradas, salidas y P/L se guardan en PostgreSQL y se muestran en el panel Paper trading.
- La clave de cifrado `SCHWAB_TOKEN_ENCRYPTION_KEY` debe ser una clave Fernet válida. El token cifrado persistido permite guardar las rotaciones del refresh token sin depender de editar variables en cada renovación.
- La aplicación debe estar aprobada por Schwab y tener permisos de Market Data antes de que OAuth y las cotizaciones puedan funcionar.

## Endpoints

- `GET /health` — comprobación de salud.
- `GET /` — dashboard.
- `GET /status` y `GET /watchlist` — estado local de solo lectura.
- `GET /etoro/watchlist` — watchlist de eToro.
- `GET /etoro/instrument-search?symbol=AAPL` — búsqueda de instrumento.
- `GET /etoro/instrument-rates?symbol=AAPL` — snapshot REST de una cotización.
- `GET /etoro/instrument-rates?symbols=AAPL,MSFT,NVDA` — snapshot REST de hasta cinco símbolos.
- `GET /etoro/instrument-stream?symbols=AAPL,MSFT,NVDA` — diagnóstico WebSocket de snapshots puntuales.
- `GET /etoro/demo/portfolio` — consulta de cartera demo.
- `POST /strategies/validate` — validar o evaluar una fórmula con los valores recibidos.
- `POST /strategies/evaluate-market` — evaluar una fórmula con una cotización eToro que cumpla los controles de calidad.
- `POST /strategies/validate-program` — validar la sintaxis de un programa Python completo sin ejecutarlo.
- `POST /strategies/simulate-momentum` — probar el motor integrado Momentum Tramos con velas/ticks sintéticos o históricos; requiere `X-Strategy-Admin-Token`, procesa como máximo 5.000 velas y 5.000 ticks, y solo emite señales simuladas.

## Campos de calidad

Las respuestas de tasas incluyen `quote_time`, `quote_age_seconds`, `quote_is_fresh`, `market_open`, `delayed_quote`, `quote_is_usable` y `quote_quality_status`. Las respuestas multi-símbolo también incluyen `all_quotes_fresh` y `all_quotes_usable`.

`quote_is_usable` es una evaluación conservadora del snapshot, no una garantía para trading. No activa órdenes ni sustituye validaciones de riesgo. Cuando el proveedor no informa si una cotización está retrasada o si el mercado está abierto, la respuesta indica que la calidad no está confirmada.

## Configuración

Render requiere `ETORO_API_KEY` y `ETORO_USER_KEY` como variables de entorno secretas. No incluirlas en código, commits ni registros. `ETORO_MAX_QUOTE_AGE_SECONDS` permite configurar el umbral de antigüedad; por defecto son 300 segundos.

Para sincronizar estrategias y capital entre dispositivos se necesita configurar `TRADEBOOT_STRATEGY_ADMIN_TOKEN` y la base de datos usada por `strategy_store.py`. Sin ese modo, las estrategias del navegador no se comparten entre dispositivos.

## Ejecutar localmente

Requiere Python 3.10 o posterior.

```bash
pip install -r requirements.txt
python app.py
```

La API escucha en `http://localhost:10000` por defecto; puede cambiarse con `PORT`.

## Motor Momentum Tramos en la rama de trabajo

- `momentum_tramos.py` contiene la implementación revisada del motor; `strategy_simulation.py` ofrece un adaptador de simulación acotado.
- El dashboard muestra **Probar motor (simulación)** para una estrategia Python cuyo nombre incluya `Momentum Tramos`. La prueba utiliza datos sintéticos y requiere almacenamiento compartido conectado.
- Los programas Python guardados no se ejecutan como código arbitrario. La simulación invoca únicamente el motor integrado revisado.
- La rama de trabajo aún debe pasar revisión antes de incorporarse a `main`; no se ha desplegado esta integración en Render.

## Pendiente antes de considerar listo para producción

1. Ejecutar pruebas automatizadas y comprobar los endpoints desplegados.
2. Validar entradas, salidas, stops y filtros con más escenarios LONG/SHORT y datos faltantes.
3. Validar snapshots durante mercado abierto y cerrado, y verificar cómo reporta eToro cotizaciones retrasadas.
4. Confirmar que las variables de entorno de eToro están configuradas y que las cotizaciones reales llegan correctamente.
5. Confirmar si se necesita persistencia compartida y configurar/verificar el token y la base de datos.
6. Implementar un consumidor WebSocket continuo si se requiere actualización en tiempo real.
7. Definir persistencia, monitoreo, límites de riesgo y requisitos de operación antes de diseñar cualquier ejecución de trading.

**Seguridad:** Tradeboot permanece en modo de solo lectura. No habilitar trading automático a partir de las pruebas de datos de mercado.
