"""Pruebas unitarias para scripts/scan_domains.py (sin acceso a la red)."""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import scan_domains as sd  # noqa: E402


HTML = """<!doctype html><html><head>
<title>  Consultorios  M\u00e9dicos </title>
<meta name="description" content="Atenci\u00f3n integral">
<meta name="generator" content="WordPress 6.5">
<meta property="og:site_name" content="Croacia y Frino">
<meta property="og:description" content="Turnos por WhatsApp">
</head><body>ignored</body></html>"""


def parse(html: str) -> sd.MetaParser:
    parser = sd.MetaParser()
    try:
        parser.feed(html)
    except sd._StopParsing:
        pass
    return parser


def test_meta_parser_extracts_head_fields() -> None:
    parser = parse(HTML)
    assert parser.title == "Consultorios M\u00e9dicos"
    assert parser.values["meta_description"] == "Atenci\u00f3n integral"
    assert parser.values["generator"] == "WordPress 6.5"
    assert parser.values["og_site_name"] == "Croacia y Frino"


def test_decode_html_prefers_utf8_over_guessed_latin1() -> None:
    # requests usa ISO-8859-1 por defecto para text/html sin charset.
    assert sd.decode_html("M\u00e9dicos".encode("utf-8"), "ISO-8859-1") == "M\u00e9dicos"


class _StallingResponse:
    """Una respuesta cuya lectura del cuerpo expira (el caso reportado de patoverde.com.ar)."""

    def __init__(self) -> None:
        self.encoding = "utf-8"
        self.status_code = 200
        self.url = "https://stalled.com.ar/"

    def iter_content(self, chunk_size: int = 65536):
        raise requests.exceptions.ConnectionError("HTTPSConnectionPool: Read timed out.")
        yield b""  # pragma: no cover - convierte esto en un generador

    def close(self) -> None:
        pass


class _StallingSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003 - replica la firma de requests
        self.calls += 1
        return _StallingResponse()


def test_probe_survives_body_read_timeout() -> None:
    session = _StallingSession()
    result = sd.probe(session, "stalled.com.ar", 5.0)
    assert result.ok is False
    assert result.status_code is None
    assert "ConnectionError" in result.error
    assert session.calls == 2  # respaldo https luego http, ninguno aborta el worker


def test_probe_safe_never_raises_on_unexpected_error() -> None:
    class BoomSession:
        def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("unexpected")

    result = sd.probe_safe(BoomSession(), "boom.com.ar", 5.0)
    assert result.domain == "boom.com.ar"
    assert result.ok is False
    assert result.error == "RuntimeError: unexpected"


def test_report_path_uses_period(tmp_path: Path) -> None:
    assert sd.report_path("2026", tmp_path) == tmp_path / "meta-2026.csv"
    assert sd.report_path("2026-09", tmp_path) == tmp_path / "meta-2026-09.csv"
    assert sd.report_path("2026-09-30", tmp_path) == tmp_path / "meta-2026-09-30.csv"


def test_period_range() -> None:
    assert sd.period_range("2026") == (date(2026, 1, 1), date(2026, 12, 31))
    assert sd.period_range("2026-09") == (date(2026, 9, 1), date(2026, 9, 30))
    assert sd.period_range("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert sd.period_range("2026-09-30") == (date(2026, 9, 30), date(2026, 9, 30))


def test_valid_period() -> None:
    assert all(sd.valid_period(p) for p in ("2026", "2026-09", "2026-09-30", "2024-02-29"))
    assert not any(sd.valid_period(p) for p in ("", "26", "2026-13", "2026-00", "2026-02-30", "2026-9", "2026/09", "x"))


def test_read_domains_from_database(tmp_path: Path) -> None:
    import duckdb

    database = tmp_path / "test.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute("CREATE TABLE domain_records (source_date DATE, domain VARCHAR)")
        connection.executemany(
            "INSERT INTO domain_records VALUES (?, ?)",
            [
                (date(2026, 9, 1), "Uno.com.ar"),
                (date(2026, 9, 1), "uno.com.ar"),
                (date(2026, 9, 30), "dos.com.ar"),
                (date(2026, 8, 31), "fuera.com.ar"),
                (date(2026, 9, 2), None),
            ],
        )
    finally:
        connection.close()
    assert sd.read_domains_from_database(database, "2026-09") == ["dos.com.ar", "uno.com.ar"]
    assert sd.read_domains_from_database(database, "2026-09-01") == ["uno.com.ar"]
    assert sd.read_domains_from_database(database, "2026") == ["dos.com.ar", "fuera.com.ar", "uno.com.ar"]
    with pytest.raises(SystemExit):
        sd.read_domains_from_database(tmp_path / "missing.duckdb", "2026-09")


def test_read_domains_from_plain_list(tmp_path: Path) -> None:
    listing = tmp_path / "domains.txt"
    listing.write_text("uno.com.ar\n# comment\n\n dos.com.ar \n", encoding="utf-8")
    assert list(sd.read_domains(listing)) == ["uno.com.ar", "dos.com.ar"]


def test_read_domains_from_csv_with_domain_column(tmp_path: Path) -> None:
    report = tmp_path / "http.csv"
    with report.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domain", "status_code"])
        writer.writerow(["Live.com.ar", "200"])
        writer.writerow(["", "200"])
    assert list(sd.read_domains(report)) == ["live.com.ar"]


def test_reconcile_report_migrates_legacy_header(tmp_path: Path) -> None:
    # Encabezado anterior a scheme/ok: las columnas deben conservarse por nombre.
    output = tmp_path / "scan.csv"
    with output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domain", "status_code", "final_url", "title", "meta_description",
                         "generator", "og_site_name", "og_description", "error"])
        writer.writerow(["a.com.ar", "200", "https://a.com.ar/", "A", "desc", "", "", "", ""])
    assert sd.reconcile_report(output) is True
    assert sd.completed_domains(output) == {"a.com.ar"}
    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == sd.FIELDS
    assert rows[1] == ["a.com.ar", "", "200", "https://a.com.ar/", "", "A", "desc", "", "", "", ""]


def test_reconcile_report_repairs_mixed_widths(tmp_path: Path) -> None:
    # Un encabezado de 9 columnas con filas de 11: las filas anchas se leen por posición.
    output = tmp_path / "scan.csv"
    with output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domain", "status_code", "final_url", "title", "meta_description",
                         "generator", "og_site_name", "og_description", "error"])
        writer.writerow(["old.com.ar", "200", "https://old.com.ar/", "Old", "", "", "", "", ""])
        writer.writerow(["new.com.ar", "https", "200", "https://new.com.ar/", "True", "New", "", "", "", "", ""])
    assert sd.reconcile_report(output) is True
    assert sd.completed_domains(output) == {"old.com.ar", "new.com.ar"}
    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == sd.FIELDS
    assert rows[1][:2] == ["old.com.ar", ""]
    assert rows[2] == ["new.com.ar", "https", "200", "https://new.com.ar/", "True", "New", "", "", "", "", ""]


def test_reconcile_report_leaves_canonical_report_untouched(tmp_path: Path) -> None:
    output = tmp_path / "scan.csv"
    with output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(sd.FIELDS)
        writer.writerow(["a.com.ar", "https", "200", "https://a.com.ar/", "True", "A", "", "", "", "", ""])
    before = output.read_bytes()
    assert sd.reconcile_report(output) is True
    assert output.read_bytes() == before
    assert sd.reconcile_report(tmp_path / "missing.csv") is False
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    assert sd.reconcile_report(empty) is False


def test_completed_domains_reads_existing_report(tmp_path: Path) -> None:
    output = tmp_path / "scan.csv"
    with output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(sd.FIELDS)
        writer.writerow(["a.com.ar", "https", "200", "https://a.com.ar/", "True", "A", "", "", "", "", ""])
    assert sd.completed_domains(output) == {"a.com.ar"}
    assert sd.completed_domains(tmp_path / "missing.csv") == set()


def test_load_into_database_bulk_imports(tmp_path: Path) -> None:
    report = tmp_path / "scan.csv"
    with report.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(sd.FIELDS)
        writer.writerow(["a.com.ar", "https", "200", "https://a.com.ar/", "True", "Tienda", "desc", "", "", "", ""])
        writer.writerow(["dead.com.ar", "", "", "", "False", "", "", "", "", "", "timeout"])
    database = tmp_path / "test.duckdb"
    assert sd.load_into_database(database, "domain_pages", report) == 2

    import duckdb

    connection = duckdb.connect(str(database))
    try:
        rows = connection.execute(
            "SELECT domain, status_code, ok, title, scanned_at IS NOT NULL FROM domain_pages ORDER BY domain"
        ).fetchall()
        # Reimportar reemplaza en lugar de duplicar.
        assert sd.load_into_database(database, "domain_pages", report) == 2
        count = connection.execute("SELECT count(*) FROM domain_pages").fetchone()[0]
    finally:
        connection.close()
    assert rows == [("a.com.ar", 200, True, "Tienda", True), ("dead.com.ar", None, False, None, True)]
    assert count == 2
