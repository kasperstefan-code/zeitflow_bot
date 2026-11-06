from datetime import date

from tests.conftest import FakeMailer, FakeTranslator, draft
from zeitflow.reporting import WeeklyReporter
from zeitflow.services import TimesheetService


async def seed(repo, world, translator):
    service = TimesheetService(repo, translator)
    await service.record(draft([world["w1"].id], world["p1"].id, day=date(2026, 3, 24)), world["foreman"])
    await service.record(draft([world["w2"].id], world["p3"].id, day=date(2026, 3, 25)), world["foreman"])
    return service


async def test_sends_one_mail_per_customer_and_logs(repo, world):
    service = await seed(repo, world, FakeTranslator())
    mailer = FakeMailer()
    reporter = WeeklyReporter(repo, service, mailer, "abrechnung@example.org", "Muster GmbH")

    result = await reporter.run(2026, 13)

    assert [r.status for r in result.results] == ["sent", "sent"]
    assert [m["recipient"] for m in mailer.sent] == ["abrechnung@example.org"] * 2
    subjects = sorted(m["subject"] for m in mailer.sent)
    assert subjects == [
        "Stundennachweis Brauerei Rothaus – KW 13/2026",
        "Stundennachweis Schwarzwaldmilch GmbH Freiburg – KW 13/2026",
    ]
    assert mailer.sent[0]["attachments"][0].filename.endswith("_KW13_2026.xlsx")
    assert "Anfahrten" in mailer.sent[0]["body"] and "Muster GmbH" in mailer.sent[0]["body"]
    assert repo.report_already_sent(world["smf"].id, 2026, 13)


async def test_second_run_skips_unless_forced(repo, world):
    service = await seed(repo, world, FakeTranslator())
    mailer = FakeMailer()
    reporter = WeeklyReporter(repo, service, mailer, "a@example.org")
    await reporter.run(2026, 13)

    again = await reporter.run(2026, 13)
    assert [r.status for r in again.results] == ["skipped", "skipped"]
    forced = await reporter.run(2026, 13, force=True)
    assert [r.status for r in forced.results] == ["sent", "sent"]
    assert len(mailer.sent) == 4


async def test_pending_translations_are_retried_before_sending(repo, world):
    translator = FakeTranslator(fail=True)
    service = await seed(repo, world, translator)
    translator.fail = False
    await WeeklyReporter(repo, service, FakeMailer(), "a@example.org").run(2026, 13)
    assert all(e.activity_de for e in repo.list_entries())


async def test_mail_failure_is_reported_with_file_and_retried_next_run(repo, world):
    service = await seed(repo, world, FakeTranslator())
    result = await WeeklyReporter(repo, service, FakeMailer(fail=True), "a@example.org").run(2026, 13)
    assert {r.status for r in result.results} == {"failed"}
    assert all(r.file is not None for r in result.results)
    assert not repo.report_already_sent(world["smf"].id, 2026, 13)
    assert "❌" in result.summary()

    retry = await WeeklyReporter(repo, service, FakeMailer(), "a@example.org").run(2026, 13)
    assert {r.status for r in retry.results} == {"sent"}


async def test_missing_recipient_fails_without_mail(repo, world):
    service = await seed(repo, world, FakeTranslator())
    mailer = FakeMailer()
    result = await WeeklyReporter(repo, service, mailer, "").run(2026, 13)
    assert {r.status for r in result.results} == {"failed"}
    assert mailer.sent == []


async def test_empty_week(repo, world):
    result = await WeeklyReporter(repo, TimesheetService(repo, FakeTranslator()), FakeMailer(), "a@x.org").run(2026, 20)
    assert result.results == []
    assert "nichts versendet" in result.summary()
