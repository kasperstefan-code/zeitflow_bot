import io
from datetime import date, time, timedelta

from openpyxl import load_workbook

from zeitflow.export import render_report, report_filename
from zeitflow.services import DayBlock, ReportRow, WeekReport

MONDAY = date(2026, 3, 23)


def make_report(rows_per_day: dict[int, list[ReportRow]], travel=None) -> WeekReport:
    days = [DayBlock(MONDAY + timedelta(days=i), rows_per_day.get(i, [])) for i in range(7)]
    return WeekReport(1, "Schwarzwaldmilch GmbH Freiburg", ["Montage", "Rückbau"], 2026, 13, days, travel)


def row(name, minutes=600, cc="23670", activity="Sterilluft L200"):
    return ReportRow(name, activity, cc, "06:30", 60, "17:30", minutes)


def load(report):
    f = render_report(report)
    return f, load_workbook(io.BytesIO(f.content)).active


def find_row(ws, col, value):
    for r in range(1, ws.max_row + 1):
        if ws.cell(r, col).value == value:
            return r
    raise AssertionError(f"{value!r} nicht gefunden")


def test_header_and_columns_follow_template():
    f, ws = load(make_report({}))
    assert f.filename == "Stundennachweis_Schwarzwaldmilch_GmbH_Freiburg_KW13_2026.xlsx"
    assert ws.title == "KW13_2026"
    assert ws["B2"].value == "Montage-Stundennachweis"
    assert ws["C3"].value == "Schwarzwaldmilch GmbH Freiburg"
    assert ws["C5"].value == "Montage; Rückbau"
    assert ws["A6"].value == 2026 and ws["A8"].value == 13
    header = [ws.cell(7, c).value for c in range(1, 9)]
    assert header == ["KW", "Monteur", "Ausgeführte Arbeiten", "Kostenstelle", "Beginn", "Pause", "Ende", "Gesamt-"]
    assert ws["H8"].value == "Std."


def test_each_worker_gets_own_row_and_blocks_grow():
    crew = [row(f"M. Monteur{i}", activity=f"Tätigkeit {i}") for i in range(5)]
    _, ws = load(make_report({1: crew, 2: [row("S. Tyschenko")]}))

    tue = find_row(ws, 1, "Di.")
    wed = find_row(ws, 1, "Mi.")
    assert wed - tue == 5, "Dienstagsblock wächst auf 5 Zeilen"
    assert [ws.cell(tue + i, 2).value for i in range(5)] == [f"M. Monteur{i}" for i in range(5)]
    assert [ws.cell(tue + i, 3).value for i in range(5)] == [f"Tätigkeit {i}" for i in range(5)]
    assert ws.cell(tue + 2, 1).value.date() == date(2026, 3, 24)
    assert ws.cell(tue, 4).value == "23670"
    assert ws.cell(tue, 5).value == time(6, 30)
    assert ws.cell(tue, 6).value == time(1, 0)
    assert ws.cell(tue, 7).value == time(17, 30)
    assert ws.cell(tue, 8).value == 10
    assert find_row(ws, 1, "Do.") - wed == 3, "Leere Tage behalten Mindesthöhe"


def test_totals_and_travel_count_below_hours():
    _, ws = load(make_report({1: [row("A"), row("B", minutes=450)]}, travel=4))
    total_row = find_row(ws, 1, "Normal-Std.")
    first = find_row(ws, 1, "Mo.")
    last = total_row - 1
    assert ws.cell(total_row, 8).value == f"=SUM(H{first}:H{last})"
    assert ws.cell(total_row + 1, 1).value == "Anfahrten:"
    assert ws.cell(total_row + 1, 8).value == 4
    assert find_row(ws, 1, "Verbrauchsmaterial:") > total_row + 1
    assert find_row(ws, 1, "Unterschrift Auftragnehmer:")


def test_travel_count_left_blank_when_unknown():
    _, ws = load(make_report({0: [row("A")]}))
    r = find_row(ws, 1, "Anfahrten:")
    assert ws.cell(r, 8).value is None


def test_filename_sanitized():
    report = make_report({})
    object.__setattr__(report, "customer_name", "Müller & Söhne / Werk 2")
    assert report_filename(report, "xlsx") == "Stundennachweis_Müller_Söhne_Werk_2_KW13_2026.xlsx"


def test_week_and_dates_are_computed_per_week_across_year_boundary():
    monday = date(2025, 12, 29)  # KW 1/2026 beginnt im Vorjahr
    days = [DayBlock(monday + timedelta(days=i), []) for i in range(7)]
    report = WeekReport(1, "Kunde", [], 2026, 1, days, None)
    _, ws = load(report)
    assert ws["A6"].value == 2026 and ws["A8"].value == 1
    dates = [ws.cell(find_row(ws, 1, abbr) + 2, 1).value.date() for abbr in ("Mo.", "Mi.", "So.")]
    assert dates == [date(2025, 12, 29), date(2025, 12, 31), date(2026, 1, 4)]
