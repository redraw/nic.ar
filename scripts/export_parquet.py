#!/usr/bin/env python3
"""Export normalized domain records from DuckDB to a Parquet file."""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


def sql_string(value: Path) -> str:
    """Return a SQL string literal for a filesystem path."""
    return "'" + str(value).replace("'", "''") + "'"


def export_parquet(database: Path, output: Path) -> None:
    """Write all normalized domain records to a Zstandard-compressed Parquet file."""
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database), read_only=True)
    try:
        connection.execute(
            f"COPY domain_records TO {sql_string(output)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/rdlist.duckdb"))
    parser.add_argument("--output", type=Path, default=Path("data/rdlist.parquet"))
    args = parser.parse_args()
    export_parquet(args.database, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
