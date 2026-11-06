from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import BaseHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from zeitflow.config import Settings
from zeitflow.i18n import DEFAULT_LANG, t
from zeitflow.models import Entry, Role, User
from zeitflow.reporting import WeeklyReporter
from zeitflow.repository import Repository
from zeitflow.services import Overlap, TimesheetService, ValidationError
from zeitflow.timeutil import short_name, today


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


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = deps(ctx).repo.get_user_by_telegram_id(update.effective_user.id)
    ctx.user_data.clear()
    await reply(update, t(user.lang if user else DEFAULT_LANG, "cancelled"))
    return ConversationHandler.END


async def end_and_redispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ein anderer Befehl während eines offenen Dialogs: Dialog beenden und den Befehl normal ausführen.

    Ohne das bliebe der alte Dialog bis zum Timeout aktiv und würde Texteingaben des neuen Dialogs abfangen.
    Die Neuzustellung läuft als Task, weil der Dialogstatus erst nach Rückkehr dieses Callbacks auf END steht.
    """
    ctx.user_data.clear()
    ctx.application.create_task(ctx.application.process_update(update), update=update)
    return ConversationHandler.END


def conversation(
    entry_points: list[BaseHandler], states: dict[object, list[BaseHandler]], timeout: int = 900
) -> ConversationHandler:
    return ConversationHandler(
        entry_points=entry_points,
        states=states,
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.COMMAND, end_and_redispatch)],
        conversation_timeout=timeout,
        allow_reentry=True,
    )


def background(ctx: ContextTypes.DEFAULT_TYPE, coro: Coroutine) -> None:
    """Langsame Arbeit (Claude API, Export, SMTP) ohne andere Nutzer zu blockieren; Fehler gehen an on_error."""
    ctx.application.create_task(coro)


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
}


def error_text(lang: str, exc: ValidationError) -> str:
    return t(lang, ERROR_TEXT_KEYS.get(exc.code, "error"))


def overlap_lines(overlaps: list[Overlap]) -> str:
    return "\n".join(
        f"• {short_name(o.user_name)} {o.existing.work_date:%d.%m.} {o.existing.start_time}–{o.existing.end_time} "
        f"({o.existing.customer_name})"
        for o in overlaps
    )
