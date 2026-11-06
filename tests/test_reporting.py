import asyncio
from datetime import date

from tests.conftest import FakeMailer, FakeTranslator, draft, make_service
from zeitflow.reporting import WeeklyReporter


def seed(repo, world, translator=None):
    service = make_service(repo, translator or FakeTranslator())
    service.record(draft([world["w1"].id], world["p1"].id, day=date(2026, 3, 24)), world["foreman"])
    service.record(draft([world["w2"].id], world["p3"].id, day=date(2026, 3, 25)), world["foreman"])
    return service


async def test_sends_one_mail_per_customer_and_logs(repo, world):
    service = seed(repo, world)
    mailer = FakeMailer()
    reporter = WeeklyReporter(repo, service, mailer, "abrechnung@example.org", "Muster GmbH")

    result = await reporter.run(2026, 13)

    assert [r.status for r in result.results] == ["sent", "sent"]
    assert [m["recipient"] for m in mailer.sent] == ["abrechnung@example.org"] * 2
    assert sorted(m["subject"] for m in mailer.sent) == [
        "Stundennachweis Brauerei Rothaus – KW 13/2026",
        "Stundennachweis Schwarzwaldmilch GmbH Freiburg – KW 13/2026",
    ]
    assert mailer.sent[0]["attachments"][0].filename.endswith("_KW13_2026.xlsx")
    assert "Anfahrten" in mailer.sent[0]["body"] and "Muster GmbH" in mailer.sent[0]["body"]
    assert repo.report_already_sent(world["smf"].id, 2026, 13)
    assert all(e.activity_de for e in repo.list_entries()), "offene Übersetzungen werden vorher nachgeholt"


async def test_unchanged_week_is_not_sent_again_unless_forced(repo, world):
    mailer = FakeMailer()
    reporter = WeeklyReporter(repo, seed(repo, world), mailer, "a@example.org")
    await reporter.run(2026, 13)

    again = await reporter.run(2026, 13)
    assert [r.status for r in again.results] == ["skipped", "skipped"]
    assert not again.needs_attention
    forced = await reporter.run(2026, 13, force=True)
    assert [r.status for r in forced.results] == ["sent", "sent"]
    assert len(mailer.sent) == 4


async def test_late_entry_after_sending_triggers_correction(repo, world):
    """Befund 4: Nachträge nach dem Sonntagsversand gehen als Korrektur raus – nur für den betroffenen Kunden."""
    service = seed(repo, world)
    mailer = FakeMailer()
    reporter = WeeklyReporter(repo, service, mailer, "a@example.org")
    await reporter.run(2026, 13)

    service.record(draft([world["w1"].id], world["p1"].id, day=date(2026, 3, 25)), world["foreman"])
    assert repo.weeks_needing_resend() == [(2026, 13)]

    result = await reporter.run(2026, 13)
    by_customer = {r.customer_name: r for r in result.results}
    assert by_customer["Schwarzwaldmilch GmbH Freiburg"].status == "sent"
    assert "Korrektur" in by_customer["Schwarzwaldmilch GmbH Freiburg"].detail
    assert by_customer["Brauerei Rothaus"].status == "skipped"
    assert mailer.sent[-1]["subject"].endswith("(korrigiert)")
    assert "korrigierte Fassung" in mailer.sent[-1]["body"]
    assert repo.weeks_needing_resend() == []


async def test_corrections_deletions_and_travel_count_trigger_resend(repo, world):
    service = seed(repo, world)
    reporter = WeeklyReporter(repo, service, FakeMailer(), "a@example.org")
    await reporter.run(2026, 13)
    entry = next(e for e in repo.list_entries() if e.customer_id == world["other"].id)

    service.change_times(entry.id, world["foreman"], start_time="07:00")
    assert repo.weeks_needing_resend() == [(2026, 13)]
    await reporter.run(2026, 13)

    repo.set_travel_count(world["smf"].id, 2026, 13, 2)
    assert repo.weeks_needing_resend() == [(2026, 13)]
    await reporter.run(2026, 13)

    service.delete(entry.id, world["foreman"])
    result = await reporter.run(2026, 13)
    rothaus = next(r for r in result.results if r.customer_name == "Brauerei Rothaus")
    assert rothaus.status == "sent", "Kunde ohne Einträge bekommt die Korrektur (leerer Nachweis)"


async def test_moving_entry_to_other_week_marks_both_weeks(repo, world):
    service = seed(repo, world)
    reporter = WeeklyReporter(repo, service, FakeMailer(), "a@example.org")
    await reporter.run(2026, 12)
    await reporter.run(2026, 13)
    service.record(draft([world["w1"].id], world["p1"].id, day=date(2026, 3, 17)), world["foreman"])
    await reporter.run(2026, 12)
    entry = next(e for e in repo.list_entries() if e.work_date == date(2026, 3, 24))
    service.change_times(entry.id, world["foreman"], work_date=date(2026, 3, 18))
    assert repo.weeks_needing_resend() == [(2026, 12), (2026, 13)]


async def test_concurrent_runs_send_only_once(repo, world):
    """Befund 6: Sonntagsjob und /versenden gleichzeitig – jede KW nur einmal versenden."""
    mailer = FakeMailer(delay=0.05)
    reporter = WeeklyReporter(repo, seed(repo, world), mailer, "a@example.org")
    await asyncio.gather(reporter.run(2026, 13), reporter.run(2026, 13))
    assert len(mailer.sent) == 2


async def test_mail_failure_is_reported_with_file_and_queued_for_retry(repo, world):
    service = seed(repo, world)
    result = await WeeklyReporter(repo, service, FakeMailer(fail=True), "a@example.org").run(2026, 13)
    assert {r.status for r in result.results} == {"failed"}
    assert all(r.file is not None for r in result.failed)
    assert not repo.report_already_sent(world["smf"].id, 2026, 13)
    assert "❌" in result.summary()
    assert repo.weeks_needing_resend() == [(2026, 13)]

    retry = await WeeklyReporter(repo, service, FakeMailer(), "a@example.org").run(2026, 13)
    assert {r.status for r in retry.results} == {"sent"}
    assert repo.weeks_needing_resend() == []


async def test_missing_recipient_fails_without_mail(repo, world):
    mailer = FakeMailer()
    result = await WeeklyReporter(repo, seed(repo, world), mailer, "").run(2026, 13)
    assert {r.status for r in result.results} == {"failed"}
    assert mailer.sent == []


async def test_empty_week(repo, world):
    reporter = WeeklyReporter(repo, make_service(repo, FakeTranslator()), FakeMailer(), "a@x.org")
    result = await reporter.run(2026, 20)
    assert result.results == []
    assert "nichts versendet" in result.summary()


async def test_unexpected_error_for_one_customer_does_not_stop_the_others(repo, world):
    class Flaky(FakeMailer):
        async def send(self, recipient, subject, body, attachments):
            if "Rothaus" in subject:
                raise RuntimeError("unerwartet")
            await super().send(recipient, subject, body, attachments)

    mailer = Flaky()
    result = await WeeklyReporter(repo, seed(repo, world), mailer, "a@example.org").run(2026, 13)
    status = {r.customer_name: r.status for r in result.results}
    assert status == {"Brauerei Rothaus": "failed", "Schwarzwaldmilch GmbH Freiburg": "sent"}
    assert repo.weeks_needing_resend() == [(2026, 13)], "Fehlversand ist protokolliert und wird wiederholt"


async def test_translation_budget_is_respected(repo, world):
    service = seed(repo, world, FakeTranslator(fail=True))
    assert await service.translate_pending(date(2026, 3, 23), date(2026, 3, 29), max_seconds=0) == 2
