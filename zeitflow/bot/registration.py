"""Anmeldung, Freigabe durch das Büro, Sprache, Hilfe."""

from __future__ import annotations

import logging

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
from zeitflow.i18n import DEFAULT_LANG, LANGUAGES, t
from zeitflow.models import Role, UserStatus

LOG = logging.getLogger(__name__)

REG_NAME = 0
MAX_NAME_LEN = 60


def _language_keyboard(prefix: str):
    return keyboard([[(label, f"{prefix}:{code}")] for code, label in LANGUAGES.items()])


def menu_text(user) -> str:
    text = t(user.lang, "menu")
    if user.can_enter_for_others:
        text += "\n\n🦺 Vorarbeiter:\n/export – Stundennachweis als Excel"
    if user.is_admin:
        text += (
            "\n\n👑 Büro:\n/mitarbeiter – Mitarbeiter, Rollen, Freigaben\n/kunden – Kunden & Projekte\n"
            "/neuerkunde – Kunde anlegen\n/neuesprojekt – Projekt anlegen\n/projekt – Projekt ändern\n"
            "/anfahrt – Anfahrten je Kunde/KW\n/versenden – Stundennachweise jetzt per E-Mail senden"
        )
    return text


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = deps(ctx).repo.get_user_by_telegram_id(update.effective_user.id)
    if user is None:
        await update.message.reply_text("🌍", reply_markup=_language_keyboard("rl"))
        return REG_NAME
    if user.status is UserStatus.PENDING:
        await update.message.reply_text(t(user.lang, "pending", name=user.name))
    elif user.status is UserStatus.INACTIVE:
        await update.message.reply_text(t(user.lang, "not_active"))
    else:
        await update.message.reply_text(menu_text(user))
    return ConversationHandler.END


async def reg_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = update.callback_query.data.split(":", 1)[1]
    ctx.user_data["reg_lang"] = lang if lang in LANGUAGES else DEFAULT_LANG
    await reply(update, t(ctx.user_data["reg_lang"], "ask_name"))
    return REG_NAME


async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = ctx.user_data.get("reg_lang", DEFAULT_LANG)
    name = " ".join(update.message.text.split())
    if len(name.split()) < 2 or len(name) > MAX_NAME_LEN:
        await update.message.reply_text(t(lang, "bad_name"))
        return REG_NAME

    d = deps(ctx)
    tg_id = update.effective_user.id
    if d.repo.get_user_by_telegram_id(tg_id):
        return ConversationHandler.END

    configured_admin = tg_id in d.settings.admin_telegram_ids
    bootstrap_admin = not d.settings.admin_telegram_ids and not d.repo.has_admin()
    if configured_admin or bootstrap_admin:
        if bootstrap_admin:
            LOG.warning("Kein Admin konfiguriert – erster Nutzer %s wird Admin. ZEITFLOW_ADMIN_IDS setzen!", tg_id)
        user = d.repo.create_user(tg_id, name, lang, Role.ADMIN, UserStatus.ACTIVE)
        await update.message.reply_text(menu_text(user))
        return ConversationHandler.END

    user = d.repo.create_user(tg_id, name, lang, Role.EMPLOYEE, UserStatus.PENDING)
    await update.message.reply_text(t(lang, "pending", name=name))
    markup = keyboard([[("✅ Freigeben", f"ap:{user.id}"), ("❌ Ablehnen", f"rj:{user.id}")]])
    for admin in d.repo.list_admins():
        try:
            await ctx.bot.send_message(
                admin.telegram_id,
                f"🆕 Neue Anmeldung: {name} ({LANGUAGES.get(lang, lang)})",
                reply_markup=markup,
            )
        except Exception:  # noqa: BLE001 – ein nicht erreichbarer Admin darf die Anmeldung nicht blockieren
            LOG.exception("Admin %s nicht erreichbar", admin.id)
    return ConversationHandler.END


async def approval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin = await current_user(update, ctx, min_role=Role.ADMIN)
    if admin is None:
        return
    action, raw_id = update.callback_query.data.split(":")
    d = deps(ctx)
    target = d.repo.get_user(int(raw_id))
    if target is None:
        await reply(update, "Mitarbeiter nicht gefunden.")
        return
    if target.status is not UserStatus.PENDING:
        await reply(update, f"{target.name} wurde bereits bearbeitet ({target.status.value}).")
        return
    approved = action == "ap"
    d.repo.update_user(target.id, status=UserStatus.ACTIVE if approved else UserStatus.INACTIVE)
    await reply(update, f"{'✅ Freigegeben' if approved else '❌ Abgelehnt'}: {target.name}")
    await ctx.bot.send_message(target.telegram_id, t(target.lang, "approved" if approved else "rejected"))


async def cmd_sprache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await current_user(update, ctx):
        await update.message.reply_text("🌍", reply_markup=_language_keyboard("sl"))


async def set_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user is None:
        return
    lang = update.callback_query.data.split(":", 1)[1]
    if lang not in LANGUAGES:
        return
    deps(ctx).repo.update_user(user.id, lang=lang)
    await reply(update, t(lang, "lang_set"))


async def cmd_hilfe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await current_user(update, ctx)
    if user:
        await update.message.reply_text(menu_text(user))


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = deps(ctx).repo.get_user_by_telegram_id(update.effective_user.id)
    ctx.user_data.clear()
    await reply(update, t(user.lang if user else DEFAULT_LANG, "cancelled"))
    return ConversationHandler.END


def handlers():
    registration = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            REG_NAME: [
                CallbackQueryHandler(reg_lang, pattern=r"^rl:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=900,
    )
    return [
        registration,
        CallbackQueryHandler(approval, pattern=r"^(ap|rj):\d+$"),
        CommandHandler("sprache", cmd_sprache),
        CallbackQueryHandler(set_lang, pattern=r"^sl:"),
        CommandHandler(["hilfe", "help"], cmd_hilfe),
    ]
