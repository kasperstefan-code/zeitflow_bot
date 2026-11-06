"""Ende-zu-Ende-Tests der Bot-Dialoge gegen eine aufzeichnende Telegram-Attrappe."""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import timedelta

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

    async def press_matching(self, tg: int, prefix: str):
        """Drückt den Button der letzten Tastatur, dessen Callback mit prefix beginnt (z. B. "ok:<nonce>")."""
        data = next(d for _, d in self.req.last_buttons() if d.startswith(prefix))
        await self.press(tg, data)

    async def edit(self, tg: int, text: str):
        msg = {
            "message_id": next(self._update_ids),
            "date": 0,
            "edit_date": 1,
            "chat": {"id": tg, "type": "private"},
            "from": self._user(tg),
            "text": text,
        }
        await self._process({"update_id": next(self._update_ids), "edited_message": msg})

    async def _process(self, data):
        await self.app.process_update(Update.de_json(data, self.app.bot))
        await self.drain()

    async def drain(self):
        """Wartet auf Hintergrund-Tasks (Übersetzung, Export, Versand, Neuzustellung von Befehlen)."""
        for _ in range(20):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            if not pending:
                return
            await asyncio.wait(pending, timeout=5)


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
    await bot.press_matching(FOREMAN_TG, "ok:")

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
    await bot.press_matching(WORKER_TG, "ok:")
    (entry,) = bot.deps.repo.list_entries()
    assert (entry.user_id, entry.work_minutes, entry.source_lang) == (bot.worker.id, 480, "pl")


async def test_worker_cannot_use_office_commands(bot):
    await bot.send(WORKER_TG, "/versenden")
    await bot.send(WORKER_TG, "/export")
    assert bot.req.texts(WORKER_TG)[-2:] == ["Brak uprawnień."] * 2


async def test_correction_and_export(bot):
    repo, service = bot.deps.repo, bot.deps.service
    today = bot.deps.today()
    service.record(draft([bot.worker.id], bot.project.id, day=today, lang="pl"), bot.worker)
    (entry,) = repo.list_entries()

    await bot.send(WORKER_TG, "/korrektur")
    await bot.press(WORKER_TG, f"k:{entry.id}")
    await bot.press(WORKER_TG, f"kf:{entry.id}:end")
    await bot.send(WORKER_TG, "16:30")
    assert repo.get_entry(entry.id).work_minutes == 540

    iso = today.isocalendar()
    await bot.send(FOREMAN_TG, "/export")
    await bot.press(FOREMAN_TG, f"xw:{iso.year}:{iso.week}")
    await bot.press(FOREMAN_TG, f"xc:{iso.year}:{iso.week}:{bot.project.customer_id}")
    docs = [p for n, p in bot.req.calls if n == "sendDocument"]
    assert len(docs) == 1


async def test_admin_manual_send(bot):
    today = bot.deps.today()
    bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today), bot.admin)
    iso = today.isocalendar()
    await bot.send(ADMIN_TG, "/anfahrt")
    await bot.press(ADMIN_TG, f"tw:{iso.year}:{iso.week}")
    await bot.press(ADMIN_TG, f"tc:{iso.year}:{iso.week}:{bot.project.customer_id}")
    await bot.send(ADMIN_TG, "3")
    assert bot.deps.repo.get_travel_count(bot.project.customer_id, iso.year, iso.week) == 3

    await bot.send(ADMIN_TG, "/versenden")
    await bot.press(ADMIN_TG, f"sw:{iso.year}:{iso.week}")
    await bot.press(ADMIN_TG, f"sm:open:{iso.year}:{iso.week}")
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


async def test_new_command_ends_open_dialog_and_runs(bot):
    """Befund 1: Text für /neuerkunde darf nicht im offenen /zeit-Dialog landen."""
    today = bot.deps.today()
    await bot.send(ADMIN_TG, "/zeit")
    await bot.press(ADMIN_TG, "w:next")
    await bot.press(ADMIN_TG, f"d:{today.isoformat()}")
    await bot.send(ADMIN_TG, "/neuerkunde")
    assert bot.req.texts(ADMIN_TG)[-1] == "Name des Kunden?"
    await bot.send(ADMIN_TG, "Brauerei Rothaus")
    assert "Brauerei Rothaus" in [c.name for c in bot.deps.repo.list_customers()]
    await bot.send(ADMIN_TG, "07:00")
    assert bot.deps.repo.list_entries() == [], "Der alte /zeit-Dialog ist beendet"


async def test_correction_not_swallowed_by_open_entry_dialog(bot):
    today = bot.deps.today()
    (entry_id,) = bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today), bot.worker).entry_ids
    await bot.send(WORKER_TG, "/zeit")
    await bot.press(WORKER_TG, f"d:{today.isoformat()}")  # wartet jetzt auf Beginn
    await bot.send(WORKER_TG, "/korrektur")
    await bot.press(WORKER_TG, f"k:{entry_id}")
    await bot.press(WORKER_TG, f"kf:{entry_id}:start")
    await bot.send(WORKER_TG, "07:30")
    assert bot.deps.repo.get_entry(entry_id).start_time == "07:30"


async def test_stale_week_button_cannot_cross_dialogs(bot):
    """Befund 2: /anfahrt KW A, dann /export KW B, dann alter Kunden-Button aus /anfahrt → keine Fehlbuchung."""
    today = bot.deps.today()
    last_week = today - timedelta(days=7)
    bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today), bot.worker)
    bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=last_week), bot.admin)
    cur, prev = today.isocalendar(), last_week.isocalendar()
    cid = bot.project.customer_id

    await bot.send(ADMIN_TG, "/anfahrt")
    await bot.press(ADMIN_TG, f"tw:{cur.year}:{cur.week}")
    stale_button = f"tc:{cur.year}:{cur.week}:{cid}"
    assert stale_button in [d for _, d in bot.req.last_buttons()]

    await bot.send(ADMIN_TG, "/export")
    await bot.press(ADMIN_TG, f"xw:{prev.year}:{prev.week}")
    await bot.press(ADMIN_TG, stale_button)  # gehört zum beendeten /anfahrt-Dialog
    await bot.send(ADMIN_TG, "4")
    assert bot.deps.repo.get_travel_count(cid, prev.year, prev.week) is None
    assert bot.deps.repo.get_travel_count(cid, cur.year, cur.week) is None

    await bot.send(ADMIN_TG, "/anfahrt")
    await bot.press(ADMIN_TG, f"tw:{prev.year}:{prev.week}")
    await bot.press(ADMIN_TG, f"tc:{prev.year}:{prev.week}:{cid}")
    await bot.send(ADMIN_TG, "4")
    assert bot.deps.repo.get_travel_count(cid, prev.year, prev.week) == 4


async def test_translation_arrives_as_follow_up_message(bot):
    await bot.send(WORKER_TG, "/zeit")
    await bot.press(WORKER_TG, f"d:{bot.deps.today().isoformat()}")
    await bot.press(WORKER_TG, "t:07:00")
    await bot.press(WORKER_TG, "t:15:00")
    await bot.press(WORKER_TG, "b:30")
    await bot.send(WORKER_TG, "Izolacja rur")
    await bot.press_matching(WORKER_TG, "ok:")
    texts = bot.req.texts(WORKER_TG)
    assert any("Tłumaczenie zostanie wykonane automatycznie." in x for x in texts)
    assert "🇩🇪 DE:Izolacja rur" in texts
    assert bot.deps.repo.list_entries()[0].activity_de == "DE:Izolacja rur"


async def test_unknown_user_never_becomes_admin(bot):
    bot.deps.repo.update_user(bot.admin.id, status=UserStatus.INACTIVE)
    await bot.send(NEW_TG, "/start")
    await bot.press(NEW_TG, "rl:de")
    await bot.send(NEW_TG, "Fremder Mensch")
    assert bot.deps.repo.get_user_by_telegram_id(NEW_TG).role is Role.EMPLOYEE
    assert bot.deps.repo.get_user_by_telegram_id(NEW_TG).status is UserStatus.PENDING


async def test_forged_buttons_are_rejected(bot):
    pending = bot.deps.repo.create_user(555, "Gesperrt Er", "de", Role.EMPLOYEE, UserStatus.PENDING)
    await bot.send(FOREMAN_TG, "/zeit")
    await bot.press(FOREMAN_TG, f"w:{pending.id}")
    assert ("▫️ Gesperrt Er", f"w:{pending.id}") not in bot.req.last_buttons()
    await bot.press(FOREMAN_TG, "w:next")
    await bot.press(FOREMAN_TG, f"d:{bot.deps.today().isoformat()}")
    await bot.press(FOREMAN_TG, "t:06:00")
    await bot.press(FOREMAN_TG, "t:15:00")
    await bot.press(FOREMAN_TG, "b:30")
    await bot.send(FOREMAN_TG, "Монтаж")
    await bot.press_matching(FOREMAN_TG, "ok:")
    assert {e.user_id for e in bot.deps.repo.list_entries()} == {bot.foreman.id}

    admin_entry = bot.deps.service.record(
        draft([bot.admin.id], bot.project.id, day=bot.deps.today(), start="16:00", end="18:00", brk=0), bot.admin
    ).entry_ids[0]
    await bot.send(FOREMAN_TG, "/korrektur")
    await bot.press(FOREMAN_TG, f"k:{admin_entry}")
    assert bot.req.texts(FOREMAN_TG)[-1] == "Недостаточно прав."


def second_customer(bot):
    other = bot.deps.repo.create_customer("Brauerei Rothaus")
    bot.deps.repo.create_project(other.id, "Druckluft", "110001")


# --- Runde 2: Dialog-Isolation unabhängig von der Registrierungsreihenfolge ------------------------------


async def test_earlier_registered_dialog_does_not_revive_forgotten_later_dialog(bot):
    """/neuerkunde vergessen, dann /zeit: Freitext im Kunden-Schritt darf keinen Kunden anlegen."""
    second_customer(bot)  # damit /zeit bei der Kundenauswahl (nur Buttons) stehen bleibt
    await bot.send(ADMIN_TG, "/neuerkunde")
    await bot.send(ADMIN_TG, "/zeit")
    await bot.press(ADMIN_TG, "w:next")
    await bot.send(ADMIN_TG, "Schwarzwaldmilch")
    assert [c.name for c in bot.deps.repo.list_customers()] == ["Brauerei Rothaus", "Schwarzwaldmilch GmbH Freiburg"]


async def test_forgotten_correction_does_not_take_text_meant_for_entry(bot):
    second_customer(bot)  # damit /zeit bei der Kundenauswahl (nur Buttons) stehen bleibt
    today = bot.deps.today()
    (entry_id,) = bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today), bot.worker).entry_ids
    await bot.send(WORKER_TG, "/korrektur")
    await bot.press(WORKER_TG, f"k:{entry_id}")
    await bot.press(WORKER_TG, f"kf:{entry_id}:activity")
    await bot.send(WORKER_TG, "/zeit")
    await bot.send(WORKER_TG, "Rothaus")
    assert bot.deps.repo.get_entry(entry_id).activity_original == "Монтаж трубопровода"


async def test_start_command_makes_open_anfahrt_dialog_dormant(bot):
    today = bot.deps.today()
    bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today), bot.worker)
    iso, cid = today.isocalendar(), bot.project.customer_id
    await bot.send(ADMIN_TG, "/anfahrt")
    await bot.press(ADMIN_TG, f"tw:{iso.year}:{iso.week}")
    await bot.press(ADMIN_TG, f"tc:{iso.year}:{iso.week}:{cid}")
    await bot.send(ADMIN_TG, "/start")
    await bot.send(ADMIN_TG, "5")
    assert bot.deps.repo.get_travel_count(cid, iso.year, iso.week) is None


async def test_info_commands_keep_the_open_dialog(bot):
    today = bot.deps.today()
    await bot.send(WORKER_TG, "/zeit")
    await bot.press(WORKER_TG, f"d:{today.isoformat()}")
    await bot.send(WORKER_TG, "/woche")
    await bot.send(WORKER_TG, "/unbekannt")
    await bot.press(WORKER_TG, "t:07:00")
    assert bot.req.texts(WORKER_TG)[-1].startswith("Koniec pracy?")


async def test_forgotten_project_edit_cannot_rename_from_stray_text(bot):
    await bot.send(ADMIN_TG, "/projekt")
    await bot.press(ADMIN_TG, f"ep:{bot.project.id}")
    await bot.press(ADMIN_TG, f"ef:{bot.project.id}:name")
    await bot.send(ADMIN_TG, "/neuesprojekt")
    await bot.press(ADMIN_TG, f"npc:{bot.project.customer_id}")
    await bot.send(ADMIN_TG, "Druckluft")
    await bot.send(ADMIN_TG, "-")
    await bot.send(ADMIN_TG, "danke")
    assert bot.deps.repo.get_project(bot.project.id).name == "Montage Dosieranlage"


async def test_stale_correction_buttons_act_on_their_own_entry(bot):
    today = bot.deps.today()
    a = bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today, start="06:00", end="08:00", brk=0),
                                bot.worker).entry_ids[0]  # fmt: skip
    b = bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today, start="09:00", end="11:00", brk=0),
                                bot.worker).entry_ids[0]  # fmt: skip
    await bot.send(WORKER_TG, "/korrektur")
    await bot.press(WORKER_TG, f"k:{a}")
    await bot.send(WORKER_TG, "/korrektur")
    await bot.press(WORKER_TG, f"k:{b}")
    await bot.press(WORKER_TG, f"kf:{a}:delete")  # Button aus der ersten Nachricht
    await bot.press(WORKER_TG, f"kd:{a}:y")
    assert bot.deps.repo.get_entry(a) is None
    assert bot.deps.repo.get_entry(b) is not None


async def test_old_save_button_does_not_save_new_draft(bot):
    today = bot.deps.today()
    await bot.send(WORKER_TG, "/zeit")
    await bot.press(WORKER_TG, f"d:{today.isoformat()}")
    await bot.press(WORKER_TG, "t:07:00")
    await bot.press(WORKER_TG, "t:15:00")
    await bot.press(WORKER_TG, "b:30")
    await bot.send(WORKER_TG, "Erste Fassung")
    old_ok = next(d for _, d in bot.req.last_buttons() if d.startswith("ok:"))
    await bot.send(WORKER_TG, "/zeit")
    await bot.press(WORKER_TG, f"d:{today.isoformat()}")
    await bot.press(WORKER_TG, old_ok)
    assert bot.deps.repo.list_entries() == []


async def test_edited_messages_are_ignored(bot):
    today = bot.deps.today()
    await bot.send(WORKER_TG, "/zeit")
    await bot.press(WORKER_TG, f"d:{today.isoformat()}")
    await bot.edit(WORKER_TG, "07:00")  # darf weder abstürzen noch als Eingabe zählen
    await bot.send(WORKER_TG, "07:00")
    assert bot.req.texts(WORKER_TG)[-1].startswith("Koniec pracy?")


async def test_configured_admin_cannot_be_demoted_in_ui(bot):
    other_admin = bot.deps.repo.create_user(777, "Zweite Admin", "de", Role.ADMIN, UserStatus.ACTIVE)
    await bot.press(other_admin.telegram_id, f"mr:{bot.admin.id}:employee")
    assert bot.deps.repo.get_user(bot.admin.id).role is Role.ADMIN


async def test_approval_message_shows_telegram_identity(bot):
    await bot.send(NEW_TG, "/start")
    await bot.press(NEW_TG, "rl:de")
    await bot.send(NEW_TG, "Kamil Kroll")
    assert any(f"ID {NEW_TG}" in x and "Kamil Kroll" in x for x in bot.req.texts(ADMIN_TG))


async def test_failing_export_tells_the_user(bot, monkeypatch, caplog):
    import zeitflow.bot.admin as admin_module

    def boom(*_, **__):
        raise RuntimeError("Renderer kaputt")

    monkeypatch.setattr(admin_module, "render_report", boom)
    today = bot.deps.today()
    bot.deps.service.record(draft([bot.worker.id], bot.project.id, day=today), bot.worker)
    iso = today.isocalendar()
    await bot.send(FOREMAN_TG, "/export")
    await bot.press(FOREMAN_TG, f"xw:{iso.year}:{iso.week}")
    await bot.press(FOREMAN_TG, f"xc:{iso.year}:{iso.week}:{bot.project.customer_id}")
    assert bot.req.texts(FOREMAN_TG)[-1] == "Произошла ошибка. Попробуйте ещё раз."
    caplog.clear()  # erwarteter Fehler – nicht als unerwarteter Bot-Fehler werten
