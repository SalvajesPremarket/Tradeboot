# Tradeboot

Servicio independiente de datos de mercado y diagnóstico para Tradeboot. No importa ni reutiliza el motor de TradeScanner.

## Estado actual

- API HTTP de solo lectura conectada a los endpoints REST de datos de mercado de eToro.
- Dashboard que actualiza snapshots puntuales de hasta cinco símbolos cada 30 segundos y evalúa las estrategias activadas como señales simuladas.
- Endpoint de diagnóstico WebSocket que autentica, solicita snapshots y cierra la conexión; todavía no es un consumidor continuo de streaming.
- Indicadores de antigüedad, estado de mercado y señales de datos retrasados cuando eToro los proporciona.
- El escaneo `/engine/demo-scan` es de solo lectura: exige el token administrativo, evalúa hasta cinco símbolos con cotizaciones válidas y no infiere LONG/SHORT ni envía órdenes.
- `demo_scan_worker.py` permite ciclos continuos de escaneo, sin órdenes, cada 30–3600 segundos. Configura `TRADEBOOT_SCAN_SYMBOLS` con 1–5 símbolos y `TRADEBOOT_SCAN_INTERVAL_SECONDS` (por defecto 60). Cada ciclo se guarda en PostgreSQL y se conserva un máximo de 2.000 ciclos; `GET /engine/demo-scan-history?limit=50` permite consultarlos con el token administrativo. En Render, debe configurarse como Background Worker con comando `python demo_scan_worker.py`, además de las variables `DATABASE_URL`, `ETORO_API_KEY` y `ETORO_USER_KEY` existentes.
- El worker es código publicado pero no queda activo en Render automáticamente; necesita crear/configurar el servicio Background Worker y sus variables de entorno.
- La ejecución Demo de una sola evaluación sigue separada y protegida por límites; no se habilita trading real ni un bucle autónomo de órdenes.
- Las estrategias se guardan localmente en el navegador salvo que se configure el almacenamiento compartido con `TRADEBOOT_STRATEGY_ADMIN_TOKEN` y la base de datos correspondiente.
- La calidad de cotización no se puede confirmar completamente cuando el proveedor omite metadatos.

## Despliegue

- Servicio: `tradeboot-api` en Render.
- URL: https://tradeboot-api.onrender.com
- Rama: `main`; el despliegue automático está activado en modo `On Commit`.
- Los cambios enviados a `main` inician un despliegue automático en Render.

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
- `POST /engine/demo-scan` — escaneo autenticado de hasta cinco símbolos con la única estrategia de fórmula activada; solo informa señales y nunca envía órdenes.
- `demo_scan_worker.py` — proceso continuo de escaneo de solo lectura para ejecutarse como Render Background Worker; usa `TRADEBOOT_SCAN_SYMBOLS` y `TRADEBOOT_SCAN_INTERVAL_SECONDS`.
- `GET /engine/demo-scan-history?limit=50` — historial persistido de ciclos de escaneo, protegido por `TRADEBOOT_STRATEGY_ADMIN_TOKEN`; acepta límites de 1 a 200.
- `GET /engine/demo-reconcile` — compara posiciones abiertas Demo con entradas recientes del ledger por instrumento, lado y monto; muestra coincidencias candidatas, ambigüedades y casos para revisión, sin confirmar ejecuciones automáticamente.
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
