from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str
    security: str  # "starttls" | "ssl" | "none"

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: Path
    timezone: ZoneInfo
    admin_telegram_ids: frozenset[int]
    report_recipient: str
    report_weekday: int  # 0 = Sonntag (python-telegram-bot-Konvention)
    report_time: time
    smtp: SmtpSettings
    anthropic_api_key: str
    translation_model: str
    company_name: str = ""

    @property
    def translation_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"ZEITFLOW_ADMIN_IDS enthält keine Telegram-ID: {part!r}")
        ids.add(int(part))
    return frozenset(ids)


def _parse_time(raw: str) -> time:
    try:
        hh, mm = raw.strip().split(":")
        return time(int(hh), int(mm))
    except ValueError as exc:
        raise ConfigError(f"Ungültige Uhrzeit {raw!r}, erwartet HH:MM") from exc


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    tz_name = env.get("ZEITFLOW_TIMEZONE", "Europe/Berlin")
    weekday = int(env.get("ZEITFLOW_REPORT_WEEKDAY", "0"))
    if not 0 <= weekday <= 6:
        raise ConfigError("ZEITFLOW_REPORT_WEEKDAY muss 0 (So) bis 6 (Sa) sein")
    security = env.get("ZEITFLOW_SMTP_SECURITY", "starttls").lower()
    if security not in {"starttls", "ssl", "none"}:
        raise ConfigError("ZEITFLOW_SMTP_SECURITY muss starttls, ssl oder none sein")
    default_port = {"starttls": 587, "ssl": 465, "none": 25}[security]
    return Settings(
        bot_token=env.get("ZEITFLOW_BOT_TOKEN", ""),
        db_path=Path(env.get("ZEITFLOW_DB_PATH", "zeitflow.db")),
        timezone=ZoneInfo(tz_name),
        admin_telegram_ids=_parse_ids(env.get("ZEITFLOW_ADMIN_IDS", "")),
        report_recipient=env.get("ZEITFLOW_REPORT_EMAIL", "").strip(),
        report_weekday=weekday,
        report_time=_parse_time(env.get("ZEITFLOW_REPORT_TIME", "19:00")),
        smtp=SmtpSettings(
            host=env.get("ZEITFLOW_SMTP_HOST", ""),
            port=int(env.get("ZEITFLOW_SMTP_PORT", str(default_port))),
            user=env.get("ZEITFLOW_SMTP_USER", ""),
            password=env.get("ZEITFLOW_SMTP_PASSWORD", ""),
            sender=env.get("ZEITFLOW_SMTP_FROM", env.get("ZEITFLOW_SMTP_USER", "")),
            security=security,
        ),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        translation_model=env.get("ZEITFLOW_TRANSLATION_MODEL", "claude-opus-5-5"),
        company_name=env.get("ZEITFLOW_COMPANY_NAME", ""),
    )
