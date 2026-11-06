"""/korrektur – Einträge ändern oder löschen. Rechte und Zeitfenster prüft der Service (nicht nur die Buttons)."""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from zeitflow.bot.common import (
    TEXT,
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
from zeitflow.bot.entry import MAX_ACTIVITY_LEN
from zeitflow.i18n import t
from zeitflow.services import OverlapError, PermissionDenied, ValidationError
from zeitflow.timeutil import parse_break, parse_date, parse_time

PICK, FIELD, VALUE, PROJECT, DELETE = range(5)
TEXT_FIELDS = ("date", "start", "end", "break", "activity")
FIELD_PROMPTS = {
    "date": "pick_date",
    "start": "ask_start",
    "end": "ask_end",
    "break": "ask_break",
    "activity": "ask_activity",
}
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
    service = deps(ctx).service
    if user.can_enter_for_others:
        # Erst den Mitarbeiter wählen – sonst wären bei großen Teams ältere Einträge nicht erreichbar.
        counts: dict[int, tuple[str, int]] = {}
        for e in service.editable_entries(user, limit=None):
            name, n = counts.get(e.user_id, (e.user_name, 0))
            counts[e.user_id] = (name, n + 1)
        if not counts:
            await update.message.reply_text(t(user.lang, "no_entries"))
            return ConversationHandler.END
        rows = [[(f"{name} ({n})", f"ku:{uid}")] for uid, (name, n) in sorted(counts.items(), key=lambda x: x[1][0])]
        await update.message.reply_text(t(user.lang, "pick_worker"), reply_markup=keyboard(rows))
        return PICK
    return await _show_entries(update, user, service.editable_entries(user))


async def on_pick_worker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    worker_id = int(update.callback_query.data.split(":")[1])
    return await _show_entries(update, user, deps(ctx).service.editable_entries(user, user_id=worker_id))


async def _show_entries(update: Update, user, entries):
    if not entries:
        await reply(update, t(user.lang, "no_entries"))
        return ConversationHandler.END
    rows = [[(entry_label(e, with_user=user.can_enter_for_others), f"k:{e.id}")] for e in entries]
    await reply(update, t(user.lang, "corr_pick"), keyboard(rows))
    return PICK


async def on_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    try:
        entry = deps(ctx).service.editable(int(update.callback_query.data.split(":")[1]), user)
    except (PermissionDenied, ValidationError):
        return await _finish(update, ctx, t(user.lang, "no_permission"))
    L, eid = user.lang, entry.id
    # Eintrags-ID steckt in jedem Folge-Button: ein alter Button kann nie einen anderen Eintrag treffen.
    rows = [
        [(t(L, "f_date"), f"kf:{eid}:date"), (t(L, "f_project"), f"kf:{eid}:project")],
        [(t(L, "f_start"), f"kf:{eid}:start"), (t(L, "f_end"), f"kf:{eid}:end"), (t(L, "f_break"), f"kf:{eid}:break")],
        [(t(L, "f_activity"), f"kf:{eid}:activity")],
        [(t(L, "f_delete"), f"kf:{eid}:delete")],
    ]
    detail = f"{entry_label(entry)}\n📝 {entry.activity_original}"
    await reply(update, f"{detail}\n\n{t(L, 'corr_what')}", keyboard(rows))
    return FIELD


async def on_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    _, raw_id, field = update.callback_query.data.split(":")
    try:
        entry_id = deps(ctx).service.editable(int(raw_id), user).id
    except (PermissionDenied, ValidationError):
        return await _finish(update, ctx, t(user.lang, "no_permission"))
    ctx.user_data[KEY] = {"id": entry_id}
    if field == "delete":
        rows = [[(t(user.lang, "yes"), f"kd:{entry_id}:y"), (t(user.lang, "no"), f"kd:{entry_id}:n")]]
        await reply(update, t(user.lang, "confirm_delete"), keyboard(rows))
        return DELETE
    if field == "project":
        projects = deps(ctx).repo.list_projects()
        rows = [[(f"{p.customer_name} · {p.name}"[:60], f"kp:{entry_id}:{p.id}")] for p in projects]
        await reply(update, t(user.lang, "pick_project"), keyboard(rows))
        return PROJECT
    if field not in TEXT_FIELDS:
        return await _finish(update, ctx, t(user.lang, "error"))
    _state(ctx)["field"] = field
    await reply(update, t(user.lang, FIELD_PROMPTS[field]))
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
            entry = service.change_activity(entry_id, user, raw[:MAX_ACTIVITY_LEN], user.lang)
            background(
                ctx,
                _translate_later(ctx, entry.id, entry.activity_original, user.lang),
                update.effective_chat.id,
                user.lang,
            )
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
            minutes = parse_break(raw)
            if minutes is None:
                await update.message.reply_text(t(user.lang, "bad_break_format"))
                return VALUE
            service.change_times(entry_id, user, break_minutes=minutes)
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
    _, entry_id, project_id = (int(x) if x.isdigit() else x for x in update.callback_query.data.split(":"))
    try:
        deps(ctx).service.change_project(entry_id, user, project_id)
    except PermissionDenied:
        return await _finish(update, ctx, t(user.lang, "no_permission"))
    except ValidationError as exc:
        return await _finish(update, ctx, error_text(user.lang, exc))
    return await _finish(update, ctx, t(user.lang, "corr_ok"))


async def on_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    _, raw_id, answer = update.callback_query.data.split(":")
    if answer == "n":
        return await _finish(update, ctx, t(user.lang, "cancelled"))
    entry_id = int(raw_id)
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
            "korrektur",
            [CommandHandler("korrektur", cmd_korrektur)],
            {
                PICK: [
                    CallbackQueryHandler(on_pick, pattern=r"^k:\d+$"),
                    CallbackQueryHandler(on_pick_worker, pattern=r"^ku:\d+$"),
                ],
                FIELD: [
                    CallbackQueryHandler(on_field, pattern=r"^kf:\d+:(date|project|start|end|break|activity|delete)$")
                ],
                VALUE: [MessageHandler(TEXT, on_value)],
                PROJECT: [CallbackQueryHandler(on_project, pattern=r"^kp:\d+:\d+$")],
                DELETE: [CallbackQueryHandler(on_delete, pattern=r"^kd:\d+:[yn]$")],
            },
        )
    ]
