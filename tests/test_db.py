"""Schema-Migration inkl. Übernahme einer Datenbank des alten Bots (zeitflow.py, Version 1.x)."""

from datetime import date

from zeitflow.db import connect, migrate
from zeitflow.models import Role, UserStatus
from zeitflow.repository import Repository

# Endzustand des Schemas nach init_db() des alten Bots
LEGACY_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, tid INTEGER UNIQUE NOT NULL, name TEXT NOT NULL,
    lang TEXT DEFAULT 'de', is_admin INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')), role TEXT DEFAULT 'employee', daily_target_hours REAL DEFAULT 8.0,
    vacation_days REAL DEFAULT 30);
CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, customer TEXT DEFAULT '',
    cost_center TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
    project_id INTEGER NOT NULL REFERENCES projects(id), edate TEXT NOT NULL, stime TEXT NOT NULL,
    etime TEXT NOT NULL, brk INTEGER DEFAULT 30, hours REAL NOT NULL, notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')), created_by INTEGER, updated_by INTEGER, updated_at TEXT,
    entry_type TEXT DEFAULT 'work', billable INTEGER DEFAULT 1, paid INTEGER DEFAULT 1);
CREATE INDEX ix_e_date ON entries(edate);
CREATE INDEX ix_e_user ON entries(user_id);
INSERT INTO users (id, tid, name, lang, is_admin, active, role) VALUES
    (1, 111, 'Anna Büro', 'de', 1, 1, 'admin'),
    (2, 222, 'Sergej Tyschenko', 'ru', 0, 1, 'foreman'),
    (3, 333, 'Janis Berzins', 'lv', 0, 0, 'employee');
INSERT INTO projects (id, name, customer, cost_center, active) VALUES
    (1, 'Montage Dosieranlage', 'Schwarzwaldmilch GmbH Freiburg', 'Projekt 23670', 1),
    (2, 'Rückbau Hamba-A', 'Schwarzwaldmilch GmbH Freiburg', '153201', 0),
    (3, '__ABWESENHEIT__', 'Intern', 'ABW', 0);
INSERT INTO entries (user_id, project_id, edate, stime, etime, brk, hours, notes, created_by, entry_type) VALUES
    (2, 1, '2026-03-24', '06:30', '17:30', 60, 10.0, 'Sterilluft L200', 1, 'work'),
    (2, 3, '2026-03-25', '00:00', '08:00', 0, 8.0, '', 2, 'vacation');
"""


def test_fresh_migration_is_idempotent(tmp_path):
    path = tmp_path / "z.db"
    conn = connect(path)
    migrate(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "customers", "projects", "entries", "week_extras", "report_runs"} <= tables


def test_legacy_database_is_migrated(tmp_path):
    path = tmp_path / "legacy.db"
    conn = connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()

    migrate(conn)
    repo = Repository(conn)

    anna = repo.get_user_by_telegram_id(111)
    assert anna.role is Role.ADMIN and anna.status is UserStatus.ACTIVE
    assert repo.get_user_by_telegram_id(222).role is Role.FOREMAN
    janis = repo.get_user_by_telegram_id(333)
    assert janis.lang == "de" and janis.status is UserStatus.INACTIVE

    customers = repo.list_customers(active_only=False)
    assert [c.name for c in customers] == ["Schwarzwaldmilch GmbH Freiburg"]
    projects = repo.list_projects(active_only=False)
    assert {p.name for p in projects} == {"Montage Dosieranlage", "Rückbau Hamba-A"}

    entries = repo.list_entries()
    assert len(entries) == 1, "Urlaub/Krank wird nicht übernommen"
    e = entries[0]
    assert (e.work_date, e.start_time, e.end_time, e.work_minutes) == (date(2026, 3, 24), "06:30", "17:30", 600)
    assert e.activity_de == "Sterilluft L200"
    assert e.cost_center == "Projekt 23670"

    legacy = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'legacy_%'")}
    assert legacy == {"legacy_users", "legacy_projects", "legacy_entries"}


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import zeitflow.db as db

    path = tmp_path / "legacy.db"
    conn = connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()

    def boom(_conn):
        raise RuntimeError("abbruch")

    monkeypatch.setattr(db, "_copy_legacy_data", boom)
    try:
        migrate(conn)
    except RuntimeError:
        pass
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "legacy_users" not in tables and "customers" not in tables
    assert "tid" in {r[1] for r in conn.execute("PRAGMA table_info(users)")}
