from datetime import date, timedelta

import pytest

from tests.conftest import TODAY, FakeTranslator, draft, make_service
from zeitflow.models import Role, UserStatus
from zeitflow.services import (
    UNTRANSLATED_PREFIX,
    OverlapError,
    PermissionDenied,
    ValidationError,
    build_week_report,
)


async def test_crew_entry_creates_one_row_per_worker_and_translates_once(service, translator, world, repo):
    ids = [world["foreman"].id, world["w1"].id, world["w2"].id]
    result = service.record(draft(ids, world["p1"].id), world["foreman"])

    assert len(result.entry_ids) == 3
    assert result.work_minutes == 600
    assert all(e.activity_de is None for e in repo.list_entries()), "Speichern wartet nicht auf die Übersetzung"

    assert await service.translate_entries(result.entry_ids, result.activity, "ru") == "DE:Монтаж трубопровода"
    assert translator.calls == [("Монтаж трубопровода", "ru")]
    entries = repo.list_entries()
    assert {e.user_id for e in entries} == set(ids)
    assert all(e.activity_de == "DE:Монтаж трубопровода" for e in entries)
    assert all(e.created_by == world["foreman"].id for e in entries)


async def test_employee_cannot_enter_for_others(service, world):
    with pytest.raises(PermissionDenied):
        service.record(draft([world["w1"].id, world["w2"].id], world["p1"].id), world["w1"])


async def test_employee_can_enter_for_self(service, world):
    assert len(service.record(draft([world["w1"].id], world["p1"].id), world["w1"]).entry_ids) == 1


async def test_inactive_or_pending_workers_are_rejected(service, world, repo):
    pending = repo.create_user(99, "Neu Ling", "de", Role.EMPLOYEE, UserStatus.PENDING)
    with pytest.raises(ValidationError) as exc:
        service.record(draft([world["foreman"].id, pending.id], world["p1"].id), world["foreman"])
    assert exc.value.code == "inactive_worker"
    assert repo.list_entries() == []


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"start": "10:00", "end": "09:00"}, "end_before_start"),
        ({"brk": 700}, "bad_break"),
        ({"activity": "   "}, "activity"),
        ({"day": TODAY + timedelta(days=1)}, "future_date"),
        ({"day": TODAY - timedelta(days=15)}, "too_old"),
    ],
)
async def test_invalid_drafts_are_rejected(service, world, kwargs, code):
    with pytest.raises(ValidationError) as exc:
        service.record(draft([world["w1"].id], world["p1"].id, **kwargs), world["w1"])
    assert exc.value.code == code


async def test_admin_may_record_further_back(service, world):
    service.record(draft([world["w1"].id], world["p1"].id, day=TODAY - timedelta(days=40)), world["admin"])
    with pytest.raises(ValidationError):
        service.record(draft([world["w1"].id], world["p1"].id, day=TODAY - timedelta(days=63)), world["admin"])


async def test_inactive_project_is_rejected(service, world, repo):
    repo.update_project(world["p1"].id, active=0)
    with pytest.raises(ValidationError):
        service.record(draft([world["w1"].id], world["p1"].id), world["w1"])


async def test_overlap_is_rejected_but_same_day_other_customer_is_fine(service, world, repo):
    w1 = world["w1"]
    service.record(draft([w1.id], world["p1"].id, start="06:00", end="12:00", brk=0), w1)
    service.record(draft([w1.id], world["p3"].id, start="12:00", end="16:00", brk=0), w1)

    with pytest.raises(OverlapError) as exc:
        service.record(
            draft([world["foreman"].id, w1.id], world["p2"].id, start="11:00", end="13:00", brk=0), world["foreman"]
        )
    assert {o.user_name for o in exc.value.overlaps} == {w1.name}
    assert len(repo.list_entries()) == 2, "Bei Überschneidung wird für niemanden gespeichert"


async def test_translation_failure_keeps_entry_and_can_be_retried(repo, world):
    failing = FakeTranslator(fail=True)
    service = make_service(repo, failing)
    result = service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    assert await service.translate_entries(result.entry_ids, result.activity, "ru") is None

    monday, sunday = date(2026, 3, 23), date(2026, 3, 29)
    assert await service.translate_pending(monday, sunday) == 1
    failing.fail = False
    assert await service.translate_pending(monday, sunday) == 0
    assert repo.list_entries()[0].activity_de == "DE:Монтаж трубопровода"


async def test_crashing_translator_never_breaks_saving(repo, world):
    class Boom:
        async def to_german(self, text, lang):
            raise RuntimeError("unerwartet")

    service = make_service(repo, Boom())
    result = service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    assert await service.translate_entries(result.entry_ids, result.activity, "ru") is None
    assert await service.translate_pending(date(2026, 3, 23), date(2026, 3, 29)) == 1


async def test_stale_translation_does_not_overwrite_newer_text(service, world, repo):
    """Befund 11: Übersetzung des alten Textes überschreibt keine inzwischen geänderte Tätigkeit."""
    entry_id = service.record(draft([world["w2"].id], world["p1"].id, lang="pl"), world["w2"]).entry_ids[0]
    service.change_activity(entry_id, world["w2"], "Izolacja rur", "pl")
    await service.translate_entries([entry_id], "Монтаж трубопровода", "ru")
    e = repo.get_entry(entry_id)
    assert (e.activity_original, e.activity_de) == ("Izolacja rur", None)
    await service.translate_entries([entry_id], "Izolacja rur", "pl")
    assert repo.get_entry(entry_id).activity_de == "DE:Izolacja rur"


async def test_change_times_recomputes_and_checks_overlap(service, world):
    w1 = world["w1"]
    first = service.record(draft([w1.id], world["p1"].id, start="06:00", end="10:00", brk=0), w1).entry_ids[0]
    service.record(draft([w1.id], world["p1"].id, start="12:00", end="16:00", brk=0), w1)

    updated = service.change_times(first, w1, end_time="11:00", break_minutes=30)
    assert updated.work_minutes == 270
    assert updated.end_time == "11:00"

    with pytest.raises(OverlapError):
        service.change_times(first, w1, end_time="13:00")
    with pytest.raises(ValidationError) as exc:
        service.change_times(first, w1, break_minutes=600)
    assert exc.value.code == "bad_break"


async def test_permissions_on_existing_entries(service, world):
    entry_id = service.record(draft([world["w1"].id], world["p1"].id), world["w1"]).entry_ids[0]
    with pytest.raises(PermissionDenied):
        service.delete(entry_id, world["w2"])
    service.change_project(entry_id, world["foreman"], world["p2"].id)
    assert service.delete(entry_id, world["foreman"]).id == entry_id


async def test_foreman_cannot_touch_admin_or_other_foreman_entries(service, world, repo):
    other_foreman = repo.create_user(77, "Olga Brigadir", "ru", Role.FOREMAN, UserStatus.ACTIVE)
    admin_entry = service.record(draft([world["admin"].id], world["p1"].id), world["admin"]).entry_ids[0]
    foreman_entry = service.record(draft([other_foreman.id], world["p3"].id), other_foreman).entry_ids[0]
    for entry_id in (admin_entry, foreman_entry):
        with pytest.raises(PermissionDenied):
            service.delete(entry_id, world["foreman"])
    assert {e.id for e in service.editable_entries(world["foreman"])}.isdisjoint({admin_entry, foreman_entry})


async def test_edit_window_is_enforced_server_side(repo, world):
    """Befund 10: Ein manipulierter Button darf keine alten (bereits abgerechneten) Einträge ändern."""
    service = make_service(repo, FakeTranslator())
    old_day = TODAY - timedelta(days=13)
    entry_id = service.record(draft([world["w1"].id], world["p1"].id, day=old_day), world["w1"]).entry_ids[0]

    later = make_service(repo, FakeTranslator())
    later.today = lambda: TODAY + timedelta(days=5)
    with pytest.raises(ValidationError) as exc:
        later.delete(entry_id, world["w1"])
    assert exc.value.code == "too_old"
    later.change_times(entry_id, world["admin"], start_time="07:00")  # Büro darf weiter zurück
    with pytest.raises(ValidationError):
        later.change_times(entry_id, world["admin"], work_date=TODAY + timedelta(days=6))


async def test_change_activity_resets_translation(service, world, repo):
    entry_id = service.record(draft([world["w2"].id], world["p1"].id, lang="pl"), world["w2"]).entry_ids[0]
    await service.translate_entries([entry_id], "Монтаж трубопровода", "pl")
    service.change_activity(entry_id, world["w2"], "  Izolacja rur ", "pl")
    e = repo.get_entry(entry_id)
    assert (e.activity_original, e.activity_de, e.source_lang) == ("Izolacja rur", None, "pl")


async def test_editable_entries_scoped_by_role(service, world):
    service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    service.record(draft([world["w2"].id], world["p1"].id), world["w2"])
    assert len(service.editable_entries(world["w1"])) == 1
    assert len(service.editable_entries(world["foreman"])) == 2


async def test_week_report_groups_by_day_and_customer(repo, world):
    service = make_service(repo, FakeTranslator())
    crew = [world["foreman"].id, world["w1"].id]
    service.record(draft(crew, world["p1"].id, day=date(2026, 3, 24)), world["foreman"])
    service.record(
        draft(
            [world["w2"].id],
            world["p2"].id,
            day=date(2026, 3, 25),
            start="08:00",
            end="16:00",
            brk=30,
            activity="Dämmung",
            lang="de",
        ),
        world["foreman"],
    )
    service.record(draft([world["w2"].id], world["p3"].id, day=date(2026, 3, 24)), world["foreman"])
    service.record(draft([world["w1"].id], world["p1"].id, day=date(2026, 3, 16)), world["admin"])
    await service.translate_pending(date(2026, 3, 1), date(2026, 3, 31))
    repo.set_travel_count(world["smf"].id, 2026, 13, 3)

    report = build_week_report(repo, world["smf"].id, 2026, 13)

    assert report.customer_name == "Schwarzwaldmilch GmbH Freiburg"
    assert report.project_names == ["Montage Dosieranlage", "Rückbau Hamba-A"]
    assert report.days[0].day == date(2026, 3, 23)
    assert len(report.days) == 7
    tuesday = report.days[1]
    assert [r.monteur for r in tuesday.rows] == ["A. Novikau", "S. Tyschenko"]
    assert all(r.cost_center == "23670" for r in tuesday.rows)
    wednesday = report.days[2]
    assert wednesday.rows[0].cost_center == "153201"
    assert wednesday.rows[0].work_minutes == 450
    assert report.total_minutes == 600 * 2 + 450
    assert report.travel_count == 3
    assert report.untranslated == 0


async def test_week_report_marks_untranslated(repo, world):
    service = make_service(repo, FakeTranslator(fail=True))
    service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    report = build_week_report(repo, world["smf"].id, 2026, 13)
    assert report.untranslated == 1
    assert report.days[1].rows[0].activity == UNTRANSLATED_PREFIX + "Монтаж трубопровода"


async def test_stale_translation_is_not_reported_back(service, world):
    entry_id = service.record(draft([world["w2"].id], world["p1"].id, lang="pl"), world["w2"]).entry_ids[0]
    service.change_activity(entry_id, world["w2"], "Izolacja rur", "pl")
    assert await service.translate_entries([entry_id], "Монтаж трубопровода", "ru") is None
