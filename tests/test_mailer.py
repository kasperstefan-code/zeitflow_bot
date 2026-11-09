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


# --- Brevo (HTTPS-API) -------------------------------------------------------------------------------------

import base64  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

from zeitflow.config import MailSettings, load_settings  # noqa: E402
from zeitflow.mailer import BrevoMailer, build_mailer  # noqa: E402


def brevo_settings(**kw):
    base = dict(provider="brevo", brevo_api_key="xkeysib-test", sender="info@firma.de", sender_name="Firma")
    return MailSettings(**{**base, **kw})


class FakeBrevo:
    """Lokaler HTTP-Server, der Anfragen aufzeichnet und eine feste Antwort liefert."""

    def __init__(self, status=201, body=None):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                outer.requests.append(
                    {
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "json": json.loads(self.rfile.read(length)),
                    }
                )
                payload = json.dumps(body if body is not None else {"messageId": "<1@brevo>"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v3/smtp/email"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_brevo():
    servers = []

    def make(**kw):
        servers.append(FakeBrevo(**kw))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


async def test_brevo_sends_payload_with_attachment(fake_brevo):
    server = fake_brevo()
    mailer = BrevoMailer(brevo_settings(), url=server.url)
    await mailer.send("abrechnung@firma.de", "Betreff", "Text", [RenderedFile("KW13.xlsx", b"\x00xlsx", XLSX)])
    (req,) = server.requests
    assert req["headers"]["api-key"] == "xkeysib-test"
    body = req["json"]
    assert body["sender"] == {"email": "info@firma.de", "name": "Firma"}
    assert body["to"] == [{"email": "abrechnung@firma.de"}]
    assert (body["subject"], body["textContent"]) == ("Betreff", "Text")
    assert body["attachment"] == [{"name": "KW13.xlsx", "content": base64.b64encode(b"\x00xlsx").decode()}]


async def test_brevo_error_is_explained(fake_brevo):
    server = fake_brevo(status=400, body={"code": "invalid_parameter", "message": "Sender is not valid"})
    with pytest.raises(MailError) as exc:
        await BrevoMailer(brevo_settings(), url=server.url).send("a@b.de", "s", "b", [])
    msg = str(exc.value)
    assert "Brevo HTTP 400" in msg and "Sender is not valid" in msg and "info@firma.de" in msg


async def test_brevo_bad_key_hint(fake_brevo):
    server = fake_brevo(status=401, body={"code": "unauthorized", "message": "Key not found"})
    with pytest.raises(MailError, match="ZEITFLOW_BREVO_API_KEY"):
        await BrevoMailer(brevo_settings(), url=server.url).send("a@b.de", "s", "b", [])


async def test_brevo_unreachable_and_unconfigured():
    with pytest.raises(MailError, match="nicht erreichbar"):
        await BrevoMailer(brevo_settings(), timeout=2, url="http://127.0.0.1:9/v3/smtp/email").send(
            "a@b.de", "s", "b", []
        )
    with pytest.raises(MailError, match="nicht konfiguriert"):
        await BrevoMailer(brevo_settings(brevo_api_key="")).send("a@b.de", "s", "b", [])


def test_provider_selection():
    smtp = load_settings({"ZEITFLOW_SMTP_HOST": "mail.gmx.net", "ZEITFLOW_SMTP_FROM": "x@gmx.net"})
    assert smtp.mail.provider == "smtp" and isinstance(build_mailer(smtp), SmtpMailer)
    brevo = load_settings(
        {"ZEITFLOW_BREVO_API_KEY": "k", "ZEITFLOW_MAIL_FROM": "info@firma.de", "ZEITFLOW_COMPANY_NAME": "Kroll"}
    )
    assert (brevo.mail.provider, brevo.mail.sender, brevo.mail.sender_name) == ("brevo", "info@firma.de", "Kroll")
    assert isinstance(build_mailer(brevo), BrevoMailer)
    fallback = load_settings({"ZEITFLOW_BREVO_API_KEY": "k", "ZEITFLOW_SMTP_FROM": "alt@firma.de"})
    assert fallback.mail.sender == "alt@firma.de"


async def test_brevo_key_with_line_break_never_leaks(fake_brevo):
    server = fake_brevo()
    mailer = BrevoMailer(brevo_settings(brevo_api_key="xkeysib-SECRET1\nSECRET2"), url=server.url)
    with pytest.raises(MailError) as exc:
        await mailer.send("a@b.de", "s", "b", [])
    assert "SECRET" not in str(exc.value) and "ungültige Zeichen" in str(exc.value)
    assert server.requests == []


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {"code": "unauthorized", "message": "We have detected you are using an unrecognised IP address 1.2.3.4"},
         "Authorised IPs"),
        (400, {"code": "invalid_parameter", "message": "DMARC policy requires domain authentication"},
         "Domain von info@firma.de bei Brevo authentifizieren"),
        (502, None, "Brevo HTTP 502"),
        (500, ["kaputt"], "Brevo HTTP 500"),
        (400, {"code": 42}, "Brevo HTTP 400 – 42"),
    ],
)  # fmt: skip
async def test_brevo_error_hints(fake_brevo, status, body, expected):
    server = fake_brevo(status=status, body=body if body is not None else "")
    with pytest.raises(MailError) as exc:
        await BrevoMailer(brevo_settings(), url=server.url).send("a@b.de", "s", "b", [])
    assert expected in str(exc.value)
    if status == 401:
        assert "API-Schlüssel ungültig" not in str(exc.value)


async def test_brevo_multiple_recipients(fake_brevo):
    server = fake_brevo()
    await BrevoMailer(brevo_settings(), url=server.url).send("abrechnung@firma.de; chef@firma.de", "s", "b", [])
    assert server.requests[0]["json"]["to"] == [{"email": "abrechnung@firma.de"}, {"email": "chef@firma.de"}]


async def test_brevo_truncated_response_is_mail_error():
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b'HTTP/1.1 201 Created\r\nContent-Length: 100\r\n\r\n{"mess')
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(MailError, match="E-Mail-Versand fehlgeschlagen"):  # nie eine rohe http.client-Ausnahme
        await BrevoMailer(brevo_settings(), url=f"http://127.0.0.1:{port}/v3/smtp/email").send("a@b.de", "s", "b", [])
    listener.close()
