from datetime import datetime
from zoneinfo import ZoneInfo

from tests.conftest import FakeMailer, FakeTranslator
from zeitflow.app import build_application, build_deps
from zeitflow.config import load_settings


def test_application_wires_handlers_and_sunday_job():
    settings = load_settings({"ZEITFLOW_BOT_TOKEN": "123:abc", "ZEITFLOW_DB_PATH": ":memory:"})
    deps = build_deps(settings, translator=FakeTranslator(), mailer=FakeMailer())
    app = build_application(settings, deps)

    commands = set()
    for group in app.handlers.values():
        for h in group:
            for entry in getattr(h, "entry_points", [h]):
                commands |= set(getattr(entry, "commands", ()))
    assert {
        "start",
        "zeit",
        "heute",
        "woche",
        "korrektur",
        "sprache",
        "hilfe",
        "export",
        "mitarbeiter",
        "kunden",
        "neuerkunde",
        "neuesprojekt",
        "projekt",
        "anfahrt",
        "versenden",
    } <= commands

    (job,) = app.job_queue.jobs()
    assert job.name == "weekly_report"
    berlin = ZoneInfo("Europe/Berlin")
    after = datetime(2026, 3, 25, 12, 0, tzinfo=berlin)  # Mittwoch
    fire = job.job.trigger.get_next_fire_time(None, after).astimezone(berlin)
    assert fire.replace(tzinfo=None) == datetime(2026, 3, 29, 19, 0), "Sonntag 19:00 Ortszeit"
    after_dst = datetime(2026, 4, 1, 12, 0, tzinfo=berlin)
    fire_dst = job.job.trigger.get_next_fire_time(None, after_dst).astimezone(berlin)
    assert (fire_dst.weekday(), fire_dst.hour) == (6, 19), "auch nach Sommerzeitumstellung"


async def test_weekly_job_reports_the_current_week(monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    from zeitflow import app as app_module

    settings = load_settings({"ZEITFLOW_BOT_TOKEN": "123:abc", "ZEITFLOW_DB_PATH": ":memory:"})
    deps = build_deps(settings, translator=FakeTranslator(), mailer=FakeMailer())
    monkeypatch.setattr(type(deps), "today", lambda self: date(2027, 1, 3))  # Sonntag der KW 53/2026
    calls = []

    async def fake_run(year, week, force=False):
        calls.append((year, week))
        from zeitflow.reporting import WeeklyRunResult

        return WeeklyRunResult(year, week)

    deps.reporter.run = fake_run

    async def fake_deliver(ctx, result, chat_ids):
        pass

    monkeypatch.setattr(app_module.admin, "deliver_result", fake_deliver)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))
    await app_module.weekly_report_job(ctx)
    assert calls == [(2026, 53)]
