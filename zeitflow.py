#!/usr/bin/env python3
"""
ZeitFlow – Telegram Bot für Zeiterfassung
Erweitert um:
- Rollen: Mitarbeiter / Vorarbeiter / Admin
- Fremderfassung für Vorarbeiter/Admins
- Korrektur vorhandener Einträge
- Protokollierung: Mitarbeiter + Ersteller + letzte Änderung
"""

import csv
import io
import logging
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:
    from openpyxl import Workbook
    from openpyxl.styles import Border, Font, PatternFill, Side

    XLSX = True
except ImportError:
    XLSX = False

LOG = logging.getLogger("zeitflow")
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

DB_PATH = Path(os.environ.get("ZEITFLOW_DB_PATH", Path(__file__).parent / "zeitflow.db"))


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in cols)


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tid         INTEGER UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                lang        TEXT DEFAULT 'de',
                is_admin    INTEGER DEFAULT 0,
                active      INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                customer    TEXT DEFAULT '',
                cost_center TEXT DEFAULT '',
                active      INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                project_id  INTEGER NOT NULL REFERENCES projects(id),
                edate       TEXT NOT NULL,
                stime       TEXT NOT NULL,
                etime       TEXT NOT NULL,
                brk         INTEGER DEFAULT 30,
                hours       REAL NOT NULL,
                notes       TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS ix_e_date ON entries(edate);
            CREATE INDEX IF NOT EXISTS ix_e_user ON entries(user_id);
            """
        )

        # migrations: users.role
        if not _column_exists(c, "users", "role"):
            c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'employee'")
            c.execute("UPDATE users SET role='admin' WHERE is_admin=1")
            c.execute("UPDATE users SET role='employee' WHERE role IS NULL OR role='' ")

        # migrations: entries.created_by / updated_by / updated_at
        if not _column_exists(c, "entries", "created_by"):
            c.execute("ALTER TABLE entries ADD COLUMN created_by INTEGER")
            c.execute("UPDATE entries SET created_by=user_id WHERE created_by IS NULL")
        if not _column_exists(c, "entries", "updated_by"):
            c.execute("ALTER TABLE entries ADD COLUMN updated_by INTEGER")
        if not _column_exists(c, "entries", "updated_at"):
            c.execute("ALTER TABLE entries ADD COLUMN updated_at TEXT")

        if c.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO projects (name,customer,cost_center) VALUES (?,?,?)",
                [
                    ("Neubau Halle B", "Schneider GmbH", "KST-4010"),
                    ("Sanierung Dach", "Gemeinde Freiburg", "KST-4020"),
                    ("Montage Lüftung", "Klinik Süd", "KST-4030"),
                    ("Heizung Austausch", "Familie Braun", "KST-4040"),
                ],
            )
            LOG.info("Demo-Projekte angelegt")


T = {
    "de": {
        "flag": "🇩🇪",
        "label": "Deutsch",
        "welcome": (
            "👋 ZeitFlow\n\n"
            "/zeit – Erfassen\n"
            "/heute – Heute\n"
            "/woche – Woche\n"
            "/projekte – Projekte\n"
            "/korrektur – Eintrag ändern\n"
            "/sprache – Sprache\n"
            "/hilfe – Hilfe"
        ),
        "admin": (
            "\n\n👑 Leitung:\n"
            "/export – Excel\n"
            "/team – Mitarbeiter\n"
            "/stats – Dashboard\n"
            "/rolle – Rolle setzen\n"
            "/addprojekt – Neues Projekt\n"
            "/editprojekt – Bearbeiten\n"
            "/delprojekt – Deaktivieren\n"
            "/deluser – Entfernen"
        ),
        "ask_name": "Vor- und Nachname?",
        "name_ok": "Hallo {name} 👋",
        "pick_target": "Für wen soll der Eintrag angelegt werden?",
        "pick_proj": "Welches Projekt?",
        "pick_date": "📅 Datum? (TT.MM.JJJJ / heute)",
        "ask_s": "🕐 Beginn?",
        "ask_e": "🕑 Ende?",
        "ask_b": "☕ Pause (Min)?",
        "ask_n": "📝 Bemerkung? (/skip)",
        "saved": "✅ Gespeichert",
        "more": "Noch ein Eintrag?",
        "y": "✅ Ja",
        "n": "❌ Fertig",
        "sum": "📋 {p}\n🏢 {c}\n👤 {employee}\n📅 {d}\n🕐 {s}–{e}\n☕ {b}min\n⏱ {h} Std.",
        "created_by": "Erfasst von",
        "updated_by": "Geändert von",
        "no_e": "Keine Einträge.",
        "tot": "Gesamt",
        "hr": "Std.",
        "bad_t": "❌ Format: 7:00",
        "bad_d": "❌ Format: TT.MM.JJJJ",
        "bad_b": "❌ Bitte Minuten als Zahl eingeben.",
        "no_adm": "⚠️ Nur Leitung.",
        "no_foreman": "⚠️ Nur Vorarbeiter oder Admin.",
        "cancel": "Abgebrochen.",
        "no_p": "Keine Projekte.",
        "today": "heute",
        "ap1": "📋 Projektname?",
        "ap2": "🏢 Kunde?",
        "ap3": "🏷 Kostenstelle?",
        "ap_ok": "✅ Projekt:\n📋 {n}\n🏢 {c}\n🏷 {k}",
        "ep_pick": "Bearbeiten?",
        "ep_what": "Was ändern?",
        "ep_val": "Neuer Wert?",
        "ep_ok": "✅ Aktualisiert.",
        "dp_pick": "Deaktivieren?",
        "dp_ask": "⚠️ ‘{n}’ deaktivieren?",
        "dp_ok": "✅ ‘{n}’ deaktiviert.",
        "team_h": "👥 Team:\n",
        "team_r": "{i} {n} – {role} – {h} Std. ({c} Eintr.) {l}\n",
        "role_pick_user": "Wessen Rolle ändern?",
        "role_pick_value": "Welche Rolle soll {name} bekommen?",
        "role_ok": "✅ {name} ist jetzt {role}.",
        "du_pick": "Entfernen?",
        "du_ask": "⚠️ {n} entfernen?",
        "du_ok": "✅ {n} entfernt.",
        "du_none": "Keine.",
        "yes": "✅ Ja",
        "no": "❌ Nein",
        "lang_ok": "✅ Deutsch",
        "export_ok": "📊 {c} Einträge",
        "export_no": "Keine Daten.",
        "corr_pick": "Welchen Eintrag willst du korrigieren?",
        "corr_field": "Was willst du ändern?",
        "corr_value": "Neuer Wert eingeben.",
        "corr_ok": "✅ Eintrag aktualisiert.",
        "corr_deleted": "🗑️ Eintrag gelöscht.",
        "corr_none": "Keine passenden Einträge gefunden.",
        "field_date": "Datum",
        "field_start": "Beginn",
        "field_end": "Ende",
        "field_break": "Pause",
        "field_notes": "Bemerkung",
        "field_delete": "Löschen",
        "role_employee": "Mitarbeiter",
        "role_foreman": "Vorarbeiter",
        "role_admin": "Admin",
        "self_entry": "🙋 Für mich",
        "skip": "/skip",
    },
    "ru": {"flag": "🇷🇺", "label": "Русский"},
    "pl": {"flag": "🇵🇱", "label": "Polski"},
    "lv": {"flag": "🇱🇻", "label": "Latviešu"},
}


def t(lang: str, key: str) -> str:
    return T.get(lang, T["de"]).get(key, T["de"].get(key, key))


# states
S_NAME = 0
S_TARGET, S_PROJ, S_DATE, S_START, S_END, S_BREAK, S_NOTES, S_MORE = range(1, 9)
S_AP1, S_AP2, S_AP3 = range(9, 12)
S_EP_PICK, S_EP_FIELD, S_EP_VAL = range(12, 15)
S_DP_PICK, S_DP_CONFIRM = range(15, 17)
S_ROLE_PICK_USER, S_ROLE_PICK_VALUE = range(17, 19)
S_DU_PICK, S_DU_CONFIRM = range(19, 21)
S_CORR_PICK, S_CORR_FIELD, S_CORR_VALUE, S_CORR_DELETE = range(21, 25)


def role_label(role: str, lang: str = "de") -> str:
    return {
        "employee": t(lang, "role_employee"),
        "foreman": t(lang, "role_foreman"),
        "admin": t(lang, "role_admin"),
    }.get(role or "employee", role or "employee")


# helpers

def L(ctx):
    return ctx.user_data.get("lang", "de")


def UID(ctx):
    return ctx.user_data.get("uid")


def ROLE(ctx):
    return ctx.user_data.get("role", "employee")


def ADM(ctx):
    return ROLE(ctx) == "admin" or bool(ctx.user_data.get("adm", False))


def FOREMAN(ctx):
    return ROLE(ctx) in {"foreman", "admin"} or ADM(ctx)


def sync_user(ctx, u: dict):
    role = u.get("role") or ("admin" if u.get("is_admin") else "employee")
    ctx.user_data.update(
        {
            "uid": u["id"],
            "lang": u["lang"],
            "adm": bool(u.get("is_admin")),
            "tid": u["tid"],
            "role": role,
        }
    )


# database helpers

def get_or_create_user(tid: int, name: str, lang: str = "de") -> dict:
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE tid=?", (tid,)).fetchone()
        if u:
            return dict(u)
        has_admin = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' OR is_admin=1").fetchone()[0]
        role = "employee" if has_admin else "admin"
        is_admin = 1 if role == "admin" else 0
        c.execute(
            "INSERT INTO users (tid,name,lang,is_admin,role) VALUES (?,?,?,?,?)",
            (tid, name, lang, is_admin, role),
        )
        return dict(c.execute("SELECT * FROM users WHERE tid=?", (tid,)).fetchone())


def get_user(tid: int) -> dict | None:
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE tid=? AND active=1", (tid,)).fetchone()
        return dict(u) if u else None


def get_users(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM users"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY name"
    with db() as c:
        return [dict(r) for r in c.execute(sql).fetchall()]



def get_projects() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM projects WHERE active=1 ORDER BY name").fetchall()]



def calc_hours(stime: str, etime: str, brk: int) -> float:
    sh, sm = map(int, stime.split(":"))
    eh, em = map(int, etime.split(":"))
    mins = (eh * 60 + em) - (sh * 60 + sm) - brk
    return round(max(0, mins) / 60, 2)



def save_entry(user_id, created_by, project_id, edate, stime, etime, brk, notes) -> float:
    hours = calc_hours(stime, etime, brk)
    with db() as c:
        c.execute(
            """
            INSERT INTO entries (user_id, created_by, project_id, edate, stime, etime, brk, hours, notes)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (user_id, created_by, project_id, edate, stime, etime, brk, hours, notes),
        )
    return hours



def get_entries(user_id=None, date_from=None, date_to=None, limit=None, editable_by=None) -> list[dict]:
    conds, params = ["1=1"], []
    if user_id:
        conds.append("e.user_id=?")
        params.append(user_id)
    if date_from:
        conds.append("e.edate>=?")
        params.append(date_from)
    if date_to:
        conds.append("e.edate<=?")
        params.append(date_to)
    if editable_by and editable_by.get("role") not in {"foreman", "admin"}:
        conds.append("e.user_id=?")
        params.append(editable_by["id"])

    sql = f"""
        SELECT
            e.*,
            u.name AS employee,
            p.name AS project,
            p.customer,
            p.cost_center,
            cu.name AS creator_name,
            uu.name AS updater_name
        FROM entries e
        JOIN users u ON e.user_id=u.id
        JOIN projects p ON e.project_id=p.id
        LEFT JOIN users cu ON e.created_by=cu.id
        LEFT JOIN users uu ON e.updated_by=uu.id
        WHERE {' AND '.join(conds)}
        ORDER BY e.edate DESC, e.id DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with db() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]



def get_entry(entry_id: int) -> dict | None:
    es = get_entries(limit=1)
    with db() as c:
        row = c.execute(
            """
            SELECT
                e.*,
                u.name AS employee,
                p.name AS project,
                p.customer,
                p.cost_center,
                cu.name AS creator_name,
                uu.name AS updater_name
            FROM entries e
            JOIN users u ON e.user_id=u.id
            JOIN projects p ON e.project_id=p.id
            LEFT JOIN users cu ON e.created_by=cu.id
            LEFT JOIN users uu ON e.updated_by=uu.id
            WHERE e.id=?
            """,
            (entry_id,),
        ).fetchone()
        return dict(row) if row else None



def update_entry(entry_id: int, field: str, value, editor_user_id: int):
    allowed = {"edate", "stime", "etime", "brk", "notes"}
    if field not in allowed:
        raise ValueError("Ungültiges Feld")

    with db() as c:
        current = c.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not current:
            raise ValueError("Eintrag nicht gefunden")

        new_data = dict(current)
        new_data[field] = value
        new_hours = calc_hours(new_data["stime"], new_data["etime"], int(new_data["brk"]))
        c.execute(
            f"UPDATE entries SET {field}=?, hours=?, updated_by=?, updated_at=datetime('now') WHERE id=?",
            (value, new_hours, editor_user_id, entry_id),
        )



def delete_entry(entry_id: int):
    with db() as c:
        c.execute("DELETE FROM entries WHERE id=?", (entry_id,))



def parse_time(s: str) -> str | None:
    s = s.strip().replace(".", ":")
    for fmt in ("%H:%M", "%H"):
        try:
            d = datetime.strptime(s, fmt)
            return f"{d.hour:02d}:{d.minute:02d}"
        except ValueError:
            pass
    return None



def parse_date(s: str) -> str | None:
    s = s.strip().lower()
    if s in ("heute", "today", "сегодня", "dzisiaj", "šodien"):
        return date.today().isoformat()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None



def fde(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return d



def entry_brief(e: dict) -> str:
    return f"{fde(e['edate'])} | {e['employee']} | {e['project']} | {e['stime']}-{e['etime']}"


# registration
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if u:
        sync_user(ctx, u)
        msg = t(u["lang"], "welcome")
        if u.get("role") in {"admin", "foreman"} or u.get("is_admin"):
            msg += t(u["lang"], "admin")
        await update.message.reply_text(msg)
    else:
        kb = [[InlineKeyboardButton(f"{v['flag']} {v['label']}", callback_data=f"rl_{k}")] for k, v in T.items()]
        await update.message.reply_text("🌍 Sprache?", reply_markup=InlineKeyboardMarkup(kb))


async def reg_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["reg_lang"] = q.data[3:]
    await q.edit_message_text(t(ctx.user_data["reg_lang"], "ask_name"))
    return S_NAME


async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name, tid = update.message.text.strip(), update.effective_user.id
    lang = ctx.user_data.get("reg_lang", "de")
    u = get_or_create_user(tid, name, lang)
    sync_user(ctx, u)
    hint = "\n\n👑 Du bist Admin!" if ROLE(ctx) == "admin" else ""
    await update.message.reply_text(t(lang, "name_ok").format(name=name) + hint)
    msg = t(lang, "welcome")
    if FOREMAN(ctx):
        msg += t(lang, "admin")
    await update.message.reply_text(msg)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t(L(ctx), "cancel"), reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# time entry
async def cmd_zeit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not UID(ctx):
        await update.message.reply_text("→ /start")
        return ConversationHandler.END

    ctx.user_data.pop("target_uid", None)
    ctx.user_data.pop("target_name", None)

    if FOREMAN(ctx):
        users = [u for u in get_users() if u["id"] != UID(ctx)]
        kb = [[InlineKeyboardButton(t(L(ctx), "self_entry"), callback_data="tu_self")]]
        kb.extend([[InlineKeyboardButton(f"👤 {u['name']}", callback_data=f"tu_{u['id']}")] for u in users])
        await update.message.reply_text(t(L(ctx), "pick_target"), reply_markup=InlineKeyboardMarkup(kb))
        return S_TARGET

    ctx.user_data["target_uid"] = UID(ctx)
    ctx.user_data["target_name"] = get_user(update.effective_user.id)["name"]
    return await _prompt_project(update.message.reply_text, ctx)


async def _prompt_project(reply_func, ctx):
    ps = get_projects()
    if not ps:
        await reply_func(t(L(ctx), "no_p"))
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{p['name']} – {p['customer']}", callback_data=f"p_{p['id']}")] for p in ps]
    await reply_func(t(L(ctx), "pick_proj"), reply_markup=InlineKeyboardMarkup(kb))
    return S_PROJ


async def z_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    payload = q.data[3:]
    if payload == "self":
        target = next((u for u in get_users() if u["id"] == UID(ctx)), None)
    else:
        target = next((u for u in get_users() if u["id"] == int(payload)), None)
    if not target:
        await q.edit_message_text("Mitarbeiter nicht gefunden.")
        return ConversationHandler.END
    ctx.user_data["target_uid"] = target["id"]
    ctx.user_data["target_name"] = target["name"]
    return await _prompt_project(q.edit_message_text, ctx)


async def z_proj(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data[2:])
    p = next((x for x in get_projects() if x["id"] == pid), None)
    if not p:
        return ConversationHandler.END
    ctx.user_data.update({"pid": pid, "pn": p["name"], "pc": p["customer"], "pk": p["cost_center"]})
    kb = [[InlineKeyboardButton(f"📅 {t(L(ctx), 'today').capitalize()}", callback_data="dt")]]
    await q.edit_message_text(t(L(ctx), "pick_date"), reply_markup=InlineKeyboardMarkup(kb))
    return S_DATE


async def z_date_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["ed"] = date.today().isoformat()
    await q.edit_message_text(t(L(ctx), "ask_s"))
    return S_START


async def z_date_txt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text(t(L(ctx), "bad_d"))
        return S_DATE
    ctx.user_data["ed"] = d
    await update.message.reply_text(t(L(ctx), "ask_s"))
    return S_START


async def z_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tm = parse_time(update.message.text)
    if not tm:
        await update.message.reply_text(t(L(ctx), "bad_t"))
        return S_START
    ctx.user_data["st"] = tm
    await update.message.reply_text(t(L(ctx), "ask_e"))
    return S_END


async def z_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tm = parse_time(update.message.text)
    if not tm:
        await update.message.reply_text(t(L(ctx), "bad_t"))
        return S_END
    ctx.user_data["et"] = tm
    kb = ReplyKeyboardMarkup([["0", "15", "30", "45", "60"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(t(L(ctx), "ask_b"), reply_markup=kb)
    return S_BREAK


async def z_break(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["bk"] = int(update.message.text.strip())
    except Exception:
        await update.message.reply_text(t(L(ctx), "bad_b"))
        return S_BREAK
    await update.message.reply_text(t(L(ctx), "ask_n"), reply_markup=ReplyKeyboardRemove())
    return S_NOTES


async def z_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud, lang = ctx.user_data, L(ctx)
    notes = "" if update.message.text.strip() == "/skip" else update.message.text.strip()
    hours = save_entry(
        user_id=ud["target_uid"],
        created_by=ud["uid"],
        project_id=ud["pid"],
        edate=ud["ed"],
        stime=ud["st"],
        etime=ud["et"],
        brk=ud["bk"],
        notes=notes,
    )
    txt = t(lang, "saved") + "\n\n" + t(lang, "sum").format(
        p=ud["pn"],
        c=ud["pc"],
        employee=ud["target_name"],
        d=fde(ud["ed"]),
        s=ud["st"],
        e=ud["et"],
        b=ud["bk"],
        h=f"{hours:.1f}",
    )
    if ud["target_uid"] != ud["uid"]:
        actor_name = next((u["name"] for u in get_users() if u["id"] == ud["uid"]), "-")
        txt += f"\n🧾 {t(lang, 'created_by')}: {actor_name}"
    if notes:
        txt += f"\n📝 {notes}"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "y"), callback_data="my"), InlineKeyboardButton(t(lang, "n"), callback_data="mn")]]
    )
    await update.message.reply_text(txt + "\n\n" + t(lang, "more"), reply_markup=kb)
    return S_MORE


async def z_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "my":
        if FOREMAN(ctx):
            users = [u for u in get_users() if u["id"] != UID(ctx)]
            kb = [[InlineKeyboardButton(t(L(ctx), "self_entry"), callback_data="tu_self")]]
            kb.extend([[InlineKeyboardButton(f"👤 {u['name']}", callback_data=f"tu_{u['id']}")] for u in users])
            await q.edit_message_text(t(L(ctx), "pick_target"), reply_markup=InlineKeyboardMarkup(kb))
            return S_TARGET
        return await _prompt_project(q.edit_message_text, ctx)
    await q.edit_message_text("👍")
    return ConversationHandler.END


# views
async def cmd_heute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not UID(ctx):
        await update.message.reply_text("→ /start")
        return
    lang, today = L(ctx), date.today().isoformat()
    es = get_entries(user_id=UID(ctx), date_from=today, date_to=today)
    if not es:
        await update.message.reply_text(t(lang, "no_e"))
        return
    txt, total = f"📊 {fde(today)}\n", 0.0
    for i, e in enumerate(es, 1):
        txt += (
            f"\n{i}. {e['project']} ({e['customer']})\n"
            f"   🏷 {e['cost_center']}\n"
            f"   {e['stime']}–{e['etime']} ☕{e['brk']}min ⏱{e['hours']:.1f}{t(lang, 'hr')}"
        )
        if e["notes"]:
            txt += f"\n   📝 {e['notes']}"
        if e.get("creator_name") and e["creator_name"] != e["employee"]:
            txt += f"\n   🧾 {t(lang, 'created_by')}: {e['creator_name']}"
        total += e["hours"]
    txt += f"\n\n━━━━━━━━━━\n{t(lang, 'tot')}: {total:.1f} {t(lang, 'hr')}"
    await update.message.reply_text(txt)


async def cmd_woche(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not UID(ctx):
        await update.message.reply_text("→ /start")
        return
    lang = L(ctx)
    ws = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    es = get_entries(user_id=UID(ctx), date_from=ws)
    if not es:
        await update.message.reply_text(t(lang, "no_e"))
        return
    txt, cur, total = "📊\n", "", 0.0
    for e in es:
        if e["edate"] != cur:
            cur = e["edate"]
            txt += f"\n📅 {fde(cur)}\n"
        txt += f"  • {e['project']} | {e['stime']}–{e['etime']} | {e['hours']:.1f}{t(lang, 'hr')}\n"
        if e.get("creator_name") and e["creator_name"] != e["employee"]:
            txt += f"    ↳ {t(lang, 'created_by')}: {e['creator_name']}\n"
        total += e["hours"]
    txt += f"\n━━━━━━━━━━\n{t(lang, 'tot')}: {total:.1f} {t(lang, 'hr')}"
    await update.message.reply_text(txt)


async def cmd_projekte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang, ps = L(ctx), get_projects()
    if not ps:
        await update.message.reply_text(t(lang, "no_p"))
        return
    txt = "📋\n"
    for p in ps:
        txt += f"\n📋 {p['name']}\n   🏢 {p['customer']}\n   🏷 {p['cost_center']}\n"
    await update.message.reply_text(txt)


async def cmd_sprache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['flag']} {v['label']}", callback_data=f"sl_{k}")] for k, v in T.items()]
    await update.message.reply_text("🌍", reply_markup=InlineKeyboardMarkup(kb))


async def sprache_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data[3:]
    ctx.user_data["lang"] = lang
    u = get_user(q.from_user.id)
    if u:
        with db() as c:
            c.execute("UPDATE users SET lang=? WHERE id=?", (lang, u["id"]))
    await q.edit_message_text(t(lang, "lang_ok"))


async def cmd_hilfe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = L(ctx)
    msg = t(lang, "welcome")
    if FOREMAN(ctx):
        msg += t(lang, "admin")
    await update.message.reply_text(msg)


# admin / management
async def cmd_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = L(ctx)
    if not FOREMAN(ctx):
        await update.message.reply_text(t(lang, "no_foreman"))
        return
    with db() as c:
        users = [dict(r) for r in c.execute("SELECT * FROM users WHERE active=1 ORDER BY name").fetchall()]
        txt = t(lang, "team_h")
        for u in users:
            s = c.execute(
                "SELECT COUNT(*) as c, COALESCE(SUM(hours),0) as h FROM entries WHERE user_id=?", (u["id"],)
            ).fetchone()
            txt += t(lang, "team_r").format(
                i="👑" if (u.get("role") == "admin" or u.get("is_admin")) else ("🦺" if u.get("role") == "foreman" else "👤"),
                n=u["name"],
                role=role_label(u.get("role") or ("admin" if u.get("is_admin") else "employee"), lang),
                h=f"{s['h']:.1f}",
                c=s["c"],
                l=T.get(u["lang"], T["de"])["flag"],
            )
    await update.message.reply_text(txt)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = L(ctx)
    if not FOREMAN(ctx):
        await update.message.reply_text(t(lang, "no_foreman"))
        return
    today = date.today().isoformat()
    ws = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    ms = date.today().replace(day=1).isoformat()
    with db() as c:
        td = c.execute("SELECT COALESCE(SUM(hours),0) as h, COUNT(*) as c FROM entries WHERE edate=?", (today,)).fetchone()
        wk = c.execute("SELECT COALESCE(SUM(hours),0) as h, COUNT(*) as c FROM entries WHERE edate>=?", (ws,)).fetchone()
        mo = c.execute("SELECT COALESCE(SUM(hours),0) as h, COUNT(*) as c FROM entries WHERE edate>=?", (ms,)).fetchone()
        bp = [
            dict(r)
            for r in c.execute(
                """
                SELECT p.name, COALESCE(SUM(e.hours),0) as h, COUNT(e.id) as c
                FROM entries e JOIN projects p ON e.project_id=p.id
                WHERE e.edate>=?
                GROUP BY p.id ORDER BY h DESC LIMIT 5
                """,
                (ms,),
            ).fetchall()
        ]
    txt = (
        f"📊 Dashboard\n\n"
        f"Heute: {td['h']:.1f} Std. ({td['c']})\n"
        f"Woche: {wk['h']:.1f} Std. ({wk['c']})\n"
        f"Monat: {mo['h']:.1f} Std. ({mo['c']})"
    )
    if bp:
        txt += "\n\n📋 Projekte (Monat):"
        for p in bp:
            txt += f"\n  • {p['name']}: {p['h']:.1f} Std."
    await update.message.reply_text(txt)


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = L(ctx)
    if not FOREMAN(ctx):
        await update.message.reply_text(t(lang, "no_foreman"))
        return
    es = get_entries()
    if not es:
        await update.message.reply_text(t(lang, "export_no"))
        return

    headers = [
        "Datum",
        "Mitarbeiter",
        "Erfasst von",
        "Projekt",
        "Kunde",
        "KST",
        "Beginn",
        "Ende",
        "Pause",
        "Std.",
        "Bemerkung",
        "Geändert von",
        "Geändert am",
    ]

    if XLSX:
        wb = Workbook()
        hf = Font(bold=True, color="FFFFFF", size=11)
        hfi = PatternFill(start_color="1e3a5f", end_color="1e3a5f", fill_type="solid")
        bd = Border(*(Side(style="thin"),) * 4)
        ws1 = wb.active
        ws1.title = "Detail"
        for col, head in enumerate(headers, 1):
            cl = ws1.cell(row=1, column=col, value=head)
            cl.font, cl.fill, cl.border = hf, hfi, bd
        for row, e in enumerate(es, 2):
            values = [
                fde(e["edate"]),
                e["employee"],
                e.get("creator_name") or e["employee"],
                e["project"],
                e["customer"],
                e["cost_center"],
                e["stime"],
                e["etime"],
                e["brk"],
                e["hours"],
                e["notes"] or "",
                e.get("updater_name") or "",
                e.get("updated_at") or "",
            ]
            for col, value in enumerate(values, 1):
                cl = ws1.cell(row=row, column=col, value=value)
                cl.border = bd
                if col == 10:
                    cl.number_format = "0.0"
        widths = [12, 20, 20, 25, 22, 14, 8, 8, 10, 10, 30, 20, 20]
        for i, w in enumerate(widths, 1):
            ws1.column_dimensions[chr(64 + i)].width = w

        ws2 = wb.create_sheet("MeinBüro Import")
        bf = PatternFill(start_color="2563eb", end_color="2563eb", fill_type="solid")
        for col, head in enumerate(
            [
                "BestellnummerShop",
                "Bestelldatum",
                "Kundennummer",
                "Firmenname",
                "Artikelnummer",
                "Menge",
                "abweichenderEinzelpreisNetto",
                "abweichenderArtikeltext",
            ],
            1,
        ):
            cl = ws2.cell(row=1, column=col, value=head)
            cl.font = Font(bold=True, color="FFFFFF", size=10)
            cl.fill = bf
        orders = defaultdict(list)
        for e in es:
            orders[(e["customer"], e["edate"])].append(e)
        r2, order_no = 2, 1
        for (customer, ed), items in sorted(orders.items(), key=lambda x: x[0][1]):
            oid = f"ZF-{ed.replace('-', '')}-{order_no:03d}"
            for it in items:
                ws2.cell(row=r2, column=1, value=oid)
                ws2.cell(row=r2, column=2, value=fde(ed))
                ws2.cell(row=r2, column=3, value="")
                ws2.cell(row=r2, column=4, value=customer)
                ws2.cell(row=r2, column=5, value="MONTAGE-H")
                ws2.cell(row=r2, column=6, value=it["hours"])
                ws2.cell(row=r2, column=7, value="")
                desc = f"{it['employee']} – {it['project']}"
                if it["notes"]:
                    desc += f" ({it['notes']})"
                if it.get("creator_name") and it["creator_name"] != it["employee"]:
                    desc += f" | erfasst von {it['creator_name']}"
                ws2.cell(row=r2, column=8, value=desc)
                r2 += 1
            order_no += 1

        ws3 = wb.create_sheet("Pro Mitarbeiter")
        gf = PatternFill(start_color="059669", end_color="059669", fill_type="solid")
        for col, head in enumerate(["Mitarbeiter", "Einträge", "Stunden", "Projekte"], 1):
            cl = ws3.cell(row=1, column=col, value=head)
            cl.font = Font(bold=True, color="FFFFFF", size=10)
            cl.fill = gf
        emp = defaultdict(lambda: {"c": 0, "h": 0.0, "p": set()})
        for e in es:
            emp[e["employee"]]["c"] += 1
            emp[e["employee"]]["h"] += e["hours"]
            emp[e["employee"]]["p"].add(e["project"])
        for row, (nm, s) in enumerate(sorted(emp.items()), 2):
            ws3.cell(row=row, column=1, value=nm)
            ws3.cell(row=row, column=2, value=s["c"])
            ws3.cell(row=row, column=3, value=round(s["h"], 1))
            ws3.cell(row=row, column=4, value=", ".join(sorted(s["p"])))

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        await update.message.reply_document(
            document=buf,
            filename=f"zeitflow_{date.today().isoformat()}.xlsx",
            caption=t(lang, "export_ok").format(c=len(es)),
        )
    else:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(headers)
        for e in es:
            w.writerow(
                [
                    fde(e["edate"]),
                    e["employee"],
                    e.get("creator_name") or e["employee"],
                    e["project"],
                    e["customer"],
                    e["cost_center"],
                    e["stime"],
                    e["etime"],
                    e["brk"],
                    f"{e['hours']:.1f}",
                    e["notes"] or "",
                    e.get("updater_name") or "",
                    e.get("updated_at") or "",
                ]
            )
        cb = io.BytesIO(("\ufeff" + buf.getvalue()).encode("utf-8"))
        await update.message.reply_document(
            document=cb,
            filename=f"zeitflow_{date.today().isoformat()}.csv",
            caption=t(lang, "export_ok").format(c=len(es)),
        )


# project management
async def cmd_addprojekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx):
        await update.message.reply_text(t(L(ctx), "no_adm"))
        return ConversationHandler.END
    await update.message.reply_text(t(L(ctx), "ap1"))
    return S_AP1


async def ap1(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["_apn"] = update.message.text.strip()
    await update.message.reply_text(t(L(ctx), "ap2"))
    return S_AP2


async def ap2(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["_apc"] = update.message.text.strip()
    await update.message.reply_text(t(L(ctx), "ap3"))
    return S_AP3


async def ap3(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud, lang = ctx.user_data, L(ctx)
    k = update.message.text.strip()
    with db() as c:
        c.execute("INSERT INTO projects (name,customer,cost_center) VALUES (?,?,?)", (ud["_apn"], ud["_apc"], k))
    await update.message.reply_text(t(lang, "ap_ok").format(n=ud["_apn"], c=ud["_apc"], k=k))
    return ConversationHandler.END


async def cmd_editprojekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx):
        await update.message.reply_text(t(L(ctx), "no_adm"))
        return ConversationHandler.END
    ps = get_projects()
    if not ps:
        await update.message.reply_text(t(L(ctx), "no_p"))
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{p['name']} – {p['customer']}", callback_data=f"ep_{p['id']}")] for p in ps]
    await update.message.reply_text(t(L(ctx), "ep_pick"), reply_markup=InlineKeyboardMarkup(kb))
    return S_EP_PICK


async def ep_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["_epid"] = int(q.data[3:])
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Name", callback_data="ef_name")],
            [InlineKeyboardButton("🏢 Kunde", callback_data="ef_customer")],
            [InlineKeyboardButton("🏷 Kostenstelle", callback_data="ef_cost_center")],
        ]
    )
    await q.edit_message_text(t(L(ctx), "ep_what"), reply_markup=kb)
    return S_EP_FIELD


async def ep_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["_epf"] = q.data[3:]
    await q.edit_message_text(t(L(ctx), "ep_val"))
    return S_EP_VAL


async def ep_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    with db() as c:
        c.execute(f"UPDATE projects SET {ud['_epf']}=? WHERE id=?", (update.message.text.strip(), ud["_epid"]))
    await update.message.reply_text(t(L(ctx), "ep_ok"))
    return ConversationHandler.END


async def cmd_delprojekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx):
        await update.message.reply_text(t(L(ctx), "no_adm"))
        return ConversationHandler.END
    ps = get_projects()
    if not ps:
        await update.message.reply_text(t(L(ctx), "no_p"))
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"❌ {p['name']}", callback_data=f"dp_{p['id']}")] for p in ps]
    await update.message.reply_text(t(L(ctx), "dp_pick"), reply_markup=InlineKeyboardMarkup(kb))
    return S_DP_PICK


async def dp_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data[3:])
    p = next((x for x in get_projects() if x["id"] == pid), None)
    ctx.user_data["_dpid"], ctx.user_data["_dpn"] = pid, p["name"] if p else "?"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(L(ctx), "yes"), callback_data="dy"), InlineKeyboardButton(t(L(ctx), "no"), callback_data="dn")]]
    )
    await q.edit_message_text(t(L(ctx), "dp_ask").format(n=ctx.user_data["_dpn"]), reply_markup=kb)
    return S_DP_CONFIRM


async def dp_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "dy":
        with db() as c:
            c.execute("UPDATE projects SET active=0 WHERE id=?", (ctx.user_data["_dpid"],))
        await q.edit_message_text(t(L(ctx), "dp_ok").format(n=ctx.user_data["_dpn"]))
    else:
        await q.edit_message_text(t(L(ctx), "cancel"))
    return ConversationHandler.END


# roles
async def cmd_rolle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx):
        await update.message.reply_text(t(L(ctx), "no_adm"))
        return ConversationHandler.END
    users = get_users()
    kb = [[InlineKeyboardButton(f"👤 {u['name']}", callback_data=f"ru_{u['id']}")] for u in users]
    await update.message.reply_text(t(L(ctx), "role_pick_user"), reply_markup=InlineKeyboardMarkup(kb))
    return S_ROLE_PICK_USER


async def role_pick_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = int(q.data[3:])
    user = next((u for u in get_users() if u["id"] == uid), None)
    if not user:
        await q.edit_message_text("Mitarbeiter nicht gefunden.")
        return ConversationHandler.END
    ctx.user_data["_ruid"] = uid
    ctx.user_data["_runame"] = user["name"]
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"👤 {t(L(ctx), 'role_employee')}", callback_data="rv_employee")],
            [InlineKeyboardButton(f"🦺 {t(L(ctx), 'role_foreman')}", callback_data="rv_foreman")],
            [InlineKeyboardButton(f"👑 {t(L(ctx), 'role_admin')}", callback_data="rv_admin")],
        ]
    )
    await q.edit_message_text(t(L(ctx), "role_pick_value").format(name=user["name"]), reply_markup=kb)
    return S_ROLE_PICK_VALUE


async def role_pick_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    role = q.data[3:]
    uid = ctx.user_data["_ruid"]
    with db() as c:
        c.execute("UPDATE users SET role=?, is_admin=? WHERE id=?", (role, 1 if role == "admin" else 0, uid))
    await q.edit_message_text(t(L(ctx), "role_ok").format(name=ctx.user_data["_runame"], role=role_label(role, L(ctx))))
    return ConversationHandler.END


# delete user
async def cmd_deluser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx):
        await update.message.reply_text(t(L(ctx), "no_adm"))
        return ConversationHandler.END
    with db() as c:
        us = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM users WHERE active=1 AND tid!=? ORDER BY name", (update.effective_user.id,)
            ).fetchall()
        ]
    if not us:
        await update.message.reply_text(t(L(ctx), "du_none"))
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"👤 {u['name']}", callback_data=f"du_{u['id']}")] for u in us]
    await update.message.reply_text(t(L(ctx), "du_pick"), reply_markup=InlineKeyboardMarkup(kb))
    return S_DU_PICK


async def du_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = int(q.data[3:])
    with db() as c:
        u = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    ctx.user_data["_duid"], ctx.user_data["_dun"] = uid, u["name"]
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(L(ctx), "yes"), callback_data="duy"), InlineKeyboardButton(t(L(ctx), "no"), callback_data="dun")]]
    )
    await q.edit_message_text(t(L(ctx), "du_ask").format(n=u["name"]), reply_markup=kb)
    return S_DU_CONFIRM


async def du_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "duy":
        with db() as c:
            c.execute("UPDATE users SET active=0 WHERE id=?", (ctx.user_data["_duid"],))
        await q.edit_message_text(t(L(ctx), "du_ok").format(n=ctx.user_data["_dun"]))
    else:
        await q.edit_message_text(t(L(ctx), "cancel"))
    return ConversationHandler.END


# correction
async def cmd_korrektur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not UID(ctx):
        await update.message.reply_text("→ /start")
        return ConversationHandler.END
    current = next((u for u in get_users() if u["id"] == UID(ctx)), None)
    since = (date.today() - timedelta(days=31)).isoformat()
    entries = get_entries(date_from=since, editable_by=current, limit=20)
    if not entries:
        await update.message.reply_text(t(L(ctx), "corr_none"))
        return ConversationHandler.END
    ctx.user_data["_corr_entries"] = {e["id"]: e for e in entries}
    kb = [[InlineKeyboardButton(entry_brief(e), callback_data=f"ce_{e['id']}")] for e in entries]
    await update.message.reply_text(t(L(ctx), "corr_pick"), reply_markup=InlineKeyboardMarkup(kb))
    return S_CORR_PICK


async def corr_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    entry_id = int(q.data[3:])
    e = ctx.user_data.get("_corr_entries", {}).get(entry_id) or get_entry(entry_id)
    if not e:
        await q.edit_message_text(t(L(ctx), "corr_none"))
        return ConversationHandler.END
    ctx.user_data["_corr_id"] = entry_id
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📅 {t(L(ctx), 'field_date')}", callback_data="cf_edate")],
            [InlineKeyboardButton(f"🕐 {t(L(ctx), 'field_start')}", callback_data="cf_stime")],
            [InlineKeyboardButton(f"🕑 {t(L(ctx), 'field_end')}", callback_data="cf_etime")],
            [InlineKeyboardButton(f"☕ {t(L(ctx), 'field_break')}", callback_data="cf_brk")],
            [InlineKeyboardButton(f"📝 {t(L(ctx), 'field_notes')}", callback_data="cf_notes")],
            [InlineKeyboardButton(f"🗑️ {t(L(ctx), 'field_delete')}", callback_data="cf_delete")],
        ]
    )
    await q.edit_message_text(entry_brief(e) + "\n\n" + t(L(ctx), "corr_field"), reply_markup=kb)
    return S_CORR_FIELD


async def corr_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    field = q.data[3:]
    if field == "delete":
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(L(ctx), "yes"), callback_data="cd_yes"), InlineKeyboardButton(t(L(ctx), "no"), callback_data="cd_no")]]
        )
        await q.edit_message_text("⚠️ Eintrag wirklich löschen?", reply_markup=kb)
        return S_CORR_DELETE
    ctx.user_data["_corr_field"] = field
    hint = t(L(ctx), "corr_value")
    if field == "edate":
        hint += "\nFormat: TT.MM.JJJJ"
    elif field in {"stime", "etime"}:
        hint += "\nFormat: HH:MM"
    elif field == "brk":
        hint += "\nFormat: Minuten als Zahl"
    elif field == "notes":
        hint += "\nMit /skip leer setzen."
    await q.edit_message_text(hint)
    return S_CORR_VALUE


async def corr_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    field = ctx.user_data.get("_corr_field")
    raw = update.message.text.strip()
    value = raw

    if field == "edate":
        value = parse_date(raw)
        if not value:
            await update.message.reply_text(t(L(ctx), "bad_d"))
            return S_CORR_VALUE
    elif field in {"stime", "etime"}:
        value = parse_time(raw)
        if not value:
            await update.message.reply_text(t(L(ctx), "bad_t"))
            return S_CORR_VALUE
    elif field == "brk":
        try:
            value = int(raw)
        except ValueError:
            await update.message.reply_text(t(L(ctx), "bad_b"))
            return S_CORR_VALUE
    elif field == "notes" and raw == "/skip":
        value = ""

    try:
        update_entry(ctx.user_data["_corr_id"], field, value, UID(ctx))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ConversationHandler.END

    e = get_entry(ctx.user_data["_corr_id"])
    txt = t(L(ctx), "corr_ok") + "\n\n" + entry_brief(e)
    if e.get("updater_name"):
        txt += f"\n🧾 {t(L(ctx), 'updated_by')}: {e['updater_name']}"
    await update.message.reply_text(txt)
    return ConversationHandler.END


async def corr_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cd_yes":
        delete_entry(ctx.user_data["_corr_id"])
        await q.edit_message_text(t(L(ctx), "corr_deleted"))
    else:
        await q.edit_message_text(t(L(ctx), "cancel"))
    return ConversationHandler.END


def main():
    token = os.environ.get("ZEITFLOW_BOT_TOKEN")
    if not token:
        LOG.error("ZEITFLOW_BOT_TOKEN fehlt!")
        return

    init_db()
    app = Application.builder().token(token).build()
    fb = [CommandHandler("cancel", cancel)]

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(reg_lang, pattern=r"^rl_")],
            states={S_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)]},
            fallbacks=fb,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("zeit", cmd_zeit)],
            states={
                S_TARGET: [CallbackQueryHandler(z_target, pattern=r"^tu_(self|\d+)$")],
                S_PROJ: [CallbackQueryHandler(z_proj, pattern=r"^p_\d+$")],
                S_DATE: [
                    CallbackQueryHandler(z_date_btn, pattern=r"^dt$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, z_date_txt),
                ],
                S_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_start)],
                S_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_end)],
                S_BREAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_break)],
                S_NOTES: [MessageHandler(filters.TEXT, z_notes)],
                S_MORE: [CallbackQueryHandler(z_more, pattern=r"^m[yn]$")],
            },
            fallbacks=fb,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("korrektur", cmd_korrektur)],
            states={
                S_CORR_PICK: [CallbackQueryHandler(corr_pick, pattern=r"^ce_\d+$")],
                S_CORR_FIELD: [CallbackQueryHandler(corr_field, pattern=r"^cf_(edate|stime|etime|brk|notes|delete)$")],
                S_CORR_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, corr_value)],
                S_CORR_DELETE: [CallbackQueryHandler(corr_delete, pattern=r"^cd_(yes|no)$")],
            },
            fallbacks=fb,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("addprojekt", cmd_addprojekt)],
            states={
                S_AP1: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap1)],
                S_AP2: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap2)],
                S_AP3: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap3)],
            },
            fallbacks=fb,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("editprojekt", cmd_editprojekt)],
            states={
                S_EP_PICK: [CallbackQueryHandler(ep_pick, pattern=r"^ep_\d+$")],
                S_EP_FIELD: [CallbackQueryHandler(ep_field, pattern=r"^ef_")],
                S_EP_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ep_val)],
            },
            fallbacks=fb,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("delprojekt", cmd_delprojekt)],
            states={
                S_DP_PICK: [CallbackQueryHandler(dp_pick, pattern=r"^dp_\d+$")],
                S_DP_CONFIRM: [CallbackQueryHandler(dp_confirm, pattern=r"^d[yn]$")],
            },
            fallbacks=fb,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("rolle", cmd_rolle)],
            states={
                S_ROLE_PICK_USER: [CallbackQueryHandler(role_pick_user, pattern=r"^ru_\d+$")],
                S_ROLE_PICK_VALUE: [CallbackQueryHandler(role_pick_value, pattern=r"^rv_(employee|foreman|admin)$")],
            },
            fallbacks=fb,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("deluser", cmd_deluser)],
            states={
                S_DU_PICK: [CallbackQueryHandler(du_pick, pattern=r"^du_\d+$")],
                S_DU_CONFIRM: [CallbackQueryHandler(du_confirm, pattern=r"^du[yn]$")],
            },
            fallbacks=fb,
        )
    )

    for cmd, fn in [
        ("start", cmd_start),
        ("heute", cmd_heute),
        ("woche", cmd_woche),
        ("projekte", cmd_projekte),
        ("sprache", cmd_sprache),
        ("export", cmd_export),
        ("team", cmd_team),
        ("stats", cmd_stats),
        ("hilfe", cmd_hilfe),
        ("help", cmd_hilfe),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(sprache_cb, pattern=r"^sl_"))

    LOG.info("ZeitFlow Bot gestartet! DB: %s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
