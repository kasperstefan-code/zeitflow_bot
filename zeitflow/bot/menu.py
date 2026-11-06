"""Telegram-Befehlsmenüs: je Sprache für Monteure, je Chat für Vorarbeiter/Admins."""

from __future__ import annotations

import logging

from telegram import Bot, BotCommand, BotCommandScopeChat
from telegram.error import TelegramError

from zeitflow.i18n import COMMAND_DESCRIPTIONS, DEFAULT_LANG, WORKER_COMMANDS
from zeitflow.models import Role, User

LOG = logging.getLogger(__name__)

FOREMAN_COMMANDS = [("export", "Stundennachweis als Excel")]
ADMIN_COMMANDS = [
    ("mitarbeiter", "Mitarbeiter, Rollen, Freigaben"),
    ("kunden", "Kunden & Projekte anzeigen"),
    ("neuerkunde", "Kunde anlegen"),
    ("neuesprojekt", "Projekt anlegen"),
    ("projekt", "Projekt ändern/deaktivieren"),
    ("anfahrt", "Anfahrten je Kunde/KW"),
    ("versenden", "Stundennachweise jetzt senden"),
    ("testmail", "Testmail an eine Adresse senden"),
]


def _worker_commands(lang: str) -> list[BotCommand]:
    descriptions = COMMAND_DESCRIPTIONS.get(lang, COMMAND_DESCRIPTIONS[DEFAULT_LANG])
    return [BotCommand(c, d) for c, d in zip(WORKER_COMMANDS, descriptions, strict=True)]


async def set_default_commands(bot: Bot) -> None:
    for lang in COMMAND_DESCRIPTIONS:
        await bot.set_my_commands(_worker_commands(lang), language_code=None if lang == DEFAULT_LANG else lang)


async def set_user_commands(bot: Bot, user: User) -> None:
    """Chat-Menü passend zur Rolle; Mitarbeiter fallen auf das Sprachmenü zurück."""
    scope = BotCommandScopeChat(user.telegram_id)
    try:
        if not user.is_active or not user.can_enter_for_others:
            await bot.delete_my_commands(scope=scope)
            return
        extra = FOREMAN_COMMANDS + (ADMIN_COMMANDS if user.role is Role.ADMIN else [])
        await bot.set_my_commands(_worker_commands(user.lang) + [BotCommand(c, d) for c, d in extra], scope=scope)
    except TelegramError:
        LOG.warning("Befehlsmenü für %s nicht gesetzt (Chat unbekannt?)", user.name)
