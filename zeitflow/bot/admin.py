"""Büro-Funktionen (nur Deutsch): Stammdaten, Mitarbeiter, Anfahrten, Export, Versand."""

from __future__ import annotations

import io
import logging
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

from zeitflow.bot.common import current_user, deps, keyboard, reply
from zeitflow.bot.registration import cancel
from zeitflow.export import render_report
from zeitflow.i18n import t
from zeitflow.models import Role, UserStatus
from zeitflow.reporting import WeeklyRunResult
from zeitflow.services import build_week_report
from zeitflow.timeutil import iso_week_range, parse_week

LOG = logging.getLogger(__name__)
TEXT = filters.TEXT & ~filters.COMMAND
ROLE_LABELS = {Role.EMPLOYEE: "👤 Mitarbeiter", Role.FOREMAN: "🦺 Vorarbeiter", Role.ADMIN: "👑 Admin"}
STATUS_LABELS = {UserStatus.PENDING: "⏳ wartet", UserStatus.ACTIVE: "", UserStatus.INACTIVE: "⛔ gesperrt"}


def _week_keyboard(ctx, prefix: str):
    today = deps(ctx).today()
    cur = today.isocalendar()
    prev = (today - timedelta(days=7)).isocalendar()
    return keyboard(
        [
            [
                (f"KW {cur.week:02d} (aktuell)", f"{prefix}:{cur.year}:{cur.week}"),
                (f"KW {prev.week:02d} (Vorwoche)", f"{prefix}:{prev.year}:{prev.week}"),
            ]
        ]
    )


def _week_from(update: Update, ctx) -> tuple[int, int] | None:
    if update.callback_query:
        _, year, week = update.callback_query.data.split(":")
        return int(year), int(week)
    return parse_week(update.message.text, deps(ctx).today())


# --- Mitarbeiter --------------------------------------------------------------------


async def cmd_mitarbeiter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return
    await _user_list(update, ctx)


async def _user_list(update: Update, ctx):
    users = deps(ctx).repo.list_users(status=None)
    rows = [[(f"{ROLE_LABELS[u.role]} {u.name} {STATUS_LABELS[u.status]}".strip(), f"mu:{u.id}")] for u in users]
    await reply(update, "Mitarbeiter auswählen:", keyboard(rows) if rows else None)


async def on_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return
    await _show_user(update, ctx, int(update.callback_query.data.split(":")[1]))


async def _show_user(update: Update, ctx, user_id: int):
    target = deps(ctx).repo.get_user(user_id)
    if target is None:
        await reply(update, "Nicht gefunden.")
        return
    rows = [[(label, f"mr:{target.id}:{role.value}") for role, label in ROLE_LABELS.items()]]
    if target.status is UserStatus.ACTIVE:
        rows.append([("⛔ Sperren", f"ms:{target.id}:inactive")])
    else:
        rows.append([("✅ Freischalten", f"ms:{target.id}:active")])
    rows.append([("⬅️ Zurück", "ml")])
    text = f"{target.name}\nRolle: {ROLE_LABELS[target.role]}\nStatus: {target.status.value}"
    await reply(update, text, keyboard(rows))


async def on_user_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin = await current_user(update, ctx, min_role=Role.ADMIN)
    if admin is None:
        return
    data = update.callback_query.data
    if data == "ml":
        await _user_list(update, ctx)
        return
    kind, raw_id, value = data.split(":")
    repo = deps(ctx).repo
    target = repo.get_user(int(raw_id))
    if target is None:
        await reply(update, "Nicht gefunden.")
        return
    if target.id == admin.id:
        await update.callback_query.answer(
            "Eigene Rolle/Status bitte von einem anderen Admin ändern lassen.", show_alert=True
        )
        return
    was_pending = target.status is UserStatus.PENDING
    if kind == "mr":
        repo.update_user(target.id, role=Role(value))
    else:
        repo.update_user(target.id, status=UserStatus(value))
        if value == "active" and was_pending:
            await ctx.bot.send_message(target.telegram_id, t(target.lang, "approved"))
    await _show_user(update, ctx, target.id)


# --- Kunden & Projekte --------------------------------------------------------------

NEW_CUSTOMER = 0
NP_CUSTOMER, NP_NAME, NP_COST = range(10, 13)
EP_PICK, EP_FIELD, EP_VALUE = range(20, 23)


async def cmd_kunden(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return
    repo = deps(ctx).repo
    lines = []
    for c in repo.list_customers(active_only=False):
        lines.append(f"🏢 {c.name}" + ("" if c.active else " (inaktiv)"))
        for p in repo.list_projects(customer_id=c.id, active_only=False):
            lines.append(f"   📋 {p.name} · KSt {p.cost_center or '–'}" + ("" if p.active else " (inaktiv)"))
    await update.message.reply_text("\n".join(lines) or "Noch keine Kunden. /neuerkunde")


async def cmd_neuerkunde(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    await update.message.reply_text("Name des Kunden?")
    return NEW_CUSTOMER


async def on_new_customer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    name = " ".join(update.message.text.split())
    repo = deps(ctx).repo
    if any(c.name.lower() == name.lower() for c in repo.list_customers(active_only=False)):
        await update.message.reply_text("Diesen Kunden gibt es schon. Anderer Name?")
        return NEW_CUSTOMER
    customer = repo.create_customer(name)
    await update.message.reply_text(f"✅ Kunde angelegt: {customer.name}\nJetzt Projekt anlegen: /neuesprojekt")
    return ConversationHandler.END


async def cmd_neuesprojekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    customers = deps(ctx).repo.list_customers()
    if not customers:
        await update.message.reply_text("Zuerst einen Kunden anlegen: /neuerkunde")
        return ConversationHandler.END
    await update.message.reply_text(
        "Für welchen Kunden?", reply_markup=keyboard([[(c.name, f"npc:{c.id}")] for c in customers])
    )
    return NP_CUSTOMER


async def on_np_customer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    ctx.user_data["np_customer"] = int(update.callback_query.data.split(":")[1])
    await reply(update, "Projektbezeichnung?")
    return NP_NAME


async def on_np_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    ctx.user_data["np_name"] = " ".join(update.message.text.split())
    await update.message.reply_text("Kostenstelle? (oder - für keine)")
    return NP_COST


async def on_np_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    cost = update.message.text.strip()
    cost = "" if cost == "-" else cost
    try:
        project = deps(ctx).repo.create_project(ctx.user_data["np_customer"], ctx.user_data["np_name"], cost)
    except KeyError:
        await update.message.reply_text("Sitzung abgelaufen, bitte /neuesprojekt neu starten.")
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ Projekt angelegt:\n🏢 {project.customer_name}\n📋 {project.name}\n🏷 {project.cost_center or '–'}"
    )
    return ConversationHandler.END


async def cmd_projekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    projects = deps(ctx).repo.list_projects()
    if not projects:
        await update.message.reply_text("Keine aktiven Projekte.")
        return ConversationHandler.END
    rows = [[(f"{p.customer_name} · {p.name}"[:60], f"ep:{p.id}")] for p in projects]
    await update.message.reply_text("Welches Projekt?", reply_markup=keyboard(rows))
    return EP_PICK


async def on_ep_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    ctx.user_data["ep_id"] = int(update.callback_query.data.split(":")[1])
    rows = [[("📋 Bezeichnung", "ef:name"), ("🏷 Kostenstelle", "ef:cost_center")], [("⛔ Deaktivieren", "ef:off")]]
    await reply(update, "Was ändern?", keyboard(rows))
    return EP_FIELD


async def on_ep_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    field = update.callback_query.data.split(":")[1]
    if field == "off":
        deps(ctx).repo.update_project(ctx.user_data["ep_id"], active=0)
        ctx.user_data.clear()
        await reply(update, "✅ Projekt deaktiviert. Vorhandene Einträge bleiben erhalten.")
        return ConversationHandler.END
    ctx.user_data["ep_field"] = field
    await reply(update, "Neuer Wert?")
    return EP_VALUE


async def on_ep_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    value = update.message.text.strip()
    deps(ctx).repo.update_project(ctx.user_data["ep_id"], **{ctx.user_data["ep_field"]: value})
    ctx.user_data.clear()
    await update.message.reply_text("✅ Gespeichert.")
    return ConversationHandler.END


# --- Anfahrten ----------------------------------------------------------------------

TR_WEEK, TR_CUSTOMER, TR_VALUE = range(30, 33)


async def cmd_anfahrt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    await update.message.reply_text("Welche KW? (oder eingeben, z. B. 13/2026)", reply_markup=_week_keyboard(ctx, "tw"))
    return TR_WEEK


async def _ask_week_customer(update: Update, ctx, prefix: str, next_state: int):
    week = _week_from(update, ctx)
    if week is None:
        await reply(update, "KW nicht erkannt. Beispiel: 13/2026")
        return None
    ctx.user_data["week"] = week
    monday, sunday = iso_week_range(*week)
    customers = deps(ctx).repo.customers_with_entries(monday, sunday)
    if not customers:
        await reply(update, f"Keine Einträge in KW {week[1]:02d}/{week[0]}.")
        return ConversationHandler.END
    rows = [[(c.name, f"{prefix}:{c.id}")] for c in customers]
    await reply(update, f"KW {week[1]:02d}/{week[0]} – welcher Kunde?", keyboard(rows))
    return next_state


async def on_tr_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    state = await _ask_week_customer(update, ctx, "tc", TR_CUSTOMER)
    return TR_WEEK if state is None else state


async def on_tr_customer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    customer_id = int(update.callback_query.data.split(":")[1])
    ctx.user_data["customer_id"] = customer_id
    year, week = ctx.user_data["week"]
    current = deps(ctx).repo.get_travel_count(customer_id, year, week)
    await reply(update, f"Anzahl Anfahrten? (aktuell: {current if current is not None else 'leer'}; - zum Leeren)")
    return TR_VALUE


async def on_tr_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    raw = update.message.text.strip()
    if raw != "-" and not (raw.isdigit() and int(raw) <= 99):
        await update.message.reply_text("Bitte eine Zahl (0–99) oder - eingeben.")
        return TR_VALUE
    year, week = ctx.user_data["week"]
    deps(ctx).repo.set_travel_count(ctx.user_data["customer_id"], year, week, None if raw == "-" else int(raw))
    ctx.user_data.clear()
    await update.message.reply_text("✅ Anfahrten gespeichert.")
    return ConversationHandler.END


# --- Export (Vorarbeiter + Admin) ---------------------------------------------------

EX_WEEK, EX_CUSTOMER = range(40, 42)


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.FOREMAN) is None:
        return ConversationHandler.END
    await update.message.reply_text("Welche KW? (oder eingeben, z. B. 13/2026)", reply_markup=_week_keyboard(ctx, "xw"))
    return EX_WEEK


async def on_ex_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.FOREMAN) is None:
        return ConversationHandler.END
    state = await _ask_week_customer(update, ctx, "xc", EX_CUSTOMER)
    return EX_WEEK if state is None else state


async def on_ex_customer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.FOREMAN) is None:
        return ConversationHandler.END
    d = deps(ctx)
    year, week = ctx.user_data["week"]
    customer_id = int(update.callback_query.data.split(":")[1])
    await reply(update, "⏳ Erstelle Stundennachweis …")
    monday, sunday = iso_week_range(year, week)
    await d.service.translate_pending(monday, sunday, customer_id=customer_id)
    report = build_week_report(d.repo, customer_id, year, week)
    file = render_report(report)
    caption = f"KW {week:02d}/{year} · {report.customer_name}"
    if report.untranslated:
        caption += f"\n⚠️ {report.untranslated} Tätigkeit(en) noch nicht übersetzt"
    await ctx.bot.send_document(
        update.effective_chat.id, document=io.BytesIO(file.content), filename=file.filename, caption=caption
    )
    ctx.user_data.clear()
    return ConversationHandler.END


# --- Versand (Admin) ----------------------------------------------------------------

SEND_WEEK, SEND_MODE = range(50, 52)


async def cmd_versenden(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    await update.message.reply_text(
        f"Stundennachweise per E-Mail an {deps(ctx).settings.report_recipient or '(keine Adresse konfiguriert)'}.\n"
        "Welche KW?",
        reply_markup=_week_keyboard(ctx, "sw"),
    )
    return SEND_WEEK


async def on_send_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    week = _week_from(update, ctx)
    if week is None:
        await reply(update, "KW nicht erkannt. Beispiel: 13/2026")
        return SEND_WEEK
    ctx.user_data["week"] = week
    rows = [[("📨 Nur noch nicht versendete", "sm:open")], [("🔁 Alle erneut senden", "sm:all")]]
    await reply(update, f"KW {week[1]:02d}/{week[0]}:", keyboard(rows))
    return SEND_MODE


async def on_send_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx, min_role=Role.ADMIN) is None:
        return ConversationHandler.END
    year, week = ctx.user_data.pop("week")
    await reply(update, "⏳ Versand läuft …")
    result = await deps(ctx).reporter.run(year, week, force=update.callback_query.data == "sm:all")
    await deliver_result(ctx, result, chat_ids=[update.effective_chat.id])
    return ConversationHandler.END


async def deliver_result(ctx, result: WeeklyRunResult, chat_ids: list[int]) -> None:
    """Meldet das Versandergebnis; nicht versendete Dateien gehen direkt per Telegram mit."""
    for chat_id in chat_ids:
        try:
            await ctx.bot.send_message(chat_id, result.summary())
            for r in result.results:
                if r.status == "failed" and r.file:
                    await ctx.bot.send_document(
                        chat_id,
                        document=io.BytesIO(r.file.content),
                        filename=r.file.filename,
                        caption=f"Nicht per E-Mail versendet: {r.customer_name}",
                    )
        except Exception:  # noqa: BLE001 – Benachrichtigung ist best effort
            LOG.exception("Versandergebnis an Chat %s nicht zustellbar", chat_id)


def handlers():
    conv = dict(fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=900, allow_reentry=True)
    return [
        CommandHandler("mitarbeiter", cmd_mitarbeiter),
        CallbackQueryHandler(on_user, pattern=r"^mu:\d+$"),
        CallbackQueryHandler(on_user_action, pattern=r"^(mr:\d+:\w+|ms:\d+:\w+|ml)$"),
        CommandHandler("kunden", cmd_kunden),
        ConversationHandler(
            entry_points=[CommandHandler("neuerkunde", cmd_neuerkunde)],
            states={NEW_CUSTOMER: [MessageHandler(TEXT, on_new_customer)]},
            **conv,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("neuesprojekt", cmd_neuesprojekt)],
            states={
                NP_CUSTOMER: [CallbackQueryHandler(on_np_customer, pattern=r"^npc:\d+$")],
                NP_NAME: [MessageHandler(TEXT, on_np_name)],
                NP_COST: [MessageHandler(TEXT, on_np_cost)],
            },
            **conv,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("projekt", cmd_projekt)],
            states={
                EP_PICK: [CallbackQueryHandler(on_ep_pick, pattern=r"^ep:\d+$")],
                EP_FIELD: [CallbackQueryHandler(on_ep_field, pattern=r"^ef:\w+$")],
                EP_VALUE: [MessageHandler(TEXT, on_ep_value)],
            },
            **conv,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("anfahrt", cmd_anfahrt)],
            states={
                TR_WEEK: [CallbackQueryHandler(on_tr_week, pattern=r"^tw:\d+:\d+$"), MessageHandler(TEXT, on_tr_week)],
                TR_CUSTOMER: [CallbackQueryHandler(on_tr_customer, pattern=r"^tc:\d+$")],
                TR_VALUE: [MessageHandler(TEXT, on_tr_value)],
            },
            **conv,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("export", cmd_export)],
            states={
                EX_WEEK: [CallbackQueryHandler(on_ex_week, pattern=r"^xw:\d+:\d+$"), MessageHandler(TEXT, on_ex_week)],
                EX_CUSTOMER: [CallbackQueryHandler(on_ex_customer, pattern=r"^xc:\d+$")],
            },
            **conv,
        ),
        ConversationHandler(
            entry_points=[CommandHandler("versenden", cmd_versenden)],
            states={
                SEND_WEEK: [
                    CallbackQueryHandler(on_send_week, pattern=r"^sw:\d+:\d+$"),
                    MessageHandler(TEXT, on_send_week),
                ],
                SEND_MODE: [CallbackQueryHandler(on_send_mode, pattern=r"^sm:(open|all)$")],
            },
            **conv,
        ),
    ]
