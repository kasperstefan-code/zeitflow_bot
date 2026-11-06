from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:.,h ](\d{2}))?\s*$")
_TODAY_WORDS = {"heute", "today", "сегодня", "сьогодні", "dzisiaj", "dziś", "šiandien"}
_YESTERDAY_WORDS = {"gestern", "yesterday", "вчера", "вчора", "wczoraj", "vakar"}

WEEKDAY_ABBR_DE = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")


def today(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def parse_time(raw: str) -> str | None:
    m = _TIME_RE.match(raw or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def parse_date(raw: str, reference: date) -> date | None:
    s = (raw or "").strip().lower()
    if s in _TODAY_WORDS:
        return reference
    if s in _YESTERDAY_WORDS:
        return reference - timedelta(days=1)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", s)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        try:
            candidate = date(reference.year, month, day)
        except ValueError:
            return None
        if candidate > reference:  # "30.12." am 02.01. meint das Vorjahr
            try:
                candidate = date(reference.year - 1, month, day)
            except ValueError:
                return None
        return candidate
    return None


def to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def work_minutes(start: str, end: str, break_minutes: int) -> int:
    """Netto-Arbeitszeit. Wirft ValueError bei Ende <= Beginn oder Pause >= Brutto."""
    gross = to_minutes(end) - to_minutes(start)
    if gross <= 0:
        raise ValueError("Ende muss nach Beginn liegen")
    if break_minutes < 0 or break_minutes >= gross:
        raise ValueError("Pause ist länger als die Arbeitszeit")
    return gross - break_minutes


def intervals_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return to_minutes(a_start) < to_minutes(b_end) and to_minutes(b_start) < to_minutes(a_end)


def iso_week_range(year: int, week: int) -> tuple[date, date]:
    monday = date.fromisocalendar(year, week, 1)
    return monday, monday + timedelta(days=6)


def parse_week(raw: str, reference: date) -> tuple[int, int] | None:
    """Akzeptiert "13", "KW13", "13/2026", "13.2026"."""
    s = (raw or "").strip().lower().replace("kw", "").replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?:[/.](\d{4}))?", s)
    if not m:
        return None
    week = int(m.group(1))
    year = int(m.group(2)) if m.group(2) else reference.isocalendar().year
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError:
        return None
    return year, week


def format_hours(minutes: int) -> str:
    value = minutes / 60
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def short_name(full_name: str) -> str:
    """Kürzt "Sergej Tyschenko" zu "S. Tyschenko"; einteilige Namen bleiben unverändert."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name.strip()
    return f"{parts[0][0]}. {' '.join(parts[1:])}"
