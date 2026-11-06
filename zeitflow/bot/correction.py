"""/korrektur – Einträge ändern oder löschen. Rechte und Zeitfenster prüft der Service (nicht nur die Buttons)."""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from zeitflow.bot.common import (
    background,
    conversation,
    current_user,
    deps,
    entry_label,
    error_text,
    keyboard,
    overlap_lines,
    reply,
)
from zeitflow.i18n import t
from zeitflow.services import OverlapError, PermissionDenied, ValidationError
from zeitflow.timeutil import parse_date, parse_time

PICK, FIELD, VALUE, PROJECT, DELETE = range(5)
TEXT_FIELDS = ("date", "start", "end", "break", "activity")
KEY = "corr"


def _state(ctx) -> dict:
    return ctx.user_data.setdefault(KEY, {})


async def _finish(update: Update, ctx, text: str):
    ctx.user_data.pop(KEY, None)
    await reply(update, text)
    return ConversationHandler.END


async def cmd_korrektur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    ctx.user_data[KEY] = {}
    entries = deps(ctx).service.editable_entries(user)
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
    try:
        entry = deps(ctx).service.editable(int(update.callback_query.data.split(":")[1]), user)
    except (PermissionDenied, ValidationError):
        return await _finish(update, ctx, t(user.lang, "no_permission"))
    _state(ctx)["id"] = entry.id
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
    if "id" not in _state(ctx):
        return await _finish(update, ctx, t(user.lang, "session_expired"))
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
        return await _finish(update, ctx, t(user.lang, "error"))
    _state(ctx)["field"] = field
    await reply(update, t(user.lang, "corr_value"))
    return VALUE


async def _translate_later(ctx, entry_id: int, text: str, lang: str) -> None:
    await deps(ctx).service.translate_entries([entry_id], text, lang)


async def on_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    entry_id, field = _state(ctx).get("id"), _state(ctx).get("field")
    if entry_id is None or field is None:
        return await _finish(update, ctx, t(user.lang, "session_expired"))
    raw = update.message.text.strip()
    service = deps(ctx).service
    try:
        if field == "activity":
            entry = service.change_activity(entry_id, user, raw, user.lang)
            background(ctx, _translate_later(ctx, entry.id, entry.activity_original, user.lang))
        elif field == "date":
            day = parse_date(raw, deps(ctx).today())
            if day is None:
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
        return await _finish(update, ctx, t(user.lang, "overlap", list=overlap_lines(exc.overlaps)))
    except PermissionDenied:
        return await _finish(update, ctx, t(user.lang, "no_permission"))
    except ValidationError as exc:
        if exc.code == "not_found":
            return await _finish(update, ctx, t(user.lang, "error"))
        await update.message.reply_text(error_text(user.lang, exc))
        return VALUE
    return await _finish(update, ctx, t(user.lang, "corr_ok"))


async def on_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    entry_id = _state(ctx).get("id")
    if entry_id is None:
        return await _finish(update, ctx, t(user.lang, "session_expired"))
    try:
        deps(ctx).service.change_project(entry_id, user, int(update.callback_query.data.split(":")[1]))
    except PermissionDenied:
        return await _finish(update, ctx, t(user.lang, "no_permission"))
    except ValidationError as exc:
        return await _finish(update, ctx, error_text(user.lang, exc))
    return await _finish(update, ctx, t(user.lang, "corr_ok"))


async def on_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    if update.callback_query.data == "kd:n":
        return await _finish(update, ctx, t(user.lang, "cancelled"))
    entry_id = _state(ctx).get("id")
    if entry_id is None:
        return await _finish(update, ctx, t(user.lang, "session_expired"))
    try:
        deps(ctx).service.delete(entry_id, user)
    except PermissionDenied:
        return await _finish(update, ctx, t(user.lang, "no_permission"))
    except ValidationError as exc:
        return await _finish(update, ctx, error_text(user.lang, exc))
    return await _finish(update, ctx, t(user.lang, "corr_deleted"))


def handlers():
    return [
        conversation(
            [CommandHandler("korrektur", cmd_korrektur)],
            {
                PICK: [CallbackQueryHandler(on_pick, pattern=r"^k:\d+$")],
                FIELD: [CallbackQueryHandler(on_field, pattern=r"^kf:\w+$")],
                VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_value)],
                PROJECT: [CallbackQueryHandler(on_project, pattern=r"^kp:\d+$")],
                DELETE: [CallbackQueryHandler(on_delete, pattern=r"^kd:[yn]$")],
            },
        )
    ]
