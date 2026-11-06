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
        if (candidate - reference).days > 180:  # "30.12." am 02.01. meint das Vorjahr, "05.01." bleibt Zukunft
            try:
                candidate = date(reference.year - 1, month, day)
            except ValueError:
                return None
        return candidate
    return None


def to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def gross_minutes(start: str, end: str) -> int:
    """Brutto-Dauer; Ende vor Beginn bedeutet Nachtschicht bis zum Folgetag (z. B. 20:00–06:00 = 600)."""
    gross = (to_minutes(end) - to_minutes(start)) % (24 * 60)
    if gross == 0:
        raise ValueError("Ende muss sich vom Beginn unterscheiden")
    return gross


def span(day: date, start: str, end: str) -> tuple[int, int]:
    """Absolute Minuten [von, bis) – vergleichbar über Tagesgrenzen hinweg (Nachtschichten)."""
    begin = day.toordinal() * 24 * 60 + to_minutes(start)
    return begin, begin + gross_minutes(start, end)


def parse_break(raw: str) -> int | None:
    """Pause in Minuten: "30", "60 min", "1:00", "1h", "0,5" (Stunden, wenn mit Komma/Punkt)."""
    s = (raw or "").strip().lower().replace("minuten", "").replace("min", "").replace("мин", "").replace("хв", "")
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(\d+)\s*h", s)
    if m:
        return int(m.group(1)) * 60
    m = re.fullmatch(r"(\d+)[,.](\d+)", s)
    if m:
        return round(float(f"{m.group(1)}.{m.group(2)}") * 60)
    return int(s) if s.isdigit() else None


def work_minutes(start: str, end: str, break_minutes: int) -> int:
    """Netto-Arbeitszeit (auch über Mitternacht). Wirft ValueError bei Ende == Beginn oder Pause >= Brutto."""
    gross = gross_minutes(start, end)
    if break_minutes < 0 or break_minutes >= gross:
        raise ValueError("Pause ist länger als die Arbeitszeit")
    return gross - break_minutes


def spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


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
