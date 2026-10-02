#!/usr/bin/env python3
"""Análisis de períodos compartido por los scripts de escaneo y clasificación.

Un período es un año (``2026``), un año y mes (``2026-09``) o una fecha completa
(``2026-09-30``). Se mapea a un rango inclusivo de ``source_date`` usado para
seleccionar dominios desde ``domain_records`` en ``data/rdlist.duckdb``.
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_DATABASE = DATA_DIR / "rdlist.duckdb"
PERIOD_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def valid_period(period: str) -> bool:
    """Acepta un período como YYYY, YYYY-MM o YYYY-MM-DD."""
    match = PERIOD_RE.match(period)
    if not match:
        return False
    year, month, day = match.groups()
    if month and not 1 <= int(month) <= 12:
        return False
    if day:
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            return False
    return True


def period_range(period: str) -> tuple[date, date]:
    """Mapea un período YYYY / YYYY-MM / YYYY-MM-DD a un rango inclusivo de source_date."""
    year, month, day = PERIOD_RE.match(period).groups()
    if day is not None:
        point = date(int(year), int(month), int(day))
        return point, point
    if month is not None:
        last = calendar.monthrange(int(year), int(month))[1]
        return date(int(year), int(month), 1), date(int(year), int(month), last)
    return date(int(year), 1, 1), date(int(year), 12, 31)
