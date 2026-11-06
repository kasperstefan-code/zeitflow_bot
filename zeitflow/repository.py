"""Datenzugriff. Einzige Stelle mit SQL außerhalb der Migrationen."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime

from zeitflow.db import transaction
from zeitflow.models import Customer, Entry, Project, Role, User, UserStatus

_ENTRY_SELECT = """
SELECT e.*, u.name AS user_name, p.name AS project_name, p.cost_center, p.customer_id,
       c.name AS customer_name
FROM entries e
JOIN users u     ON u.id = e.user_id
JOIN projects p  ON p.id = e.project_id
JOIN customers c ON c.id = p.customer_id
"""


def _user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        telegram_id=row["telegram_id"],
        name=row["name"],
        lang=row["lang"],
        role=Role(row["role"]),
        status=UserStatus(row["status"]),
    )


def _project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        name=row["name"],
        cost_center=row["cost_center"],
        active=bool(row["active"]),
    )


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        user_id=row["user_id"],
        user_name=row["user_name"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        cost_center=row["cost_center"],
        work_date=date.fromisoformat(row["work_date"]),
        start_time=row["start_time"],
        end_time=row["end_time"],
        break_minutes=row["break_minutes"],
        work_minutes=row["work_minutes"],
        activity_original=row["activity_original"],
        activity_de=row["activity_de"],
        source_lang=row["source_lang"],
        created_by=row["created_by"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def utc_stamp() -> str:
    """Sortierbarer UTC-Zeitstempel mit Millisekunden (gleiches Format wie SQLite datetime('now') + .fff)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- Benutzer -----------------------------------------------------------------

    def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        return _user(row) if row else None

    def get_user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _user(row) if row else None

    def create_user(self, telegram_id: int, name: str, lang: str, role: Role, status: UserStatus) -> User:
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO users (telegram_id, name, lang, role, status) VALUES (?,?,?,?,?)",
                (telegram_id, name, lang, role.value, status.value),
            )
        return self.get_user(cur.lastrowid)

    def list_users(self, status: UserStatus | None = UserStatus.ACTIVE) -> list[User]:
        if status is None:
            rows = self.conn.execute("SELECT * FROM users ORDER BY name").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM users WHERE status=? ORDER BY name", (status.value,)).fetchall()
        return [_user(r) for r in rows]

    def list_admins(self) -> list[User]:
        rows = self.conn.execute("SELECT * FROM users WHERE role='admin' AND status='active' ORDER BY name").fetchall()
        return [_user(r) for r in rows]

    def has_admin(self) -> bool:
        return self.conn.execute("SELECT 1 FROM users WHERE role='admin' AND status='active'").fetchone() is not None

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {"name", "lang", "role", "status"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"Unzulässige Felder: {set(fields) - allowed}")
        cols = ", ".join(f"{k}=?" for k in fields)
        values = [v.value if hasattr(v, "value") else v for v in fields.values()]
        with transaction(self.conn):
            self.conn.execute(f"UPDATE users SET {cols} WHERE id=?", (*values, user_id))

    # --- Kunden & Projekte --------------------------------------------------------

    def create_customer(self, name: str) -> Customer:
        with transaction(self.conn):
            cur = self.conn.execute("INSERT INTO customers (name) VALUES (?)", (name.strip(),))
        return Customer(id=cur.lastrowid, name=name.strip(), active=True)

    def get_customer(self, customer_id: int) -> Customer | None:
        row = self.conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        return Customer(row["id"], row["name"], bool(row["active"])) if row else None

    def list_customers(self, active_only: bool = True) -> list[Customer]:
        sql = "SELECT * FROM customers" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return [Customer(r["id"], r["name"], bool(r["active"])) for r in self.conn.execute(sql)]

    def set_customer_active(self, customer_id: int, active: bool) -> None:
        with transaction(self.conn):
            self.conn.execute("UPDATE customers SET active=? WHERE id=?", (int(active), customer_id))

    def create_project(self, customer_id: int, name: str, cost_center: str) -> Project:
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO projects (customer_id, name, cost_center) VALUES (?,?,?)",
                (customer_id, name.strip(), cost_center.strip()),
            )
        return self.get_project(cur.lastrowid)

    def get_project(self, project_id: int) -> Project | None:
        row = self.conn.execute(
            "SELECT p.*, c.name AS customer_name FROM projects p JOIN customers c ON c.id=p.customer_id WHERE p.id=?",
            (project_id,),
        ).fetchone()
        return _project(row) if row else None

    def list_projects(self, customer_id: int | None = None, active_only: bool = True) -> list[Project]:
        conds, params = ["1=1"], []
        if active_only:
            conds.append("p.active=1 AND c.active=1")
        if customer_id is not None:
            conds.append("p.customer_id=?")
            params.append(customer_id)
        rows = self.conn.execute(
            "SELECT p.*, c.name AS customer_name FROM projects p JOIN customers c ON c.id=p.customer_id "
            f"WHERE {' AND '.join(conds)} ORDER BY c.name, p.name",
            params,
        ).fetchall()
        return [_project(r) for r in rows]

    def update_project(self, project_id: int, touch_since: date | None = None, **fields) -> int:
        """Ändert ein Projekt. Mit touch_since werden alle KWs ab diesem Tag, in denen das Projekt vorkommt, als
        geändert markiert (Name/Kostenstelle stehen im Stundennachweis) – gibt deren Anzahl zurück."""
        allowed = {"name", "cost_center", "active"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"Unzulässige Felder: {set(fields) - allowed}")
        cols = ", ".join(f"{k}=?" for k in fields)
        weeks: set[tuple[int, int, int]] = set()
        with transaction(self.conn):
            self.conn.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), project_id))
            if touch_since is not None:
                rows = self.conn.execute(
                    "SELECT DISTINCT p.customer_id, e.work_date FROM entries e JOIN projects p ON p.id=e.project_id "
                    "WHERE e.project_id=? AND e.work_date>=?",
                    (project_id, touch_since.isoformat()),
                ).fetchall()
                for r in rows:
                    iso = date.fromisoformat(r["work_date"]).isocalendar()
                    weeks.add((r["customer_id"], iso.year, iso.week))
                for customer_id, year, week in weeks:
                    self._touch(customer_id, year, week)
        return len(weeks)

    # --- Einträge -----------------------------------------------------------------

    def insert_entries(self, rows: Iterable[dict]) -> list[int]:
        ids = []
        with transaction(self.conn):
            for r in rows:
                cur = self.conn.execute(
                    """
                    INSERT INTO entries (user_id, project_id, work_date, start_time, end_time, break_minutes,
                                         work_minutes, activity_original, activity_de, source_lang, created_by)
                    VALUES (:user_id, :project_id, :work_date, :start_time, :end_time, :break_minutes,
                            :work_minutes, :activity_original, :activity_de, :source_lang, :created_by)
                    """,
                    {**r, "work_date": r["work_date"].isoformat()},
                )
                ids.append(cur.lastrowid)
                self._touch_entry(cur.lastrowid)
        return ids

    # --- Änderungsprotokoll je Kunde/KW -------------------------------------------

    def _entry_scope(self, entry_id: int) -> tuple[int, date] | None:
        row = self.conn.execute(
            "SELECT p.customer_id, e.work_date FROM entries e JOIN projects p ON p.id=e.project_id WHERE e.id=?",
            (entry_id,),
        ).fetchone()
        return (row["customer_id"], date.fromisoformat(row["work_date"])) if row else None

    def _touch(self, customer_id: int, iso_year: int, iso_week: int) -> None:
        self.conn.execute(
            """
            INSERT INTO week_changes (customer_id, iso_year, iso_week, changed_at) VALUES (?,?,?,?)
            ON CONFLICT (customer_id, iso_year, iso_week) DO UPDATE SET changed_at=excluded.changed_at
            """,
            (customer_id, iso_year, iso_week, utc_stamp()),
        )

    def _touch_scope(self, scope: tuple[int, date] | None) -> None:
        if scope:
            iso = scope[1].isocalendar()
            self._touch(scope[0], iso.year, iso.week)

    def _touch_entry(self, entry_id: int) -> None:
        self._touch_scope(self._entry_scope(entry_id))

    def week_changed_at(self, customer_id: int, iso_year: int, iso_week: int) -> str | None:
        row = self.conn.execute(
            "SELECT changed_at FROM week_changes WHERE customer_id=? AND iso_year=? AND iso_week=?",
            (customer_id, iso_year, iso_week),
        ).fetchone()
        return row["changed_at"] if row else None

    def get_entry(self, entry_id: int) -> Entry | None:
        row = self.conn.execute(_ENTRY_SELECT + " WHERE e.id=?", (entry_id,)).fetchone()
        return _entry(row) if row else None

    def list_entries(
        self,
        *,
        user_id: int | None = None,
        customer_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        untranslated_only: bool = False,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Entry]:
        conds, params = ["1=1"], []
        if user_id is not None:
            conds.append("e.user_id=?")
            params.append(user_id)
        if customer_id is not None:
            conds.append("p.customer_id=?")
            params.append(customer_id)
        if date_from is not None:
            conds.append("e.work_date>=?")
            params.append(date_from.isoformat())
        if date_to is not None:
            conds.append("e.work_date<=?")
            params.append(date_to.isoformat())
        if untranslated_only:
            conds.append("e.activity_de IS NULL")
        order = "DESC" if newest_first else "ASC"
        sql = (
            _ENTRY_SELECT
            + f" WHERE {' AND '.join(conds)} ORDER BY e.work_date {order}, e.start_time {order}, u.name, e.id"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_entry(r) for r in self.conn.execute(sql, params)]

    def customers_with_entries(self, date_from: date, date_to: date) -> list[Customer]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT c.* FROM entries e
            JOIN projects p  ON p.id = e.project_id
            JOIN customers c ON c.id = p.customer_id
            WHERE e.work_date BETWEEN ? AND ?
            ORDER BY c.name
            """,
            (date_from.isoformat(), date_to.isoformat()),
        ).fetchall()
        return [Customer(r["id"], r["name"], bool(r["active"])) for r in rows]

    def update_entry(self, entry_id: int, actor_id: int, **fields) -> None:
        allowed = {
            "project_id",
            "work_date",
            "start_time",
            "end_time",
            "break_minutes",
            "work_minutes",
            "activity_original",
            "activity_de",
            "source_lang",
        }
        if not fields or set(fields) - allowed:
            raise ValueError(f"Unzulässige Felder: {set(fields) - allowed}")
        values = [v.isoformat() if isinstance(v, date) else v for v in fields.values()]
        cols = ", ".join(f"{k}=?" for k in fields)
        with transaction(self.conn):
            before = self._entry_scope(entry_id)
            self.conn.execute(
                f"UPDATE entries SET {cols}, updated_by=?, updated_at=? WHERE id=?",
                (*values, actor_id, _now(), entry_id),
            )
            # Datum oder Projekt können Woche/Kunde wechseln – beide Seiten markieren.
            self._touch_scope(before)
            self._touch_entry(entry_id)

    def set_translation(self, entry_ids: Iterable[int], activity_original: str, activity_de: str) -> list[int]:
        """Setzt die Übersetzung nur, wenn der Text inzwischen nicht geändert oder anders übersetzt wurde."""
        updated = []
        with transaction(self.conn):
            for entry_id in entry_ids:
                cur = self.conn.execute(
                    "UPDATE entries SET activity_de=? WHERE id=? AND activity_de IS NULL AND activity_original=?",
                    (activity_de, entry_id, activity_original),
                )
                if cur.rowcount:
                    updated.append(entry_id)
                    self._touch_entry(entry_id)
        return updated

    def delete_entry(self, entry_id: int) -> None:
        with transaction(self.conn):
            self._touch_entry(entry_id)
            self.conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))

    # --- Wochen-Zusatzdaten & Versandprotokoll -----------------------------------

    def get_travel_count(self, customer_id: int, iso_year: int, iso_week: int) -> int | None:
        row = self.conn.execute(
            "SELECT travel_count FROM week_extras WHERE customer_id=? AND iso_year=? AND iso_week=?",
            (customer_id, iso_year, iso_week),
        ).fetchone()
        return row["travel_count"] if row else None

    def set_travel_count(self, customer_id: int, iso_year: int, iso_week: int, count: int | None) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO week_extras (customer_id, iso_year, iso_week, travel_count) VALUES (?,?,?,?)
                ON CONFLICT (customer_id, iso_year, iso_week) DO UPDATE SET travel_count=excluded.travel_count
                """,
                (customer_id, iso_year, iso_week, count),
            )
            self._touch(customer_id, iso_year, iso_week)

    def log_report_run(
        self,
        customer_id: int,
        iso_year: int,
        iso_week: int,
        recipient: str,
        status: str,
        error: str | None = None,
        snapshot_at: str | None = None,
    ) -> None:
        """snapshot_at = Zeitpunkt, zu dem der versendete Datenstand gelesen wurde."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO report_runs (customer_id, iso_year, iso_week, recipient, status, error, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (customer_id, iso_year, iso_week, recipient, status, error, snapshot_at or utc_stamp()),
            )

    def last_sent_at(self, customer_id: int, iso_year: int, iso_week: int) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(created_at) AS t FROM report_runs "
            "WHERE customer_id=? AND iso_year=? AND iso_week=? AND status='sent'",
            (customer_id, iso_year, iso_week),
        ).fetchone()
        return row["t"]

    def report_already_sent(self, customer_id: int, iso_year: int, iso_week: int) -> bool:
        return self.last_sent_at(customer_id, iso_year, iso_week) is not None

    def weeks_needing_resend(self) -> list[tuple[int, int]]:
        """KWs mit Änderungen nach dem letzten Versand oder mit fehlgeschlagenem, nie geglücktem Versand."""
        rows = self.conn.execute(
            """
            WITH sent AS (
                SELECT customer_id, iso_year, iso_week, MAX(created_at) AS sent_at
                FROM report_runs WHERE status='sent' GROUP BY customer_id, iso_year, iso_week
            )
            SELECT w.iso_year, w.iso_week FROM week_changes w
            JOIN sent s USING (customer_id, iso_year, iso_week)
            WHERE w.changed_at > s.sent_at
            UNION
            SELECT r.iso_year, r.iso_week FROM report_runs r
            LEFT JOIN sent s USING (customer_id, iso_year, iso_week)
            WHERE r.status='failed' AND s.sent_at IS NULL
            ORDER BY 1, 2
            """
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def unsent_changed_weeks(self) -> list[tuple[int, int, int]]:
        """(Kunde, Jahr, KW) mit Aktivität, für die noch nie erfolgreich versendet wurde."""
        rows = self.conn.execute(
            """
            SELECT w.customer_id, w.iso_year, w.iso_week FROM week_changes w
            WHERE NOT EXISTS (
                SELECT 1 FROM report_runs r WHERE r.customer_id=w.customer_id AND r.iso_year=w.iso_year
                AND r.iso_week=w.iso_week AND r.status='sent'
            )
            ORDER BY 2, 3, 1
            """
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def customers_sent_for_week(self, iso_year: int, iso_week: int) -> list[Customer]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT c.* FROM report_runs r JOIN customers c ON c.id = r.customer_id
            WHERE r.iso_year=? AND r.iso_week=? AND r.status='sent' ORDER BY c.name
            """,
            (iso_year, iso_week),
        ).fetchall()
        return [Customer(r["id"], r["name"], bool(r["active"])) for r in rows]
