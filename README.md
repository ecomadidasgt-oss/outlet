# Sincronización de Outlet

Preparación de una integración de servidor para mantener una colección de VTEX según los descuentos publicados. No se ha conectado a la cuenta, ejecutado ni programado.

## Repositorio independiente y GitHub Actions

Copiar el contenido de esta carpeta a la raíz de `ecomadidasgt-oss/outlet`, incluyendo `.github/workflows/outlet.yml`, `.gitignore`, `sync.py` y este README. No subir el tema VTEX, los datos de `reports/` ni credenciales.

En **Settings → Secrets and variables → Actions** configurar:

| Tipo | Nombre | Valor |
| --- | --- | --- |
| Secret | `VTEX_APP_KEY` | Clave de la integración |
| Secret | `VTEX_APP_TOKEN` | Token nuevo |
| Variable | `OUTLET_GROUP_ID` | Grupo manual confirmado de la colección 138 |
| Variable | `VTEX_SALES_CHANNEL` | Canal comercial confirmado de la tienda |
| Variable | `OUTLET_SCHEDULE_ENABLED` | `false` inicialmente |
| Variable | `OUTLET_APPLY_CHANGES` | `false` inicialmente |

En **Actions → Sincronizar Outlet VTEX → Run workflow**, dejar **apply_changes** desactivado para obtener el primer informe. Este proceso consulta el catálogo real y consume minutos de Actions; no es una prueba automática de código. El informe se descarga desde los artefactos de esa ejecución y se conserva siete días. Revisarlo antes de ejecutar con aplicación activada.

La programación queda desactivada mientras `OUTLET_SCHEDULE_ENABLED` no sea exactamente `true`. Al activarla, se ejecutará una vez al día a las 12:17 UTC (06:17 de Guatemala), con posibles retrasos propios de GitHub. Para aplicar cambios en ejecuciones programadas también hay que establecer `OUTLET_APPLY_CHANGES=true`. No hay disparadores por push ni pull request.

El trabajo usa Ubuntu, tiene un límite de 45 minutos y evita ejecuciones simultáneas dentro de este workflow. Empezar con un informe para medir la duración. Revisar el presupuesto de Actions y el almacenamiento compartido de la cuenta para mantenerse dentro de las cuotas gratuitas; el archivo no configura ni garantiza el presupuesto de GitHub.

### Revisión paralela y recuperación de avance

- Hasta cuatro consultas de precios simultáneas, con un límite global aproximado de 6.7 inicios de petición por segundo y pausa compartida ante respuestas 429/5xx.
- Se guardan `checkpoint.json` y `progress.json` al comenzar y cada 25 productos. El informe final `plan-*.json` solo aparece al completar el catálogo.
- GitHub intenta conservar el checkpoint en caché y sube los informes disponibles como artefactos incluso si el paso falla. Una cancelación forzada de todo el runner puede impedir esos pasos; no es almacenamiento garantizado.
- Una ejecución nueva de informe puede retomar las observaciones guardadas si tienen menos de dos horas y coinciden la cuenta, colección, grupo, canal y todos los IDs de producto/SKU. El log indica cuántos productos se recuperaron. No se reutilizan puntos de avance de la versión anterior que no guardaba archivos.
- El modo de aplicación ignora los resultados en caché y vuelve a consultar los precios. Los informes recuperados pueden contener precios observados hasta dos horas antes; se muestran con la fecha de inicio del escaneo. No se aplican directamente desde un archivo de informe.
- `progress.json` y `checkpoint.json` son archivos parciales de diagnóstico, no autorizaciones para dar de baja productos. El programa no modifica asociaciones antes de completar el escaneo y releer la colección.
- No se ha medido todavía la duración de esta versión contra la cuenta real. Las mejoras buscan evitar repetir los 45 minutos del escaneo secuencial de 10,376 productos.

La regla de protección de bajas superiores al 25% sigue activa. Una aplicación puede quedar parcial si falla o alcanza su límite de tiempo: revisar el informe y recalcular antes de continuar.

## Regla preparada

- Un producto califica si al menos una de sus variantes del vendedor propio (`sellerId=1`) tiene `0 < Price < ListPrice` en el canal indicado.
- Se asocian todos los SKU de ese producto al grupo manual de la colección. No se modifican precios ni inventario.
- Si ningún SKU tiene descuento y todos sus precios pueden evaluarse, se retiran sus asociaciones del grupo.
- Si falta un producto, un SKU o un precio, se conserva su estado anterior y se incluye en el informe como desconocido. Esto también significa que los productos no visibles en el canal no se depuran automáticamente.
- Las promociones de carrito, cupones o precios personalizados no se consideran salvo que estén reflejados en `Price` frente a `ListPrice` del catálogo público.
- La URL de Outlet se conserva si ya apunta a la colección administrada. El ID 138 se toma de la captura histórica; requiere confirmación de la configuración actual.

## Datos que faltan

1. Confirmar colección 138, canal comercial de la web y grupo manual (`SubCollectionId`).
2. Revisar el tipo de la colección. Este programa exige `Type=Manual` y un único grupo inclusivo, sin banderas de preventa/lanzamiento. Las colecciones automáticas o híbridas no se modifican.
3. Crear un token nuevo y asignar permisos de lectura del catálogo y colecciones y de edición de asociaciones de SKU. La herramienta no necesita permisos de precios, órdenes, clientes o pagos.
4. Configurar GitHub Actions como se indica arriba, o elegir otro servidor/equipo para ejecutarlo con regularidad. La programación de GitHub viene desactivada inicialmente.

## Credenciales

Configurar `VTEX_APP_KEY` y `VTEX_APP_TOKEN` como variables de entorno en el equipo del proceso. No deben aparecer en el frontend, Git, argumentos de línea de comandos ni informes. El programa no carga un archivo `.env` automáticamente.

El token compartido en la conversación no se copió a archivos ni se usó en solicitudes. Conviene revocarlo y usar uno nuevo.

## Primera ejecución: solo informe

Requiere Python 3.9 o superior. Reemplazar los identificadores por los confirmados:

```powershell
python integrations/outlet-sync/sync.py --collection 138 --group ID_DEL_GRUPO --channel ID_DEL_CANAL
```

Se recorre el catálogo por el endpoint privado de productos/SKU, evitando el límite de paginación del buscador público global. Después se consultan las ofertas de cada producto en el canal indicado. Los informes quedan en `reports/`, excluido de Git. Un catálogo grande puede requerir un recorrido prolongado; habrá que ajustar la frecuencia según la duración real.

## Aplicar después de revisar el informe

```powershell
python integrations/outlet-sync/sync.py --collection 138 --group ID_DEL_GRUPO --channel ID_DEL_CANAL --apply
```

Cada aplicación calcula un plan nuevo y relee los miembros antes de escribir. Si cambió la colección, se detiene. Las bajas superiores al 25% requieren `--allow-large-removal` tras revisar el informe. El plan registra cada asociación confirmada; si una ejecución se interrumpe, puede haber cambios parciales. Una nueva ejecución calcula las diferencias restantes.

El archivo `sync.lock` evita ejecuciones simultáneas en el mismo directorio. Si el proceso termina abruptamente, revisar que no siga ejecutándose antes de eliminar ese archivo. No ejecutar copias simultáneas desde distintos equipos o carpetas.

Al programarlo, usar un único proceso, recoger el código de salida, conservar los informes y alertar si falla. El cambio de colección puede tardar en verse por la indexación de VTEX. No hay cambios al código de `/outlet` en esta entrega.

## Referencias oficiales consultadas

- [Catalog API: catálogo, grupos y asociaciones de SKU](https://developers.vtex.com/docs/api-reference/catalog-api).
- [Esquema oficial con rutas y formatos de la API](https://github.com/vtex/openapi-schemas/blob/master/VTEX%20-%20Catalog%20API.json).
- [Search API: precios y ofertas de la tienda](https://developers.vtex.com/docs/api-reference/search-api).
