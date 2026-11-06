"""Wöchentlicher Versand: pro Kunde ein Stundennachweis an die zentrale Abrechnungsadresse."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from zeitflow.export import RenderedFile, render_report
from zeitflow.mailer import Mailer, MailError
from zeitflow.repository import Repository, utc_stamp
from zeitflow.services import DayBlock, ReportRow, TimesheetService, WeekReport, build_week_report
from zeitflow.timeutil import iso_week_range

LOG = logging.getLogger(__name__)

# Wie weit zurück der Sonntagslauf nie versendete Wochen und offene Übersetzungen sucht (≥ Korrekturfenster Büro).
LOOKBACK_DAYS = 70
TRANSLATION_BUDGET_SECONDS = 180


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

    def pending_weeks(self) -> list[tuple[int, int]]:
        """Alle KWs, die der Sonntagslauf bearbeiten muss.

        Laufende KW; bereits versendete KWs mit späteren Änderungen oder Fehlversand; vergangene KWs mit
        Einträgen, die nie versendet wurden (z. B. am Sonntag noch leer, Nachtrag kam später); KWs mit noch
        offenen Übersetzungen (eine gelungene Übersetzung löst dann den Korrekturversand aus).
        """
        today = self.service.today()
        current = tuple(today.isocalendar())[:2]
        since = today - timedelta(days=LOOKBACK_DAYS)
        weeks = {current, *self.repo.weeks_needing_resend()}
        for customer_id, year, week in self.repo.unsent_changed_weeks():
            monday, sunday = iso_week_range(year, week)
            if (
                (year, week) < current
                and sunday >= since
                and self.repo.list_entries(customer_id=customer_id, date_from=monday, date_to=sunday, limit=1)
            ):
                weeks.add((year, week))
        for e in self.repo.list_entries(date_from=since, untranslated_only=True):
            weeks.add(tuple(e.work_date.isocalendar())[:2])
        return sorted(weeks)

    async def run(self, iso_year: int, iso_week: int, *, force: bool = False) -> WeeklyRunResult:
        monday, sunday = iso_week_range(iso_year, iso_week)
        # Außerhalb der Sperre und mit Zeitlimit: eine langsame Claude API blockiert weder /versenden noch den Versand.
        await self.service.translate_pending(monday, sunday, max_seconds=TRANSLATION_BUDGET_SECONDS)
        # Sonntagsjob und /versenden können gleichzeitig laufen – ohne Sperre ginge eine Woche doppelt raus.
        async with self._lock:
            return await self._run(iso_year, iso_week, force)

    async def _run(self, iso_year: int, iso_week: int, force: bool) -> WeeklyRunResult:
        monday, sunday = iso_week_range(iso_year, iso_week)
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
        file = None
        try:
            report = build_week_report(self.repo, customer_id, iso_year, iso_week)
            file = render_report(report)
            if not self.recipient:
                return CustomerResult(name, "failed", "ZEITFLOW_REPORT_EMAIL fehlt", file)
            subject = f"Stundennachweis {name} – KW {iso_week:02d}/{iso_year}" + (" (korrigiert)" if correction else "")
            await self.mailer.send(
                self.recipient, subject=subject, body=self._body(report, correction), attachments=[file]
            )
        except Exception as exc:  # noqa: BLE001 – ein Kunde darf den Versand der anderen nie verhindern
            if isinstance(exc, MailError):
                LOG.error("Versand an %s für %s fehlgeschlagen: %s", self.recipient, name, exc)
            else:
                LOG.exception("Stundennachweis für %s KW %s/%s fehlgeschlagen", name, iso_week, iso_year)
            self.repo.log_report_run(customer_id, iso_year, iso_week, self.recipient, "failed", str(exc), snapshot)
            return CustomerResult(name, "failed", str(exc) or type(exc).__name__, file)
        label = "Korrektur" if correction else ""
        self.repo.log_report_run(customer_id, iso_year, iso_week, self.recipient, "sent", None, snapshot)
        hints = [label] if label else []
        if report.untranslated:
            hints.append(f"{report.untranslated} Tätigkeit(en) nicht übersetzt")
        return CustomerResult(name, "sent", ", ".join(hints), file)

    async def send_test(self, recipient: str) -> str:
        """Testmail mit Beispiel-Stundennachweis – wird NICHT protokolliert, beeinflusst also keinen echten Versand.

        Nimmt die aktuelle KW des ersten Kunden mit Einträgen, sonst eine Beispielwoche. Wirft MailError.
        """
        iso = self.service.today().isocalendar()
        monday, sunday = iso_week_range(iso.year, iso.week)
        customers = self.repo.customers_with_entries(monday, sunday)
        if customers:
            report = build_week_report(self.repo, customers[0].id, iso.year, iso.week)
        else:
            report = _demo_report(monday, iso.year, iso.week)
        file = render_report(report)
        await self.mailer.send(
            recipient,
            subject=f"TEST – Stundennachweis {report.customer_name} – KW {iso.week:02d}/{iso.year}",
            body="Dies ist eine Testmail von ZeitFlow. Sie wurde nicht als Versand verbucht.\n\n"
            + self._body(report, correction=False),
            attachments=[file],
        )
        return report.customer_name

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


def _demo_report(monday, iso_year: int, iso_week: int) -> WeekReport:
    row = ReportRow("M. Mustermann", "Beispiel: Montage Rohrleitung DN50", "12345", "07:00", 30, "15:30", 480)
    days = [DayBlock(monday + timedelta(days=i), [row] if i == 0 else []) for i in range(7)]
    return WeekReport(0, "Beispielkunde (Test)", ["Beispielprojekt"], iso_year, iso_week, days, None)
