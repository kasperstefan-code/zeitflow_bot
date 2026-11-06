from datetime import date

import pytest

from zeitflow.timeutil import (
    intervals_overlap,
    iso_week_range,
    parse_date,
    parse_time,
    parse_week,
    short_name,
    work_minutes,
)

REF = date(2026, 3, 25)


@pytest.mark.parametrize(
    "raw,expected",
    [("6:30", "06:30"), ("06.30", "06:30"), ("7", "07:00"), ("17,45", "17:45"), (" 8:00 ", "08:00"), ("6h30", "06:30")],
)
def test_parse_time_valid(raw, expected):
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["", "24:00", "7:60", "abc", "7:5", None])
def test_parse_time_invalid(raw):
    assert parse_time(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("heute", REF),
        ("сегодня", REF),
        ("vakar", date(2026, 3, 24)),
        ("24.03.2026", date(2026, 3, 24)),
        ("24.03.26", date(2026, 3, 24)),
        ("24.3.", date(2026, 3, 24)),
        ("2026-03-24", date(2026, 3, 24)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw, REF) == expected


def test_parse_date_invalid():
    assert parse_date("31.02.", REF) is None
    assert parse_date("morgen", REF) is None


def test_work_minutes():
    assert work_minutes("06:30", "17:30", 60) == 600
    with pytest.raises(ValueError):
        work_minutes("10:00", "09:00", 0)
    with pytest.raises(ValueError):
        work_minutes("10:00", "11:00", 60)
    with pytest.raises(ValueError):
        work_minutes("10:00", "11:00", -5)


def test_intervals_overlap_touching_is_not_overlap():
    assert intervals_overlap("06:00", "12:00", "11:00", "15:00")
    assert not intervals_overlap("06:00", "12:00", "12:00", "15:00")


def test_iso_week_range():
    assert iso_week_range(2026, 13) == (date(2026, 3, 23), date(2026, 3, 29))


@pytest.mark.parametrize(
    "raw,expected", [("13", (2026, 13)), ("KW13/2025", (2025, 13)), ("13.2026", (2026, 13)), ("54", None), ("x", None)]
)
def test_parse_week(raw, expected):
    assert parse_week(raw, REF) == expected


def test_short_name():
    assert short_name("Sergej Tyschenko") == "S. Tyschenko"
    assert short_name("Anna Maria von Berg") == "A. Maria von Berg"
    assert short_name("Kroll") == "Kroll"


def test_short_date_around_new_year_means_previous_year():
    """Befund 12: am 02.01.2027 meint "30.12." den 30.12.2026."""
    assert parse_date("30.12.", date(2027, 1, 2)) == date(2026, 12, 30)
    assert parse_date("29.2.", date(2025, 1, 2)) is None


def test_short_future_date_stays_in_the_future():
    assert parse_date("05.01.", date(2027, 1, 2)) == date(2027, 1, 5)
