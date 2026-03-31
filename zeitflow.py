#!/usr/bin/env python3
"""
ZeitFlow – All-in-One Telegram Bot für Zeiterfassung
=====================================================
Ein einziger Prozess: Bot + Datenbank + Export.
Kein separater Server nötig. Deployt auf Railway/Render/Fly.io.

    pip install python-telegram-bot openpyxl
    export ZEITFLOW_BOT_TOKEN="..."
    python zeitflow.py
"""

import os, io, csv, sqlite3, secrets, logging
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict
from contextlib import contextmanager

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Border, Side
    XLSX = True
except ImportError:
    XLSX = False

LOG = logging.getLogger("zeitflow")
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

# ─── Datenbank (SQLite, persistente Datei) ───
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


def init_db():
    with db() as c:
        c.executescript("""
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
        """)
        if c.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
            c.executemany("INSERT INTO projects (name,customer,cost_center) VALUES (?,?,?)", [
                ("Neubau Halle B", "Schneider GmbH", "KST-4010"),
                ("Sanierung Dach", "Gemeinde Freiburg", "KST-4020"),
                ("Montage Lüftung", "Klinik Süd", "KST-4030"),
                ("Heizung Austausch", "Familie Braun", "KST-4040"),
            ])
            LOG.info("Demo-Projekte angelegt")


# ─── Hilfsfunktionen ───

def get_or_create_user(tid: int, name: str, lang: str = "de") -> dict:
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE tid=?", (tid,)).fetchone()
        if u:
            return dict(u)
        has_admin = c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
        c.execute("INSERT INTO users (tid,name,lang,is_admin) VALUES (?,?,?,?)",
                  (tid, name, lang, 0 if has_admin else 1))
        return dict(c.execute("SELECT * FROM users WHERE tid=?", (tid,)).fetchone())


def get_user(tid: int) -> dict | None:
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE tid=? AND active=1", (tid,)).fetchone()
        return dict(u) if u else None


def get_projects() -> list:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM projects WHERE active=1 ORDER BY name").fetchall()]


def save_entry(user_id, project_id, edate, stime, etime, brk, notes) -> float:
    sh, sm = map(int, stime.split(":"))
    eh, em = map(int, etime.split(":"))
    hours = round(max(0, (eh*60+em)-(sh*60+sm)-brk) / 60, 2)
    with db() as c:
        c.execute("INSERT INTO entries (user_id,project_id,edate,stime,etime,brk,hours,notes) VALUES (?,?,?,?,?,?,?,?)",
                  (user_id, project_id, edate, stime, etime, brk, hours, notes))
    return hours


def get_entries(user_id=None, date_from=None, date_to=None) -> list:
    conds, params = ["1=1"], []
    if user_id: conds.append("e.user_id=?"); params.append(user_id)
    if date_from: conds.append("e.edate>=?"); params.append(date_from)
    if date_to: conds.append("e.edate<=?"); params.append(date_to)
    with db() as c:
        return [dict(r) for r in c.execute(f"""
            SELECT e.*, u.name AS employee, p.name AS project, p.customer, p.cost_center
            FROM entries e JOIN users u ON e.user_id=u.id JOIN projects p ON e.project_id=p.id
            WHERE {' AND '.join(conds)} ORDER BY e.edate DESC, u.name, e.stime
        """, params).fetchall()]


def parse_time(s):
    s = s.strip().replace(".", ":")
    for f in ("%H:%M", "%H"):
        try: d = datetime.strptime(s, f); return f"{d.hour:02d}:{d.minute:02d}"
        except ValueError: pass
    return None


def parse_date(s):
    s = s.strip().lower()
    if s in ("heute", "today", "сегодня", "dzisiaj", "šodien"): return date.today().isoformat()
    for f in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try: return datetime.strptime(s, f).date().isoformat()
        except ValueError: pass
    return None


def fde(d):
    try: return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
    except: return d


# ─── Übersetzungen ───
T = {
  "de": {"flag":"🇩🇪","label":"Deutsch",
    "welcome":"👋 ZeitFlow\n\n/zeit – Erfassen\n/heute – Heute\n/woche – Woche\n/projekte – Projekte\n/sprache – Sprache\n/hilfe – Hilfe",
    "admin":"\n\n👑 Chef:\n/export – Excel\n/team – Mitarbeiter\n/stats – Dashboard\n/addprojekt – Neues Projekt\n/editprojekt – Bearbeiten\n/delprojekt – Deaktivieren\n/makeadmin – Rechte\n/deluser – Entfernen",
    "ask_name":"Vor- und Nachname?","name_ok":"Hallo {name} 👋",
    "pick_proj":"Welches Projekt?","pick_date":"📅 Datum? (TT.MM.JJJJ / heute)",
    "ask_s":"🕐 Beginn?","ask_e":"🕑 Ende?","ask_b":"☕ Pause (Min)?","ask_n":"📝 Bemerkung? (/skip)",
    "saved":"✅ Gespeichert","more":"Noch ein Eintrag?","y":"✅ Ja","n":"❌ Fertig",
    "sum":"📋 {p}\n🏢 {c}\n📅 {d}\n🕐 {s}–{e}\n☕ {b}min\n⏱ {h} Std.",
    "no_e":"Keine Einträge.","tot":"Gesamt","hr":"Std.","bad_t":"❌ Format: 7:00","bad_d":"❌ Format: TT.MM.JJJJ",
    "no_adm":"⚠️ Nur Chef.","cancel":"Abgebrochen.","no_p":"Keine Projekte.","today":"heute",
    "ap1":"📋 Projektname?","ap2":"🏢 Kunde?","ap3":"🏷 Kostenstelle?",
    "ap_ok":"✅ Projekt:\n📋 {n}\n🏢 {c}\n🏷 {k}",
    "ep_pick":"Bearbeiten?","ep_what":"Was ändern?","ep_val":"Neuer Wert?","ep_ok":"✅ Aktualisiert.",
    "dp_pick":"Deaktivieren?","dp_ask":"⚠️ '{n}' deaktivieren?","dp_ok":"✅ '{n}' deaktiviert.",
    "team_h":"👥 Team:\n","team_r":"{i} {n} – {h} Std. ({c} Eintr.) {l}\n",
    "ma_pick":"Admin-Rechte vergeben?","ma_ok":"✅ {n} ist Admin.","ma_no":"Alle sind Admin.",
    "du_pick":"Entfernen?","du_ask":"⚠️ {n} entfernen?","du_ok":"✅ {n} entfernt.","du_none":"Keine.",
    "yes":"✅ Ja","no":"❌ Nein","lang_ok":"✅ Deutsch",
    "export_ok":"📊 {c} Einträge","export_no":"Keine Daten."},
  "ru": {"flag":"🇷🇺","label":"Русский",
    "welcome":"👋 ZeitFlow\n\n/zeit – Время\n/heute – Сегодня\n/woche – Неделя\n/projekte – Проекты\n/sprache – Язык\n/hilfe – Помощь",
    "admin":"\n\n👑\n/export /team /stats\n/addprojekt /editprojekt /delprojekt\n/makeadmin /deluser",
    "ask_name":"Имя и фамилия?","name_ok":"Привет, {name} 👋",
    "pick_proj":"Проект?","pick_date":"📅 Дата? (ДД.ММ.ГГГГ / сегодня)",
    "ask_s":"🕐 Начало?","ask_e":"🕑 Конец?","ask_b":"☕ Перерыв?","ask_n":"📝 Заметка? (/skip)",
    "saved":"✅","more":"Ещё?","y":"✅ Да","n":"❌ Нет",
    "sum":"📋 {p}\n🏢 {c}\n📅 {d}\n🕐 {s}–{e}\n☕ {b}мин\n⏱ {h}ч.",
    "no_e":"Нет.","tot":"Итого","hr":"ч.","bad_t":"❌","bad_d":"❌",
    "no_adm":"⚠️","cancel":"Отмена.","no_p":"Нет.","today":"сегодня",
    "ap1":"📋?","ap2":"🏢?","ap3":"🏷?","ap_ok":"✅ {n}/{c}/{k}",
    "ep_pick":"?","ep_what":"?","ep_val":"?","ep_ok":"✅",
    "dp_pick":"?","dp_ask":"⚠️ '{n}'?","dp_ok":"✅ '{n}'",
    "team_h":"👥\n","team_r":"{i} {n} – {h}ч. ({c}) {l}\n",
    "ma_pick":"?","ma_ok":"✅ {n}","ma_no":"—",
    "du_pick":"?","du_ask":"⚠️ {n}?","du_ok":"✅ {n}","du_none":"—",
    "yes":"✅","no":"❌","lang_ok":"✅ Русский",
    "export_ok":"📊 {c}","export_no":"—"},
  "pl": {"flag":"🇵🇱","label":"Polski",
    "welcome":"👋 ZeitFlow\n\n/zeit – Czas\n/heute – Dziś\n/woche – Tydzień\n/projekte – Projekty\n/sprache – Język\n/hilfe – Pomoc",
    "admin":"\n\n👑\n/export /team /stats\n/addprojekt /editprojekt /delprojekt\n/makeadmin /deluser",
    "ask_name":"Imię i nazwisko?","name_ok":"Cześć, {name} 👋",
    "pick_proj":"Projekt?","pick_date":"📅 Data? (DD.MM.RRRR / dzisiaj)",
    "ask_s":"🕐 Start?","ask_e":"🕑 Koniec?","ask_b":"☕ Przerwa?","ask_n":"📝 Uwaga? (/skip)",
    "saved":"✅","more":"Kolejny?","y":"✅ Tak","n":"❌ Nie",
    "sum":"📋 {p}\n🏢 {c}\n📅 {d}\n🕐 {s}–{e}\n☕ {b}min\n⏱ {h}g.",
    "no_e":"Brak.","tot":"Łącznie","hr":"g.","bad_t":"❌","bad_d":"❌",
    "no_adm":"⚠️","cancel":"Anulowano.","no_p":"Brak.","today":"dzisiaj",
    "ap1":"📋?","ap2":"🏢?","ap3":"🏷?","ap_ok":"✅ {n}/{c}/{k}",
    "ep_pick":"?","ep_what":"?","ep_val":"?","ep_ok":"✅",
    "dp_pick":"?","dp_ask":"⚠️ '{n}'?","dp_ok":"✅ '{n}'",
    "team_h":"👥\n","team_r":"{i} {n} – {h}g. ({c}) {l}\n",
    "ma_pick":"?","ma_ok":"✅ {n}","ma_no":"—",
    "du_pick":"?","du_ask":"⚠️ {n}?","du_ok":"✅ {n}","du_none":"—",
    "yes":"✅","no":"❌","lang_ok":"✅ Polski",
    "export_ok":"📊 {c}","export_no":"—"},
  "lv": {"flag":"🇱🇻","label":"Latviešu",
    "welcome":"👋 ZeitFlow\n\n/zeit – Laiks\n/heute – Šodien\n/woche – Nedēļa\n/projekte – Projekti\n/sprache – Valoda\n/hilfe – Palīdzība",
    "admin":"\n\n👑\n/export /team /stats\n/addprojekt /editprojekt /delprojekt\n/makeadmin /deluser",
    "ask_name":"Vārds?","name_ok":"Sveiks, {name} 👋",
    "pick_proj":"Projekts?","pick_date":"📅 Datums? (DD.MM.GGGG / šodien)",
    "ask_s":"🕐 Sākums?","ask_e":"🕑 Beigas?","ask_b":"☕ Pārtr.?","ask_n":"📝 Piezīme? (/skip)",
    "saved":"✅","more":"Vēl?","y":"✅ Jā","n":"❌ Nē",
    "sum":"📋 {p}\n🏢 {c}\n📅 {d}\n🕐 {s}–{e}\n☕ {b}min\n⏱ {h}st.",
    "no_e":"Nav.","tot":"Kopā","hr":"st.","bad_t":"❌","bad_d":"❌",
    "no_adm":"⚠️","cancel":"Atcelts.","no_p":"Nav.","today":"šodien",
    "ap1":"📋?","ap2":"🏢?","ap3":"🏷?","ap_ok":"✅ {n}/{c}/{k}",
    "ep_pick":"?","ep_what":"?","ep_val":"?","ep_ok":"✅",
    "dp_pick":"?","dp_ask":"⚠️ '{n}'?","dp_ok":"✅ '{n}'",
    "team_h":"👥\n","team_r":"{i} {n} – {h}st. ({c}) {l}\n",
    "ma_pick":"?","ma_ok":"✅ {n}","ma_no":"—",
    "du_pick":"?","du_ask":"⚠️ {n}?","du_ok":"✅ {n}","du_none":"—",
    "yes":"✅","no":"❌","lang_ok":"✅ Latviešu",
    "export_ok":"📊 {c}","export_no":"—"},
}

def t(l, k): return T.get(l, T["de"]).get(k, T["de"].get(k, k))

# ─── States ───
S_NAME = 0
S_PROJ, S_DATE, S_START, S_END, S_BREAK, S_NOTES, S_MORE = range(1, 8)
S_AP1, S_AP2, S_AP3 = range(8, 11)
S_EP_PICK, S_EP_FIELD, S_EP_VAL = range(11, 14)
S_DP_PICK, S_DP_CONFIRM = range(14, 16)
S_MA_PICK = 16
S_DU_PICK, S_DU_CONFIRM = range(17, 19)

# ─── Shortcuts ───
def L(ctx): return ctx.user_data.get("lang", "de")
def UID(ctx): return ctx.user_data.get("uid")
def ADM(ctx): return ctx.user_data.get("adm", False)

def sync_user(ctx, u):
    ctx.user_data.update({"uid": u["id"], "lang": u["lang"], "adm": bool(u["is_admin"]), "tid": u["tid"]})


# ═══════════════════════════════════════════════════════════
#  Registrierung
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if u:
        sync_user(ctx, u)
        msg = t(u["lang"], "welcome")
        if u["is_admin"]: msg += t(u["lang"], "admin")
        await update.message.reply_text(msg)
    else:
        kb = [[InlineKeyboardButton(f"{v['flag']} {v['label']}", callback_data=f"rl_{k}")]
              for k, v in T.items()]
        await update.message.reply_text("🌍 Sprache?", reply_markup=InlineKeyboardMarkup(kb))

async def reg_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["reg_lang"] = q.data[3:]
    await q.edit_message_text(t(ctx.user_data["reg_lang"], "ask_name"))
    return S_NAME

async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name, tid = update.message.text.strip(), update.effective_user.id
    lang = ctx.user_data.get("reg_lang", "de")
    u = get_or_create_user(tid, name, lang)
    sync_user(ctx, u)
    hint = "\n\n👑 Du bist Chef!" if u["is_admin"] else ""
    await update.message.reply_text(t(lang, "name_ok").format(name=name) + hint)
    msg = t(lang, "welcome")
    if u["is_admin"]: msg += t(lang, "admin")
    await update.message.reply_text(msg)
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t(L(ctx), "cancel"), reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
#  Zeiterfassung
# ═══════════════════════════════════════════════════════════

async def cmd_zeit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not UID(ctx): await update.message.reply_text("→ /start"); return ConversationHandler.END
    ps = get_projects()
    if not ps: await update.message.reply_text(t(L(ctx), "no_p")); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{p['name']} – {p['customer']}", callback_data=f"p_{p['id']}")]
          for p in ps]
    await update.message.reply_text(t(L(ctx), "pick_proj"), reply_markup=InlineKeyboardMarkup(kb))
    return S_PROJ

async def z_proj(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pid = int(q.data[2:])
    ps = get_projects()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p: return ConversationHandler.END
    ctx.user_data.update({"pid": pid, "pn": p["name"], "pc": p["customer"], "pk": p["cost_center"]})
    kb = [[InlineKeyboardButton(f"📅 {t(L(ctx),'today').capitalize()}", callback_data="dt")]]
    await q.edit_message_text(t(L(ctx), "pick_date"), reply_markup=InlineKeyboardMarkup(kb))
    return S_DATE

async def z_date_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["ed"] = date.today().isoformat()
    await q.edit_message_text(t(L(ctx), "ask_s"))
    return S_START

async def z_date_txt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d: await update.message.reply_text(t(L(ctx), "bad_d")); return S_DATE
    ctx.user_data["ed"] = d
    await update.message.reply_text(t(L(ctx), "ask_s"))
    return S_START

async def z_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tm = parse_time(update.message.text)
    if not tm: await update.message.reply_text(t(L(ctx), "bad_t")); return S_START
    ctx.user_data["st"] = tm
    await update.message.reply_text(t(L(ctx), "ask_e"))
    return S_END

async def z_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tm = parse_time(update.message.text)
    if not tm: await update.message.reply_text(t(L(ctx), "bad_t")); return S_END
    ctx.user_data["et"] = tm
    kb = ReplyKeyboardMarkup([["0","15","30","45","60"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(t(L(ctx), "ask_b"), reply_markup=kb)
    return S_BREAK

async def z_break(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["bk"] = int(update.message.text.strip())
    except: ctx.user_data["bk"] = 30
    await update.message.reply_text(t(L(ctx), "ask_n"), reply_markup=ReplyKeyboardRemove())
    return S_NOTES

async def z_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud, l = ctx.user_data, L(ctx)
    notes = "" if update.message.text.strip() == "/skip" else update.message.text.strip()
    hours = save_entry(ud["uid"], ud["pid"], ud["ed"], ud["st"], ud["et"], ud["bk"], notes)
    txt = t(l, "saved") + "\n\n" + t(l, "sum").format(
        p=ud["pn"], c=ud["pc"], d=fde(ud["ed"]), s=ud["st"], e=ud["et"], b=ud["bk"], h=f"{hours:.1f}")
    if notes: txt += f"\n📝 {notes}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t(l, "y"), callback_data="my"),
        InlineKeyboardButton(t(l, "n"), callback_data="mn"),
    ]])
    await update.message.reply_text(txt + "\n\n" + t(l, "more"), reply_markup=kb)
    return S_MORE

async def z_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "my":
        ps = get_projects()
        kb = [[InlineKeyboardButton(f"{p['name']} – {p['customer']}", callback_data=f"p_{p['id']}")]
              for p in ps]
        await q.edit_message_text(t(L(ctx), "pick_proj"), reply_markup=InlineKeyboardMarkup(kb))
        return S_PROJ
    await q.edit_message_text("👍")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
#  Anzeige
# ═══════════════════════════════════════════════════════════

async def cmd_heute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not UID(ctx): await update.message.reply_text("→ /start"); return
    l, today = L(ctx), date.today().isoformat()
    es = get_entries(user_id=UID(ctx), date_from=today, date_to=today)
    if not es: await update.message.reply_text(t(l, "no_e")); return
    txt, tot = f"📊 {fde(today)}\n", 0.0
    for i, e in enumerate(es, 1):
        txt += f"\n{i}. {e['project']} ({e['customer']})\n   🏷 {e['cost_center']}\n   {e['stime']}–{e['etime']} ☕{e['brk']}min ⏱{e['hours']:.1f}{t(l,'hr')}"
        if e["notes"]: txt += f"\n   📝 {e['notes']}"
        tot += e["hours"]
    txt += f"\n\n━━━━━━━━━━\n{t(l,'tot')}: {tot:.1f} {t(l,'hr')}"
    await update.message.reply_text(txt)

async def cmd_woche(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not UID(ctx): await update.message.reply_text("→ /start"); return
    l = L(ctx)
    ws = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    es = get_entries(user_id=UID(ctx), date_from=ws)
    if not es: await update.message.reply_text(t(l, "no_e")); return
    txt, cur, tot = "📊\n", "", 0.0
    for e in es:
        if e["edate"] != cur: cur = e["edate"]; txt += f"\n📅 {fde(cur)}\n"
        txt += f"  • {e['project']} | {e['stime']}–{e['etime']} | {e['hours']:.1f}{t(l,'hr')}\n"
        tot += e["hours"]
    txt += f"\n━━━━━━━━━━\n{t(l,'tot')}: {tot:.1f} {t(l,'hr')}"
    await update.message.reply_text(txt)

async def cmd_projekte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    l, ps = L(ctx), get_projects()
    if not ps: await update.message.reply_text(t(l, "no_p")); return
    txt = "📋\n"
    for p in ps: txt += f"\n📋 {p['name']}\n   🏢 {p['customer']}\n   🏷 {p['cost_center']}\n"
    await update.message.reply_text(txt)

async def cmd_sprache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['flag']} {v['label']}", callback_data=f"sl_{k}")] for k, v in T.items()]
    await update.message.reply_text("🌍", reply_markup=InlineKeyboardMarkup(kb))

async def sprache_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    l = q.data[3:]
    ctx.user_data["lang"] = l
    u = get_user(q.from_user.id)
    if u:
        with db() as c: c.execute("UPDATE users SET lang=? WHERE id=?", (l, u["id"]))
    await q.edit_message_text(t(l, "lang_ok"))

async def cmd_hilfe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    l = L(ctx)
    msg = t(l, "welcome")
    if ADM(ctx): msg += t(l, "admin")
    await update.message.reply_text(msg)


# ═══════════════════════════════════════════════════════════
#  Admin: Team, Stats, Export
# ═══════════════════════════════════════════════════════════

async def cmd_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    l = L(ctx)
    if not ADM(ctx): await update.message.reply_text(t(l, "no_adm")); return
    with db() as c:
        users = [dict(r) for r in c.execute("SELECT * FROM users WHERE active=1 ORDER BY name").fetchall()]
        txt = t(l, "team_h")
        for u in users:
            s = c.execute("SELECT COUNT(*) as c, COALESCE(SUM(hours),0) as h FROM entries WHERE user_id=?", (u["id"],)).fetchone()
            txt += t(l, "team_r").format(i="👑" if u["is_admin"] else "👤", n=u["name"],
                h=f"{s['h']:.1f}", c=s["c"], l=T.get(u["lang"], T["de"])["flag"])
    await update.message.reply_text(txt)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    l = L(ctx)
    if not ADM(ctx): await update.message.reply_text(t(l, "no_adm")); return
    today = date.today().isoformat()
    ws = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    ms = date.today().replace(day=1).isoformat()
    with db() as c:
        td = c.execute("SELECT COALESCE(SUM(hours),0) as h, COUNT(*) as c FROM entries WHERE edate=?", (today,)).fetchone()
        wk = c.execute("SELECT COALESCE(SUM(hours),0) as h, COUNT(*) as c FROM entries WHERE edate>=?", (ws,)).fetchone()
        mo = c.execute("SELECT COALESCE(SUM(hours),0) as h, COUNT(*) as c FROM entries WHERE edate>=?", (ms,)).fetchone()
        bp = [dict(r) for r in c.execute("""
            SELECT p.name, COALESCE(SUM(e.hours),0) as h, COUNT(e.id) as c
            FROM entries e JOIN projects p ON e.project_id=p.id WHERE e.edate>=?
            GROUP BY p.id ORDER BY h DESC LIMIT 5""", (ms,)).fetchall()]
    txt = (f"📊 Dashboard\n\nHeute: {td['h']:.1f} Std. ({td['c']})\n"
           f"Woche: {wk['h']:.1f} Std. ({wk['c']})\nMonat: {mo['h']:.1f} Std. ({mo['c']})")
    if bp:
        txt += "\n\n📋 Projekte (Monat):"
        for p in bp: txt += f"\n  • {p['name']}: {p['h']:.1f} Std."
    await update.message.reply_text(txt)

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    l = L(ctx)
    if not ADM(ctx): await update.message.reply_text(t(l, "no_adm")); return
    es = get_entries()
    if not es: await update.message.reply_text(t(l, "export_no")); return

    if XLSX:
        wb = Workbook()
        hf, hfi = Font(bold=True, color="FFFFFF", size=11), PatternFill(start_color="1e3a5f", end_color="1e3a5f", fill_type="solid")
        bd = Border(*(Side(style="thin"),)*4)
        ws1 = wb.active; ws1.title = "Detail"
        for c, h in enumerate(["Datum","Mitarbeiter","Projekt","Kunde","KST","Beginn","Ende","Pause","Std.","Bemerkung"], 1):
            cl = ws1.cell(row=1, column=c, value=h); cl.font, cl.fill, cl.border = hf, hfi, bd
        for r, e in enumerate(es, 2):
            for c, v in enumerate([fde(e["edate"]),e["employee"],e["project"],e["customer"],e["cost_center"],
                                   e["stime"],e["etime"],e["brk"],e["hours"],e["notes"] or ""], 1):
                cl = ws1.cell(row=r, column=c, value=v); cl.border = bd
                if c == 9: cl.number_format = "0.0"
        for i, w in enumerate([12,20,25,22,14,8,8,10,10,30], 1): ws1.column_dimensions[chr(64+i)].width = w

        ws2 = wb.create_sheet("MeinBüro Import")
        bf = PatternFill(start_color="2563eb", end_color="2563eb", fill_type="solid")
        for c, h in enumerate(["BestellnummerShop","Bestelldatum","Kundennummer","Firmenname",
                                "Artikelnummer","Menge","abweichenderEinzelpreisNetto","abweichenderArtikeltext"], 1):
            cl = ws2.cell(row=1, column=c, value=h); cl.font = Font(bold=True, color="FFFFFF", size=10); cl.fill = bf
        ords = defaultdict(list)
        for e in es: ords[(e["customer"], e["edate"])].append(e)
        r2, on = 2, 1
        for (cu, ed), items in sorted(ords.items(), key=lambda x: x[0][1]):
            oid = f"ZF-{ed.replace('-','')}-{on:03d}"
            for it in items:
                ws2.cell(row=r2,column=1,value=oid); ws2.cell(row=r2,column=2,value=fde(ed))
                ws2.cell(row=r2,column=3,value=""); ws2.cell(row=r2,column=4,value=cu)
                ws2.cell(row=r2,column=5,value="MONTAGE-H"); ws2.cell(row=r2,column=6,value=it["hours"])
                ws2.cell(row=r2,column=7,value="")
                d = f"{it['employee']} – {it['project']}"
                if it["notes"]: d += f" ({it['notes']})"
                ws2.cell(row=r2,column=8,value=d); r2 += 1
            on += 1

        ws3 = wb.create_sheet("Pro Mitarbeiter")
        gf = PatternFill(start_color="059669", end_color="059669", fill_type="solid")
        for c, h in enumerate(["Mitarbeiter","Einträge","Stunden","Projekte"], 1):
            cl = ws3.cell(row=1, column=c, value=h); cl.font = Font(bold=True, color="FFFFFF", size=10); cl.fill = gf
        emp = defaultdict(lambda: {"c":0,"h":0.0,"p":set()})
        for e in es: emp[e["employee"]]["c"]+=1; emp[e["employee"]]["h"]+=e["hours"]; emp[e["employee"]]["p"].add(e["project"])
        for r, (nm, s) in enumerate(sorted(emp.items()), 2):
            ws3.cell(row=r,column=1,value=nm); ws3.cell(row=r,column=2,value=s["c"])
            ws3.cell(row=r,column=3,value=round(s["h"],1)); ws3.cell(row=r,column=4,value=", ".join(sorted(s["p"])))

        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        await update.message.reply_document(document=buf, filename=f"zeitflow_{date.today().isoformat()}.xlsx",
                                             caption=t(l, "export_ok").format(c=len(es)))
    else:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Datum","Mitarbeiter","Projekt","Kunde","KST","Beginn","Ende","Pause","Std.","Bemerkung"])
        for e in es: w.writerow([fde(e["edate"]),e["employee"],e["project"],e["customer"],e["cost_center"],
                                 e["stime"],e["etime"],e["brk"],f"{e['hours']:.1f}",e["notes"] or ""])
        cb = io.BytesIO(("\ufeff"+buf.getvalue()).encode("utf-8"))
        await update.message.reply_document(document=cb, filename=f"zeitflow_{date.today().isoformat()}.csv",
                                             caption=t(l, "export_ok").format(c=len(es)))


# ═══════════════════════════════════════════════════════════
#  Admin: Projekte verwalten
# ═══════════════════════════════════════════════════════════

async def cmd_addprojekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx): await update.message.reply_text(t(L(ctx), "no_adm")); return ConversationHandler.END
    await update.message.reply_text(t(L(ctx), "ap1")); return S_AP1

async def ap1(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["_apn"] = update.message.text.strip()
    await update.message.reply_text(t(L(ctx), "ap2")); return S_AP2

async def ap2(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["_apc"] = update.message.text.strip()
    await update.message.reply_text(t(L(ctx), "ap3")); return S_AP3

async def ap3(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud, l = ctx.user_data, L(ctx)
    k = update.message.text.strip()
    with db() as c: c.execute("INSERT INTO projects (name,customer,cost_center) VALUES (?,?,?)", (ud["_apn"], ud["_apc"], k))
    await update.message.reply_text(t(l, "ap_ok").format(n=ud["_apn"], c=ud["_apc"], k=k))
    return ConversationHandler.END

async def cmd_editprojekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx): await update.message.reply_text(t(L(ctx), "no_adm")); return ConversationHandler.END
    ps = get_projects()
    if not ps: await update.message.reply_text(t(L(ctx), "no_p")); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{p['name']} – {p['customer']}", callback_data=f"ep_{p['id']}")] for p in ps]
    await update.message.reply_text(t(L(ctx), "ep_pick"), reply_markup=InlineKeyboardMarkup(kb)); return S_EP_PICK

async def ep_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["_epid"] = int(q.data[3:])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Name", callback_data="ef_name")],
          [InlineKeyboardButton("🏢 Kunde", callback_data="ef_customer")],
          [InlineKeyboardButton("🏷 Kostenstelle", callback_data="ef_cost_center")]])
    await q.edit_message_text(t(L(ctx), "ep_what"), reply_markup=kb); return S_EP_FIELD

async def ep_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["_epf"] = q.data[3:]
    await q.edit_message_text(t(L(ctx), "ep_val")); return S_EP_VAL

async def ep_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    with db() as c: c.execute(f"UPDATE projects SET {ud['_epf']}=? WHERE id=?", (update.message.text.strip(), ud["_epid"]))
    await update.message.reply_text(t(L(ctx), "ep_ok")); return ConversationHandler.END

async def cmd_delprojekt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx): await update.message.reply_text(t(L(ctx), "no_adm")); return ConversationHandler.END
    ps = get_projects()
    if not ps: await update.message.reply_text(t(L(ctx), "no_p")); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"❌ {p['name']}", callback_data=f"dp_{p['id']}")] for p in ps]
    await update.message.reply_text(t(L(ctx), "dp_pick"), reply_markup=InlineKeyboardMarkup(kb)); return S_DP_PICK

async def dp_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    pid = int(q.data[3:])
    ps = get_projects(); p = next((x for x in ps if x["id"] == pid), None)
    ctx.user_data["_dpid"], ctx.user_data["_dpn"] = pid, p["name"] if p else "?"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(L(ctx),"yes"), callback_data="dy"),
                                 InlineKeyboardButton(t(L(ctx),"no"), callback_data="dn")]])
    await q.edit_message_text(t(L(ctx), "dp_ask").format(n=ctx.user_data["_dpn"]), reply_markup=kb); return S_DP_CONFIRM

async def dp_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "dy":
        with db() as c: c.execute("UPDATE projects SET active=0 WHERE id=?", (ctx.user_data["_dpid"],))
        await q.edit_message_text(t(L(ctx), "dp_ok").format(n=ctx.user_data["_dpn"]))
    else: await q.edit_message_text(t(L(ctx), "cancel"))
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
#  Admin: Team verwalten
# ═══════════════════════════════════════════════════════════

async def cmd_makeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx): await update.message.reply_text(t(L(ctx), "no_adm")); return ConversationHandler.END
    with db() as c:
        us = [dict(r) for r in c.execute("SELECT * FROM users WHERE active=1 AND is_admin=0 ORDER BY name").fetchall()]
    if not us: await update.message.reply_text(t(L(ctx), "ma_no")); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"👤 {u['name']}", callback_data=f"ma_{u['id']}")] for u in us]
    await update.message.reply_text(t(L(ctx), "ma_pick"), reply_markup=InlineKeyboardMarkup(kb)); return S_MA_PICK

async def ma_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = int(q.data[3:])
    with db() as c:
        c.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,))
        u = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    await q.edit_message_text(t(L(ctx), "ma_ok").format(n=u["name"])); return ConversationHandler.END

async def cmd_deluser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADM(ctx): await update.message.reply_text(t(L(ctx), "no_adm")); return ConversationHandler.END
    with db() as c:
        us = [dict(r) for r in c.execute("SELECT * FROM users WHERE active=1 AND tid!=? ORDER BY name",
              (update.effective_user.id,)).fetchall()]
    if not us: await update.message.reply_text(t(L(ctx), "du_none")); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{'👑' if u['is_admin'] else '👤'} {u['name']}", callback_data=f"du_{u['id']}")]
          for u in us]
    await update.message.reply_text(t(L(ctx), "du_pick"), reply_markup=InlineKeyboardMarkup(kb)); return S_DU_PICK

async def du_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = int(q.data[3:])
    with db() as c: u = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    ctx.user_data["_duid"], ctx.user_data["_dun"] = uid, u["name"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(L(ctx),"yes"), callback_data="duy"),
                                 InlineKeyboardButton(t(L(ctx),"no"), callback_data="dun")]])
    await q.edit_message_text(t(L(ctx), "du_ask").format(n=u["name"]), reply_markup=kb); return S_DU_CONFIRM

async def du_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "duy":
        with db() as c: c.execute("UPDATE users SET active=0 WHERE id=?", (ctx.user_data["_duid"],))
        await q.edit_message_text(t(L(ctx), "du_ok").format(n=ctx.user_data["_dun"]))
    else: await q.edit_message_text(t(L(ctx), "cancel"))
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    token = os.environ.get("ZEITFLOW_BOT_TOKEN")
    if not token: LOG.error("ZEITFLOW_BOT_TOKEN fehlt!"); return
    init_db()
    app = Application.builder().token(token).build()
    fb = [CommandHandler("cancel", cancel)]

    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(reg_lang, pattern=r"^rl_")],
        states={S_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)]}, fallbacks=fb))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("zeit", cmd_zeit)],
        states={S_PROJ: [CallbackQueryHandler(z_proj, pattern=r"^p_\d+$")],
                S_DATE: [CallbackQueryHandler(z_date_btn, pattern=r"^dt$"), MessageHandler(filters.TEXT & ~filters.COMMAND, z_date_txt)],
                S_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_start)],
                S_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_end)],
                S_BREAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_break)],
                S_NOTES: [MessageHandler(filters.TEXT, z_notes)],
                S_MORE: [CallbackQueryHandler(z_more, pattern=r"^m[yn]$")]}, fallbacks=fb))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("addprojekt", cmd_addprojekt)],
        states={S_AP1: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap1)],
                S_AP2: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap2)],
                S_AP3: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap3)]}, fallbacks=fb))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("editprojekt", cmd_editprojekt)],
        states={S_EP_PICK: [CallbackQueryHandler(ep_pick, pattern=r"^ep_\d+$")],
                S_EP_FIELD: [CallbackQueryHandler(ep_field, pattern=r"^ef_")],
                S_EP_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ep_val)]}, fallbacks=fb))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("delprojekt", cmd_delprojekt)],
        states={S_DP_PICK: [CallbackQueryHandler(dp_pick, pattern=r"^dp_\d+$")],
                S_DP_CONFIRM: [CallbackQueryHandler(dp_confirm, pattern=r"^d[yn]$")]}, fallbacks=fb))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("makeadmin", cmd_makeadmin)],
        states={S_MA_PICK: [CallbackQueryHandler(ma_pick, pattern=r"^ma_\d+$")]}, fallbacks=fb))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("deluser", cmd_deluser)],
        states={S_DU_PICK: [CallbackQueryHandler(du_pick, pattern=r"^du_\d+$")],
                S_DU_CONFIRM: [CallbackQueryHandler(du_confirm, pattern=r"^du[yn]$")]}, fallbacks=fb))

    for cmd, fn in [("start",cmd_start),("heute",cmd_heute),("woche",cmd_woche),("projekte",cmd_projekte),
                    ("sprache",cmd_sprache),("export",cmd_export),("team",cmd_team),("stats",cmd_stats),
                    ("hilfe",cmd_hilfe),("help",cmd_hilfe)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(sprache_cb, pattern=r"^sl_"))

    LOG.info(f"ZeitFlow Bot gestartet! DB: {DB_PATH}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
