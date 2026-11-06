"""Export-Formate für den Wochen-Stundennachweis.

Neue Formate (z. B. PDF) implementieren das Protokoll ``ReportRenderer`` und werden in
``RENDERERS`` registriert; Versand und Bot greifen nur über dieses Protokoll zu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from zeitflow.export.excel import ExcelRenderer
from zeitflow.services import WeekReport


@dataclass(frozen=True)
class RenderedFile:
    filename: str
    content: bytes
    mime_type: str


class ReportRenderer(Protocol):
    extension: str
    mime_type: str

    def render(self, report: WeekReport) -> bytes: ...


RENDERERS: dict[str, ReportRenderer] = {"xlsx": ExcelRenderer()}


def report_filename(report: WeekReport, extension: str) -> str:
    safe = re.sub(r"[^\w-]+", "_", report.customer_name).strip("_") or "Kunde"
    return f"Stundennachweis_{safe}_KW{report.iso_week:02d}_{report.iso_year}.{extension}"


def render_report(report: WeekReport, fmt: str = "xlsx") -> RenderedFile:
    renderer = RENDERERS[fmt]
    return RenderedFile(
        filename=report_filename(report, renderer.extension),
        content=renderer.render(report),
        mime_type=renderer.mime_type,
    )
