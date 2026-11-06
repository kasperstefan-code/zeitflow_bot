"""Ende-zu-Ende-Tests der Bot-Dialoge gegen eine aufzeichnende Telegram-Attrappe."""

from __future__ import annotations

import itertools
import json

import pytest
from telegram import Update
from telegram.request import BaseRequest

from tests.conftest import FakeMailer, FakeTranslator, draft
from zeitflow.app import build_application, build_deps
from zeitflow.config import load_settings
from zeitflow.models import Role, UserStatus

ADMIN_TG, FOREMAN_TG, WORKER_TG, NEW_TG = 100, 200, 300, 400


class RecordingRequest(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1000)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 1.0

    async def do_request(self, url, method, request_data=None, **_):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "ZeitFlow", "username": "zeitflow_bot"}
        elif name in {"sendMessage", "editMessageText", "sendDocument"}:
            result = {
                "message_id": next(self._ids),
                "date": 0,
                "chat": {"id": int(params.get("chat_id", 0) or 0), "type": "private"},
                "text": params.get("text", ""),
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [
            p.get("text", "")
            for n, p in self.calls
            if n in {"sendMessage", "editMessageText"} and (chat_id is None or int(p.get("chat_id", 0)) == chat_id)
        ]

    def last_buttons(self) -> list[tuple[str, str]]:
        for _name, p in reversed(self.calls):
            if "reply_markup" in p:
                markup = p["reply_markup"]
                markup = json.loads(markup) if isinstance(markup, str) else markup
                return [(b["text"], b["callback_data"]) for row in markup["inline_keyboard"] for b in row]
        return []


class Harness:
    def __init__(self, app, request: RecordingRequest):
        self.app, self.req = app, request
        self._update_ids = itertools.count(1)
        self.deps = app.bot_data["deps"]

    def _user(self, tg):
        return {"id": tg, "is_bot": False, "first_name": f"U{tg}"}

    async def send(self, tg: int, text: str):
        msg = {
            "message_id": next(self._update_ids),
            "date": 0,
            "chat": {"id": tg, "type": "private"},
            "from": self._user(tg),
            "text": text,
        }
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self._process({"update_id": next(self._update_ids), "message": msg})

    async def press(self, tg: int, data: str):
        cq = {
            "id": str(next(self._update_ids)),
            "from": self._user(tg),
            "chat_instance": "ci",
            "data": data,
            "message": {"message_id": 1, "date": 0, "chat": {"id": tg, "type": "private"}, "text": "…"},
        }
        await self._process({"update_id": next(self._update_ids), "callback_query": cq})

    async def _process(self, data):
        await self.app.process_update(Update.de_json(data, self.app.bot))


@pytest.fixture
async def bot(tmp_path, caplog):
    settings = load_settings(
        {
            "ZEITFLOW_BOT_TOKEN": "123:abc",
            "ZEITFLOW_DB_PATH": str(tmp_path / "bot.db"),
            "ZEITFLOW_ADMIN_IDS": str(ADMIN_TG),
            "ZEITFLOW_REPORT_EMAIL": "abrechnung@example.org",
        }
    )
    request = RecordingRequest()
    mailer = FakeMailer()
    deps = build_deps(settings, translator=FakeTranslator(), mailer=mailer)
    app = build_application(settings, deps, request=request)
    await app.initialize()
    h = Harness(app, request)
    h.mailer = mailer
    repo = deps.repo
    h.admin = repo.create_user(ADMIN_TG, "Anna Büro", "de", Role.ADMIN, UserStatus.ACTIVE)
    h.foreman = repo.create_user(FOREMAN_TG, "Sergej Tyschenko", "ru", Role.FOREMAN, UserStatus.ACTIVE)
    h.worker = repo.create_user(WORKER_TG, "Kamil Kroll", "pl", Role.EMPLOYEE, UserStatus.ACTIVE)
    customer = repo.create_customer("Schwarzwaldmilch GmbH Freiburg")
    h.project = repo.create_project(customer.id, "Montage Dosieranlage", "23670")
    yield h
    await app.shutdown()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert not errors, f"Unerwartete Fehler im Bot: {[r.getMessage() for r in errors]}"


async def test_registration_requires_admin_approval(bot):
    await bot.send(NEW_TG, "/start")
    await bot.press(NEW_TG, "rl:lt")
    await bot.send(NEW_TG, "Jonas Petraitis")

    user = bot.deps.repo.get_user_by_telegram_id(NEW_TG)
    assert user.status is UserStatus.PENDING and user.lang == "lt"
    assert any("laukia biuro patvirtinimo" in t for t in bot.req.texts(NEW_TG))
    assert any("Neue Anmeldung: Jonas Petraitis" in t for t in bot.req.texts(ADMIN_TG))

    await bot.send(NEW_TG, "/zeit")
    assert bot.req.texts(NEW_TG)[-1] == "Jūsų prieiga dar nepatvirtinta."

    await bot.press(WORKER_TG, f"ap:{user.id}")
    assert bot.deps.repo.get_user(user.id).status is UserStatus.PENDING, "Nur Admins dürfen freigeben"

    await bot.press(ADMIN_TG, f"ap:{user.id}")
    assert bot.deps.repo.get_user(user.id).status is UserStatus.ACTIVE
    assert "Prieiga suteikta" in bot.req.texts(NEW_TG)[-1]


async def test_foreman_records_crew_in_russian(bot):
    await bot.send(FOREMAN_TG, "/zeit")
    assert ("✅ Я", f"w:{bot.foreman.id}") in bot.req.last_buttons()
    await bot.press(FOREMAN_TG, f"w:{bot.worker.id}")
    await bot.press(FOREMAN_TG, "w:next")  # einziger Kunde/Projekt wird automatisch gewählt
    today = bot.deps.today()
    assert (("Сегодня", f"d:{today.isoformat()}")) in bot.req.last_buttons()
    await bot.press(FOREMAN_TG, f"d:{today.isoformat()}")
    await bot.press(FOREMAN_TG, "t:06:30")
    await bot.send(FOREMAN_TG, "17.30")
    await bot.press(FOREMAN_TG, "b:60")
    await bot.send(FOREMAN_TG, "Монтаж трубопровода DN50")
    assert "⏱ 10 ч" in bot.req.texts(FOREMAN_TG)[-1]
    await bot.press(FOREMAN_TG, "ok")

    entries = bot.deps.repo.list_entries()
    assert {e.user_name for e in entries} == {"Sergej Tyschenko", "Kamil Kroll"}
    assert all(e.activity_de == "DE:Монтаж трубопровода DN50" and e.work_minutes == 600 for e in entries)
    assert "🇩🇪 DE:Монтаж трубопровода DN50" in bot.req.texts(FOREMAN_TG)[-1]

    await bot.press(FOREMAN_TG, "m:n")
    assert bot.req.texts(FOREMAN_TG)[-1] == "👍 Спасибо!"


async def test_worker_sees_no_crew_selection_and_invalid_input_is_reprompted(bot):
    await bot.send(WORKER_TG, "/zeit")
    today = bot.deps.today()
    await bot.press(WORKER_TG, f"d:{today.isoformat()}")
    await bot.send(WORKER_TG, "25:00")
    assert bot.req.texts(WORKER_TG)[-1] == "Nie rozpoznano godziny. Przykład: 7:00"
    await bot.send(WORKER_TG, "8")
    await bot.send(WORKER_TG, "7")
    assert bot.req.texts(WORKER_TG)[-1] == "Koniec musi być po początku."
    await bot.send(WORKER_TG, "16:30")
    await bot.press(WORKER_TG, "b:30")
    await bot.send(WORKER_TG, "Izolacja rur")
    await bot.press(WORKER_TG, "ok")
    (entry,) = bot.deps.repo.list_entries()
    assert (entry.user_id, entry.work_minutes, entry.source_lang) == (bot.worker.id, 480, "pl")


async def test_worker_cannot_use_office_commands(bot):
    await bot.send(WORKER_TG, "/versenden")
    await bot.send(WORKER_TG, "/export")
    assert bot.req.texts(WORKER_TG)[-2:] == ["Brak uprawnień."] * 2


async def test_correction_and_export(bot):
    repo, service = bot.deps.repo, bot.deps.service
    today = bot.deps.today()
    await service.record(draft([bot.worker.id], bot.project.id, day=today, lang="pl"), bot.worker)
    (entry,) = repo.list_entries()

    await bot.send(WORKER_TG, "/korrektur")
    await bot.press(WORKER_TG, f"k:{entry.id}")
    await bot.press(WORKER_TG, "kf:end")
    await bot.send(WORKER_TG, "16:30")
    assert repo.get_entry(entry.id).work_minutes == 540

    iso = today.isocalendar()
    await bot.send(FOREMAN_TG, "/export")
    await bot.press(FOREMAN_TG, f"xw:{iso.year}:{iso.week}")
    await bot.press(FOREMAN_TG, f"xc:{bot.project.customer_id}")
    docs = [p for n, p in bot.req.calls if n == "sendDocument"]
    assert len(docs) == 1


async def test_admin_manual_send(bot):
    today = bot.deps.today()
    await bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today), bot.admin)
    iso = today.isocalendar()
    await bot.send(ADMIN_TG, "/anfahrt")
    await bot.press(ADMIN_TG, f"tw:{iso.year}:{iso.week}")
    await bot.press(ADMIN_TG, f"tc:{bot.project.customer_id}")
    await bot.send(ADMIN_TG, "3")
    assert bot.deps.repo.get_travel_count(bot.project.customer_id, iso.year, iso.week) == 3

    await bot.send(ADMIN_TG, "/versenden")
    await bot.press(ADMIN_TG, f"sw:{iso.year}:{iso.week}")
    await bot.press(ADMIN_TG, "sm:open")
    assert len(bot.mailer.sent) == 1
    assert any("✅ Schwarzwaldmilch GmbH Freiburg" in t for t in bot.req.texts(ADMIN_TG))


async def test_admin_manages_roles(bot):
    await bot.send(ADMIN_TG, "/mitarbeiter")
    await bot.press(ADMIN_TG, f"mu:{bot.worker.id}")
    await bot.press(ADMIN_TG, f"mr:{bot.worker.id}:foreman")
    assert bot.deps.repo.get_user(bot.worker.id).role is Role.FOREMAN
    await bot.press(ADMIN_TG, f"ms:{bot.worker.id}:inactive")
    assert bot.deps.repo.get_user(bot.worker.id).status is UserStatus.INACTIVE
    await bot.press(ADMIN_TG, f"mr:{bot.admin.id}:employee")
    assert bot.deps.repo.get_user(bot.admin.id).role is Role.ADMIN, "Admin kann sich nicht selbst herabstufen"
