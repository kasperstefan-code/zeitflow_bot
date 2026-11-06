from datetime import date

import pytest

from tests.conftest import FakeTranslator, draft
from zeitflow.services import (
    UNTRANSLATED_PREFIX,
    OverlapError,
    PermissionDenied,
    TimesheetService,
    ValidationError,
    build_week_report,
)


async def test_crew_entry_creates_one_row_per_worker_and_translates_once(service, translator, world, repo):
    ids = [world["foreman"].id, world["w1"].id, world["w2"].id]
    result = await service.record(draft(ids, world["p1"].id), world["foreman"])

    assert len(result.entry_ids) == 3
    assert result.work_minutes == 600
    assert translator.calls == [("Монтаж трубопровода", "ru")]
    entries = repo.list_entries()
    assert {e.user_id for e in entries} == set(ids)
    assert all(e.activity_de == "DE:Монтаж трубопровода" for e in entries)
    assert all(e.created_by == world["foreman"].id for e in entries)


async def test_employee_cannot_enter_for_others(service, world):
    with pytest.raises(PermissionDenied):
        await service.record(draft([world["w1"].id, world["w2"].id], world["p1"].id), world["w1"])


async def test_employee_can_enter_for_self(service, world):
    result = await service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    assert len(result.entry_ids) == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"start": "10:00", "end": "09:00"}, {"brk": 700}, {"activity": "   "}],
)
async def test_invalid_drafts_are_rejected(service, world, kwargs):
    with pytest.raises(ValidationError):
        await service.record(draft([world["w1"].id], world["p1"].id, **kwargs), world["w1"])


async def test_inactive_project_is_rejected(service, world, repo):
    repo.update_project(world["p1"].id, active=0)
    with pytest.raises(ValidationError):
        await service.record(draft([world["w1"].id], world["p1"].id), world["w1"])


async def test_overlap_is_rejected_but_same_day_other_customer_is_fine(service, world, repo):
    w1 = world["w1"]
    await service.record(draft([w1.id], world["p1"].id, start="06:00", end="12:00", brk=0), w1)
    await service.record(draft([w1.id], world["p3"].id, start="12:00", end="16:00", brk=0), w1)

    with pytest.raises(OverlapError) as exc:
        await service.record(
            draft([world["foreman"].id, w1.id], world["p2"].id, start="11:00", end="13:00", brk=0), world["foreman"]
        )
    assert {o.user_name for o in exc.value.overlaps} == {w1.name}
    assert len(repo.list_entries()) == 2, "Bei Überschneidung wird für niemanden gespeichert"


async def test_translation_failure_keeps_entry_and_can_be_retried(repo, world):
    failing = FakeTranslator(fail=True)
    service = TimesheetService(repo, failing)
    result = await service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    assert result.activity_de is None
    assert repo.list_entries()[0].activity_de is None

    remaining = await service.translate_pending(date(2026, 3, 23), date(2026, 3, 29))
    assert remaining == 1

    failing.fail = False
    assert await service.translate_pending(date(2026, 3, 23), date(2026, 3, 29)) == 0
    assert repo.list_entries()[0].activity_de == "DE:Монтаж трубопровода"


async def test_change_times_recomputes_and_checks_overlap(service, world, repo):
    w1 = world["w1"]
    first = (await service.record(draft([w1.id], world["p1"].id, start="06:00", end="10:00", brk=0), w1)).entry_ids[0]
    await service.record(draft([w1.id], world["p1"].id, start="12:00", end="16:00", brk=0), w1)

    updated = service.change_times(first, w1, end_time="11:00", break_minutes=30)
    assert updated.work_minutes == 270
    assert updated.end_time == "11:00"

    with pytest.raises(OverlapError):
        service.change_times(first, w1, end_time="13:00")


async def test_permissions_on_existing_entries(service, world):
    entry_id = (await service.record(draft([world["w1"].id], world["p1"].id), world["w1"])).entry_ids[0]
    with pytest.raises(PermissionDenied):
        service.delete(entry_id, world["w2"])
    service.change_project(entry_id, world["foreman"], world["p2"].id)
    deleted = service.delete(entry_id, world["foreman"])
    assert deleted.id == entry_id


async def test_change_activity_retranslates(service, translator, world, repo):
    entry_id = (await service.record(draft([world["w2"].id], world["p1"].id, lang="pl"), world["w2"])).entry_ids[0]
    await service.change_activity(entry_id, world["w2"], "Izolacja rur", "pl")
    e = repo.get_entry(entry_id)
    assert (e.activity_original, e.activity_de, e.source_lang) == ("Izolacja rur", "DE:Izolacja rur", "pl")
    assert e.created_by == world["w2"].id


async def test_editable_entries_scoped_by_role(service, world):
    await service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    await service.record(draft([world["w2"].id], world["p1"].id), world["w2"])
    today = date(2026, 3, 25)
    assert len(service.editable_entries(world["w1"], today)) == 1
    assert len(service.editable_entries(world["foreman"], today)) == 2


async def test_week_report_groups_by_day_and_customer(repo, world):
    service = TimesheetService(repo, FakeTranslator())
    crew = [world["foreman"].id, world["w1"].id]
    await service.record(draft(crew, world["p1"].id, day=date(2026, 3, 24)), world["foreman"])
    await service.record(
        draft(
            [world["w2"].id],
            world["p2"].id,
            day=date(2026, 3, 28),
            start="08:00",
            end="16:00",
            brk=30,
            activity="Dämmung",
            lang="de",
        ),
        world["foreman"],
    )
    await service.record(draft([world["w2"].id], world["p3"].id, day=date(2026, 3, 24)), world["foreman"])
    await service.record(draft([world["w1"].id], world["p1"].id, day=date(2026, 3, 30)), world["foreman"])
    repo.set_travel_count(world["smf"].id, 2026, 13, 3)

    report = build_week_report(repo, world["smf"].id, 2026, 13)

    assert report.customer_name == "Schwarzwaldmilch GmbH Freiburg"
    assert report.project_names == ["Montage Dosieranlage", "Rückbau Hamba-A"]
    assert [d.day for d in report.days][0] == date(2026, 3, 23)
    assert len(report.days) == 7
    tuesday = report.days[1]
    assert [r.monteur for r in tuesday.rows] == ["A. Novikau", "S. Tyschenko"]
    assert all(r.cost_center == "23670" for r in tuesday.rows)
    saturday = report.days[5]
    assert saturday.rows[0].cost_center == "153201"
    assert saturday.rows[0].work_minutes == 450
    assert report.total_minutes == 600 * 2 + 450
    assert report.travel_count == 3
    assert report.untranslated == 0


async def test_week_report_marks_untranslated(repo, world):
    service = TimesheetService(repo, FakeTranslator(fail=True))
    await service.record(draft([world["w1"].id], world["p1"].id), world["w1"])
    report = build_week_report(repo, world["smf"].id, 2026, 13)
    assert report.untranslated == 1
    assert report.days[1].rows[0].activity == UNTRANSLATED_PREFIX + "Монтаж трубопровода"
