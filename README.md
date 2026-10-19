# Tradeboot

Servicio independiente para desarrollar y validar Tradeboot.

## Estado actual

- API HTTP de solo lectura; no conecta con eToro ni con datos de mercado.
- No envía órdenes reales ni tiene ejecución de trading habilitada.
- La lista de seguimiento es local y en memoria; no persiste entre reinicios.
- No importa ni reutiliza componentes de TradeScanner.

## Ejecutar localmente

Requiere Python 3.10 o posterior. No necesita dependencias externas.

```bash
python tradeboot.py
python app.py
```

La API escucha en `http://localhost:10000` por defecto; puede cambiarse con `PORT`.

## Endpoints

- `GET /health` — comprobación de salud.
- `GET /` — resumen del servicio.
- `GET /status` — estado seguro del módulo.
- `GET /watchlist` — instantánea de la lista de seguimiento.

Estos endpoints son de solo lectura. La integración oficial con eToro y cualquier operación de trading siguen deshabilitadas hasta diseñarse, verificarse y autorizarse explícitamente.
