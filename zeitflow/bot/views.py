"""/heute und /woche – eigene Einträge ansehen."""

from __future__ import annotations

from datetime import timedelta

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from zeitflow.bot.common import current_user, deps, fmt_hours
from zeitflow.i18n import t
from zeitflow.models import Entry


def _entry_line(e: Entry) -> str:
    activity = e.activity_original if len(e.activity_original) <= 60 else e.activity_original[:59] + "…"
    return (
        f"🕐 {e.start_time}–{e.end_time} ☕{e.break_minutes} ⏱{fmt_hours(e.work_minutes)} h\n"
        f"   🏢 {e.customer_name} · {e.project_name}\n   📝 {activity}"
    )


async def cmd_heute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return
    today = deps(ctx).today()
    entries = deps(ctx).repo.list_entries(user_id=user.id, date_from=today, date_to=today)
    if not entries:
        await update.message.reply_text(t(user.lang, "no_entries"))
        return
    total = sum(e.work_minutes for e in entries)
    body = "\n".join(_entry_line(e) for e in entries)
    await update.message.reply_text(
        f"📅 {today:%d.%m.%Y}\n\n{body}\n\n━━━━━━━━\n{t(user.lang, 'total')}: {fmt_hours(total)} h"
    )


async def cmd_woche(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return
    today = deps(ctx).today()
    monday = today - timedelta(days=today.weekday())
    entries = deps(ctx).repo.list_entries(user_id=user.id, date_from=monday, date_to=monday + timedelta(days=6))
    iso = today.isocalendar()
    header = "📊 " + t(user.lang, "week_title", week=iso.week, year=iso.year)
    if not entries:
        await update.message.reply_text(f"{header}\n\n{t(user.lang, 'no_entries')}")
        return
    lines, current = [header], None
    for e in entries:
        if e.work_date != current:
            current = e.work_date
            lines.append(f"\n📅 {e.work_date:%d.%m.}")
        lines.append(f"  • {e.start_time}–{e.end_time} {fmt_hours(e.work_minutes)} h · {e.customer_name}")
    total = sum(e.work_minutes for e in entries)
    lines.append(f"\n━━━━━━━━\n{t(user.lang, 'total')}: {fmt_hours(total)} h")
    await update.message.reply_text("\n".join(lines))


def handlers():
    return [CommandHandler("heute", cmd_heute), CommandHandler("woche", cmd_woche)]
