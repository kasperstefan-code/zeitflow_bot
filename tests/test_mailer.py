import smtplib

import pytest

from zeitflow.config import SmtpSettings
from zeitflow.export import RenderedFile
from zeitflow.mailer import MailError, SmtpMailer

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def settings(**kw):
    base = dict(
        host="smtp.example.org", port=587, user="bot", password="pw", sender="bot@example.org", security="starttls"
    )
    return SmtpSettings(**{**base, **kw})


def test_message_has_attachment():
    msg = SmtpMailer(settings()).build_message(
        "a@example.org", "Betreff", "Text", [RenderedFile("x.xlsx", b"data", XLSX)]
    )
    assert msg["To"] == "a@example.org" and msg["From"] == "bot@example.org"
    parts = list(msg.iter_attachments())
    assert parts[0].get_filename() == "x.xlsx"
    assert parts[0].get_content() == b"data"


async def test_not_configured():
    with pytest.raises(MailError):
        await SmtpMailer(settings(host="")).send("a@example.org", "s", "b", [])


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout):
        self.calls = [("connect", host, port)]
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"]))


async def test_send_uses_starttls_and_login(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    await SmtpMailer(settings()).send("a@example.org", "s", "b", [])
    assert FakeSMTP.instances[0].calls == [
        ("connect", "smtp.example.org", 587),
        ("starttls",),
        ("login", "bot"),
        ("send", "a@example.org"),
    ]


async def test_smtp_errors_become_mail_error(monkeypatch):
    class Broken(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    monkeypatch.setattr(smtplib, "SMTP", Broken)
    with pytest.raises(MailError):
        await SmtpMailer(settings()).send("a@example.org", "s", "b", [])


async def test_non_ascii_password_becomes_mail_error(monkeypatch):
    class Unicode(FakeSMTP):
        def login(self, user, password):
            password.encode("ascii")

    monkeypatch.setattr(smtplib, "SMTP", Unicode)
    with pytest.raises(MailError):
        await SmtpMailer(settings(password="pässwort")).send("a@example.org", "s", "b", [])
