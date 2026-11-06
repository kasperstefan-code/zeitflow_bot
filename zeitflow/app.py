from __future__ import annotations

import logging
import sys
from datetime import timedelta

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import BaseRequest

from zeitflow.bot import admin, correction, entry, registration, views
from zeitflow.bot.common import Deps, bind_dialogs, cancel
from zeitflow.bot.menu import set_default_commands, set_user_commands
from zeitflow.config import ConfigError, Settings, load_settings
from zeitflow.db import connect, migrate
from zeitflow.i18n import DEFAULT_LANG, t
from zeitflow.mailer import Mailer, SmtpMailer
from zeitflow.models import Role, UserStatus
from zeitflow.reporting import WeeklyReporter, WeeklyRunResult
from zeitflow.repository import Repository
from zeitflow.services import TimesheetService
from zeitflow.timeutil import today
from zeitflow.translation import Translator, build_translator

LOG = logging.getLogger("zeitflow")

RETRY_DELAY = timedelta(minutes=30)
MAX_RETRIES = 3


class StartupError(RuntimeError):
    pass


def sync_configured_admins(repo: Repository, admin_ids: frozenset[int]) -> None:
    """Konfigurierte Admin-IDs sind maßgeblich – auch für Nutzer, die schon als Mitarbeiter existieren."""
    for tg_id in admin_ids:
        user = repo.get_user_by_telegram_id(tg_id)
        if user and (user.role is not Role.ADMIN or user.status is not UserStatus.ACTIVE):
            LOG.info("%s wird laut ZEITFLOW_ADMIN_IDS Admin", user.name)
            repo.update_user(user.id, role=Role.ADMIN, status=UserStatus.ACTIVE)


def build_deps(settings: Settings, translator: Translator | None = None, mailer: Mailer | None = None) -> Deps:
    conn = connect(settings.db_path)
    migrate(conn)
    repo = Repository(conn)
    sync_configured_admins(repo, settings.admin_telegram_ids)
    if not settings.admin_telegram_ids and not repo.has_admin():
        raise StartupError("ZEITFLOW_ADMIN_IDS fehlt und es gibt noch keinen Admin – sonst würde ein Fremder Admin")
    service = TimesheetService(
        repo,
        translator or build_translator(settings.anthropic_api_key, settings.translation_model),
        today=lambda: today(settings.timezone),
    )
    reporter = WeeklyReporter(
        repo, service, mailer or SmtpMailer(settings.smtp), settings.report_recipient, settings.company_name
    )
    return Deps(settings=settings, repo=repo, service=service, reporter=reporter)


async def _notify_admins(ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    d: Deps = ctx.application.bot_data["deps"]
    for a in d.repo.list_admins():
        try:
            await ctx.bot.send_message(a.telegram_id, text)
        except TelegramError:
            LOG.warning("Admin %s nicht erreichbar", a.name)


async def weekly_report_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Sonntagslauf: laufende KW plus alles, was Reporter.pending_weeks() als offen meldet.

    Wiederholungen übernehmen die KWs des ursprünglichen Laufs – auch wenn sie nach Mitternacht laufen.
    """
    data = (ctx.job.data if ctx.job else None) or {}
    attempt = data.get("attempt", 0)
    try:
        d: Deps = ctx.application.bot_data["deps"]
        weeks = sorted({*map(tuple, data.get("weeks", [])), *d.reporter.pending_weeks()})
        LOG.info("Wochenversand (Versuch %s) für KW %s", attempt + 1, ", ".join(f"{w}/{y}" for y, w in weeks))
        current = tuple(d.today().isocalendar())[:2]
        results: list[WeeklyRunResult] = []
        for year, week in weeks:
            results.append(await d.reporter.run(year, week))
        admin_chats = [a.telegram_id for a in d.repo.list_admins()]
        for result in results:
            LOG.info(result.summary())
            if result.needs_attention or (result.iso_year, result.iso_week) == current:
                await admin.deliver_result(ctx, result, chat_ids=admin_chats)
        failed_weeks = [(r.iso_year, r.iso_week) for r in results if r.failed]
        if failed_weeks and d.settings.report_recipient:
            await _retry_or_give_up(ctx, attempt, failed_weeks)
    except Exception:  # noqa: BLE001 – Job darf nie still scheitern
        LOG.exception("Wochenversand abgebrochen")
        await _retry_or_give_up(ctx, attempt, data.get("weeks", []), crashed=True)


async def _retry_or_give_up(ctx, attempt: int, weeks: list, crashed: bool = False) -> None:
    final = attempt + 1 >= MAX_RETRIES
    prefix = "❌ Wochenversand abgebrochen – Details im Log." if crashed else "⚠️ Nicht alle Stundennachweise versendet."
    if final:
        LOG.error("Wochenversand: keine weiteren automatischen Versuche")
        await _notify_admins(ctx, f"{prefix} Keine weiteren automatischen Versuche – bitte /versenden nutzen.")
        return
    if crashed:
        await _notify_admins(ctx, f"{prefix} Neuer Versuch in {int(RETRY_DELAY.total_seconds() // 60)} Minuten.")
    ctx.job_queue.run_once(
        weekly_report_job,
        when=RETRY_DELAY,
        data={"attempt": attempt + 1, "weeks": [list(w) for w in weeks]},
        name="weekly_retry",
    )


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    LOG.error("Unbehandelter Fehler", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        d: Deps = ctx.application.bot_data["deps"]
        user = d.repo.get_user_by_telegram_id(update.effective_user.id) if update.effective_user else None
        try:
            await ctx.bot.send_message(update.effective_chat.id, t(user.lang if user else DEFAULT_LANG, "error"))
        except TelegramError:
            pass


async def configure_commands(app: Application) -> None:
    await set_default_commands(app.bot)
    d: Deps = app.bot_data["deps"]
    for user in d.repo.list_users():
        if user.can_enter_for_others:
            await set_user_commands(app.bot, user)


def build_application(settings: Settings, deps: Deps | None = None, request: BaseRequest | None = None) -> Application:
    builder = Application.builder().token(settings.bot_token).post_init(configure_commands)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()
    app.bot_data["deps"] = deps or build_deps(settings)
    for module in (registration, entry, views, correction, admin):
        for handler in module.handlers():
            app.add_handler(handler)
    app.add_handler(CommandHandler("cancel", cancel))  # /cancel ohne offenen Dialog
    bind_dialogs(app)
    app.add_error_handler(on_error)
    report_time = settings.report_time.replace(tzinfo=settings.timezone)
    app.job_queue.run_daily(weekly_report_job, time=report_time, days=(settings.report_weekday,), name="weekly_report")
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings()
        if not settings.bot_token:
            raise ConfigError("ZEITFLOW_BOT_TOKEN fehlt")
        app = build_application(settings)
    except (ConfigError, StartupError) as exc:
        LOG.error("Start abgebrochen: %s", exc)
        sys.exit(2)
    if not settings.report_recipient:
        LOG.warning("ZEITFLOW_REPORT_EMAIL fehlt – Stundennachweise gehen nur per Telegram an die Admins")
    LOG.info(
        "ZeitFlow gestartet (DB: %s, Versand: Tag %s um %s %s)",
        settings.db_path,
        settings.report_weekday,
        settings.report_time,
        settings.timezone,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)
