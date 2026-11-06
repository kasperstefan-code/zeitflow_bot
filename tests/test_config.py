from datetime import time

import pytest

from zeitflow.config import ConfigError, load_settings


def test_defaults():
    s = load_settings({})
    assert s.report_weekday == 0
    assert s.report_time == time(19, 0)
    assert str(s.timezone) == "Europe/Berlin"
    assert s.smtp.port == 587 and s.smtp.security == "starttls"
    assert not s.smtp.configured
    assert not s.translation_enabled
    assert s.translation_model == "claude-opus-5-5"


def test_admin_ids_and_smtp():
    s = load_settings(
        {
            "ZEITFLOW_ADMIN_IDS": "123, 456;789",
            "ZEITFLOW_SMTP_HOST": "smtp.example.org",
            "ZEITFLOW_SMTP_USER": "bot@example.org",
            "ZEITFLOW_SMTP_SECURITY": "ssl",
        }
    )
    assert s.admin_telegram_ids == {123, 456, 789}
    assert s.smtp.port == 465
    assert s.smtp.sender == "bot@example.org"
    assert s.smtp.configured


@pytest.mark.parametrize(
    "env",
    [
        {"ZEITFLOW_ADMIN_IDS": "abc"},
        {"ZEITFLOW_REPORT_TIME": "19 Uhr"},
        {"ZEITFLOW_REPORT_WEEKDAY": "7"},
        {"ZEITFLOW_SMTP_SECURITY": "tls"},
    ],
)
def test_invalid_config(env):
    with pytest.raises(ConfigError):
        load_settings(env)
