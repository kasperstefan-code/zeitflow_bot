from __future__ import annotations

from datetime import date

import pytest

from zeitflow.db import connect, migrate
from zeitflow.models import Role, UserStatus
from zeitflow.repository import Repository
from zeitflow.services import EntryDraft, TimesheetService


class FakeTranslator:
    def __init__(self, result: str | None = "AUTO", fail: bool = False):
        self.result = result
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def to_german(self, text: str, source_lang: str) -> str | None:
        self.calls.append((text, source_lang))
        if self.fail:
            return None
        return f"DE:{text}" if self.result == "AUTO" else self.result


class FakeMailer:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[dict] = []

    async def send(self, recipient, subject, body, attachments):
        from zeitflow.mailer import MailError

        if self.fail:
            raise MailError("SMTP down")
        self.sent.append({"recipient": recipient, "subject": subject, "body": body, "attachments": attachments})


@pytest.fixture
def repo():
    conn = connect(":memory:")
    migrate(conn)
    yield Repository(conn)
    conn.close()


@pytest.fixture
def translator():
    return FakeTranslator()


@pytest.fixture
def service(repo, translator):
    return TimesheetService(repo, translator)


@pytest.fixture
def world(repo):
    """Kleine Firma: Admin, Vorarbeiter, zwei Monteure, zwei Kunden."""
    admin = repo.create_user(1, "Anna Büro", "de", Role.ADMIN, UserStatus.ACTIVE)
    foreman = repo.create_user(2, "Sergej Tyschenko", "ru", Role.FOREMAN, UserStatus.ACTIVE)
    w1 = repo.create_user(3, "Andrej Novikau", "uk", Role.EMPLOYEE, UserStatus.ACTIVE)
    w2 = repo.create_user(4, "Kamil Kroll", "pl", Role.EMPLOYEE, UserStatus.ACTIVE)
    smf = repo.create_customer("Schwarzwaldmilch GmbH Freiburg")
    other = repo.create_customer("Brauerei Rothaus")
    p1 = repo.create_project(smf.id, "Montage Dosieranlage", "23670")
    p2 = repo.create_project(smf.id, "Rückbau Hamba-A", "153201")
    p3 = repo.create_project(other.id, "Druckluft", "110001")
    return {
        "admin": admin,
        "foreman": foreman,
        "w1": w1,
        "w2": w2,
        "smf": smf,
        "other": other,
        "p1": p1,
        "p2": p2,
        "p3": p3,
    }


def draft(
    user_ids,
    project_id,
    day=date(2026, 3, 24),
    start="06:30",
    end="17:30",
    brk=60,
    activity="Монтаж трубопровода",
    lang="ru",
) -> EntryDraft:
    return EntryDraft(tuple(user_ids), project_id, day, start, end, brk, activity, lang)
