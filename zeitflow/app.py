from __future__ import annotations

import logging
import sys

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes
from telegram.request import BaseRequest

from zeitflow.bot import admin, correction, entry, registration, views
from zeitflow.bot.common import Deps
from zeitflow.config import ConfigError, Settings, load_settings
from zeitflow.db import connect, migrate
from zeitflow.i18n import COMMAND_DESCRIPTIONS, DEFAULT_LANG, WORKER_COMMANDS, t
from zeitflow.mailer import Mailer, SmtpMailer
from zeitflow.models import Role
from zeitflow.reporting import WeeklyReporter
from zeitflow.repository import Repository
from zeitflow.services import TimesheetService
from zeitflow.translation import Translator, build_translator

LOG = logging.getLogger("zeitflow")

FOREMAN_COMMANDS = [("export", "Stundennachweis als Excel")]
ADMIN_COMMANDS = [
    ("mitarbeiter", "Mitarbeiter, Rollen, Freigaben"),
    ("kunden", "Kunden & Projekte anzeigen"),
    ("neuerkunde", "Kunde anlegen"),
    ("neuesprojekt", "Projekt anlegen"),
    ("projekt", "Projekt ändern/deaktivieren"),
    ("anfahrt", "Anfahrten je Kunde/KW"),
    ("versenden", "Stundennachweise jetzt senden"),
]


def build_deps(settings: Settings, translator: Translator | None = None, mailer: Mailer | None = None) -> Deps:
    conn = connect(settings.db_path)
    migrate(conn)
    repo = Repository(conn)
    service = TimesheetService(
        repo, translator or build_translator(settings.anthropic_api_key, settings.translation_model)
    )
    reporter = WeeklyReporter(
        repo, service, mailer or SmtpMailer(settings.smtp), settings.report_recipient, settings.company_name
    )
    return Deps(settings=settings, repo=repo, service=service, reporter=reporter)


async def weekly_report_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    d: Deps = ctx.application.bot_data["deps"]
    iso = d.today().isocalendar()
    LOG.info("Wochenversand KW %s/%s startet", iso.week, iso.year)
    result = await d.reporter.run(iso.year, iso.week)
    LOG.info(result.summary())
    await admin.deliver_result(ctx, result, chat_ids=[a.telegram_id for a in d.repo.list_admins()])


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
    for lang, descriptions in COMMAND_DESCRIPTIONS.items():
        commands = [BotCommand(c, d) for c, d in zip(WORKER_COMMANDS, descriptions, strict=True)]
        await app.bot.set_my_commands(commands, language_code=None if lang == DEFAULT_LANG else lang)
    d: Deps = app.bot_data["deps"]
    for user in d.repo.list_users():
        if not user.can_enter_for_others:
            continue
        base = [BotCommand(c, desc) for c, desc in zip(WORKER_COMMANDS, COMMAND_DESCRIPTIONS[user.lang], strict=True)]
        extra = FOREMAN_COMMANDS + (ADMIN_COMMANDS if user.role is Role.ADMIN else [])
        try:
            await app.bot.set_my_commands(
                base + [BotCommand(c, desc) for c, desc in extra], scope=BotCommandScopeChat(user.telegram_id)
            )
        except TelegramError:
            LOG.warning("Befehlsmenü für %s nicht gesetzt (Chat unbekannt?)", user.name)


def build_application(settings: Settings, deps: Deps | None = None, request: BaseRequest | None = None) -> Application:
    builder = Application.builder().token(settings.bot_token).post_init(configure_commands)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()
    app.bot_data["deps"] = deps or build_deps(settings)
    for module in (registration, entry, views, correction, admin):
        for handler in module.handlers():
            app.add_handler(handler)
    app.add_error_handler(on_error)
    report_time = settings.report_time.replace(tzinfo=settings.timezone)
    app.job_queue.run_daily(weekly_report_job, time=report_time, days=(settings.report_weekday,), name="weekly_report")
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigError as exc:
        LOG.error("Konfigurationsfehler: %s", exc)
        sys.exit(2)
    if not settings.bot_token:
        LOG.error("ZEITFLOW_BOT_TOKEN fehlt")
        sys.exit(2)
    if not settings.report_recipient:
        LOG.warning("ZEITFLOW_REPORT_EMAIL fehlt – Stundennachweise gehen nur per Telegram an die Admins")
    app = build_application(settings)
    LOG.info(
        "ZeitFlow gestartet (DB: %s, Versand: Tag %s um %s %s)",
        settings.db_path,
        settings.report_weekday,
        settings.report_time,
        settings.timezone,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)
