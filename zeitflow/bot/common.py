from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from zeitflow.config import Settings
from zeitflow.i18n import DEFAULT_LANG, t
from zeitflow.models import Entry, Role, User
from zeitflow.reporting import WeeklyReporter
from zeitflow.repository import Repository
from zeitflow.services import Overlap, TimesheetService
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


def overlap_lines(overlaps: list[Overlap]) -> str:
    return "\n".join(
        f"• {short_name(o.user_name)} {o.existing.work_date:%d.%m.} {o.existing.start_time}–{o.existing.end_time} "
        f"({o.existing.customer_name})"
        for o in overlaps
    )
