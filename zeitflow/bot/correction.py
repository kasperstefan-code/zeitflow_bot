"""/korrektur – eigene (bzw. als Vorarbeiter alle) Einträge der letzten 14 Tage ändern oder löschen."""

from __future__ import annotations

from datetime import timedelta

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from zeitflow.bot.common import current_user, deps, entry_label, keyboard, overlap_lines, reply
from zeitflow.bot.registration import cancel
from zeitflow.i18n import t
from zeitflow.services import OverlapError, PermissionDenied, ValidationError
from zeitflow.timeutil import parse_date, parse_time

PICK, FIELD, VALUE, PROJECT, DELETE = range(5)
TEXT_FIELDS = ("date", "start", "end", "break", "activity")


async def cmd_korrektur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    entries = deps(ctx).service.editable_entries(user, deps(ctx).today())
    if not entries:
        await update.message.reply_text(t(user.lang, "no_entries"))
        return ConversationHandler.END
    rows = [[(entry_label(e, with_user=user.can_enter_for_others), f"k:{e.id}")] for e in entries]
    await update.message.reply_text(t(user.lang, "corr_pick"), reply_markup=keyboard(rows))
    return PICK


async def on_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    entry = deps(ctx).repo.get_entry(int(update.callback_query.data.split(":")[1]))
    if entry is None or (entry.user_id != user.id and not user.can_enter_for_others):
        await reply(update, t(user.lang, "no_permission"))
        return ConversationHandler.END
    ctx.user_data["corr_id"] = entry.id
    L = user.lang
    rows = [
        [(t(L, "f_date"), "kf:date"), (t(L, "f_project"), "kf:project")],
        [(t(L, "f_start"), "kf:start"), (t(L, "f_end"), "kf:end"), (t(L, "f_break"), "kf:break")],
        [(t(L, "f_activity"), "kf:activity")],
        [(t(L, "f_delete"), "kf:delete")],
    ]
    detail = f"{entry_label(entry)}\n📝 {entry.activity_original}"
    await reply(update, f"{detail}\n\n{t(L, 'corr_what')}", keyboard(rows))
    return FIELD


async def on_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    field = update.callback_query.data.split(":")[1]
    if field == "delete":
        rows = [[(t(user.lang, "yes"), "kd:y"), (t(user.lang, "no"), "kd:n")]]
        await reply(update, t(user.lang, "confirm_delete"), keyboard(rows))
        return DELETE
    if field == "project":
        projects = deps(ctx).repo.list_projects()
        rows = [[(f"{p.customer_name} · {p.name}"[:60], f"kp:{p.id}")] for p in projects]
        await reply(update, t(user.lang, "pick_project"), keyboard(rows))
        return PROJECT
    if field not in TEXT_FIELDS:
        return ConversationHandler.END
    ctx.user_data["corr_field"] = field
    await reply(update, t(user.lang, "corr_value"))
    return VALUE


async def _finish(update: Update, ctx, user, text: str):
    ctx.user_data.clear()
    await reply(update, text)
    return ConversationHandler.END


async def on_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    entry_id, field = ctx.user_data.get("corr_id"), ctx.user_data.get("corr_field")
    if entry_id is None or field is None:
        return await _finish(update, ctx, user, t(user.lang, "session_expired"))
    raw = update.message.text.strip()
    service = deps(ctx).service
    try:
        if field == "activity":
            await service.change_activity(entry_id, user, raw, user.lang)
        elif field == "date":
            today = deps(ctx).today()
            day = parse_date(raw, today)
            if day is None or day > today or day < today - timedelta(days=62):
                await update.message.reply_text(t(user.lang, "bad_date"))
                return VALUE
            service.change_times(entry_id, user, work_date=day)
        elif field in ("start", "end"):
            value = parse_time(raw)
            if value is None:
                await update.message.reply_text(t(user.lang, "bad_time"))
                return VALUE
            service.change_times(entry_id, user, **{f"{field}_time": value})
        else:
            if not raw.isdigit():
                await update.message.reply_text(t(user.lang, "bad_break"))
                return VALUE
            service.change_times(entry_id, user, break_minutes=int(raw))
    except OverlapError as exc:
        return await _finish(update, ctx, user, t(user.lang, "overlap", list=overlap_lines(exc.overlaps)))
    except PermissionDenied:
        return await _finish(update, ctx, user, t(user.lang, "no_permission"))
    except ValidationError:
        await update.message.reply_text(t(user.lang, "bad_break" if field == "break" else "end_before_start"))
        return VALUE
    return await _finish(update, ctx, user, t(user.lang, "corr_ok"))


async def on_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    try:
        deps(ctx).service.change_project(ctx.user_data["corr_id"], user, int(update.callback_query.data.split(":")[1]))
    except (PermissionDenied, ValidationError, KeyError):
        return await _finish(update, ctx, user, t(user.lang, "error"))
    return await _finish(update, ctx, user, t(user.lang, "corr_ok"))


async def on_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    if update.callback_query.data == "kd:n":
        return await _finish(update, ctx, user, t(user.lang, "cancelled"))
    try:
        deps(ctx).service.delete(ctx.user_data["corr_id"], user)
    except (PermissionDenied, ValidationError, KeyError):
        return await _finish(update, ctx, user, t(user.lang, "error"))
    return await _finish(update, ctx, user, t(user.lang, "corr_deleted"))


def handlers():
    return [
        ConversationHandler(
            entry_points=[CommandHandler("korrektur", cmd_korrektur)],
            states={
                PICK: [CallbackQueryHandler(on_pick, pattern=r"^k:\d+$")],
                FIELD: [CallbackQueryHandler(on_field, pattern=r"^kf:\w+$")],
                VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_value)],
                PROJECT: [CallbackQueryHandler(on_project, pattern=r"^kp:\d+$")],
                DELETE: [CallbackQueryHandler(on_delete, pattern=r"^kd:[yn]$")],
            },
            fallbacks=[CommandHandler("cancel", cancel), CommandHandler("korrektur", cmd_korrektur)],
            conversation_timeout=900,
            allow_reentry=True,
        )
    ]
