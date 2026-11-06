"""/zeit – geführte Erfassung. Vorarbeiter können mehrere Monteure gleichzeitig erfassen."""

from __future__ import annotations

from datetime import date, timedelta

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
    chunk,
    conversation,
    current_user,
    deps,
    error_text,
    fmt_date,
    fmt_hours,
    keyboard,
    overlap_lines,
    reply,
)
from zeitflow.i18n import t
from zeitflow.models import User
from zeitflow.services import EDIT_WINDOW_DAYS, MAX_PAST_DAYS, EntryDraft, OverlapError, ValidationError
from zeitflow.timeutil import parse_date, parse_time, short_name, to_minutes, work_minutes

(WORKERS, CUSTOMER, PROJECT, DATE, START, END, BREAK, ACTIVITY, CONFIRM, MORE) = range(10)

START_TIMES = ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30"]
END_TIMES = ["15:00", "15:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00"]
BREAKS = [0, 15, 30, 45, 60]
MAX_ACTIVITY_LEN = 500


def _draft(ctx) -> dict:
    return ctx.user_data.setdefault("draft", {})


async def _expired(update: Update, user: User) -> int:
    await reply(update, t(user.lang, "session_expired"))
    return ConversationHandler.END


# --- Monteure ---------------------------------------------------------------------


def _workers_keyboard(ctx, user: User):
    selected = set(_draft(ctx).get("workers", []))
    rows = []
    for u in deps(ctx).repo.list_users():
        label = t(user.lang, "me") if u.id == user.id else u.name
        rows.append([(("✅ " if u.id in selected else "▫️ ") + label, f"w:{u.id}")])
    rows.append([(t(user.lang, "next"), "w:next")])
    return keyboard(rows)


async def cmd_zeit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    ctx.user_data["draft"] = {"workers": [user.id]}
    if user.can_enter_for_others:
        await update.message.reply_text(t(user.lang, "pick_workers"), reply_markup=_workers_keyboard(ctx, user))
        return WORKERS
    return await _ask_customer(update, ctx, user)


async def on_worker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None or not user.can_enter_for_others:
        return ConversationHandler.END
    draft = _draft(ctx)
    value = update.callback_query.data.split(":", 1)[1]
    if value == "next":
        if not draft.get("workers"):
            await update.callback_query.answer(t(user.lang, "need_worker"), show_alert=True)
            return WORKERS
        return await _ask_customer(update, ctx, user)
    worker_id = int(value)
    workers = draft.setdefault("workers", [])
    if worker_id in workers:
        workers.remove(worker_id)
    else:
        worker = deps(ctx).repo.get_user(worker_id)
        if worker is not None and worker.is_active:
            workers.append(worker_id)
    await reply(update, t(user.lang, "pick_workers"), _workers_keyboard(ctx, user))
    return WORKERS


# --- Kunde & Projekt --------------------------------------------------------------


async def _ask_customer(update: Update, ctx, user: User):
    customers = [c for c in deps(ctx).repo.list_customers() if deps(ctx).repo.list_projects(customer_id=c.id)]
    if not customers:
        await reply(update, t(user.lang, "no_projects"))
        return ConversationHandler.END
    if len(customers) == 1:
        return await _ask_project(update, ctx, user, customers[0].id)
    await reply(update, t(user.lang, "pick_customer"), keyboard([[(c.name, f"c:{c.id}")] for c in customers]))
    return CUSTOMER


async def on_customer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    return await _ask_project(update, ctx, user, int(update.callback_query.data.split(":")[1]))


async def _ask_project(update: Update, ctx, user: User, customer_id: int):
    projects = deps(ctx).repo.list_projects(customer_id=customer_id)
    if not projects:
        await reply(update, t(user.lang, "no_projects"))
        return ConversationHandler.END
    if len(projects) == 1:
        return await _set_project(update, ctx, user, projects[0].id)
    rows = [[(f"{p.name} ({p.cost_center})" if p.cost_center else p.name, f"p:{p.id}")] for p in projects]
    await reply(update, t(user.lang, "pick_project"), keyboard(rows))
    return PROJECT


async def on_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    return await _set_project(update, ctx, user, int(update.callback_query.data.split(":")[1]))


async def _set_project(update: Update, ctx, user: User, project_id: int):
    project = deps(ctx).repo.get_project(project_id)
    if project is None or not project.active:
        await reply(update, t(user.lang, "no_projects"))
        return ConversationHandler.END
    draft = _draft(ctx)
    draft["project_id"] = project.id
    if "date" in draft:  # Folgeeintrag am selben Tag
        return await _ask_start(update, ctx, user)
    today = deps(ctx).today()
    rows = [
        [
            (t(user.lang, "today"), f"d:{today.isoformat()}"),
            (t(user.lang, "yesterday"), f"d:{(today - timedelta(days=1)).isoformat()}"),
        ]
    ]
    await reply(update, t(user.lang, "pick_date"), keyboard(rows))
    return DATE


# --- Datum & Zeiten ---------------------------------------------------------------


async def on_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    today = deps(ctx).today()
    if update.callback_query:
        day = parse_date(update.callback_query.data.split(":", 1)[1], today)
    else:
        day = parse_date(update.message.text, today)
    if day is None:
        await reply(update, t(user.lang, "bad_date"))
        return DATE
    if day > today:
        await reply(update, t(user.lang, "future_date"))
        return DATE
    if day < today - timedelta(days=MAX_PAST_DAYS if user.is_admin else EDIT_WINDOW_DAYS):
        await reply(update, t(user.lang, "too_old"))
        return DATE
    _draft(ctx)["date"] = day.isoformat()
    return await _ask_start(update, ctx, user)


async def _ask_start(update: Update, ctx, user: User):
    await reply(update, t(user.lang, "ask_start"), keyboard(chunk([(x, f"t:{x}") for x in START_TIMES], 3)))
    return START


def _time_input(update: Update) -> str | None:
    raw = update.callback_query.data.split(":", 1)[1] if update.callback_query else update.message.text
    return parse_time(raw)


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    value = _time_input(update)
    if value is None:
        await reply(update, t(user.lang, "bad_time"))
        return START
    _draft(ctx)["start"] = value
    options = [x for x in END_TIMES if to_minutes(x) > to_minutes(value)]
    await reply(update, t(user.lang, "ask_end"), keyboard(chunk([(x, f"t:{x}") for x in options], 3)))
    return END


async def on_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    value = _time_input(update)
    if value is None:
        await reply(update, t(user.lang, "bad_time"))
        return END
    if "start" not in _draft(ctx):
        return await _expired(update, user)
    if to_minutes(value) <= to_minutes(_draft(ctx)["start"]):
        await reply(update, t(user.lang, "end_before_start"))
        return END
    _draft(ctx)["end"] = value
    await reply(update, t(user.lang, "ask_break"), keyboard([[(str(b), f"b:{b}") for b in BREAKS]]))
    return BREAK


async def on_break(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    raw = update.callback_query.data.split(":", 1)[1] if update.callback_query else update.message.text.strip()
    draft = _draft(ctx)
    if "end" not in draft:
        return await _expired(update, user)
    try:
        minutes = int(raw)
        work_minutes(draft["start"], draft["end"], minutes)
    except ValueError:
        await reply(update, t(user.lang, "bad_break"))
        return BREAK
    draft["break"] = minutes
    await reply(update, t(user.lang, "ask_activity"))
    return ACTIVITY


# --- Tätigkeit, Bestätigung, Speichern -------------------------------------------


async def on_activity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    draft = _draft(ctx)
    if "break" not in draft:
        return await _expired(update, user)
    draft["activity"] = update.message.text.strip()[:MAX_ACTIVITY_LEN]
    repo = deps(ctx).repo
    project = repo.get_project(draft["project_id"])
    workers = [repo.get_user(uid) for uid in draft["workers"]]
    summary = t(
        user.lang,
        "summary",
        workers=", ".join(short_name(w.name) for w in workers if w),
        customer=project.customer_name,
        project=project.name,
        date=fmt_date(date.fromisoformat(draft["date"])),
        start=draft["start"],
        end=draft["end"],
        brk=draft["break"],
        hours=fmt_hours(work_minutes(draft["start"], draft["end"], draft["break"])),
        activity=draft["activity"],
    )
    markup = keyboard([[(t(user.lang, "save"), "ok"), (t(user.lang, "cancel"), "x")]])
    await update.message.reply_text(f"{summary}\n\n{t(user.lang, 'confirm')}", reply_markup=markup)
    return CONFIRM


async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    if update.callback_query.data == "x":
        ctx.user_data.clear()
        await reply(update, t(user.lang, "cancelled"))
        return ConversationHandler.END
    draft = _draft(ctx)
    if "activity" not in draft:
        return await _expired(update, user)
    await update.callback_query.answer()
    entry = EntryDraft(
        user_ids=tuple(draft["workers"]),
        project_id=draft["project_id"],
        work_date=date.fromisoformat(draft["date"]),
        start_time=draft["start"],
        end_time=draft["end"],
        break_minutes=draft["break"],
        activity=draft["activity"],
        source_lang=user.lang,
    )
    service = deps(ctx).service
    try:
        result = service.record(entry, user)
    except OverlapError as exc:
        await update.callback_query.edit_message_text(t(user.lang, "overlap", list=overlap_lines(exc.overlaps)))
        ctx.user_data.clear()
        return ConversationHandler.END
    except ValidationError as exc:
        await update.callback_query.edit_message_text(error_text(user.lang, exc))
        ctx.user_data.clear()
        return ConversationHandler.END

    background(ctx, _translate_and_report(ctx, update.effective_chat.id, user, result.entry_ids, result.activity))
    lines = [t(user.lang, "saved", count=len(result.entry_ids))]
    if user.lang != "de":
        lines.append(t(user.lang, "translation_pending"))
    lines.append("")
    lines.append(t(user.lang, "more"))
    for key in ("project_id", "start", "end", "break", "activity"):
        draft.pop(key, None)
    markup = keyboard([[(t(user.lang, "yes"), "m:y"), (t(user.lang, "no"), "m:n")]])
    await update.callback_query.edit_message_text("\n".join(lines), reply_markup=markup)
    return MORE


async def _translate_and_report(ctx, chat_id: int, user: User, entry_ids: list[int], activity: str) -> None:
    activity_de = await deps(ctx).service.translate_entries(entry_ids, activity, user.lang)
    if activity_de and user.lang != "de":
        await ctx.bot.send_message(chat_id, t(user.lang, "translated", text=activity_de))


async def on_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return ConversationHandler.END
    if update.callback_query.data == "m:y" and _draft(ctx).get("workers"):
        return await _ask_customer(update, ctx, user)
    ctx.user_data.clear()
    await reply(update, t(user.lang, "done"))
    return ConversationHandler.END


def handlers():
    text = filters.TEXT & ~filters.COMMAND
    return [
        conversation(
            [CommandHandler("zeit", cmd_zeit)],
            {
                WORKERS: [CallbackQueryHandler(on_worker, pattern=r"^w:")],
                CUSTOMER: [CallbackQueryHandler(on_customer, pattern=r"^c:\d+$")],
                PROJECT: [CallbackQueryHandler(on_project, pattern=r"^p:\d+$")],
                DATE: [CallbackQueryHandler(on_date, pattern=r"^d:"), MessageHandler(text, on_date)],
                START: [CallbackQueryHandler(on_start, pattern=r"^t:"), MessageHandler(text, on_start)],
                END: [CallbackQueryHandler(on_end, pattern=r"^t:"), MessageHandler(text, on_end)],
                BREAK: [CallbackQueryHandler(on_break, pattern=r"^b:\d+$"), MessageHandler(text, on_break)],
                ACTIVITY: [MessageHandler(text, on_activity)],
                CONFIRM: [CallbackQueryHandler(on_confirm, pattern=r"^(ok|x)$")],
                MORE: [CallbackQueryHandler(on_more, pattern=r"^m:[yn]$")],
            },
            timeout=1800,
        )
    ]
