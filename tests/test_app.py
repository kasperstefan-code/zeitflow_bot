from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import FakeMailer, FakeTranslator, draft
from zeitflow import app as app_module
from zeitflow.app import MAX_RETRIES, StartupError, build_application, build_deps, weekly_report_job
from zeitflow.config import load_settings
from zeitflow.models import Role, UserStatus


def settings_for(tmp_path, **extra):
    env = {"ZEITFLOW_BOT_TOKEN": "123:abc", "ZEITFLOW_DB_PATH": str(tmp_path / "z.db"), "ZEITFLOW_ADMIN_IDS": "1"}
    env.update(extra)
    return load_settings(env)


def test_application_wires_handlers_and_sunday_job(tmp_path):
    settings = settings_for(tmp_path)
    app = build_application(settings, build_deps(settings, translator=FakeTranslator(), mailer=FakeMailer()))

    commands = set()
    for group in app.handlers.values():
        for h in group:
            for entry in getattr(h, "entry_points", [h]):
                commands |= set(getattr(entry, "commands", ()))
    assert {
        "start", "zeit", "heute", "woche", "korrektur", "sprache", "hilfe", "export",
        "mitarbeiter", "kunden", "neuerkunde", "neuesprojekt", "projekt", "anfahrt", "versenden",
    } <= commands  # fmt: skip

    (job,) = app.job_queue.jobs()
    assert job.name == "weekly_report"
    berlin = ZoneInfo("Europe/Berlin")
    fire = job.job.trigger.get_next_fire_time(None, datetime(2026, 3, 25, 12, 0, tzinfo=berlin)).astimezone(berlin)
    assert fire.replace(tzinfo=None) == datetime(2026, 3, 29, 19, 0), "Sonntag 19:00 Ortszeit"
    fire_dst = job.job.trigger.get_next_fire_time(None, datetime(2026, 4, 1, 12, 0, tzinfo=berlin)).astimezone(berlin)
    assert (fire_dst.weekday(), fire_dst.hour) == (6, 19), "auch nach Sommerzeitumstellung"


def test_refuses_to_start_without_any_admin(tmp_path):
    """Befund 9: ohne Admin-IDs würde der erste Fremde Admin."""
    with pytest.raises(StartupError):
        build_deps(settings_for(tmp_path, ZEITFLOW_ADMIN_IDS=""), translator=FakeTranslator(), mailer=FakeMailer())


def test_existing_admin_allows_start_without_ids_and_ids_promote_users(tmp_path):
    deps = build_deps(settings_for(tmp_path, ZEITFLOW_ADMIN_IDS="42"), translator=FakeTranslator(), mailer=FakeMailer())
    worker = deps.repo.create_user(43, "Max Muster", "de", Role.EMPLOYEE, UserStatus.ACTIVE)
    deps.repo.create_user(42, "Anna Büro", "de", Role.ADMIN, UserStatus.ACTIVE)

    build_deps(settings_for(tmp_path, ZEITFLOW_ADMIN_IDS=""), translator=FakeTranslator(), mailer=FakeMailer())
    again = build_deps(
        settings_for(tmp_path, ZEITFLOW_ADMIN_IDS="43"), translator=FakeTranslator(), mailer=FakeMailer()
    )
    promoted = again.repo.get_user(worker.id)
    assert (promoted.role, promoted.status) == (Role.ADMIN, UserStatus.ACTIVE)


class FakeJobQueue:
    def __init__(self):
        self.scheduled = []

    def run_once(self, callback, when, data, name):
        self.scheduled.append((when, data))


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, chat_id, text, **_):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, filename, caption):
        self.documents.append((chat_id, filename))


def job_ctx(deps, attempt=None):
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"deps": deps}),
        bot=FakeBot(),
        job_queue=FakeJobQueue(),
        job=SimpleNamespace(data={"attempt": attempt}) if attempt is not None else None,
    )


def job_deps(tmp_path, mailer, today):
    deps = build_deps(settings_for(tmp_path, ZEITFLOW_REPORT_EMAIL="a@example.org"), FakeTranslator(), mailer)
    deps.today = lambda: today
    deps.service.today = lambda: today
    admin = deps.repo.create_user(1, "Anna Büro", "de", Role.ADMIN, UserStatus.ACTIVE)
    customer = deps.repo.create_customer("Kunde A")
    project = deps.repo.create_project(customer.id, "Projekt", "1")
    return deps, admin, project


async def test_weekly_job_sends_current_week_across_year_boundary(tmp_path):
    deps, admin, project = job_deps(tmp_path, FakeMailer(), date(2027, 1, 3))  # Sonntag der KW 53/2026
    deps.service.record(draft([admin.id], project.id, day=date(2026, 12, 30)), admin)
    ctx = job_ctx(deps)
    await weekly_report_job(ctx)
    assert deps.repo.report_already_sent(1, 2026, 53)
    assert any("KW 53/2026" in text for _, text in ctx.bot.messages)


async def test_weekly_job_resends_corrected_older_weeks(tmp_path):
    mailer = FakeMailer()
    deps, admin, project = job_deps(tmp_path, mailer, date(2026, 3, 29))
    deps.service.record(draft([admin.id], project.id, day=date(2026, 3, 24)), admin)
    await weekly_report_job(job_ctx(deps))

    deps.today = deps.service.today = lambda: date(2026, 4, 5)
    deps.service.record(draft([admin.id], project.id, day=date(2026, 3, 25)), admin)  # Nachtrag zur KW 13
    ctx = job_ctx(deps)
    await weekly_report_job(ctx)
    assert [m["subject"] for m in mailer.sent][-1].endswith("KW 13/2026 (korrigiert)")
    assert any("KW 13/2026" in text for _, text in ctx.bot.messages)


async def test_weekly_job_retries_failed_mail_and_gives_up(tmp_path):
    deps, admin, project = job_deps(tmp_path, FakeMailer(fail=True), date(2026, 3, 29))
    deps.service.record(draft([admin.id], project.id, day=date(2026, 3, 24)), admin)
    ctx = job_ctx(deps)
    await weekly_report_job(ctx)
    assert ctx.job_queue.scheduled[0][1] == {"attempt": 1}
    assert ctx.bot.documents, "nicht versendete Datei geht an die Admins"

    last = job_ctx(deps, attempt=MAX_RETRIES - 1)
    await weekly_report_job(last)
    assert last.job_queue.scheduled == []


async def test_weekly_job_crash_notifies_admins_and_retries(tmp_path, monkeypatch):
    deps, admin, project = job_deps(tmp_path, FakeMailer(), date(2026, 3, 29))

    async def boom(*_, **__):
        raise RuntimeError("DB kaputt")

    monkeypatch.setattr(deps.reporter, "run", boom)
    ctx = job_ctx(deps)
    await weekly_report_job(ctx)
    assert any("abgebrochen" in text for _, text in ctx.bot.messages)
    assert ctx.job_queue.scheduled


def test_main_exits_cleanly_without_admins(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEITFLOW_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ZEITFLOW_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.delenv("ZEITFLOW_ADMIN_IDS", raising=False)
    with pytest.raises(SystemExit) as exc:
        app_module.main()
    assert exc.value.code == 2
