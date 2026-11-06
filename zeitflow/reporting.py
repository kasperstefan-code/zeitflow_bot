"""Wöchentlicher Versand: pro Kunde ein Stundennachweis an die zentrale Abrechnungsadresse."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from zeitflow.export import RenderedFile, render_report
from zeitflow.mailer import Mailer, MailError
from zeitflow.repository import Repository
from zeitflow.services import TimesheetService, WeekReport, build_week_report
from zeitflow.timeutil import iso_week_range

LOG = logging.getLogger(__name__)


@dataclass
class CustomerResult:
    customer_name: str
    status: str  # "sent" | "skipped" | "failed"
    detail: str = ""
    file: RenderedFile | None = None


@dataclass
class WeeklyRunResult:
    iso_year: int
    iso_week: int
    results: list[CustomerResult] = field(default_factory=list)

    def summary(self) -> str:
        if not self.results:
            return f"KW {self.iso_week:02d}/{self.iso_year}: keine Einträge – nichts versendet."
        icons = {"sent": "✅", "skipped": "⏭", "failed": "❌"}
        lines = [f"📧 Stundennachweise KW {self.iso_week:02d}/{self.iso_year}:"]
        for r in self.results:
            lines.append(f"{icons[r.status]} {r.customer_name}" + (f" – {r.detail}" if r.detail else ""))
        return "\n".join(lines)


class WeeklyReporter:
    def __init__(self, repo: Repository, service: TimesheetService, mailer: Mailer, recipient: str, company: str = ""):
        self.repo = repo
        self.service = service
        self.mailer = mailer
        self.recipient = recipient
        self.company = company

    async def run(self, iso_year: int, iso_week: int, *, force: bool = False) -> WeeklyRunResult:
        monday, sunday = iso_week_range(iso_year, iso_week)
        await self.service.translate_pending(monday, sunday)
        result = WeeklyRunResult(iso_year, iso_week)
        for customer in self.repo.customers_with_entries(monday, sunday):
            if not force and self.repo.report_already_sent(customer.id, iso_year, iso_week):
                result.results.append(CustomerResult(customer.name, "skipped", "bereits versendet"))
                continue
            report = build_week_report(self.repo, customer.id, iso_year, iso_week)
            file = render_report(report)
            hint = f"{report.untranslated} Tätigkeit(en) nicht übersetzt" if report.untranslated else ""
            if not self.recipient:
                result.results.append(CustomerResult(customer.name, "failed", "ZEITFLOW_REPORT_EMAIL fehlt", file))
                continue
            try:
                await self.mailer.send(
                    self.recipient,
                    subject=f"Stundennachweis {customer.name} – KW {iso_week:02d}/{iso_year}",
                    body=self._body(report),
                    attachments=[file],
                )
            except MailError as exc:
                LOG.error("Versand an %s für %s fehlgeschlagen: %s", self.recipient, customer.name, exc)
                self.repo.log_report_run(customer.id, iso_year, iso_week, self.recipient, "failed", str(exc))
                result.results.append(CustomerResult(customer.name, "failed", str(exc), file))
                continue
            self.repo.log_report_run(customer.id, iso_year, iso_week, self.recipient, "sent")
            result.results.append(CustomerResult(customer.name, "sent", hint, file))
        return result

    def _body(self, report: WeekReport) -> str:
        hours = f"{report.total_minutes / 60:.2f}".replace(".", ",")
        notes = []
        if report.travel_count is None:
            notes.append("Das Feld „Anfahrten“ ist noch nachzutragen.")
        if report.untranslated:
            notes.append(f"{report.untranslated} Tätigkeit(en) konnten nicht übersetzt werden (markiert).")
        sender = f"\n\n{self.company}" if self.company else ""
        return (
            f"Guten Tag,\n\nanbei der Montage-Stundennachweis für {report.customer_name}, "
            f"KW {report.iso_week:02d}/{report.iso_year} ({hours} Std.).\n"
            + "".join(f"{n}\n" for n in notes)
            + f"\nDiese E-Mail wurde automatisch von ZeitFlow erstellt.{sender}"
        )
