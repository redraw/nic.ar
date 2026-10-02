#!/usr/bin/env python3
"""Sondea dominios registrados una sola vez y captura tanto la disponibilidad como los metadatos de la página.

Para cada dominio obtiene la página de inicio una única vez (HTTPS primero, HTTP
como respaldo), registra el esquema, el estado y la URL final, y extrae el título
más los metadatos de descripción/generador/OpenGraph de un prefijo acotado del
HTML. Eso reemplaza las dos pasadas anteriores (una verificación de
disponibilidad y una obtención de metadatos por separado) con un solo
recorrido de red por dominio.

La entrada puede ser un archivo simple con un dominio por línea o un CSV con una
columna ``domain``. Cada resultado se agrega al CSV de salida a medida que
termina, de modo que volver a ejecutar el comando sobre la misma entrada continúa
en lugar de volver a obtener lo que ya tiene fila. Pase ``--limit N`` para
limitar cuántos dominios *pendientes* se procesan, lo que facilita repartir un
registro grande en varios días. Pase ``--database`` para cargar el CSV terminado
en una tabla de DuckDB en una sola transacción.

Solo el período es obligatorio: ``scan_domains.py 2026-09`` selecciona los
dominios distintos registrados en ``domain_records`` para ese período, escribe
``data/meta-2026-09.csv`` y carga ``data/rdlist.duckdb``. El período puede ser un
año (``2026``), un año y mes (``2026-09``) o una fecha completa (``2026-09-30``)
y se mapea a un rango inclusivo de ``source_date``. Pase ``--input`` para una
lista de dominios simple o un CSV en lugar de la base de datos, y sobrescriba
cualquier ruta con ``--output``/``--database``.
"""
from __future__ import annotations

import argparse
import csv
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

import duckdb
import requests
from tqdm import tqdm

from periods import DEFAULT_DATABASE, DATA_DIR, period_range, valid_period

USER_AGENT = "Mozilla/5.0 (compatible; nicar-scan/1.0)"
MAX_BYTES = 200_000
FIELDS = [
    "domain", "scheme", "status_code", "final_url", "ok", "title",
    "meta_description", "generator", "og_site_name", "og_description", "error",
]
PAGE_COLUMNS = {
    "domain": "VARCHAR", "scheme": "VARCHAR", "status_code": "INTEGER", "final_url": "VARCHAR",
    "ok": "BOOLEAN", "title": "VARCHAR", "meta_description": "VARCHAR", "generator": "VARCHAR",
    "og_site_name": "VARCHAR", "og_description": "VARCHAR", "error": "VARCHAR",
}


class _StopParsing(Exception):
    """Se lanza una vez que se consumió por completo el encabezado del documento."""


class MetaParser(HTMLParser):
    """Recolecta el título del documento y el puñado de metaetiquetas que nos interesan."""

    WANTED = {
        ("name", "description"): "meta_description",
        ("name", "generator"): "generator",
        ("property", "og:site_name"): "og_site_name",
        ("property", "og:description"): "og_description",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self._in_title = False
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attributes = {key.lower(): (value or "") for key, value in attrs}
            content = attributes.get("content", "").strip()
            if not content:
                return
            for (key, name), field in self.WANTED.items():
                if attributes.get(key, "").lower() == name:
                    self.values.setdefault(field, content)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            raise _StopParsing

    @property
    def title(self) -> str:
        return " ".join("".join(self._title).split())


@dataclass(frozen=True)
class Result:
    """Disponibilidad más metadatos para un solo dominio."""

    domain: str
    scheme: str
    status_code: int | None
    final_url: str
    ok: bool
    title: str
    meta_description: str
    generator: str
    og_site_name: str
    og_description: str
    error: str


def decode_html(raw: bytes, encoding: str | None) -> str:
    """Decodifica un prefijo de bytes, probando UTF-8 antes que la codificación declarada (a menudo adivinada)."""
    fallbacks = [encoding, "latin-1"]
    for candidate in dict.fromkeys(["utf-8", *filter(None, fallbacks)]):
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def probe(session: requests.Session, domain: str, timeout: float) -> Result:
    """Obtiene un dominio una sola vez, devolviendo disponibilidad y metadatos de la primera respuesta."""
    last_error = ""
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/"
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                # Una lectura del cuerpo estancada puede lanzar ReadTimeout/ConnectionError aquí,
                # no solo desde el GET anterior; se mantiene dentro del try para hacer el respaldo.
                for chunk in response.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BYTES:
                        break
                encoding = response.encoding
            finally:
                response.close()
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        parser = MetaParser()
        try:
            parser.feed(decode_html(b"".join(chunks)[:MAX_BYTES], encoding))
        except _StopParsing:
            pass
        return Result(
            domain=domain,
            scheme=scheme,
            status_code=response.status_code,
            final_url=response.url,
            ok=response.status_code == 200,
            title=parser.title,
            meta_description=parser.values.get("meta_description", ""),
            generator=parser.values.get("generator", ""),
            og_site_name=parser.values.get("og_site_name", ""),
            og_description=parser.values.get("og_description", ""),
            error="",
        )
    return Result(domain, "", None, "", False, "", "", "", "", "", last_error)


def read_domains(path: Path) -> Iterator[str]:
    """Genera dominios desde una lista simple o un CSV que tenga una columna ``domain``."""
    with path.open(encoding="utf-8") as handle:
        header = handle.readline()
        handle.seek(0)
        if "," in header and "domain" in header.lower():
            for row in csv.DictReader(handle):
                domain = (row.get("domain") or "").strip().lower()
                if domain:
                    yield domain
        else:
            for line in handle:
                domain = line.strip().lower()
                if domain and not domain.startswith("#"):
                    yield domain


def reconcile_report(output: Path) -> bool:
    """Alinea un informe existente con ``FIELDS`` antes de continuar, devolviendo si se debe agregar.

    Un informe escrito por una versión anterior puede no tener el encabezado
    actual (por ejemplo, escaneos anteriores a ``scheme``/``ok``). Agregar filas
    nuevas sobre él mezclaría anchos de fila y produciría un CSV que DuckDB ya no
    puede analizar, así que se reescribe con el esquema canónico antes de
    continuar. Las columnas presentes se conservan por nombre y las ausentes
    quedan vacías. Las filas que ya tienen el ancho canónico se interpretan por
    posición, lo que también repara informes previamente mezclados.
    """
    if not output.exists() or output.stat().st_size == 0:
        return False
    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return False
    header = rows[0]
    if header == FIELDS:
        return True
    canonical: list[list[str]] = []
    for row in rows[1:]:
        if len(row) == len(FIELDS):
            values = row
        else:
            by_name = {name: row[index] for index, name in enumerate(header) if index < len(row)}
            values = [by_name.get(name, "") for name in FIELDS]
        if values and values[0]:
            canonical.append(values)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerows(canonical)
    temporary.replace(output)
    return True


def completed_domains(output: Path) -> set[str]:
    """Devuelve los dominios que ya están presentes en un informe existente."""
    if not output.exists() or output.stat().st_size == 0:
        return set()
    with output.open(encoding="utf-8", newline="") as handle:
        return {row["domain"] for row in csv.DictReader(handle) if row.get("domain")}


def probe_safe(session: requests.Session, domain: str, timeout: float) -> Result:
    """Llama a ``probe`` pero nunca lanza: un dominio defectuoso no debe abortar el lote."""
    try:
        return probe(session, domain, timeout)
    except Exception as exc:  # noqa: BLE001 - registrar y continuar
        return Result(domain, "", None, "", False, "", "", "", "", "", f"{type(exc).__name__}: {exc}")


def scan_all(domains: list[str], output: Path, workers: int, timeout: float,
             limit: int | None = None) -> tuple[int, int, int]:
    """Transmite dominios por un pool acotado, agregando cada resultado a medida que llega."""
    output.parent.mkdir(parents=True, exist_ok=True)
    append = reconcile_report(output)
    done = completed_domains(output)
    pending = [domain for domain in domains if domain not in done]
    if limit:
        pending = pending[:limit]
    ok = errors = 0
    with output.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not append:
            writer.writerow(FIELDS)
        with requests.Session() as session:
            session.headers["User-Agent"] = USER_AGENT
            with ThreadPoolExecutor(max_workers=workers) as pool:
                window = workers * 2
                queue = iter(pending)
                futures: set = set()

                def fill() -> None:
                    while len(futures) < window:
                        try:
                            futures.add(pool.submit(probe_safe, session, next(queue), timeout))
                        except StopIteration:
                            return

                fill()
                with tqdm(total=len(pending), unit="domain") as bar:
                    while futures:
                        finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                        for future in finished:
                            result = future.result()
                            writer.writerow([
                                result.domain, result.scheme, result.status_code or "", result.final_url,
                                result.ok, result.title, result.meta_description, result.generator,
                                result.og_site_name, result.og_description, result.error,
                            ])
                            ok += result.ok
                            errors += result.status_code is None
                            bar.update(1)
                        fill()
    return len(pending), ok, errors


def report_path(period: str, data_dir: Path = DATA_DIR) -> Path:
    """Devuelve la ruta del informe de metadatos para un período."""
    return data_dir / f"meta-{period}.csv"


def read_domains_from_database(database: Path, period: str) -> list[str]:
    """Devuelve los dominios distintos registrados dentro de las fechas de origen del período."""
    if not database.exists():
        raise SystemExit(f"Database not found: {database}")
    start, end = period_range(period)
    connection = duckdb.connect(str(database), read_only=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT lower(trim(domain)) AS domain FROM domain_records "
            "WHERE source_date BETWEEN ? AND ? AND domain IS NOT NULL AND trim(domain) <> '' "
            "ORDER BY domain",
            [start, end],
        ).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def csv_columns(report: Path, canonical: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Construye una especificación de columnas para ``read_csv_auto`` y expresiones select por columna.

    DuckDB mapea las ``columns`` explícitas por posición, por lo que la
    especificación sigue el orden del encabezado del propio archivo. Las columnas
    que faltan en el archivo se seleccionan como NULL tipados, lo que mantiene
    importables los informes antiguos (por ejemplo, escaneos escritos antes de
    ``scheme``/``ok``).
    """
    with report.open(encoding="utf-8") as handle:
        header = next(handle, "").rstrip("\r\n").split(",")
    present = {name.strip(): canonical[name.strip()] for name in header if name.strip() in canonical}
    spec = "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in present.items()) + "}"
    exprs = {name: (name if name in present else f"CAST(NULL AS {kind})") for name, kind in canonical.items()}
    return spec, exprs


def load_into_database(database: Path, table: str, report: Path) -> int:
    """Carga en bloque un CSV de escaneo en una tabla de DuckDB en una sola transacción."""
    if not table.isidentifier():
        raise SystemExit(f"Invalid table name: {table!r}")
    spec, exprs = csv_columns(report, PAGE_COLUMNS)
    csv_source = f"read_csv_auto(?, header = true, columns = {spec}, nullstr = '')"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "domain VARCHAR PRIMARY KEY, scheme VARCHAR, status_code INTEGER, final_url VARCHAR, "
            "ok BOOLEAN, title VARCHAR, meta_description VARCHAR, generator VARCHAR, "
            "og_site_name VARCHAR, og_description VARCHAR, error VARCHAR, scanned_at TIMESTAMP)"
        )
        loaded = connection.execute(f"SELECT count(*) FROM {csv_source} WHERE domain <> ''", [str(report)]).fetchone()[0]
        connection.execute(
            f"INSERT OR REPLACE INTO {table} "
            f"SELECT {', '.join(exprs[name] for name in PAGE_COLUMNS)}, current_timestamp "
            f"FROM {csv_source} WHERE domain <> ''",
            [str(report)],
        )
    finally:
        connection.close()
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("period", help="Period to operate on: YYYY, YYYY-MM, or YYYY-MM-DD (e.g. 2026, 2026-09, 2026-09-30)")
    parser.add_argument("--input", type=Path, help="Domain list file (default: read domains from --database)")
    parser.add_argument("--output", type=Path, help="CSV report (default: data/meta-<period>.csv)")
    parser.add_argument("--workers", type=int, default=32, help="Concurrent requests")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout in seconds")
    parser.add_argument("--limit", type=int, help="Process at most this many pending domains")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="DuckDB database to read domains from and bulk-load into")
    parser.add_argument("--table", default="domain_pages", help="Table used with --database")
    args = parser.parse_args()
    if not valid_period(args.period):
        parser.error("period must be YYYY, YYYY-MM, or YYYY-MM-DD, e.g. 2026, 2026-09, 2026-09-30")

    report = args.output or report_path(args.period)
    domains = list(read_domains(args.input)) if args.input else read_domains_from_database(args.database, args.period)
    print(f"domains={len(domains)}")
    checked, ok, errors = scan_all(domains, report, args.workers, args.timeout, args.limit)
    print(f"checked={checked} ok={ok} unreachable={errors}")
    loaded = load_into_database(args.database, args.table, report)
    print(f"loaded={loaded} table={args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
