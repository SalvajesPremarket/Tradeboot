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
- Se pueden activar varias estrategias Python compatibles con motores revisados; cada una se evalúa de forma independiente en cada ciclo. Las señales, stops, entradas, salidas y P/L se guardan en PostgreSQL y se muestran en el panel Paper trading.
- La clave de cifrado `SCHWAB_TOKEN_ENCRYPTION_KEY` debe ser una clave Fernet válida. El token cifrado persistido permite guardar las rotaciones del refresh token sin depender de editar variables en cada renovación.
- La aplicación debe estar aprobada por Schwab y tener permisos de Market Data antes de que OAuth y las cotizaciones puedan funcionar.

## Schwab + Twelve Data (escáner global en paper trading)

- Schwab sigue siendo el proveedor preferido de cotizaciones e históricos para acciones de EE. UU.; las órdenes de Schwab no se usan.
- `TWELVE_DATA_API_KEY` configura la API de Twelve Data. `TWELVE_DATA_MARKET_COUNTRIES` es opcional; si no se define, Tradeboot obtiene el catálogo de bolsas/países de Twelve Data y rota las consultas de ganadores entre ellos.
- La ruta `/market_movers/stocks` consume 100 créditos por solicitud y requiere el plan Pro (individual) o Venture (empresa), según la documentación del proveedor. Las solicitudes se rotan por país y se cachean para respetar los límites por minuto.
- Twelve Data aporta candidatos globales, datos de precios para mercados internacionales y comunicados oficiales. Para acciones de EE. UU. se intenta Schwab primero y Twelve Data sirve de respaldo cuando está configurado.
- La cobertura efectiva depende de los mercados habilitados en la cuenta de Twelve Data. La rotación descubre países del catálogo, pero no puede superar las restricciones del plan contratado.
- El contexto de *float* solo se acepta si el proveedor devuelve un campo explícito de float; las acciones en circulación no se confunden con float. Las reglas obligatorias de JeanLong no se desactivan si falta float o una noticia fresca, por lo que el motor rechaza la entrada en esos casos.
- No incluir la API key en el repositorio ni en los logs; añadirla como secreto de entorno en Render.

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
- El dashboard muestra **Probar motor (simulación)** para la estrategia Python compatible con Momentum Tramos. La prueba utiliza datos sintéticos y requiere almacenamiento compartido conectado.
- Los programas Python guardados no se ejecutan como código arbitrario. La activación identifica el motor revisado por equivalencia de sintaxis (ignora comentarios, espacios y docstrings); cambiar la lógica requiere revisar e integrar un motor específico.
- La integración de motores revisados está en `main`; Render despliega automáticamente los cambios enviados a esta rama.

## Pendiente antes de considerar listo para producción

1. Ejecutar pruebas automatizadas y comprobar los endpoints desplegados.
2. Validar entradas, salidas, stops y filtros con más escenarios LONG/SHORT y datos faltantes.
3. Validar snapshots durante mercado abierto y cerrado, y verificar cómo reporta eToro cotizaciones retrasadas.
4. Confirmar que las variables de entorno de eToro están configuradas y que las cotizaciones reales llegan correctamente.
5. Confirmar si se necesita persistencia compartida y configurar/verificar el token y la base de datos.
6. Implementar un consumidor WebSocket continuo si se requiere actualización en tiempo real.
7. Definir persistencia, monitoreo, límites de riesgo y requisitos de operación antes de diseñar cualquier ejecución de trading.

**Seguridad:** Tradeboot permanece en modo de solo lectura. No habilitar trading automático a partir de las pruebas de datos de mercado.

## Acceso por cuenta y editor de estrategias

- La autenticación usa cuentas almacenadas en PostgreSQL y contraseñas protegidas con PBKDF2-SHA256; las sesiones usan cookies `HttpOnly; Secure; SameSite=Lax` con caducidad de siete días.
- El administrador inicial se crea al iniciar sesión cuando están configuradas las variables `TRADEBOOT_ADMIN_USERNAME` y `TRADEBOOT_ADMIN_PASSWORD`. Si no se definen, el sistema usa el nombre `admin` y el valor existente de `TRADEBOOT_STRATEGY_ADMIN_TOKEN` como contraseña de arranque, para facilitar la transición desde el token privado existente. La contraseña de arranque debe tener al menos 12 caracteres.
- Después de iniciar sesión, el administrador puede crear usuarios y habilitar o revocar el acceso al editor de estrategias. Los usuarios ordinarios no obtienen permisos de administración.
- El token privado nunca se entrega al navegador como sesión; las llamadas de sincronización usan la cookie de sesión.
- Cada cuenta puede cambiar su contraseña. La contraseña inicial de cada usuario debe tener al menos 12 caracteres.
- Endpoints: `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST /auth/password`, `GET /auth/users`, `POST /auth/users` y `PUT /auth/users/editor`.
- Configurar `DATABASE_URL` en Render sigue siendo necesario para las cuentas y la sincronización compartida.
