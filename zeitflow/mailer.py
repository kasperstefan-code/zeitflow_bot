from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from zeitflow.config import SmtpSettings
from zeitflow.export import RenderedFile


class MailError(RuntimeError):
    pass


class Mailer(Protocol):
    async def send(self, recipient: str, subject: str, body: str, attachments: list[RenderedFile]) -> None: ...


class SmtpMailer:
    def __init__(self, settings: SmtpSettings, timeout: float = 30.0):
        self.settings = settings
        self.timeout = timeout

    def build_message(self, recipient: str, subject: str, body: str, attachments: list[RenderedFile]) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.settings.sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)
        for f in attachments:
            maintype, subtype = f.mime_type.split("/", 1)
            msg.add_attachment(f.content, maintype=maintype, subtype=subtype, filename=f.filename)
        return msg

    def _send_sync(self, msg: EmailMessage) -> None:
        s = self.settings
        try:
            if s.security == "ssl":
                smtp = smtplib.SMTP_SSL(s.host, s.port, timeout=self.timeout, context=ssl.create_default_context())
            else:
                smtp = smtplib.SMTP(s.host, s.port, timeout=self.timeout)
            with smtp:
                if s.security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if s.user:
                    smtp.login(s.user, s.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"E-Mail-Versand fehlgeschlagen: {exc}") from exc

    async def send(self, recipient: str, subject: str, body: str, attachments: list[RenderedFile]) -> None:
        if not self.settings.configured:
            raise MailError("SMTP ist nicht konfiguriert (ZEITFLOW_SMTP_HOST / ZEITFLOW_SMTP_FROM)")
        msg = self.build_message(recipient, subject, body, attachments)
        await asyncio.to_thread(self._send_sync, msg)
