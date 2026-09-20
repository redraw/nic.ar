#!/usr/bin/env python3
"""Build the normalized NIC Argentina RDList DuckDB dataset.

The script deliberately downloads one listing and one CSV at a time.  A source
row is committed only after its published SHA-256 matches the downloaded file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

import duckdb
import requests
from urllib3.exceptions import InsecureRequestWarning

LIST_URL = "https://rdlist.nic.ar/rd_list/"
HASH_RE = re.compile(r"^[a-f0-9]{64}$", re.I)
LOG = logging.getLogger("rdlist")


@dataclass(frozen=True)
class Listing:
    source_date: str  # ISO date
    published_hash: str
    certification: str | None
    csv_url: str


class ListingParser(HTMLParser):
    """Small, dependency-free parser for the NIC table and page links."""
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url, self.rows, self.pages = base_url, [], set()
        self._row, self._cell, self._href, self._cell_href = [], [], None, None
        self._in_row = self._in_cell = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr": self._in_row, self._row = True, []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell, self._cell, self._cell_href = True, [], None
        elif tag == "a":
            self._href = attrs.get("href")
            if self._in_cell: self._cell_href = self._href

    def handle_data(self, data):
        if self._in_cell: self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._row.append(("".join(self._cell).strip(), self._cell_href))
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            self.rows.append(self._row); self._in_row = False
        elif tag == "a" and self._href:
            page = parse_qs(urlparse(urljoin(self.base_url, self._href)).query).get("page", [None])[0]
            if page and page.isdigit(): self.pages.add(int(page))
            self._href = None


def parse_listing(html: str, base_url: str = LIST_URL) -> tuple[list[Listing], set[int]]:
    parser = ListingParser(base_url); parser.feed(html)
    found: list[Listing] = []
    for row in parser.rows:
        if len(row) < 4 or not HASH_RE.fullmatch(row[1][0].strip()): continue
        try: source_date = datetime.strptime(row[0][0].strip(), "%d-%m-%Y").date().isoformat()
        except ValueError: continue
        href = row[3][1]
        if not href: raise ValueError(f"CSV link absent for {source_date}")
        found.append(Listing(source_date, row[1][0].strip().lower(), row[2][0].strip() or None, urljoin(base_url, href)))
    return found, parser.pages


def fetch_page(session: requests.Session, page: int) -> tuple[list[Listing], set[int]]:
    LOG.info("Fetching listing page %s", page)
    response = session.get(LIST_URL, params={"page": page} if page > 1 else {}, timeout=60)
    response.raise_for_status()
    items, pages = parse_listing(response.text, response.url)
    LOG.info("Listing page %s: %s source files found", page, len(items))
    return items, pages


def decode_csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try: text = raw.decode(encoding); break
        except UnicodeDecodeError: continue
    else: raise ValueError("Unsupported CSV encoding")
    sample = text[:8192]
    try: dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error: dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames: raise ValueError("CSV has no header")
    fields = [f.strip().lstrip("\ufeff") for f in reader.fieldnames]
    rows = [{fields[i]: (row.get(reader.fieldnames[i]) or "").strip() for i in range(len(fields))} for row in reader]
    return fields, rows


def normalize_domain_record(source_date: str, row_number: int, row: dict[str, str]) -> tuple:
    """Turn one NIC CSV row into the canonical, queryable representation."""
    def normalized_name(value: str | None) -> str | None:
        value = (value or "").strip().strip(".").lower()
        return value or None

    def value_or_none(value: str | None) -> str | None:
        return value or None

    domain_name, zone = normalized_name(row.get("dominio")), normalized_name(row.get("zona"))
    registered_at = None
    if timestamp := row.get("fecha_registro"):
        try:
            registered_at = datetime.fromisoformat(timestamp)
        except ValueError:
            pass
    return (
        source_date, row_number,
        f"{domain_name}.{zone}" if domain_name and zone else None,
        domain_name, zone, value_or_none(row.get("id_dominio")), value_or_none(row.get("tipo")),
        registered_at, value_or_none(row.get("titular")), value_or_none(row.get("tipo_doc")),
        value_or_none(row.get("numero_doc")),
    )


def populate_domain_records_from_raw(con: duckdb.DuckDBPyConnection) -> None:
    """One-time migration support for databases created before domain_records."""
    con.execute("""
      INSERT INTO domain_records
      WITH extracted AS (
        SELECT source_date, row_number, json_transform(payload, '{
          "dominio":"VARCHAR", "zona":"VARCHAR", "id_dominio":"VARCHAR",
          "tipo":"VARCHAR", "fecha_registro":"VARCHAR", "titular":"VARCHAR",
          "tipo_doc":"VARCHAR", "numero_doc":"VARCHAR"
        }') AS fields FROM records
      ), normalized AS (
        SELECT *, nullif(lower(trim(trim(fields.dominio), '.')), '') AS domain_name,
          nullif(lower(trim(trim(fields.zona), '.')), '') AS zone FROM extracted
      )
      SELECT source_date, row_number,
        CASE WHEN domain_name IS NOT NULL AND zone IS NOT NULL
          THEN domain_name || '.' || zone END AS domain,
        domain_name, zone, nullif(fields.id_dominio, ''), nullif(fields.tipo, ''),
        try_cast(fields.fecha_registro AS TIMESTAMP), nullif(fields.titular, ''),
        nullif(fields.tipo_doc, ''), nullif(fields.numero_doc, '')
      FROM normalized ORDER BY domain, source_date, row_number
    """)


def ensure_schema(con: duckdb.DuckDBPyConnection) -> bool:
    """Create/migrate atomically; return whether the catalog changed."""
    con.execute("BEGIN TRANSACTION")
    try:
        changed = _ensure_schema(con)
        con.execute("COMMIT")
        return changed
    except Exception:
        con.execute("ROLLBACK")
        raise


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> bool:
    con.execute("""
      CREATE TABLE IF NOT EXISTS sources (
        source_date DATE PRIMARY KEY, csv_url VARCHAR NOT NULL, published_hash VARCHAR NOT NULL,
        downloaded_hash VARCHAR NOT NULL, certification VARCHAR, row_count BIGINT NOT NULL,
        imported_at TIMESTAMP NOT NULL
      );
      CREATE OR REPLACE VIEW dataset_status AS
        SELECT min(source_date) AS first_date, max(source_date) AS last_date,
          count(*) AS source_files, coalesce(sum(row_count), 0) AS records,
          max(imported_at) AS last_imported_at FROM sources;
    """)
    domain_records_exists = con.execute("""
      SELECT count(*) FROM information_schema.tables
      WHERE table_catalog = current_database() AND table_schema = 'main'
        AND table_name = 'domain_records'
    """).fetchone()[0]
    records_exists = con.execute("""
      SELECT count(*) FROM information_schema.tables
      WHERE table_catalog = current_database() AND table_schema = 'main'
        AND table_name = 'records'
    """).fetchone()[0]
    changed = False
    if not domain_records_exists:
        con.execute("""
          CREATE TABLE domain_records (
        source_date DATE NOT NULL, row_number BIGINT NOT NULL,
        domain VARCHAR, domain_name VARCHAR, zone VARCHAR, domain_id VARCHAR,
        record_type VARCHAR, registered_at TIMESTAMP, holder_name VARCHAR,
        document_type VARCHAR, document_number VARCHAR,
        PRIMARY KEY (source_date, row_number)
          )
        """)
        changed = True
        if records_exists:
            LOG.info("Materializing typed domain records from the legacy raw table")
            populate_domain_records_from_raw(con)
    if records_exists:
        con.execute("DROP VIEW IF EXISTS record_fields")
        con.execute("DROP TABLE records")
        changed = True
    return changed


def import_listing(con: duckdb.DuckDBPyConnection, session: requests.Session, item: Listing) -> bool:
    old = con.execute("SELECT published_hash FROM sources WHERE source_date = ?", [item.source_date]).fetchone()
    if old and old[0] == item.published_hash:
        LOG.info("%s: unchanged; skipped", item.source_date)
        return False
    action = "reimporting" if old else "importing"
    LOG.info("%s: %s %s", item.source_date, action, item.csv_url)
    response = session.get(item.csv_url, timeout=120); response.raise_for_status()
    raw = response.content; actual = hashlib.sha256(raw).hexdigest()
    if actual != item.published_hash:
        LOG.error("%s: SHA-256 mismatch (expected %s, got %s)", item.source_date, item.published_hash, actual)
        raise ValueError(f"SHA-256 mismatch for {item.source_date}: expected {item.published_hash}, got {actual}")
    LOG.info("%s: SHA-256 verified", item.source_date)
    _, rows = decode_csv(raw)
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM domain_records WHERE source_date = ?", [item.source_date])
        con.execute("DELETE FROM sources WHERE source_date = ?", [item.source_date])
        if rows:
            con.executemany("INSERT INTO domain_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                normalize_domain_record(item.source_date, number, record)
                for number, record in enumerate(rows, start=1)
            ])
        con.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, current_timestamp)", [
            item.source_date, item.csv_url, item.published_hash, actual, item.certification, len(rows)
        ])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK"); raise
    LOG.info("%s: imported %s records", item.source_date, len(rows))
    return True


def listings_for_mode(session: requests.Session, mode: str, recent_pages: int) -> Iterable[Listing]:
    if mode == "latest":
        for page in range(1, recent_pages + 1):
            items, _ = fetch_page(session, page)
            for item in items: yield item
        return
    page = 1
    while True:
        items, pages = fetch_page(session, page)
        if not items: break
        for item in items: yield item
        # Only advance through the contiguous, published pagination sequence.
        if page + 1 not in pages: break
        page += 1


def run(mode: str, database: Path, recent_pages: int = 3, verify: bool | str = True) -> bool:
    database.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Starting %s update into %s", mode, database)
    con = duckdb.connect(str(database))
    try:
        changed = ensure_schema(con)
        if mode != "migrate":
            changed = download_listings(con, mode, recent_pages, verify) or changed
    finally:
        con.close()
    LOG.info("Update finished; changed=%s", changed)
    return changed


def download_listings(con: duckdb.DuckDBPyConnection, mode: str, recent_pages: int,
                      verify: bool | str) -> bool:
    changed = False
    with requests.Session() as session:
        # The session setting is shared by listing and CSV downloads alike.
        session.verify = verify
        if verify is False:
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
            LOG.warning("TLS certificate verification is disabled (--insecure)")
        session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.3"
        for item in listings_for_mode(session, mode, recent_pages):
            changed = import_listing(con, session, item) or changed
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("bootstrap", "latest", "migrate"),
                        help="migrate upgrades the local schema without network access")
    parser.add_argument("--database", type=Path, default=Path("data/rdlist.duckdb"))
    parser.add_argument("--recent-pages", type=int, default=3)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification for all requests")
    tls.add_argument("--ca-bundle", type=Path, help="PEM file with the CA certificate to trust")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    verify: bool | str = str(args.ca_bundle) if args.ca_bundle else not args.insecure
    changed = run(args.mode, args.database, args.recent_pages, verify)
    print(f"changed={'true' if changed else 'false'}")
    return 0

if __name__ == "__main__": raise SystemExit(main())
