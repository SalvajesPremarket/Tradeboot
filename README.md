# Tradeboot

Tradeboot será un módulo independiente para preparar y administrar operaciones largas o cortas. No importa ni reutiliza el motor de TradeScanner.

## Estado de la integración con eToro

Esta rama añade un cliente API **de solo lectura** y una comprobación de conectividad. No envía, modifica ni cierra órdenes. La ejecución de operaciones reales no está implementada.

### Credenciales necesarias

1. Inicia sesión en eToro y abre **Settings → Trading → API Key Management**.
2. Crea una clave para el entorno **Demo** y selecciona permisos de lectura. Para el futuro, la ejecución de órdenes necesitará permisos de escritura y controles adicionales; no los habilites para esta comprobación.
3. Guarda la clave pública como `ETORO_API_KEY` y la clave de usuario como `ETORO_USER_KEY` en las variables de entorno del entorno de prueba.
4. No pegues credenciales en el chat, no las incluyas en Git y no las guardes en archivos versionados. `.env.example` solo documenta los nombres, no contiene claves reales.

Cada solicitud añade `x-request-id` único y envía las dos claves en los encabezados requeridos por eToro. Los errores no incluyen encabezados, claves ni cuerpos de respuesta.

## Verificación local

Requiere Python 3.10 o posterior. No se necesita una dependencia externa para el cliente API.

En un entorno local de prueba, configura las variables de entorno y ejecuta:

```bash
python -m unittest -v
python check_etoro.py
```

El comprobador consulta la lista predeterminada y el portafolio Demo; no imprime posiciones ni datos de cuenta. Un resultado correcto confirma que las credenciales permiten esas consultas, no que el sistema esté listo para operar automáticamente.

## Componentes

- `tradeboot.py`: estructura inicial aislada de Tradeboot.
- `etoro_client.py`: consultas de solo lectura a la API de eToro.
- `check_etoro.py`: comprobación segura de conexión.
- `test_etoro_client.py`: pruebas con respuestas simuladas; no contactan a eToro ni envían órdenes.
- `.env.example`: nombres de variables sin credenciales.

## Pendiente antes de considerar Tradeboot listo

- Ejecutar las pruebas en CI y corregir cualquier fallo.
- Configurar las claves Demo en un entorno seguro y verificar una respuesta real de eToro.
- Definir estrategia, tamaño máximo de posición, pérdida diaria máxima, stop loss y reglas para operaciones largas/cortas.
- Implementar y probar el ciclo de decisión en simulación, registro y controles contra órdenes duplicadas.
- Solo tras validación separada, diseñar la ejecución de órdenes y un interruptor explícito entre Demo y Real.

**Seguridad:** nunca activar ejecución real ni automatización de órdenes por defecto. Una prueba de conexión no es autorización para operar.
