"""Pruebas unitarias para scripts/classify_domains.py (sin acceso a la red)."""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import classify_domains as cd  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers
        self.last = None

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002 - replica la firma de requests
        self.last = {"url": url, "headers": headers, "json": json, "timeout": timeout}
        return FakeResponse(self.payload, self.status_code, self.headers)


def test_build_payload_includes_state_and_choice_questions() -> None:
    payload = cd.build_payload(cd.MODEL, cd.Candidate("uno.com.ar", "Tienda", "Compra online"))
    assert payload["model"] == cd.MODEL
    assert payload["state"] == {"domain": "uno.com.ar", "title": "Tienda", "description": "Compra online"}
    assert payload["questions"]["site_type"]["type"] == "choice"
    assert "ecommerce" in payload["questions"]["site_type"]["criteria"]
    assert "salud" in payload["questions"]["sector"]["criteria"]


def test_build_payload_omits_empty_metadata() -> None:
    payload = cd.build_payload(cd.MODEL, cd.Candidate("dos.com.ar", "", ""))
    assert payload["state"] == {"domain": "dos.com.ar"}


def test_decide_parses_choice_answers() -> None:
    session = FakeSession({
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "site_type": {"type": "choice", "choice": "servicios", "confidence": 0.99},
            "sector": {"type": "choice", "choice": "salud", "confidence": 1.0},
        },
        "usage": {"cost": 0.000025788},
    })
    row = cd.decide(session, "key", cd.MODEL, cd.Candidate("clinica.com.ar", "Consultorios", ""), 60, cd.Pacer())
    assert session.last["url"] == cd.ENDPOINT
    assert session.last["headers"]["Authorization"] == "Bearer key"
    assert row.site_type == "servicios"
    assert row.site_type_confidence == "0.99"
    assert row.sector == "salud"
    assert row.model == "typesafe/jev-1.13-20260917"
    assert row.cost == "0.0000257880"
    assert row.error == ""


def test_decide_surfaces_api_error() -> None:
    session = FakeSession({"error": {"message": "insufficient credits"}})
    row = cd.decide(session, "key", cd.MODEL, cd.Candidate("x.com.ar", "", ""), 60, cd.Pacer())
    assert row.error == "insufficient credits"
    assert row.site_type == ""


def test_read_candidates_from_metadata_csv(tmp_path: Path) -> None:
    source = tmp_path / "meta.csv"
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domain", "title", "meta_description"])
        writer.writerow(["Uno.com.ar", "Tienda", "Descripci\u00f3n"])
    assert cd.read_candidates(source) == [cd.Candidate("uno.com.ar", "Tienda", "Descripci\u00f3n")]


def test_read_candidates_skips_unreachable_when_ok_column_present(tmp_path: Path) -> None:
    source = tmp_path / "scan.csv"
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domain", "status_code", "ok", "title"])
        writer.writerow(["live.com.ar", "200", "True", "Tienda"])
        writer.writerow(["dead.com.ar", "", "False", ""])
    assert [c.domain for c in cd.read_candidates(source)] == ["live.com.ar"]


def test_read_candidates_from_plain_list(tmp_path: Path) -> None:
    listing = tmp_path / "domains.txt"
    listing.write_text("uno.com.ar\n# comment\n\ndos.com.ar\n", encoding="utf-8")
    assert [c.domain for c in cd.read_candidates(listing)] == ["uno.com.ar", "dos.com.ar"]


def test_read_candidates_from_database(tmp_path: Path) -> None:
    import duckdb

    database = tmp_path / "test.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute("CREATE TABLE domain_records (source_date DATE, domain VARCHAR)")
        connection.executemany(
            "INSERT INTO domain_records VALUES (?, ?)",
            [
                (date(2026, 9, 1), "Uno.com.ar"),
                (date(2026, 9, 15), "dos.com.ar"),
                (date(2026, 9, 20), "tres.com.ar"),
                (date(2026, 8, 1), "fuera.com.ar"),
            ],
        )
        connection.execute(
            "CREATE TABLE domain_pages (domain VARCHAR, title VARCHAR, meta_description VARCHAR, "
            "og_description VARCHAR, ok BOOLEAN)"
        )
        connection.executemany(
            "INSERT INTO domain_pages VALUES (?, ?, ?, ?, ?)",
            [
                ("uno.com.ar", "Tienda", "Compra", "", True),
                ("dos.com.ar", "Dos", "d", "", False),
                ("tres.com.ar", "Tres", "", "og tres", True),
                ("fuera.com.ar", "Fuera", "f", "", True),
            ],
        )
    finally:
        connection.close()
    # ok=False se omite, fuera de período se excluye, og_description rellena meta.
    assert cd.read_candidates_from_database(database, "2026-09") == [
        cd.Candidate("tres.com.ar", "Tres", "og tres"),
        cd.Candidate("uno.com.ar", "Tienda", "Compra"),
    ]
    with pytest.raises(SystemExit):
        cd.read_candidates_from_database(tmp_path / "missing.duckdb", "2026-09")


def test_report_path_uses_period(tmp_path: Path) -> None:
    assert cd.report_path("2026", tmp_path) == tmp_path / "categories-2026.csv"
    assert cd.report_path("2026-09", tmp_path) == tmp_path / "categories-2026-09.csv"
    assert cd.report_path("2026-09-30", tmp_path) == tmp_path / "categories-2026-09-30.csv"


def test_valid_period() -> None:
    assert all(cd.valid_period(p) for p in ("2026", "2026-09", "2026-09-30"))
    assert not any(cd.valid_period(p) for p in ("", "26", "2026-13", "2026-02-30", "2026-9"))


def test_resolve_api_key_precedence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert cd.resolve_api_key("explicit") == "explicit"
    assert cd.resolve_api_key(None) == "env-key"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    database = tmp_path / "opencode.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE credential (integration_id TEXT, value TEXT, active INTEGER, time_updated INTEGER)"
    )
    connection.execute(
        "INSERT INTO credential VALUES (?, ?, 1, 1)",
        ("openrouter", json.dumps({"type": "key", "key": "db-key"})),
    )
    connection.commit()
    connection.close()
    assert cd.resolve_api_key(None, database) == "db-key"


def test_load_into_database_upserts(tmp_path: Path) -> None:
    report = tmp_path / "cat.csv"
    with report.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(cd.FIELDS)
        writer.writerow(["a.com.ar", "ecommerce", "0.9", "retail", "0.8", "typesafe/jev-1.13", "0.001", ""])
        writer.writerow(["b.com.ar", "", "", "", "", "typesafe/jev-1.13", "0", "error"])
    database = tmp_path / "test.duckdb"
    assert cd.load_into_database(database, "domain_categories", report) == 1

    import duckdb

    connection = duckdb.connect(str(database))
    try:
        rows = connection.execute("SELECT domain, site_type, sector, classified_at IS NOT NULL FROM domain_categories").fetchall()
        # Reimportar reemplaza en lugar de duplicar.
        assert cd.load_into_database(database, "domain_categories", report) == 1
        count = connection.execute("SELECT count(*) FROM domain_categories").fetchone()[0]
    finally:
        connection.close()
    assert rows == [("a.com.ar", "ecommerce", "retail", True)]
    assert count == 1
