from __future__ import annotations

import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    BaseHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

from zeitflow.config import Settings
from zeitflow.i18n import DEFAULT_LANG, t
from zeitflow.models import Entry, Role, User
from zeitflow.reporting import WeeklyReporter
from zeitflow.repository import Repository
from zeitflow.services import Overlap, TimesheetService, ValidationError
from zeitflow.timeutil import short_name, today

LOG = logging.getLogger(__name__)


@dataclass
class Deps:
    settings: Settings
    repo: Repository
    service: TimesheetService
    reporter: WeeklyReporter

    def today(self) -> date:
        return today(self.settings.timezone)


def deps(ctx: ContextTypes.DEFAULT_TYPE) -> Deps:
    return ctx.application.bot_data["deps"]


def lang_of(user: User | None) -> str:
    return user.lang if user else DEFAULT_LANG


async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Antwortet auf Nachricht oder Button – bei Buttons wird die Nachricht ersetzt."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def current_user(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, min_role: Role = Role.EMPLOYEE
) -> User | None:
    """Lädt den Benutzer bei jedem Update aus der DB und prüft Status und Rolle."""
    user = deps(ctx).repo.get_user_by_telegram_id(update.effective_user.id)
    if user is None:
        await reply(update, t(DEFAULT_LANG, "not_registered"))
        return None
    if not user.is_active:
        await reply(update, t(user.lang, "not_active"))
        return None
    allowed = {
        Role.EMPLOYEE: True,
        Role.FOREMAN: user.can_enter_for_others,
        Role.ADMIN: user.is_admin,
    }[min_role]
    if not allowed:
        await reply(update, t(user.lang, "no_permission"))
        return None
    return user


ACTIVE_DIALOG = "active_dialog"
# Zustandsschlüssel der Dialoge in user_data – /cancel räumt alle weg.
DIALOG_STATE_KEYS = ("reg_lang", "draft", "corr", "np", "ep", "anfahrt")

# Nur echte neue Textnachrichten – bearbeitete Nachrichten haben kein update.message.
TEXT = filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = deps(ctx).repo.get_user_by_telegram_id(update.effective_user.id)
    for key in (ACTIVE_DIALOG, *DIALOG_STATE_KEYS):
        ctx.user_data.pop(key, None)
    await reply(update, t(user.lang if user else DEFAULT_LANG, "cancelled"))
    return ConversationHandler.END


class Dialog(ConversationHandler):
    """ConversationHandler, von dem pro Nutzer immer nur der zuletzt gestartete Dialog Eingaben annimmt.

    Alle Dialoge liegen in einer Handler-Gruppe; PTB nimmt den ersten passenden. Ohne diese Sperre würde ein
    vergessener, früher registrierter Dialog Texte und Buttons eines später gestarteten Dialogs abfangen
    (bis zu seinem Timeout). Ein Einstiegsbefehl macht seinen Dialog aktiv, alle anderen ruhen danach.
    """

    def __init__(self, name: str, entry_points, states, timeout: int):
        super().__init__(
            entry_points=entry_points,
            states=states,
            fallbacks=[CommandHandler("cancel", cancel)],
            conversation_timeout=timeout,
            allow_reentry=True,
            name=name,
        )
        self.application = None

    def check_update(self, update: object):
        result = super().check_update(update)
        if result is None or result[2] in self.entry_points or self.application is None:
            return result
        user_data = self.application.user_data.get(update.effective_user.id, {})
        return result if user_data.get(ACTIVE_DIALOG) == self.name else None

    async def handle_update(self, update, application, check_result, context):
        result = await super().handle_update(update, application, check_result, context)
        # Erst nach erfolgreichem Einstieg aktiv setzen – ein abgestürzter Einstieg darf keinen alten Dialog wecken.
        if check_result[2] in self.entry_points:
            context.user_data[ACTIVE_DIALOG] = self.name
        return result


def conversation(name: str, entry_points: list[BaseHandler], states: dict, timeout: int = 900) -> Dialog:
    return Dialog(name, entry_points, states, timeout)


def bind_dialogs(application) -> None:
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, Dialog):
                handler.application = application


async def ignore_edits(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Bearbeitete Nachrichten (auch Befehle) nie als neue Eingabe werten."""
    raise ApplicationHandlerStop


async def stale_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Button ohne passenden offenen Dialog (z. B. nach Timeout) – Rückmeldung statt Stille."""
    user = deps(ctx).repo.get_user_by_telegram_id(update.effective_user.id)
    await update.callback_query.answer(t(lang_of(user), "session_expired"), show_alert=True)
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


async def stray_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Text ohne offenen Dialog – z. B. eine Eingabe nach Ablauf der Sitzung."""
    user = deps(ctx).repo.get_user_by_telegram_id(update.effective_user.id)
    if user is None:
        await update.message.reply_text(t(DEFAULT_LANG, "not_registered"))
    else:
        await update.message.reply_text(t(user.lang, "no_dialog"))


def background(ctx: ContextTypes.DEFAULT_TYPE, coro: Coroutine, chat_id: int | None = None, lang: str = DEFAULT_LANG):
    """Langsame Arbeit (Claude API, Export, SMTP) ohne andere Nutzer zu blockieren.

    Fehler werden geloggt und – wenn ein Chat bekannt ist – dem Nutzer gemeldet, statt ihn bei "⏳" stehen zu lassen.
    """

    async def guarded():
        try:
            await coro
        except Exception:  # noqa: BLE001 – Hintergrundaufgabe darf nicht still scheitern
            LOG.exception("Hintergrundaufgabe fehlgeschlagen")
            if chat_id is not None:
                try:
                    await ctx.bot.send_message(chat_id, t(lang, "error"))
                except TelegramError:
                    pass

    ctx.application.create_task(guarded())


async def notify_user(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> bool:
    """Benachrichtigung, die eine bereits gespeicherte Änderung nicht scheitern lässt (z. B. Bot blockiert)."""
    try:
        await ctx.bot.send_message(chat_id, text)
        return True
    except TelegramError:
        LOG.warning("Nutzer %s nicht erreichbar", chat_id)
        return False


def telegram_label(tg_user) -> str:
    parts = [tg_user.full_name]
    if tg_user.username:
        parts.append(f"@{tg_user.username}")
    parts.append(f"ID {tg_user.id}")
    return " · ".join(parts)


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


def chunk(items: list[tuple[str, str]], size: int) -> list[list[tuple[str, str]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def fmt_hours(minutes: int) -> str:
    return f"{minutes / 60:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def entry_label(e: Entry, with_user: bool = True) -> str:
    who = f" {short_name(e.user_name)}" if with_user else ""
    label = f"{e.work_date:%d.%m.} {e.start_time}–{e.end_time}{who} · {e.customer_name}"
    return label if len(label) <= 60 else label[:59] + "…"


ERROR_TEXT_KEYS = {
    "end_before_start": "end_before_start",
    "bad_break": "bad_break",
    "future_date": "future_date",
    "too_old": "too_old",
    "activity": "ask_activity",
    "inactive_worker": "inactive_worker",
    "project": "project_inactive",
}


def error_text(lang: str, exc: ValidationError) -> str:
    return t(lang, ERROR_TEXT_KEYS.get(exc.code, "error"))


def overlap_lines(overlaps: list[Overlap]) -> str:
    return "\n".join(
        f"• {short_name(o.user_name)} {o.existing.work_date:%d.%m.} {o.existing.start_time}–{o.existing.end_time} "
        f"({o.existing.customer_name})"
        for o in overlaps
    )
