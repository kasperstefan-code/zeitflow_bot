from __future__ import annotations

import asyncio
import base64
import http.client
import json
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Protocol

from zeitflow.config import MailSettings, Settings, SmtpSettings
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
        except (smtplib.SMTPException, OSError, UnicodeError, ValueError) as exc:
            raise MailError(f"E-Mail-Versand fehlgeschlagen: {exc}") from exc

    async def send(self, recipient: str, subject: str, body: str, attachments: list[RenderedFile]) -> None:
        if not self.settings.configured:
            raise MailError("SMTP ist nicht konfiguriert (ZEITFLOW_SMTP_HOST / ZEITFLOW_SMTP_FROM)")
        msg = self.build_message(recipient, subject, body, attachments)
        await asyncio.to_thread(self._send_sync, msg)


BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_SAFE_KEY = re.compile(r"[\x21-\x7e]+")


def _recipients(raw: str) -> list[dict]:
    """ "a@x.de, b@y.de" → mehrere Empfänger (beim SMTP-Versand ging das über den To-Header)."""
    return [{"email": addr} for _, addr in getaddresses([raw.replace(";", ",")]) if addr]


def _brevo_hint(status: int, detail: str, sender: str) -> str:
    low = detail.lower()
    if "ip" in low.split() or "ip address" in low or "unrecognised ip" in low or "unrecognized ip" in low:
        return "Brevo blockiert unbekannte IP – unter Security → Authorised IPs die IP-Sperre deaktivieren"
    if "dmarc" in low or "domain authentication" in low:
        return (
            f"Domain von {sender} bei Brevo authentifizieren (Senders, Domains → Domains) oder anderen Absender wählen"
        )
    if "sender" in low:
        return f"Absender {sender} bei Brevo unter Senders bestätigen"
    if status == 401:
        return "API-Schlüssel ungültig (ZEITFLOW_BREVO_API_KEY prüfen)"
    if status == 403:
        return "Zugriff verweigert – Konto bzw. IP-Freigabe bei Brevo prüfen"
    return ""


class BrevoMailer:
    """Versand über die Brevo-HTTPS-API – für Hoster, die SMTP-Ports sperren (z. B. Railway Hobby)."""

    def __init__(self, settings: MailSettings, timeout: float = 30.0, url: str = BREVO_URL):
        self.settings = settings
        self.timeout = timeout
        self.url = url

    def build_payload(self, recipient: str, subject: str, body: str, attachments: list[RenderedFile]) -> dict:
        payload = {
            "sender": {"email": self.settings.sender, "name": self.settings.sender_name[:70]},
            "to": _recipients(recipient),
            "subject": subject,
            "textContent": body,
        }
        if attachments:
            payload["attachment"] = [
                {"name": f.filename, "content": base64.b64encode(f.content).decode("ascii")} for f in attachments
            ]
        return payload

    def _post(self, payload: dict) -> None:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "api-key": self.settings.brevo_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                data = json.loads(exc.read().decode("utf-8") or "{}")
                if isinstance(data, dict):
                    detail = str(data.get("message") or data.get("code") or "")
            except Exception:  # noqa: BLE001 – Fehlertext ist nur Zusatzinfo
                pass
            hint = _brevo_hint(exc.code, detail, self.settings.sender)
            parts = [p for p in (f"Brevo HTTP {exc.code}", detail, hint) if p]
            raise MailError("E-Mail-Versand fehlgeschlagen: " + " – ".join(parts)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MailError(f"E-Mail-Versand fehlgeschlagen: Brevo nicht erreichbar ({exc})") from exc
        except http.client.HTTPException as exc:
            raise MailError(
                f"E-Mail-Versand fehlgeschlagen: ungültige Antwort von Brevo ({type(exc).__name__})"
            ) from exc
        except (UnicodeError, ValueError):
            # Kein str(exc): urllib zitiert bei ungültigen Headern den kompletten API-Schlüssel.
            raise MailError(
                "E-Mail-Versand fehlgeschlagen: ZEITFLOW_BREVO_API_KEY enthält ungültige Zeichen "
                "(Leerzeichen/Zeilenumbruch beim Kopieren?)"
            ) from None

    async def send(self, recipient: str, subject: str, body: str, attachments: list[RenderedFile]) -> None:
        if not self.settings.brevo_api_key or not self.settings.sender:
            raise MailError("Brevo ist nicht konfiguriert (ZEITFLOW_BREVO_API_KEY / ZEITFLOW_MAIL_FROM)")
        if not _SAFE_KEY.fullmatch(self.settings.brevo_api_key):
            raise MailError(
                "E-Mail-Versand fehlgeschlagen: ZEITFLOW_BREVO_API_KEY enthält ungültige Zeichen "
                "(Leerzeichen/Zeilenumbruch beim Kopieren?)"
            )
        payload = self.build_payload(recipient, subject, body, attachments)
        if not payload["to"]:
            raise MailError("E-Mail-Versand fehlgeschlagen: keine gültige Empfängeradresse")
        await asyncio.to_thread(self._post, payload)


def build_mailer(settings: Settings) -> Mailer:
    if settings.mail.provider == "brevo":
        return BrevoMailer(settings.mail)
    return SmtpMailer(settings.smtp)
