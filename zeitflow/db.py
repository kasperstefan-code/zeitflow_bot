"""SQLite-Verbindung und versionierte Schema-Migrationen (PRAGMA user_version)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOG = logging.getLogger(__name__)

SCHEMA_V1 = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    lang          TEXT    NOT NULL DEFAULT 'de',
    role          TEXT    NOT NULL DEFAULT 'employee' CHECK (role IN ('employee','foreman','admin')),
    status        TEXT    NOT NULL DEFAULT 'pending'  CHECK (status IN ('pending','active','inactive')),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE customers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL UNIQUE,
    active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    name         TEXT    NOT NULL,
    cost_center  TEXT    NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE entries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    project_id         INTEGER NOT NULL REFERENCES projects(id),
    work_date          TEXT    NOT NULL,
    start_time         TEXT    NOT NULL,
    end_time           TEXT    NOT NULL,
    break_minutes      INTEGER NOT NULL DEFAULT 0,
    work_minutes       INTEGER NOT NULL,
    activity_original  TEXT    NOT NULL DEFAULT '',
    activity_de        TEXT,
    source_lang        TEXT    NOT NULL DEFAULT 'de',
    created_by         INTEGER NOT NULL REFERENCES users(id),
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_by         INTEGER REFERENCES users(id),
    updated_at         TEXT
);
CREATE INDEX ix_entries_date    ON entries(work_date);
CREATE INDEX ix_entries_user    ON entries(user_id, work_date);
CREATE INDEX ix_entries_project ON entries(project_id, work_date);

CREATE TABLE week_extras (
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    iso_year      INTEGER NOT NULL,
    iso_week      INTEGER NOT NULL,
    travel_count  INTEGER,
    PRIMARY KEY (customer_id, iso_year, iso_week)
);

CREATE TABLE report_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    iso_year     INTEGER NOT NULL,
    iso_week     INTEGER NOT NULL,
    recipient    TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('sent','failed')),
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_report_runs_week ON report_runs(customer_id, iso_year, iso_week);
"""

LEGACY_TABLES = ("users", "projects", "entries")
LEGACY_ABSENCE_PROJECT = "__ABWESENHEIT__"
SUPPORTED_LANGS = {"de", "ru", "uk", "pl", "lt"}


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _is_legacy_schema(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "users") and "tid" in _columns(conn, "users")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    legacy = _is_legacy_schema(conn)
    if legacy:
        LOG.info(
            "Alte ZeitFlow-Datenbank erkannt – Daten werden übernommen, alte Tabellen bleiben als legacy_* erhalten"
        )
        for t in LEGACY_TABLES:
            conn.execute(f"ALTER TABLE {t} RENAME TO legacy_{t}")
    # Nicht executescript(): das committet implizit und bräche die Atomarität der Migration.
    for statement in SCHEMA_V1.split(";"):
        if statement.strip():
            conn.execute(statement)
    if legacy:
        _copy_legacy_data(conn)


def _copy_legacy_data(conn: sqlite3.Connection) -> None:
    legacy_user_cols = _columns(conn, "legacy_users")
    for u in conn.execute("SELECT * FROM legacy_users").fetchall():
        role = u["role"] if "role" in legacy_user_cols and u["role"] else ("admin" if u["is_admin"] else "employee")
        lang = u["lang"] if u["lang"] in SUPPORTED_LANGS else "de"
        status = "active" if u["active"] else "inactive"
        conn.execute(
            "INSERT INTO users (id, telegram_id, name, lang, role, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (u["id"], u["tid"], u["name"], lang, role, status, u["created_at"]),
        )

    customer_ids: dict[str, int] = {}
    for p in conn.execute("SELECT * FROM legacy_projects WHERE name <> ?", (LEGACY_ABSENCE_PROJECT,)).fetchall():
        cname = (p["customer"] or "").strip() or "Ohne Kunde"
        if cname not in customer_ids:
            cur = conn.execute("INSERT INTO customers (name) VALUES (?)", (cname,))
            customer_ids[cname] = cur.lastrowid
        conn.execute(
            "INSERT INTO projects (id, customer_id, name, cost_center, active) VALUES (?,?,?,?,?)",
            (p["id"], customer_ids[cname], p["name"], p["cost_center"] or "", p["active"]),
        )

    entry_cols = _columns(conn, "legacy_entries")
    type_filter = "AND e.entry_type = 'work'" if "entry_type" in entry_cols else ""
    created_by = "COALESCE(e.created_by, e.user_id)" if "created_by" in entry_cols else "e.user_id"
    conn.execute(
        f"""
        INSERT INTO entries (user_id, project_id, work_date, start_time, end_time, break_minutes,
                             work_minutes, activity_original, activity_de, source_lang, created_by, created_at)
        SELECT e.user_id, e.project_id, e.edate, e.stime, e.etime, COALESCE(e.brk, 0),
               CAST(ROUND(e.hours * 60) AS INTEGER), COALESCE(e.notes, ''), COALESCE(e.notes, ''), 'de',
               {created_by}, e.created_at
        FROM legacy_entries e
        JOIN projects p ON p.id = e.project_id
        WHERE 1=1 {type_filter}
        """
    )


MIGRATIONS = [_migrate_v1]


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, step in enumerate(MIGRATIONS, start=1):
        if current >= version:
            continue
        LOG.info("Datenbank-Migration auf Version %s", version)
        conn.execute("BEGIN")
        with transaction(conn):
            step(conn)
            conn.execute(f"PRAGMA user_version = {version}")
