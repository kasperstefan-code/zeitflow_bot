"""Wöchentlicher Versand: pro Kunde ein Stundennachweis an die zentrale Abrechnungsadresse."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from zeitflow.export import RenderedFile, render_report
from zeitflow.mailer import Mailer, MailError
from zeitflow.repository import Repository, utc_stamp
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

    @property
    def needs_attention(self) -> bool:
        """True, wenn versendet wurde oder etwas schiefging – reine "bereits versendet"-Läufe sind still."""
        return any(r.status != "skipped" for r in self.results)

    @property
    def failed(self) -> list[CustomerResult]:
        return [r for r in self.results if r.status == "failed"]


class WeeklyReporter:
    """Versendet je Kunde und KW: erstmalig, oder erneut als Korrektur, wenn sich die Woche nach dem Versand
    geändert hat (Nachträge, Korrekturen, Löschungen, Anfahrten, nachgeholte Übersetzungen)."""

    def __init__(self, repo: Repository, service: TimesheetService, mailer: Mailer, recipient: str, company: str = ""):
        self.repo = repo
        self.service = service
        self.mailer = mailer
        self.recipient = recipient
        self.company = company
        self._lock = asyncio.Lock()

    async def run(self, iso_year: int, iso_week: int, *, force: bool = False) -> WeeklyRunResult:
        # Sonntagsjob und /versenden können gleichzeitig laufen – ohne Sperre ginge eine Woche doppelt raus.
        async with self._lock:
            return await self._run(iso_year, iso_week, force)

    async def _run(self, iso_year: int, iso_week: int, force: bool) -> WeeklyRunResult:
        monday, sunday = iso_week_range(iso_year, iso_week)
        await self.service.translate_pending(monday, sunday)
        result = WeeklyRunResult(iso_year, iso_week)
        candidates = {c.id: c for c in self.repo.customers_with_entries(monday, sunday)}
        candidates.update({c.id: c for c in self.repo.customers_sent_for_week(iso_year, iso_week)})
        for customer in sorted(candidates.values(), key=lambda c: c.name):
            last_sent = self.repo.last_sent_at(customer.id, iso_year, iso_week)
            changed = self.repo.week_changed_at(customer.id, iso_year, iso_week)
            is_correction = last_sent is not None
            if is_correction and not force and not (changed and changed > last_sent):
                result.results.append(CustomerResult(customer.name, "skipped", "bereits versendet"))
                continue
            result.results.append(await self._send(customer.id, customer.name, iso_year, iso_week, is_correction))
        return result

    async def _send(
        self, customer_id: int, name: str, iso_year: int, iso_week: int, correction: bool
    ) -> CustomerResult:
        snapshot = utc_stamp()  # Änderungen ab hier lösen den nächsten Korrekturversand aus
        report = build_week_report(self.repo, customer_id, iso_year, iso_week)
        file = render_report(report)
        label = "Korrektur" if correction else ""
        if not self.recipient:
            return CustomerResult(name, "failed", "ZEITFLOW_REPORT_EMAIL fehlt", file)
        subject = f"Stundennachweis {name} – KW {iso_week:02d}/{iso_year}" + (" (korrigiert)" if correction else "")
        try:
            await self.mailer.send(
                self.recipient, subject=subject, body=self._body(report, correction), attachments=[file]
            )
        except MailError as exc:
            LOG.error("Versand an %s für %s fehlgeschlagen: %s", self.recipient, name, exc)
            self.repo.log_report_run(customer_id, iso_year, iso_week, self.recipient, "failed", str(exc), snapshot)
            return CustomerResult(name, "failed", str(exc), file)
        self.repo.log_report_run(customer_id, iso_year, iso_week, self.recipient, "sent", None, snapshot)
        hints = [label] if label else []
        if report.untranslated:
            hints.append(f"{report.untranslated} Tätigkeit(en) nicht übersetzt")
        return CustomerResult(name, "sent", ", ".join(hints), file)

    def _body(self, report: WeekReport, correction: bool) -> str:
        hours = f"{report.total_minutes / 60:.2f}".replace(".", ",")
        notes = []
        if correction:
            notes.append("Dies ist eine korrigierte Fassung und ersetzt den zuvor versendeten Stundennachweis.")
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
