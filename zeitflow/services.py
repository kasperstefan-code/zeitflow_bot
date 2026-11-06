"""Fachlogik: Erfassung, Korrektur, Übersetzung und Aufbau des Wochen-Stundennachweises."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from zeitflow.models import Entry, User
from zeitflow.repository import Repository
from zeitflow.timeutil import intervals_overlap, iso_week_range, short_name, work_minutes
from zeitflow.translation import Translator

UNTRANSLATED_PREFIX = "[nicht übersetzt] "


class ValidationError(ValueError):
    pass


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Overlap:
    user_name: str
    existing: Entry


class OverlapError(ValidationError):
    def __init__(self, overlaps: list[Overlap]):
        super().__init__("Zeitraum überschneidet sich mit vorhandenen Einträgen")
        self.overlaps = overlaps


@dataclass(frozen=True)
class EntryDraft:
    user_ids: tuple[int, ...]
    project_id: int
    work_date: date
    start_time: str
    end_time: str
    break_minutes: int
    activity: str
    source_lang: str


@dataclass(frozen=True)
class RecordResult:
    entry_ids: list[int]
    work_minutes: int
    activity_de: str | None


class TimesheetService:
    def __init__(self, repo: Repository, translator: Translator):
        self.repo = repo
        self.translator = translator

    # --- Erfassung ----------------------------------------------------------------

    async def record(self, draft: EntryDraft, actor: User) -> RecordResult:
        if not draft.user_ids:
            raise ValidationError("Kein Monteur ausgewählt")
        if not actor.can_enter_for_others and set(draft.user_ids) != {actor.id}:
            raise PermissionDenied("Nur Vorarbeiter und Admins dürfen für andere erfassen")
        project = self.repo.get_project(draft.project_id)
        if project is None or not project.active:
            raise ValidationError("Projekt nicht gefunden oder inaktiv")
        if not draft.activity.strip():
            raise ValidationError("Tätigkeit fehlt")
        try:
            minutes = work_minutes(draft.start_time, draft.end_time, draft.break_minutes)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        overlaps = []
        for uid in draft.user_ids:
            overlaps.extend(self._overlaps(uid, draft.work_date, draft.start_time, draft.end_time))
        if overlaps:
            raise OverlapError(overlaps)

        ids = self.repo.insert_entries(
            {
                "user_id": uid,
                "project_id": draft.project_id,
                "work_date": draft.work_date,
                "start_time": draft.start_time,
                "end_time": draft.end_time,
                "break_minutes": draft.break_minutes,
                "work_minutes": minutes,
                "activity_original": draft.activity.strip(),
                "activity_de": None,
                "source_lang": draft.source_lang,
                "created_by": actor.id,
            }
            for uid in draft.user_ids
        )
        # Erst speichern, dann übersetzen: ein API-Ausfall darf keine Erfassung verlieren.
        activity_de = await self.translator.to_german(draft.activity, draft.source_lang)
        if activity_de is not None:
            self.repo.set_translation(ids, activity_de)
        return RecordResult(entry_ids=ids, work_minutes=minutes, activity_de=activity_de)

    def _overlaps(self, user_id: int, day: date, start: str, end: str, ignore_id: int | None = None) -> list[Overlap]:
        return [
            Overlap(e.user_name, e)
            for e in self.repo.list_entries(user_id=user_id, date_from=day, date_to=day)
            if e.id != ignore_id and intervals_overlap(start, end, e.start_time, e.end_time)
        ]

    # --- Korrektur ----------------------------------------------------------------

    def editable_entries(self, actor: User, today: date, days: int = 14, limit: int = 30) -> list[Entry]:
        user_id = None if actor.can_enter_for_others else actor.id
        return self.repo.list_entries(
            user_id=user_id, date_from=today - timedelta(days=days), limit=limit, newest_first=True
        )

    def _editable(self, entry_id: int, actor: User) -> Entry:
        entry = self.repo.get_entry(entry_id)
        if entry is None:
            raise ValidationError("Eintrag nicht gefunden")
        if entry.user_id != actor.id and not actor.can_enter_for_others:
            raise PermissionDenied("Kein Zugriff auf diesen Eintrag")
        return entry

    def change_times(
        self,
        entry_id: int,
        actor: User,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        break_minutes: int | None = None,
        work_date: date | None = None,
    ) -> Entry:
        entry = self._editable(entry_id, actor)
        start = start_time or entry.start_time
        end = end_time or entry.end_time
        brk = entry.break_minutes if break_minutes is None else break_minutes
        day = work_date or entry.work_date
        try:
            minutes = work_minutes(start, end, brk)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        overlaps = self._overlaps(entry.user_id, day, start, end, ignore_id=entry.id)
        if overlaps:
            raise OverlapError(overlaps)
        self.repo.update_entry(
            entry.id,
            actor.id,
            work_date=day,
            start_time=start,
            end_time=end,
            break_minutes=brk,
            work_minutes=minutes,
        )
        return self.repo.get_entry(entry.id)

    def change_project(self, entry_id: int, actor: User, project_id: int) -> Entry:
        entry = self._editable(entry_id, actor)
        project = self.repo.get_project(project_id)
        if project is None or not project.active:
            raise ValidationError("Projekt nicht gefunden oder inaktiv")
        self.repo.update_entry(entry.id, actor.id, project_id=project_id)
        return self.repo.get_entry(entry.id)

    async def change_activity(self, entry_id: int, actor: User, text: str, source_lang: str) -> Entry:
        entry = self._editable(entry_id, actor)
        if not text.strip():
            raise ValidationError("Tätigkeit fehlt")
        activity_de = await self.translator.to_german(text, source_lang)
        self.repo.update_entry(
            entry.id, actor.id, activity_original=text.strip(), activity_de=activity_de, source_lang=source_lang
        )
        return self.repo.get_entry(entry.id)

    def delete(self, entry_id: int, actor: User) -> Entry:
        entry = self._editable(entry_id, actor)
        self.repo.delete_entry(entry.id)
        return entry

    # --- Übersetzungen nachholen --------------------------------------------------

    async def translate_pending(self, date_from: date, date_to: date, customer_id: int | None = None) -> int:
        """Übersetzt noch offene Tätigkeiten; gleiche Texte nur einmal. Gibt Anzahl offener Einträge danach zurück."""
        pending = self.repo.list_entries(
            customer_id=customer_id, date_from=date_from, date_to=date_to, untranslated_only=True
        )
        groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for e in pending:
            groups[(e.activity_original, e.source_lang)].append(e.id)
        remaining = 0
        for (text, lang), ids in groups.items():
            result = await self.translator.to_german(text, lang)
            if result is None:
                remaining += len(ids)
            else:
                self.repo.set_translation(ids, result)
        return remaining


# --- Wochen-Stundennachweis -------------------------------------------------------


@dataclass(frozen=True)
class ReportRow:
    monteur: str
    activity: str
    cost_center: str
    start_time: str
    break_minutes: int
    end_time: str
    work_minutes: int


@dataclass(frozen=True)
class DayBlock:
    day: date
    rows: list[ReportRow]


@dataclass(frozen=True)
class WeekReport:
    customer_id: int
    customer_name: str
    project_names: list[str]
    iso_year: int
    iso_week: int
    days: list[DayBlock]
    travel_count: int | None
    untranslated: int = field(default=0)

    @property
    def total_minutes(self) -> int:
        return sum(r.work_minutes for d in self.days for r in d.rows)

    @property
    def is_empty(self) -> bool:
        return not any(d.rows for d in self.days)


def _activity_text(entry: Entry) -> str:
    if entry.activity_de is not None:
        return entry.activity_de
    return UNTRANSLATED_PREFIX + entry.activity_original


def build_week_report(repo: Repository, customer_id: int, iso_year: int, iso_week: int) -> WeekReport:
    customer = repo.get_customer(customer_id)
    if customer is None:
        raise ValidationError("Kunde nicht gefunden")
    monday, sunday = iso_week_range(iso_year, iso_week)
    entries = repo.list_entries(customer_id=customer_id, date_from=monday, date_to=sunday)

    by_day: dict[date, list[ReportRow]] = defaultdict(list)
    project_names: list[str] = []
    untranslated = 0
    for e in sorted(entries, key=lambda x: (x.work_date, x.start_time, x.user_name)):
        if e.project_name not in project_names:
            project_names.append(e.project_name)
        if e.activity_de is None:
            untranslated += 1
        by_day[e.work_date].append(
            ReportRow(
                monteur=short_name(e.user_name),
                activity=_activity_text(e),
                cost_center=e.cost_center,
                start_time=e.start_time,
                break_minutes=e.break_minutes,
                end_time=e.end_time,
                work_minutes=e.work_minutes,
            )
        )

    days = [DayBlock(day=monday + timedelta(days=i), rows=by_day.get(monday + timedelta(days=i), [])) for i in range(7)]
    return WeekReport(
        customer_id=customer.id,
        customer_name=customer.name,
        project_names=project_names,
        iso_year=iso_year,
        iso_week=iso_week,
        days=days,
        travel_count=repo.get_travel_count(customer_id, iso_year, iso_week),
        untranslated=untranslated,
    )
