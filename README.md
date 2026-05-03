# Seguimiento de precios e inventario con web scraping

Este proyecto implementa un sistema de seguimiento para productos de "milfshakes.es" (https://milfshakes.es/) usando scraping con Python. Guarda un historial de capturas en SQLite, detecta bajadas de precio y cambios de stock, y genera gráficos de evolución temporal.

## Requisitos

- Dependencias expuestas en `requirements.txt`

## Instalación

```powershell
pip install -r requirements.txt
```

## Uso

### 1. Captura manual de uno o varios productos

```powershell
python trabajo.py scrape --url https://milfshakes.es/products/san-jordi --url https://milfshakes.es/products/friends-tee
```

### 2. Captura desde archivo

Guarda una URL por línea en un archivo, por ejemplo `sample_urls.txt`, y ejecuta:

```powershell
python trabajo.py scrape --urls-file sample_urls.txt
```

### 2.1. Analizar todos los productos de la web

```powershell
python trabajo.py scrape --all
```

Este modo lee el sitemap de Shopify y recorre todos los productos publicados.

Además, al terminar cada ejecución de `scrape --all`, se genera un único reporte-resumen en la carpeta `reports` con los datos capturados en esa corrida.


### 3. Captura periódica

```powershell
python trabajo.py scrape --urls-file sample_urls.txt --repeat 3 --interval 3600
```

### 4. Generar reportes

Los gráficos se guardan en la carpeta `reports`:

```powershell
python trabajo.py report
```

Para filtrar por un producto concreto:

```powershell
python trabajo.py report --product "THE SILVER ROSE"
```

### 5. Listar productos guardados

```powershell
python trabajo.py list
```

## Funcionalidades

- Scraping con `requests` + `beautifulsoup4` y fallback al endpoint JSON de Shopify
- Entrada de URLs por consola, archivo o argumentos de línea de comandos
- Extracción de nombre, precio actual, disponibilidad y fecha de actualización
- Historial completo en SQLite
- Actualización manual o periódica
- Gráficos de evolución de precios con `matplotlib`
- Detección de cambios de precio y stock
- Validación de URLs y manejo de errores de red o de estructura HTML

## URLs de ejemplo

Consulta `sample_urls.txt` para varias URLs de producto ya preparadas.

## Notas

- El script intenta primero el endpoint Shopify `.js`, que suele ser más estable que depender solo del HTML visible.
- Si se lanza sobre una URL de colección o portada, intenta descubrir enlaces de producto dentro de la página.
- Conviene revisar periódicamente que el sitio no cambie su estructura o sus condiciones de uso.
