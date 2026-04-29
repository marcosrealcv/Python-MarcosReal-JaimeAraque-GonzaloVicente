from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import matplotlib.pyplot as plt
import requests
from bs4 import BeautifulSoup


DB_PATH = Path("price_tracker.sqlite3")
REPORTS_DIR = Path("reports")
SHOP_URL = "https://milfshakes.es"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
DEFAULT_TIMEOUT = 20
LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)


@dataclass
class ProductSnapshot:
    url: str
    handle: str
    name: str
    price_cents: int
    currency: str
    available: bool
    source_url: str


# Devuelve la fecha y hora actual en UTC, en formato ISO, para guardar
# marcas temporales consistentes en base de datos y reportes.
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Valida que la cadena recibida sea una URL HTTP/HTTPS con dominio.
# Si no cumple, lanza un error claro para cortar entradas invalidas.
def normalize_url(raw_url: str) -> str:
    candidate = raw_url.strip()
    if not candidate:
        raise ValueError("URL vacía")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"URL inválida: {raw_url}")
    return candidate


# Convierte cualquier URL de producto a una ruta canonica sin prefijos
# regionales, para tratar como el mismo producto las variantes por idioma/pais.
def canonicalize_product_url(raw_url: str) -> str:
    normalized_url = normalize_url(raw_url)
    parsed = urlparse(normalized_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if "products" in path_parts:
        products_index = path_parts.index("products")
        canonical_path = "/" + "/".join(path_parts[products_index:])
    elif len(path_parts) >= 3 and LOCALE_SEGMENT_RE.match(path_parts[0]) and path_parts[1] == "products":
        canonical_path = "/" + "/".join(path_parts[1:])
    else:
        canonical_path = parsed.path
    return f"{parsed.scheme}://{parsed.netloc}{canonical_path}"


# Interpreta precios en distintos formatos y los normaliza a centimos.
# Soporta numeros directos y textos con separadores de miles/decimales.
def money_to_cents(value: object) -> int:
    if value is None:
        raise ValueError("No se encontró precio")
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    text = str(value).strip().replace("€", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return int((Decimal(text) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"No se pudo interpretar el precio: {value}") from exc


# Formatea un entero en centimos como precio en euros para mostrar en consola.
def cents_to_euros(cents: int) -> str:
    return f"€{cents / 100:.2f}"


# Crea una sesion HTTP reutilizable con cabeceras de navegador.
# Esto mejora estabilidad en scraping y evita repetir configuracion.
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


# Descarga una pagina HTML y verifica que la respuesta del servidor sea correcta.
def fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.text


# Descarga contenido XML (por ejemplo sitemaps) con las mismas validaciones HTTP.
def fetch_xml(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.text


# Recorre el sitemap principal y los sub-sitemaps de productos para sacar
# un listado unico de URLs de producto ya canonicadas.
def extract_product_urls_from_sitemap(session: requests.Session) -> list[str]:
    sitemap_xml = fetch_xml(session, f"{SHOP_URL}/sitemap.xml")
    sitemap_root = ET.fromstring(sitemap_xml)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    product_sitemaps: list[str] = []
    for sitemap in sitemap_root.findall("sm:sitemap", namespace):
        loc = sitemap.findtext("sm:loc", default="", namespaces=namespace).strip()
        if "/sitemap_products_" in loc:
            product_sitemaps.append(loc)

    product_urls: list[str] = []
    seen = set()
    for sitemap_url in product_sitemaps:
        product_xml = fetch_xml(session, sitemap_url)
        product_root = ET.fromstring(product_xml)
        for url_node in product_root.findall("sm:url", namespace):
            loc = url_node.findtext("sm:loc", default="", namespaces=namespace).strip()
            if "/products/" not in loc:
                continue
            normalized = canonicalize_product_url(loc)
            if normalized not in seen:
                product_urls.append(normalized)
                seen.add(normalized)

    if not product_urls:
        raise ValueError("No se encontraron productos en el sitemap")
    return product_urls


# Extrae URLs de producto desde una pagina dada. Si ya es una URL de producto,
# la devuelve directamente; si no, parsea enlaces del HTML y deduplica.
def extract_product_urls(page_url: str, session: requests.Session) -> list[str]:
    if "/products/" in urlparse(page_url).path:
        return [canonicalize_product_url(page_url)]

    html = fetch_html(session, page_url)
    soup = BeautifulSoup(html, "lxml")
    discovered: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/products/" not in href:
            continue
        absolute_url = urljoin(page_url, href)
        parsed = urlparse(absolute_url)
        if parsed.netloc and parsed.netloc.endswith("milfshakes.es"):
            discovered.append(canonicalize_product_url(absolute_url))

    deduplicated: list[str] = []
    seen = set()
    for candidate in discovered:
        if candidate not in seen:
            deduplicated.append(candidate)
            seen.add(candidate)

    if not deduplicated:
        raise ValueError(f"No se encontraron URLs de producto en {page_url}")
    return deduplicated


# Convierte el JSON de Shopify en un snapshot interno con los campos que usa
# el sistema para guardar historico, alertas y salida por consola.
def parse_product_json(payload: dict, source_url: str) -> ProductSnapshot:
    variants = payload.get("variants") or []
    first_variant = variants[0] if variants else {}
    available = bool(payload.get("available"))
    if first_variant:
        available = bool(first_variant.get("available", available))

    price_cents = int(payload.get("price") or first_variant.get("price") or 0)
    currency = payload.get("currency") or "EUR"
    handle = payload.get("handle") or urlparse(source_url).path.rstrip("/").split("/")[-1]
    title = payload.get("title") or handle

    return ProductSnapshot(
        url=canonicalize_product_url(source_url),
        handle=handle,
        name=title.strip(),
        price_cents=price_cents,
        currency=currency,
        available=available,
        source_url=source_url,
    )


# Intenta obtener datos del producto via endpoint .js de Shopify (fuente principal)
# y, si falla, aplica fallback al HTML usando bloques JSON-LD.
def scrape_product(session: requests.Session, product_url: str) -> ProductSnapshot:
    normalized_url = normalize_url(product_url)
    parsed = urlparse(normalized_url)
    if "/products/" not in parsed.path:
        raise ValueError(f"La URL no parece ser de producto: {product_url}")

    product_json_url = normalized_url.split("?")[0].rstrip("/") + ".js"
    try:
        response = session.get(product_json_url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        return parse_product_json(payload, normalized_url)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        html = fetch_html(session, normalized_url)
        soup = BeautifulSoup(html, "lxml")
        ld_json_blocks = soup.find_all("script", attrs={"type": "application/ld+json"})
        for block in ld_json_blocks:
            try:
                payload = json.loads(block.get_text(strip=True))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("@type") == "Product":
                offers = payload.get("offers") or {}
                price_cents = money_to_cents(offers.get("price"))
                available = str(offers.get("availability", "")).lower().endswith("instock")
                title = payload.get("name") or urlparse(normalized_url).path.rstrip("/").split("/")[-1]
                return ProductSnapshot(
                    url=canonicalize_product_url(normalized_url),
                    handle=urlparse(normalized_url).path.rstrip("/").split("/")[-1],
                    name=title.strip(),
                    price_cents=price_cents,
                    currency=offers.get("priceCurrency") or "EUR",
                    available=available,
                    source_url=normalized_url,
                )
        raise ValueError(f"No se pudo extraer el producto desde {normalized_url}")


        # Crea las tablas necesarias si no existen: productos, capturas y alertas.
        # Es idempotente para que el script pueda arrancar sin pasos manuales.
def init_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            handle TEXT NOT NULL,
            name TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EUR',
            last_price_cents INTEGER NOT NULL,
            last_available INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            available INTEGER NOT NULL,
            currency TEXT NOT NULL,
            source_url TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            message TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
        """
    )
    connection.commit()


# Busca el producto por URL canonica y devuelve su ID.
# Si no existe, lo inserta con su estado inicial y devuelve el nuevo ID.
def get_product_id(connection: sqlite3.Connection, snapshot: ProductSnapshot) -> int:
    row = connection.execute("SELECT id FROM products WHERE url = ?", (snapshot.url,)).fetchone()
    if row:
        return int(row[0])

    cursor = connection.execute(
        """
        INSERT INTO products (url, handle, name, currency, last_price_cents, last_available, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.url,
            snapshot.handle,
            snapshot.name,
            snapshot.currency,
            snapshot.price_cents,
            int(snapshot.available),
            utc_now_iso(),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


# Recupera la ultima captura guardada de un producto concreto.
# Se usa para comparar precio/stock y generar alertas.
def get_previous_capture(connection: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT price_cents, available, captured_at
        FROM captures
        WHERE product_id = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()


# Inserta una alerta de cambio en la tabla de alertas con detalle del valor
# anterior, valor nuevo y mensaje final mostrado al usuario.
def store_alert(
    connection: sqlite3.Connection,
    product_id: int,
    alert_type: str,
    old_value: object,
    new_value: object,
    message: str,
) -> None:
    connection.execute(
        """
        INSERT INTO alerts (product_id, created_at, alert_type, old_value, new_value, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (product_id, utc_now_iso(), alert_type, str(old_value), str(new_value), message),
    )


# Guarda una nueva captura del producto y actualiza su estado actual.
# Tambien detecta cambios respecto a la captura anterior y registra alertas.
def store_capture(connection: sqlite3.Connection, snapshot: ProductSnapshot) -> list[str]:
    product_id = get_product_id(connection, snapshot)
    previous = get_previous_capture(connection, product_id)
    alerts: list[str] = []

    if previous is not None:
        previous_price = int(previous[0])
        previous_available = bool(previous[1])
        if snapshot.price_cents < previous_price:
            message = (
                f"Bajada de precio para {snapshot.name}: "
                f"{cents_to_euros(previous_price)} -> {cents_to_euros(snapshot.price_cents)}"
            )
            store_alert(connection, product_id, "price_drop", previous_price, snapshot.price_cents, message)
            alerts.append(message)
        elif snapshot.price_cents > previous_price:
            message = (
                f"Subida de precio para {snapshot.name}: "
                f"{cents_to_euros(previous_price)} -> {cents_to_euros(snapshot.price_cents)}"
            )
            store_alert(connection, product_id, "price_increase", previous_price, snapshot.price_cents, message)
            alerts.append(message)

        if bool(snapshot.available) != previous_available:
            message = (
                f"Cambio de stock para {snapshot.name}: "
                f"{'disponible' if previous_available else 'agotado'} -> {'disponible' if snapshot.available else 'agotado'}"
            )
            store_alert(connection, product_id, "availability_change", previous_available, snapshot.available, message)
            alerts.append(message)

    connection.execute(
        """
        INSERT INTO captures (product_id, captured_at, price_cents, available, currency, source_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            utc_now_iso(),
            snapshot.price_cents,
            int(snapshot.available),
            snapshot.currency,
            snapshot.source_url,
        ),
    )
    connection.execute(
        """
        UPDATE products
        SET handle = ?, name = ?, currency = ?, last_price_cents = ?, last_available = ?, last_seen_at = ?
        WHERE id = ?
        """,
        (
            snapshot.handle,
            snapshot.name,
            snapshot.currency,
            snapshot.price_cents,
            int(snapshot.available),
            utc_now_iso(),
            product_id,
        ),
    )
    connection.commit()
    return alerts


# Lee un archivo de URLs (una por linea), ignorando vacios y comentarios.
def load_urls_from_file(file_path: Path) -> list[str]:
    urls: list[str] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        urls.append(cleaned)
    return urls


# Reune las URLs de entrada segun el modo elegido: sitemap completo, argumentos,
# archivo, stdin o input manual. Al final, valida y normaliza cada URL.
def gather_input_urls(args: argparse.Namespace) -> list[str]:
    if getattr(args, "all_products", False):
        session = build_session()
        return extract_product_urls_from_sitemap(session)

    collected: list[str] = []
    if args.urls:
        collected.extend(args.urls)
    if args.urls_file:
        collected.extend(load_urls_from_file(Path(args.urls_file)))
    if not collected and not sys.stdin.isatty():
        collected.extend(line.strip() for line in sys.stdin.read().splitlines() if line.strip())
    if not collected:
        raw = input("Introduce una o varias URLs separadas por coma: ").strip()
        collected.extend(part.strip() for part in raw.split(",") if part.strip())
    normalized: list[str] = []
    for raw_url in collected:
        normalized.append(normalize_url(raw_url))
    return normalized


# Elimina URLs duplicadas usando una clave canonica para que el mismo producto
# no se procese varias veces dentro de la misma ejecucion.
def deduplicate_urls(urls: Iterable[str]) -> list[str]:
    deduplicated: list[str] = []
    seen = set()
    for raw_url in urls:
        normalized_url = canonicalize_product_url(raw_url)
        dedupe_key = normalized_url.rstrip("/")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduplicated.append(normalized_url)
    return deduplicated


# Normaliza el nombre del producto para comparaciones robustas:
# minusculas uniformes y espacios compactados.
def normalize_product_name(name: str) -> str:
    return " ".join(name.casefold().split())


# Genera un unico reporte resumen de la corrida de scraping actual,
# mostrando top de precios y distribucion de disponibilidad.
def generate_scrape_run_report(
    connection: sqlite3.Connection,
    run_started_at: str,
    output_dir: Path,
    run_label: str,
) -> None:
    rows = connection.execute(
        """
        SELECT p.name, c.price_cents, c.available, c.captured_at
        FROM captures c
        JOIN products p ON p.id = c.product_id
        WHERE c.captured_at >= ?
        ORDER BY c.captured_at ASC, c.id ASC
        """,
        (run_started_at,),
    ).fetchall()

    if not rows:
        return

    latest_per_product: dict[str, tuple[int, int]] = {}
    for row in rows:
        latest_per_product[row["name"]] = (int(row["price_cents"]), int(row["available"]))

    products_sorted = sorted(latest_per_product.items(), key=lambda item: item[1][0], reverse=True)
    top_products = products_sorted[:20]
    names = [item[0] for item in top_products]
    prices = [item[1][0] / 100 for item in top_products]

    total_products = len(latest_per_product)
    available_count = sum(1 for _, values in latest_per_product.items() if values[1] == 1)
    out_of_stock_count = total_products - available_count
    avg_price = sum(values[0] for values in latest_per_product.values()) / max(1, total_products) / 100

    output_dir.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(1, 2, figsize=(16, 6))

    axes[0].barh(names[::-1], prices[::-1], color="#1f77b4", alpha=0.85)
    axes[0].set_title("Top 20 precios capturados")
    axes[0].set_xlabel("Precio (€)")

    stock_values = [available_count, out_of_stock_count]
    stock_labels = ["Disponible", "Agotado"]
    stock_colors = ["#2ca02c", "#d62728"]
    axes[1].pie(stock_values, labels=stock_labels, autopct="%1.1f%%", colors=stock_colors, startangle=90)
    axes[1].set_title("Estado de stock")

    figure.suptitle(
        f"Resumen captura {run_label} | Productos: {total_products} | Precio medio: €{avg_price:.2f}",
        fontsize=12,
    )
    figure.tight_layout()

    report_path = output_dir / f"scrape_run_{run_label}.png"
    figure.savefig(report_path, dpi=160)
    plt.close(figure)
    print(f"[REPORTE-RUN] {report_path}")


# Flujo principal de scraping: resuelve URLs objetivo, evita duplicados,
# extrae productos, guarda capturas/alertas y aplica politicas de error.
def scrape_urls(
    urls: Iterable[str],
    database_path: Path,
    stop_on_error: bool = False,
    create_run_report: bool = False,
) -> int:
    session = build_session()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    init_db(connection)
    run_started_at = utc_now_iso()
    run_label = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_input_urls = deduplicate_urls(urls)

    total_captures = 0
    seen_products = set()
    seen_names = set()
    for input_url in unique_input_urls:
        try:
            target_urls = extract_product_urls(input_url, session)
        except Exception as exc:
            print(f"[ERROR] {input_url}: {exc}")
            if stop_on_error:
                connection.close()
                raise RuntimeError(f"Ejecución detenida por error: {input_url}: {exc}") from exc
            continue

        for product_url in deduplicate_urls(target_urls):
            product_key = product_url.split("?")[0].rstrip("/")
            if product_key in seen_products:
                continue
            seen_products.add(product_key)
            try:
                snapshot = scrape_product(session, product_url)
                product_name_key = normalize_product_name(snapshot.name)
                if product_name_key in seen_names:
                    continue
                seen_names.add(product_name_key)
                alerts = store_capture(connection, snapshot)
                total_captures += 1
                availability = "disponible" if snapshot.available else "agotado"
                print(
                    f"[OK] {snapshot.name} | {cents_to_euros(snapshot.price_cents)} | "
                    f"{availability} | {snapshot.url}"
                )
                for message in alerts:
                    print(f"  [ALERTA] {message}")
            except requests.RequestException as exc:
                print(f"[ERROR] Conexión fallida en {product_url}: {exc}")
                if stop_on_error:
                    if create_run_report and total_captures > 0:
                        generate_scrape_run_report(connection, run_started_at, REPORTS_DIR, run_label)
                    connection.close()
                    raise RuntimeError(f"Ejecución detenida por error de conexión en {product_url}: {exc}") from exc
            except Exception as exc:
                print(f"[ERROR] No se pudo procesar {product_url}: {exc}")
                if stop_on_error:
                    if create_run_report and total_captures > 0:
                        generate_scrape_run_report(connection, run_started_at, REPORTS_DIR, run_label)
                    connection.close()
                    raise RuntimeError(f"Ejecución detenida por error en {product_url}: {exc}") from exc

    if create_run_report and total_captures > 0:
        generate_scrape_run_report(connection, run_started_at, REPORTS_DIR, run_label)

    connection.close()
    return total_captures


# Muestra el catalogo guardado en base de datos con ultimo precio,
# estado de stock y URL canonica registrada.
def list_products(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    init_db(connection)
    rows = connection.execute(
        """
        SELECT id, name, url, last_price_cents, last_available, last_seen_at
        FROM products
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    if not rows:
        print("No hay productos guardados todavía.")
        connection.close()
        return
    for row in rows:
        status = "disponible" if row["last_available"] else "agotado"
        print(f"{row['id']}: {row['name']} | {cents_to_euros(row['last_price_cents'])} | {status} | {row['url']}")
    connection.close()


# Devuelve el historico temporal de un producto para construir graficos
# y analizar su evolucion de precio/stock.
def fetch_history(connection: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT captured_at, price_cents, available
        FROM captures
        WHERE product_id = ?
        ORDER BY captured_at ASC, id ASC
        """,
        (product_id,),
    ).fetchall()


# Limpia un texto para usarlo como nombre de archivo sin caracteres problematicos.
def sanitize_filename(name: str) -> str:
    allowed = []
    for character in name:
        if character.isalnum() or character in {"-", "_"}:
            allowed.append(character)
        elif character.isspace():
            allowed.append("_")
    result = "".join(allowed).strip("_")
    return result or "reporte"


# Genera reportes por producto con evolucion temporal de precio y disponibilidad,
# y guarda cada grafico en la carpeta de salida indicada.
def generate_reports(database_path: Path, output_dir: Path, product_name: str | None = None) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    init_db(connection)
    output_dir.mkdir(parents=True, exist_ok=True)

    query = "SELECT id, name, url FROM products"
    params: tuple[object, ...] = ()
    if product_name:
        query += " WHERE name LIKE ? OR url LIKE ?"
        params = (f"%{product_name}%", f"%{product_name}%")
    rows = connection.execute(query + " ORDER BY name COLLATE NOCASE", params).fetchall()

    if not rows:
        print("No hay datos para generar reportes.")
        connection.close()
        return

    for row in rows:
        history = fetch_history(connection, int(row["id"]))
        if not history:
            continue
        timestamps = [datetime.fromisoformat(item["captured_at"]) for item in history]
        prices = [item["price_cents"] / 100 for item in history]
        availability = [bool(item["available"]) for item in history]

        figure, price_axis = plt.subplots(figsize=(11, 5))
        price_axis.plot(timestamps, prices, marker="o", linewidth=2, color="#1f77b4", label="Precio")
        price_axis.set_title(row["name"])
        price_axis.set_xlabel("Fecha")
        price_axis.set_ylabel("Precio (€)")
        price_axis.grid(True, alpha=0.25)

        availability_axis = price_axis.twinx()
        availability_axis.step(
            timestamps,
            [1 if value else 0 for value in availability],
            where="post",
            color="#d62728",
            alpha=0.35,
            label="Stock",
        )
        availability_axis.set_yticks([0, 1])
        availability_axis.set_yticklabels(["Agotado", "Disponible"])
        availability_axis.set_ylim(-0.1, 1.1)

        chart_path = output_dir / f"{row['id']}_{sanitize_filename(row['name'])}.png"
        figure.tight_layout()
        figure.savefig(chart_path, dpi=160)
        plt.close(figure)
        print(f"[REPORTE] {chart_path}")

    connection.close()


# Ejecuta capturas periodicas segun intervalo y repeticiones configuradas.
# Reutiliza el mismo flujo de scraping en cada iteracion.
def periodic_scrape(
    urls: list[str],
    database_path: Path,
    interval_seconds: int,
    repeat: int | None,
    stop_on_error: bool = False,
    create_run_report: bool = False,
) -> None:
    iteration = 0
    while True:
        iteration += 1
        print(f"\n--- Captura {iteration} | {utc_now_iso()} ---")
        scrape_urls(
            urls,
            database_path,
            stop_on_error=stop_on_error,
            create_run_report=create_run_report,
        )
        if repeat is not None and iteration >= repeat:
            break
        time.sleep(interval_seconds)


    # Define y organiza todos los argumentos de linea de comandos del script.
    # Aqui se declaran subcomandos y opciones disponibles para cada modo.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sistema de seguimiento de precios e inventario para milfshakes.es"
    )
    parser.add_argument("--db", default=str(DB_PATH), help="Ruta de la base de datos SQLite")

    subparsers = parser.add_subparsers(dest="command")

    scrape_parser = subparsers.add_parser("scrape", help="Realiza una captura de precios e inventario")
    scrape_parser.add_argument("--url", dest="urls", action="append", help="URL de producto o colección")
    scrape_parser.add_argument("--urls-file", help="Archivo de texto con URLs, una por línea")
    scrape_parser.add_argument("--all", dest="all_products", action="store_true", help="Analiza todos los productos del sitio usando el sitemap")
    scrape_parser.add_argument("--stop-on-error", action="store_true", help="Detiene la ejecución al primer error")
    scrape_parser.add_argument("--repeat", type=int, help="Número de capturas consecutivas")
    scrape_parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Intervalo entre capturas periódicas en segundos",
    )

    report_parser = subparsers.add_parser("report", help="Genera gráficos del historial")
    report_parser.add_argument("--product", help="Filtra por nombre o URL parcial")
    report_parser.add_argument("--output-dir", default=str(REPORTS_DIR), help="Carpeta de salida para los PNG")

    subparsers.add_parser("list", help="Lista los productos almacenados")
    return parser


# Punto de entrada del programa: interpreta argumentos y delega en la accion
# correspondiente (scrape, report o list), con sus validaciones basicas.
def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    database_path = Path(args.db)

    if args.command == "scrape":
        urls = gather_input_urls(args)
        if not urls:
            print("No se proporcionaron URLs.")
            return 1
        create_run_report = bool(getattr(args, "all_products", False))
        if args.repeat and args.repeat > 1:
            periodic_scrape(
                urls,
                database_path,
                max(1, args.interval),
                args.repeat,
                stop_on_error=args.stop_on_error,
                create_run_report=create_run_report,
            )
        else:
            scrape_urls(
                urls,
                database_path,
                stop_on_error=args.stop_on_error,
                create_run_report=create_run_report,
            )
        return 0

    if args.command == "report":
        generate_reports(database_path, Path(args.output_dir), args.product)
        return 0

    if args.command == "list":
        list_products(database_path)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())