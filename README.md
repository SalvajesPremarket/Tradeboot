# Tradeboot

Tradebot independiente para operar con eToro. Este repositorio es separado de TradeScanner y no importa ni reutiliza su motor.

## Estado de la integración eToro

- Cliente REST oficial mediante HTTPS, con credenciales por variables de entorno.
- Modo predeterminado: `demo`.
- Lectura de watchlist y P&L de la cuenta seleccionada.
- Ejecución de órdenes: **no implementada**; nunca se envían órdenes desde esta versión.
- El modo real permanece bloqueado salvo configuración explícita; no habilitarlo para las pruebas iniciales.

## Requisitos

- Python 3.10 o posterior.
- Cuenta eToro verificada y acceso a API habilitado para las credenciales. Consulta la documentación oficial: https://builders.etoro.com/get-started

## Configuración local

1. Instala dependencias: `python -m pip install -r requirements.txt`.
2. Copia `.env.example` a `.env` en tu entorno local y completa `ETORO_API_KEY` y `ETORO_USER_KEY`. No subas el archivo `.env` a GitHub.
3. Mantén `ETORO_MODE=demo` y `ETORO_LIVE_TRADING_ENABLED=false`.
4. Ejecuta pruebas: `python -m unittest discover -s tests -v`.

La librería lee variables de entorno, pero no carga automáticamente el archivo `.env`; para desarrollo, exporta esas variables en tu terminal o usa un cargador local que no se incluya en producción.

## Uso de solo lectura

```python
from etoro_client import EtoroClient

client = EtoroClient()
print(client.status())
watchlist = client.get_default_watchlist()
pnl = client.get_account_pnl()
```

Las respuestas de eToro se devuelven al llamador; el cliente no registra cabeceras ni credenciales. Los errores no incluyen cuerpos de respuesta potencialmente sensibles.

## Pendiente antes de considerarlo listo

1. Configurar las dos claves en el entorno de ejecución privado.
2. Verificar llamadas de lectura reales en la API eToro y en modo demo.
3. Añadir interfaz/estado de aplicación y gestión de errores operativa.
4. Diseñar y probar la ejecución de órdenes con controles de riesgo, primero en demo, antes de plantear cualquier uso real.

**No se debe considerar listo para operar con dinero real todavía.**
