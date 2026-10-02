#!/usr/bin/env python3
"""Clasifica dominios con TypeSafe Jev a través de la API Decisions de OpenRouter.

Jev es un modelo de *decisiones* de System One, no un modelo de chat: cada
solicitud envía un estado (el dominio más sus metadatos extraídos) y preguntas
tipadas, y recibe una ``choice`` con probabilidades y una confianza por pregunta.
Este script lee los candidatos escaneados de un período desde la tabla
``domain_pages`` (o un CSV/lista vía ``--input``), le pide a Jev un tipo de sitio
y un sector por dominio, y agrega cada respuesta al CSV de salida a medida que
termina. Volver a ejecutar el comando sobre la misma entrada continúa desde donde
se detuvo, y el ``usage.cost`` informado se acumula. Los fallos transitorios
(errores de transporte, 429/5xx y el 402 de presupuesto en vuelo) se reintentan
con un retroceso compartido que respeta ``Retry-After``.

Pase ``--database`` para cargar el CSV terminado en una tabla de DuckDB en una
sola transacción, en lugar de hacer upsert fila por fila.

Solo el período es obligatorio: ``classify_domains.py 2026-09`` selecciona los
dominios escaneados registrados ese período desde ``domain_pages``, escribe
``data/categories-2026-09.csv`` y carga ``data/rdlist.duckdb``. El período puede
ser un año (``2026``), un año y mes (``2026-09``) o una fecha completa
(``2026-09-30``). Pase ``--input`` para un CSV de metadatos o una lista de
dominios en lugar de la base de datos, y sobrescriba cualquier ruta con
``--output``/``--database``.

La clave de API se resuelve, en orden, desde --api-key, la variable de entorno
OPENROUTER_API_KEY o la credencial ``openrouter`` activa almacenada por OpenCode
en su base de datos local.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import duckdb
import requests
from tqdm import tqdm

from periods import DEFAULT_DATABASE, DATA_DIR, period_range, valid_period

MODEL = "typesafe/jev-1.13"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
OPENCODE_DB = Path.home() / ".local/share/opencode/opencode.db"
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 524, 529}
IN_FLIGHT_BUDGET = "openrouter_in_flight_budget"
MAX_ATTEMPTS = 5
BASE_DELAY = 1.0
MAX_DELAY = 30.0
FIELDS = ["domain", "site_type", "site_type_confidence", "sector", "sector_confidence", "model", "cost", "error"]
CATEGORY_COLUMNS = {
    "domain": "VARCHAR", "site_type": "VARCHAR", "site_type_confidence": "DOUBLE",
    "sector": "VARCHAR", "sector_confidence": "DOUBLE", "model": "VARCHAR",
    "cost": "DOUBLE", "error": "VARCHAR",
}

SITE_TYPE_CRITERIA = {
    "corporativo": "Sitio institucional o corporativo que presenta una empresa o marca, sin venta online.",
    "ecommerce": "Tienda online que vende productos o servicios, con carrito, checkout o catálogo de precios.",
    "blog_medio": "Blog, revista o sitio de noticias y contenido editorial.",
    "app_saas": "Aplicación web o software como servicio: el visitante usa una herramienta en línea.",
    "servicios": "Prestador de servicios profesionales o técnicos que ofrece sus servicios y datos de contacto.",
    "institucional": "Organización, club, ONG, escuela, asociación o entidad sin fin de lucro.",
    "parked": "Dominio estacionado, página en construcción o sin contenido real.",
    "otro": "No encaja en ninguna de las categorías anteriores.",
}

SECTOR_CRITERIA = {
    "retail": "Comercio minorista de productos de consumo.",
    "salud": "Medicina, consultorios, clínicas, profesionales de la salud.",
    "educacion": "Colegios, universidades, cursos y capacitación.",
    "gastronomia": "Restaurantes, bares, delivery y alimentos.",
    "turismo": "Turismo, hoteles, alojamiento, viajes y recreación.",
    "finanzas": "Bancos, seguros, inversiones y servicios financieros.",
    "gobierno": "Organismos públicos y estatales.",
    "ong": "Organizaciones sin fines de lucro y fundaciones.",
    "tech": "Tecnología, software, informática, internet y electrónica.",
    "construccion": "Obra, materiales de construcción, arquitectura e ingeniería civil.",
    "automotor": "Automóviles, motos, repuestos y servicios para vehículos.",
    "inmobiliaria": "Inmobiliarias, venta y alquiler de propiedades, desarrollos.",
    "legal": "Abogados, estudios jurídicos y servicios legales.",
    "deportes": "Deportes, gimnasios, clubes deportivos y actividad física.",
    "agro": "Agricultura, ganadería, campo y agroindustria.",
    "industria": "Manufactura, fábricas, producción y maquinaria.",
    "transporte": "Logística, fletes, transporte de pasajeros y de carga.",
    "medios": "Medios de comunicación, radio, televisión, prensa y portales de noticias.",
    "marketing": "Publicidad, diseño, marketing digital y comunicación.",
    "eventos": "Eventos, fiestas, salones, catering y entretenimiento.",
    "mascotas": "Veterinarias, mascotas y sus productos.",
    "belleza": "Estética, peluquería, cosmética y cuidado personal.",
    "empleo": "Búsqueda de empleo, reclutamiento y recursos humanos.",
    "xxx": "Contenido para adultos, pornografía y servicios sexuales.",
    "otro": "No encaja en ninguna de las categorías anteriores.",
}

QUESTIONS = {
    "site_type": {
        "type": "choice",
        "instructions": "¿Qué tipo de sitio web es este dominio?",
        "criteria": SITE_TYPE_CRITERIA,
    },
    "sector": {
        "type": "choice",
        "instructions": "¿A qué rubro o sector pertenece el contenido de este sitio?",
        "criteria": SECTOR_CRITERIA,
    },
}


@dataclass(frozen=True)
class Candidate:
    domain: str
    title: str
    meta_description: str


@dataclass(frozen=True)
class Row:
    domain: str
    site_type: str
    site_type_confidence: str
    sector: str
    sector_confidence: str
    model: str
    cost: str
    error: str


class RetryableError(Exception):
    """Un fallo transitorio de Decisions que es seguro reintentar tras una pausa."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Pacer:
    """Coordina el retroceso entre workers para que un límite de tasa pause todo el pool."""

    def __init__(self) -> None:
        self._until = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Bloquea hasta que transcurra la pausa compartida."""
        while True:
            with self._lock:
                remaining = self._until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5.0))

    def pause(self, seconds: float) -> None:
        """Extiende la pausa compartida a al menos ``seconds`` desde ahora."""
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)


def resolve_api_key(explicit: str | None, database: Path | None = None) -> str:
    """Busca una clave de OpenRouter en el argumento, el entorno o el almacén de OpenCode."""
    if explicit:
        return explicit
    if key := os.environ.get("OPENROUTER_API_KEY"):
        return key
    path = database or OPENCODE_DB
    if not path.exists():
        raise SystemExit(f"No OPENROUTER_API_KEY set and OpenCode database not found at {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM credential WHERE integration_id = 'openrouter' AND active = 1 "
            "ORDER BY time_updated DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise SystemExit(f"No active 'openrouter' credential found in {path}")
    value = json.loads(row[0])
    key = value.get("key") or value.get("apiKey")
    if not key:
        raise SystemExit("The stored 'openrouter' credential has no key field")
    return key


def read_candidates(path: Path) -> list[Candidate]:
    """Lee candidatos de clasificación desde un CSV de metadatos o una lista simple de dominios."""
    with path.open(encoding="utf-8") as handle:
        header = handle.readline()
        handle.seek(0)
        if "," in header and "domain" in header.lower():
            candidates = []
            for row in csv.DictReader(handle):
                domain = (row.get("domain") or "").strip().lower()
                if not domain:
                    continue
                if (ok := row.get("ok")) is not None and ok.strip().lower() != "true":
                    continue
                title = (row.get("title") or "").strip()
                meta = (row.get("meta_description") or row.get("og_description") or "").strip()
                candidates.append(Candidate(domain, title, meta))
            return candidates
        return [Candidate(line.strip().lower(), "", "") for line in handle if line.strip() and not line.startswith("#")]


def completed_domains(output: Path) -> set[str]:
    """Devuelve los dominios ya clasificados en un informe existente."""
    if not output.exists() or output.stat().st_size == 0:
        return set()
    with output.open(encoding="utf-8", newline="") as handle:
        return {row["domain"] for row in csv.DictReader(handle) if row.get("domain")}


def build_payload(model: str, candidate: Candidate) -> dict:
    """Compone la solicitud a la API Decisions para un dominio."""
    state = {"domain": candidate.domain}
    if candidate.title:
        state["title"] = candidate.title
    if candidate.meta_description:
        state["description"] = candidate.meta_description
    return {"model": model, "state": state, "questions": QUESTIONS}


def retry_after_seconds(response: requests.Response) -> float | None:
    """Lee un encabezado Retry-After numérico; se ignoran los valores con fecha HTTP."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def is_in_flight_budget(payload: object) -> bool:
    """Detecta el 402 transitorio que se lanza cuando las solicitudes en curso agotan el presupuesto en vuelo."""
    error = payload.get("error") if isinstance(payload, dict) else None
    metadata = error.get("metadata") if isinstance(error, dict) else None
    return isinstance(metadata, dict) and metadata.get("limit_source") == IN_FLIGHT_BUDGET


def request_once(session: requests.Session, api_key: str, model: str, candidate: Candidate, timeout: float) -> Row:
    """Realiza una llamada a Decisions, lanzando RetryableError ante un fallo transitorio."""
    try:
        response = session.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=build_payload(model, candidate),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RetryableError(f"{type(exc).__name__}: {exc}") from exc
    retry_after = retry_after_seconds(response)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code in RETRYABLE_STATUSES or (response.status_code == 402 and is_in_flight_budget(payload)):
        raise RetryableError(f"HTTP {response.status_code}", retry_after)
    if payload is None:
        return Row(candidate.domain, "", "", "", "", model, "0", f"Invalid JSON response (HTTP {response.status_code})")
    if error := payload.get("error"):
        return Row(candidate.domain, "", "", "", "", model, "0", error.get("message", str(error)))

    answers = payload.get("answers") or {}
    site = answers.get("site_type") or {}
    sector = answers.get("sector") or {}
    usage = payload.get("usage") or {}
    return Row(
        domain=candidate.domain,
        site_type=str(site.get("choice", "")),
        site_type_confidence=str(site.get("confidence", "")),
        sector=str(sector.get("choice", "")),
        sector_confidence=str(sector.get("confidence", "")),
        model=str(payload.get("model", model)),
        cost=f"{float(usage.get('cost', 0)):.10f}",
        error="",
    )


def decide(session: requests.Session, api_key: str, model: str, candidate: Candidate,
           timeout: float, pacer: Pacer, attempts: int = MAX_ATTEMPTS) -> Row:
    """Consulta a Jev por un dominio, reintentando fallos transitorios con retroceso coordinado."""
    for attempt in range(attempts):
        pacer.wait()
        try:
            return request_once(session, api_key, model, candidate, timeout)
        except RetryableError as exc:
            if attempt + 1 >= attempts:
                return Row(candidate.domain, "", "", "", "", model, "0", str(exc))
            delay = exc.retry_after if exc.retry_after is not None else min(MAX_DELAY, BASE_DELAY * 2 ** attempt) + random.random()
            pacer.pause(delay)
    return Row(candidate.domain, "", "", "", "", model, "0", "Retries exhausted")


def classify_all(candidates: list[Candidate], output: Path, api_key: str, model: str,
                 timeout: float, workers: int, limit: int | None = None, attempts: int = MAX_ATTEMPTS) -> tuple[int, float]:
    """Transmite candidatos por un pool acotado, agregando cada respuesta a medida que llega."""
    output.parent.mkdir(parents=True, exist_ok=True)
    done = completed_domains(output)
    pending = [item for item in candidates if item.domain not in done]
    if limit:
        pending = pending[:limit]
    append = output.exists() and output.stat().st_size > 0
    total_cost = 0.0
    pacer = Pacer()
    with output.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not append:
            writer.writerow(FIELDS)
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                queue = iter(pending)
                futures: set = set()

                def fill() -> None:
                    while len(futures) < workers * 2:
                        try:
                            futures.add(pool.submit(decide, session, api_key, model, next(queue), timeout, pacer, attempts))
                        except StopIteration:
                            return

                fill()
                with tqdm(total=len(pending), unit="domain") as bar:
                    while futures:
                        finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                        for future in finished:
                            row = future.result()
                            writer.writerow([row.domain, row.site_type, row.site_type_confidence, row.sector,
                                             row.sector_confidence, row.model, row.cost, row.error])
                            total_cost += float(row.cost)
                            bar.update(1)
                        fill()
    return len(pending), total_cost


def read_candidates_from_database(database: Path, period: str) -> list[Candidate]:
    """Lee candidatos escaneados para los dominios del período desde ``domain_pages``."""
    if not database.exists():
        raise SystemExit(f"Database not found: {database}")
    start, end = period_range(period)
    connection = duckdb.connect(str(database), read_only=True)
    try:
        rows = connection.execute(
            "SELECT p.domain, p.title, p.meta_description, p.og_description "
            "FROM domain_pages p "
            "JOIN (SELECT DISTINCT lower(trim(domain)) AS domain FROM domain_records "
            "      WHERE source_date BETWEEN ? AND ? AND domain IS NOT NULL AND trim(domain) <> '') d "
            "  ON p.domain = d.domain "
            "WHERE p.ok "
            "ORDER BY p.domain",
            [start, end],
        ).fetchall()
    finally:
        connection.close()
    return [Candidate(domain, title or "", meta or og or "") for domain, title, meta, og in rows]


def report_path(period: str, data_dir: Path = DATA_DIR) -> Path:
    """Devuelve la ruta del informe de categorías para un período."""
    return data_dir / f"categories-{period}.csv"


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
    """Carga en bloque un CSV de clasificación en una tabla de DuckDB en una sola transacción."""
    if not table.isidentifier():
        raise SystemExit(f"Invalid table name: {table!r}")
    spec, exprs = csv_columns(report, CATEGORY_COLUMNS)
    columns = ["domain", "site_type", "site_type_confidence", "sector", "sector_confidence", "model"]
    csv_source = f"read_csv_auto(?, header = true, columns = {spec}, nullstr = '')"
    valid = "WHERE domain <> '' AND site_type IS NOT NULL AND site_type <> ''"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "domain VARCHAR PRIMARY KEY, site_type VARCHAR, site_type_confidence DOUBLE, "
            "sector VARCHAR, sector_confidence DOUBLE, model VARCHAR, classified_at TIMESTAMP)"
        )
        loaded = connection.execute(f"SELECT count(*) FROM {csv_source} {valid}", [str(report)]).fetchone()[0]
        connection.execute(
            f"INSERT OR REPLACE INTO {table} "
            f"SELECT {', '.join(exprs[name] for name in columns)}, current_timestamp "
            f"FROM {csv_source} {valid}",
            [str(report)],
        )
    finally:
        connection.close()
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("period", help="Period to operate on: YYYY, YYYY-MM, or YYYY-MM-DD (e.g. 2026, 2026-09, 2026-09-30)")
    parser.add_argument("--input", type=Path, help="Metadata CSV (default: read candidates from --database)")
    parser.add_argument("--output", type=Path, help="CSV report (default: data/categories-<period>.csv)")
    parser.add_argument("--api-key", help="OpenRouter API key (defaults to env, then OpenCode's store)")
    parser.add_argument("--model", default=MODEL, help="Jev model id (default: typesafe/jev-1.13)")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent decision requests")
    parser.add_argument("--attempts", type=int, default=MAX_ATTEMPTS, help="Attempts per domain on transient failures")
    parser.add_argument("--limit", type=int, help="Classify at most this many pending domains")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="DuckDB database to read candidates from and bulk-load into")
    parser.add_argument("--table", default="domain_categories", help="Table used with --database")
    args = parser.parse_args()
    if not valid_period(args.period):
        parser.error("period must be YYYY, YYYY-MM, or YYYY-MM-DD, e.g. 2026, 2026-09, 2026-09-30")

    report = args.output or report_path(args.period)
    api_key = resolve_api_key(args.api_key)
    candidates = read_candidates(args.input) if args.input else read_candidates_from_database(args.database, args.period)
    print(f"candidates={len(candidates)}")
    classified, cost = classify_all(candidates, report, api_key, args.model, args.timeout, args.workers, args.limit, args.attempts)
    print(f"classified={classified} cost=${cost:.4f}")
    loaded = load_into_database(args.database, args.table, report)
    print(f"loaded={loaded} table={args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
