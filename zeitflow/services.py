"""Fachlogik: Erfassung, Korrektur, Übersetzung und Aufbau des Wochen-Stundennachweises."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from zeitflow.models import Entry, Role, User
from zeitflow.repository import Repository
from zeitflow.timeutil import iso_week_range, short_name, span, spans_overlap, to_minutes, work_minutes
from zeitflow.translation import Translator

UNTRANSLATED_PREFIX = "[nicht übersetzt] "
EDIT_WINDOW_DAYS = 14
MAX_PAST_DAYS = 62

LOG = logging.getLogger(__name__)


class ValidationError(ValueError):
    """code: maschinenlesbarer Grund, z. B. für die passende Bot-Meldung."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Overlap:
    user_name: str
    existing: Entry


class OverlapError(ValidationError):
    def __init__(self, overlaps: list[Overlap]):
        super().__init__("overlap", "Zeitraum überschneidet sich mit vorhandenen Einträgen")
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
    activity: str


def _net_minutes(start: str, end: str, break_minutes: int) -> int:
    if to_minutes(end) == to_minutes(start):
        raise ValidationError("end_before_start")
    try:
        return work_minutes(start, end, break_minutes)
    except ValueError as exc:
        raise ValidationError("bad_break", str(exc)) from exc


class TimesheetService:
    def __init__(self, repo: Repository, translator: Translator, today: Callable[[], date] = date.today):
        self.repo = repo
        self.translator = translator
        self.today = today

    # --- Erfassung ----------------------------------------------------------------

    def _check_date(self, day: date, actor: User) -> None:
        today = self.today()
        if day > today:
            raise ValidationError("future_date")
        oldest = today - timedelta(days=MAX_PAST_DAYS if actor.is_admin else EDIT_WINDOW_DAYS)
        if day < oldest:
            raise ValidationError("too_old")

    def record(self, draft: EntryDraft, actor: User) -> RecordResult:
        """Speichert sofort; die Übersetzung folgt separat über translate_entries()."""
        if not draft.user_ids:
            raise ValidationError("no_worker")
        if not actor.can_enter_for_others and set(draft.user_ids) != {actor.id}:
            raise PermissionDenied("Nur Vorarbeiter und Admins dürfen für andere erfassen")
        for uid in draft.user_ids:
            worker = self.repo.get_user(uid)
            if worker is None or not worker.is_active:
                raise ValidationError("inactive_worker")
        project = self.repo.get_project(draft.project_id)
        if project is None or not project.active:
            raise ValidationError("project")
        activity = draft.activity.strip()
        if not activity:
            raise ValidationError("activity")
        self._check_date(draft.work_date, actor)
        minutes = _net_minutes(draft.start_time, draft.end_time, draft.break_minutes)

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
                "activity_original": activity,
                "activity_de": None,
                "source_lang": draft.source_lang,
                "created_by": actor.id,
            }
            for uid in draft.user_ids
        )
        return RecordResult(entry_ids=ids, work_minutes=minutes, activity=activity)

    async def translate_entries(self, entry_ids: list[int], activity: str, source_lang: str) -> str | None:
        """Übersetzt einen Text einmal für alle Einträge. Wurde der Text inzwischen geändert, bleibt er unberührt."""
        try:
            result = await self.translator.to_german(activity, source_lang)
        except Exception:  # noqa: BLE001 – Übersetzung ist nachholbar, darf nie eine Erfassung stören
            LOG.exception("Übersetzung fehlgeschlagen")
            return None
        if result is None:
            return None
        updated = self.repo.set_translation(entry_ids, activity, result)
        # Text wurde inzwischen geändert → veraltete Übersetzung weder speichern noch melden.
        return result if updated else None

    def _overlaps(self, user_id: int, day: date, start: str, end: str, ignore_id: int | None = None) -> list[Overlap]:
        # Vortag und Folgetag mitprüfen – Nachtschichten reichen über Mitternacht.
        new = span(day, start, end)
        nearby = self.repo.list_entries(
            user_id=user_id, date_from=day - timedelta(days=1), date_to=day + timedelta(days=1)
        )
        return [
            Overlap(e.user_name, e)
            for e in nearby
            if e.id != ignore_id and spans_overlap(new, span(e.work_date, e.start_time, e.end_time))
        ]

    # --- Korrektur ----------------------------------------------------------------

    def _may_edit(self, actor: User, entry: Entry) -> bool:
        if actor.is_admin or entry.user_id == actor.id:
            return True
        if actor.role is Role.FOREMAN:
            owner = self.repo.get_user(entry.user_id)
            return entry.created_by == actor.id or (owner is not None and owner.role is Role.EMPLOYEE)
        return False

    def editable_entries(self, actor: User, user_id: int | None = None, limit: int | None = 60) -> list[Entry]:
        """Korrigierbare Einträge im Zeitfenster; Monteure sehen nur eigene, sonst optional auf user_id gefiltert."""
        days = MAX_PAST_DAYS if actor.is_admin else EDIT_WINDOW_DAYS
        if not actor.can_enter_for_others:
            user_id = actor.id
        candidates = self.repo.list_entries(
            user_id=user_id, date_from=self.today() - timedelta(days=days), newest_first=True
        )
        editable = [e for e in candidates if self._may_edit(actor, e)]
        return editable[:limit] if limit else editable

    def editable(self, entry_id: int, actor: User) -> Entry:
        entry = self.repo.get_entry(entry_id)
        if entry is None:
            raise ValidationError("not_found")
        if not self._may_edit(actor, entry):
            raise PermissionDenied("Kein Zugriff auf diesen Eintrag")
        self._check_date(entry.work_date, actor)
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
        entry = self.editable(entry_id, actor)
        start = start_time or entry.start_time
        end = end_time or entry.end_time
        brk = entry.break_minutes if break_minutes is None else break_minutes
        day = work_date or entry.work_date
        self._check_date(day, actor)
        minutes = _net_minutes(start, end, brk)
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
        entry = self.editable(entry_id, actor)
        project = self.repo.get_project(project_id)
        if project is None or not project.active:
            raise ValidationError("project")
        self.repo.update_entry(entry.id, actor.id, project_id=project_id)
        return self.repo.get_entry(entry.id)

    def change_activity(self, entry_id: int, actor: User, text: str, source_lang: str) -> Entry:
        """Setzt den neuen Text; die Übersetzung folgt über translate_entries()."""
        entry = self.editable(entry_id, actor)
        text = text.strip()
        if not text:
            raise ValidationError("activity")
        self.repo.update_entry(entry.id, actor.id, activity_original=text, activity_de=None, source_lang=source_lang)
        return self.repo.get_entry(entry.id)

    def delete(self, entry_id: int, actor: User) -> Entry:
        entry = self.editable(entry_id, actor)
        self.repo.delete_entry(entry.id)
        return entry

    # --- Übersetzungen nachholen --------------------------------------------------

    async def translate_pending(
        self, date_from: date, date_to: date, customer_id: int | None = None, max_seconds: float | None = None
    ) -> int:
        """Übersetzt noch offene Tätigkeiten; gleiche Texte nur einmal. Gibt Anzahl offener Einträge danach zurück.

        max_seconds begrenzt die Gesamtdauer – bei API-Ausfall wird der Rest beim nächsten Lauf nachgeholt.
        """
        deadline = time.monotonic() + max_seconds if max_seconds is not None else None
        pending = self.repo.list_entries(
            customer_id=customer_id, date_from=date_from, date_to=date_to, untranslated_only=True
        )
        groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for e in pending:
            groups[(e.activity_original, e.source_lang)].append(e.id)
        remaining = 0
        for (text, lang), ids in groups.items():
            if deadline is not None and time.monotonic() > deadline:
                remaining += len(ids)
                continue
            if await self.translate_entries(ids, text, lang) is None:
                remaining += len(ids)
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
        raise ValidationError("not_found", "Kunde nicht gefunden")
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
