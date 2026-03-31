# ZeitFlow Bot – Deployment ohne eigenen Server

## Übersicht

```
┌──────────────┐      ┌──────────────────────┐
│  Mitarbeiter │      │  Railway / Render     │
│  Telegram    │ ───→ │  ┌────────────────┐  │
│  🇩🇪 🇷🇺 🇵🇱 🇱🇻  │      │  │  zeitflow.py   │  │
└──────────────┘      │  │  + SQLite DB    │  │
                      │  └────────────────┘  │
┌──────────────┐      │                      │
│    Chef      │      │  Kosten: 0-5 €/Monat │
│  Telegram    │ ───→ │  Keine Wartung nötig  │
│  /export     │      │                      │
└──────────────┘      └──────────────────────┘
```

**Eine Datei. Ein Prozess. Kein Server nötig.**

---

## Option A: Railway (empfohlen)

### Schritt 1: Telegram-Bot erstellen (2 Minuten)

1. Öffne Telegram → suche **@BotFather**
2. Schreibe `/newbot`
3. Name eingeben: `ZeitFlow Musterfirma`
4. Username: `zeitflow_musterfirma_bot`
5. **Token kopieren** (sieht so aus: `7123456789:AAF...`)

### Schritt 2: GitHub-Repo erstellen (3 Minuten)

1. Gehe zu **github.com** → "New repository"
2. Name: `zeitflow-bot`
3. Lade diese Dateien hoch:
   - `zeitflow.py`
   - `requirements.txt`
   - `Dockerfile`
   - `railway.json`

### Schritt 3: Auf Railway deployen (5 Minuten)

1. Gehe zu **railway.app** → "Start a New Project"
2. Wähle "Deploy from GitHub repo"
3. Verbinde dein GitHub-Konto
4. Wähle das `zeitflow-bot` Repo
5. Railway erkennt das Dockerfile automatisch

### Schritt 4: Bot-Token eintragen

1. In Railway: Klicke auf deinen Service
2. Gehe zu **Variables**
3. Füge hinzu:
   - `ZEITFLOW_BOT_TOKEN` = `dein-bot-token-von-botfather`
4. Railway startet den Bot automatisch neu

### Schritt 5: Persistentes Volume (wichtig!)

Damit die Datenbank bei Restarts erhalten bleibt:

1. In Railway: Service → **Volumes**
2. "Add Volume" klicken
3. Mount Path: `/data`
4. Die Umgebungsvariable `ZEITFLOW_DB_PATH` ist bereits auf `/data/zeitflow.db` gesetzt

### Fertig!

Der Bot läuft. Öffne Telegram, suche deinen Bot und schreibe `/start`.

### Railway-Kosten

| Nutzung | Kosten |
|---------|--------|
| Trial (500 Std/Monat) | **kostenlos** |
| Hobby ($5/Monat Credit) | **~$1-2/Monat** |
| Pro (für mehrere Bots) | $20/Monat |

Ein einzelner Bot braucht ~50 MB RAM und fast keine CPU.

---

## Option B: Render

### Schritte

1. **render.com** → "New Background Worker"
2. GitHub-Repo verbinden
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python zeitflow.py`
5. Environment: `ZEITFLOW_BOT_TOKEN` eintragen
6. **Disk** hinzufügen: Mount Path `/data`, mindestens 1 GB

### Render-Kosten

| Plan | Kosten |
|------|--------|
| Free (läuft nicht 24/7) | kostenlos |
| Starter | $7/Monat |

Für Produktionsbetrieb: Starter nehmen.

---

## Option C: Fly.io

```bash
# Fly CLI installieren (einmalig)
curl -L https://fly.io/install.sh | sh

# Im Projektordner:
fly launch --name zeitflow-musterfirma
fly secrets set ZEITFLOW_BOT_TOKEN="dein-token"
fly volumes create zeitflow_data --size 1
fly deploy
```

Kosten: ~$3-5/Monat.

---

## Für dich als ZeitFlow-Anbieter

### Kundeneinrichtung in 10 Minuten

Ablauf pro Neukunde:

1. Bot bei BotFather erstellen (2 Min)
2. GitHub-Repo forken / Template nutzen (1 Min)
3. Auf Railway deployen (3 Min)
4. Token + Volume eintragen (2 Min)
5. Kunde testet: `/start` in Telegram (2 Min)

### Skalierung: Ein Repo, viele Kunden

Jeder Kunde bekommt:
- Eigenen Telegram-Bot (eigenes Token)
- Eigene Railway-Instanz (eigene DB)
- Eigenes Volume (eigene Daten)

Du verwaltest alles über Railway Dashboard.

### Preisgestaltung

| Was der Kunde zahlt | Was es dich kostet |
|--------------------|--------------------|
| Einrichtung: 500 € | 10 Minuten Arbeit |
| Monatlich: 49-99 € | ~2-5 € Hosting |

→ **Marge: 90%+**

### Wartung

- **Updates**: Code in GitHub pushen → Railway deployed automatisch
- **Backup**: SQLite-DB aus dem Volume kopieren (Railway CLI)
- **Monitoring**: Railway zeigt Logs und Metriken

---

## Dem Kunden erklären

### Was der Chef seinen Leuten sagt:

> "Öffne Telegram, suche @zeitflow_musterfirma_bot,
> schreib /start, wähle deine Sprache, gib deinen Namen ein.
> Ab jetzt tippst du /zeit wenn du anfängst und trägst ein.
> Fertig. Kein Account, kein Passwort, keine App."

### Was der Chef selbst kann:

> "Du tippst /export und bekommst eine Excel-Datei
> mit allen Stunden aller Mitarbeiter. Die schiebst du
> in Mein Büro rein und machst die Rechnung."

---

## Dateien im Paket

```
zeitflow-bot/
├── zeitflow.py          ← Der komplette Bot (eine Datei)
├── requirements.txt     ← Python-Abhängigkeiten
├── Dockerfile           ← Container-Build
├── railway.json         ← Railway-Konfiguration
├── Procfile             ← Fallback für Render
└── DEPLOY.md            ← Diese Anleitung
```
