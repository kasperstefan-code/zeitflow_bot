"""Montage-Stundennachweis als Excel – Layout nach Vorlage docs/vorlagen/SMF_2026_KW13.xlsx.

Abweichungen von der Vorlage (fachlich gewünscht):
- Spalte "Kostenstelle" zwischen "Ausgeführte Arbeiten" und "Beginn"
- jeder Monteur mit eigener Tätigkeit in eigener Zeile; Tagesblöcke wachsen mit
- Zeile "Anfahrten" direkt unter "Normal-Std."
"""

from __future__ import annotations

import io
import math
from datetime import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from zeitflow.services import WeekReport
from zeitflow.timeutil import WEEKDAY_ABBR_DE

THIN = Side(style="thin")
MEDIUM = Side(style="medium")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FONT = Font(name="Calibri", size=11)
FONT_SMALL = Font(name="Calibri", size=9)
FONT_ACTIVITY = Font(name="Calibri", size=10)
FONT_TITLE = Font(name="Comic Sans MS", size=11, bold=True)
FONT_BOLD = Font(name="Calibri", size=11, bold=True)

# Spalten: A Tag | B Monteur | C Tätigkeit | D Kostenstelle | E Beginn | F Pause | G Ende | H Std.
COL_DAY, COL_MONTEUR, COL_ACTIVITY, COL_COST, COL_START, COL_BREAK, COL_END, COL_HOURS = range(1, 9)
LAST_COL = COL_HOURS
WIDTHS = {1: 7.3, 2: 14.0, 3: 39.4, 4: 12.0, 5: 8.2, 6: 8.2, 7: 8.2, 8: 8.0}
MIN_ROWS_PER_DAY = 3
ROW_HEIGHT = 13.5
ACTIVITY_CHARS_PER_LINE = 42
FIRST_DATA_ROW = 9
TIME_FORMAT = "h:mm"
HOURS_FORMAT = "0.0#"


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _minutes_as_time(minutes: int) -> time:
    hh, mm = divmod(max(0, minutes), 60)
    return time(min(hh, 23), mm)


def _text(ws: Worksheet, row: int, col: int, value: str):
    """Freitext immer als Text speichern – sonst wird z. B. "=..." als Formel ausgeführt."""
    cell = ws.cell(row, col, value)
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def _style_row(ws: Worksheet, row: int, top: Side = THIN) -> None:
    for col in range(1, LAST_COL + 1):
        cell = ws.cell(row, col)
        cell.border = Border(left=THIN, right=THIN, top=top, bottom=THIN)
        cell.font = FONT
        cell.alignment = Alignment(vertical="top")


class ExcelRenderer:
    extension = "xlsx"
    mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def render(self, report: WeekReport) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.title = f"KW{report.iso_week:02d}_{report.iso_year}"
        for col, width in WIDTHS.items():
            ws.column_dimensions[get_column_letter(col)].width = width

        self._header(ws, report)
        last_data_row = self._days(ws, report)
        self._footer(ws, report, last_data_row)
        self._page_setup(ws)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _header(self, ws: Worksheet, report: WeekReport) -> None:
        ws.row_dimensions[1].height = 30
        ws["B2"] = "Montage-Stundennachweis"
        ws["B2"].font = FONT_TITLE
        for row, label, value in (
            (3, "Kunde:", report.customer_name),
            (5, "Projekt:", "; ".join(report.project_names)),
        ):
            ws.cell(row, COL_MONTEUR, label).font = FONT
            _text(ws, row, COL_ACTIVITY, value).font = FONT
            ws.merge_cells(start_row=row, start_column=COL_ACTIVITY, end_row=row, end_column=LAST_COL)
            ws.cell(row, COL_ACTIVITY).alignment = Alignment(horizontal="left", wrap_text=True)
            for col in range(COL_MONTEUR, LAST_COL + 1):
                ws.cell(row, col).border = Border(bottom=THIN)
        if len(report.project_names) > 1:
            ws.row_dimensions[5].height = ROW_HEIGHT * math.ceil(len(ws["C5"].value) / 90)

        ws["A6"] = report.iso_year
        ws["A6"].alignment = Alignment(horizontal="center")
        ws["A6"].font = FONT

        headers = {
            COL_DAY: ("KW", report.iso_week),
            COL_MONTEUR: ("Monteur", None),
            COL_ACTIVITY: ("Ausgeführte Arbeiten", None),
            COL_COST: ("Kostenstelle", None),
            COL_START: ("Beginn", None),
            COL_BREAK: ("Pause", None),
            COL_END: ("Ende", None),
            COL_HOURS: ("Gesamt-", "Std."),
        }
        for col, (top, bottom) in headers.items():
            c7, c8 = ws.cell(7, col, top), ws.cell(8, col, bottom)
            for c in (c7, c8):
                c.font = FONT_SMALL if col in (COL_ACTIVITY, COL_COST, COL_HOURS) else FONT
                c.alignment = Alignment(horizontal="center", vertical="center")
            c7.border = Border(left=THIN, right=THIN, top=THIN)
            c8.border = Border(left=THIN, right=THIN, bottom=THIN)
            if col in (COL_START, COL_BREAK, COL_END):
                ws.merge_cells(start_row=7, start_column=col, end_row=8, end_column=col)
                c7.border = BOX

    def _days(self, ws: Worksheet, report: WeekReport) -> int:
        row = FIRST_DATA_ROW
        for block in report.days:
            n = max(MIN_ROWS_PER_DAY, len(block.rows))
            for i in range(n):
                _style_row(ws, row + i, top=MEDIUM if i == 0 else THIN)
                ws.row_dimensions[row + i].height = ROW_HEIGHT

            ws.cell(row, COL_DAY, WEEKDAY_ABBR_DE[block.day.weekday()])
            ws.cell(row + 1, COL_DAY, "den:")
            date_cell = ws.cell(row + 2, COL_DAY, block.day)
            date_cell.number_format = "dd.mm."
            date_cell.alignment = Alignment(horizontal="left", vertical="top")

            for i, r in enumerate(block.rows):
                rr = row + i
                _text(ws, rr, COL_MONTEUR, r.monteur)
                act = _text(ws, rr, COL_ACTIVITY, r.activity)
                act.font = FONT_ACTIVITY
                act.alignment = Alignment(vertical="top", wrap_text=True)
                _text(ws, rr, COL_COST, r.cost_center).alignment = Alignment(horizontal="center", vertical="top")
                for col, value in (
                    (COL_START, _hhmm(r.start_time)),
                    (COL_BREAK, _minutes_as_time(r.break_minutes)),
                    (COL_END, _hhmm(r.end_time)),
                ):
                    c = ws.cell(rr, col, value)
                    c.number_format = TIME_FORMAT
                    c.alignment = Alignment(horizontal="center", vertical="top")
                h = ws.cell(rr, COL_HOURS, r.work_minutes / 60)
                h.number_format = HOURS_FORMAT
                lines = max(1, math.ceil(len(r.activity) / ACTIVITY_CHARS_PER_LINE))
                ws.row_dimensions[rr].height = ROW_HEIGHT * lines
            row += n
        return row - 1

    def _footer(self, ws: Worksheet, report: WeekReport, last_data_row: int) -> None:
        row = last_data_row + 1
        summary = [
            (
                "Normal-Std.",
                f"=SUM({get_column_letter(COL_HOURS)}{FIRST_DATA_ROW}:{get_column_letter(COL_HOURS)}{last_data_row})",
            ),
            ("Anfahrten:", report.travel_count),
        ]
        for i, (label, value) in enumerate(summary):
            r = row + i
            _style_row(ws, r, top=MEDIUM if i == 0 else THIN)
            ws.cell(r, COL_DAY, label).font = FONT_BOLD
            ws.merge_cells(start_row=r, start_column=COL_DAY, end_row=r, end_column=COL_MONTEUR)
            cell = ws.cell(r, COL_HOURS, value)
            cell.font = FONT_BOLD
            cell.alignment = Alignment(horizontal="right")
            if i == 0:
                cell.number_format = HOURS_FORMAT
        row += len(summary)

        for i in range(3):
            _style_row(ws, row + i)
            ws.merge_cells(start_row=row + i, start_column=COL_ACTIVITY, end_row=row + i, end_column=LAST_COL)
        ws.cell(row, COL_DAY, "Verbrauchsmaterial:")
        ws.merge_cells(start_row=row, start_column=COL_DAY, end_row=row + 2, end_column=COL_MONTEUR)
        row += 5

        ws.row_dimensions[row].height = 30
        for col in range(COL_DAY, LAST_COL + 1):
            if col != COL_COST:
                ws.cell(row, col).border = Border(bottom=THIN)
        ws.cell(row + 1, COL_DAY, "Unterschrift Auftragnehmer:").font = FONT
        ws.cell(row + 1, COL_START, "Unterschrift Kunde:").font = FONT

    def _page_setup(self, ws: Worksheet) -> None:
        ws.page_setup.orientation = "portrait"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "7:8"
        ws.page_margins.left = ws.page_margins.right = 0.5
        ws.page_margins.top = ws.page_margins.bottom = 0.6
