# ZeitFlow 2 – Betrieb & Deployment

Mobile Zeiterfassung für Montageteams über Telegram. Monteure erfassen täglich Kunde, Projekt,
Beginn/Pause/Ende und ihre Tätigkeit – in ihrer eigenen Sprache. Jeden **Sonntag um 19:00 Uhr**
(Europe/Berlin) erzeugt der Bot pro Kunde einen Montage-Stundennachweis (Excel) und schickt ihn an die
zentrale Abrechnungsadresse.

## Funktionsüberblick

| Wer | Befehl | Zweck |
|---|---|---|
| alle | `/start` | Anmeldung (Sprache + Name); Freigabe durch das Büro nötig |
| alle | `/zeit` | Arbeitszeit erfassen (Buttons, Freitext für Tätigkeit) |
| alle | `/heute`, `/woche` | eigene Einträge |
| alle | `/korrektur` | Einträge der letzten 14 Tage ändern/löschen |
| alle | `/sprache`, `/hilfe` | Sprache wechseln, Übersicht |
| Vorarbeiter | `/zeit` | zusätzlich: mehrere Monteure gleichzeitig erfassen (jeder bekommt eine eigene Zeile) |
| Vorarbeiter | `/export` | Stundennachweis einer KW als Excel in den Chat |
| Admin | `/mitarbeiter` | Rollen (Mitarbeiter/Vorarbeiter/Admin), Freischalten, Sperren |
| Admin | `/kunden`, `/neuerkunde`, `/neuesprojekt`, `/projekt` | Stammdaten inkl. Kostenstelle |
| Admin | `/anfahrt` | Anzahl Anfahrten je Kunde und KW nachtragen |
| Admin | `/versenden` | Versand sofort auslösen (offene oder alle erneut) |

Sprachen der Monteur-Oberfläche: Deutsch, Russisch, Ukrainisch, Polnisch, Litauisch.
Büro-Befehle sind deutsch.

### Stundennachweis

Layout nach `docs/vorlagen/SMF_2026_KW13.xlsx` mit diesen Änderungen:

- Spalte **Kostenstelle** zwischen „Ausgeführte Arbeiten“ und „Beginn“ (aus dem Projekt)
- **jeder Monteur eine eigene Zeile** mit eigener Tätigkeit; Tagesblöcke wachsen bei großen Trupps
- **Anfahrten** direkt unter „Normal-Std.“ – bleibt leer, bis es per `/anfahrt` oder in der Datei
  nachgetragen wird
- KW, Jahr und Tagesdaten werden für jede Woche aus dem ISO-Kalender berechnet (auch über den Jahreswechsel)
- Tätigkeiten erscheinen auf Deutsch; nicht übersetzbare Texte werden mit „[nicht übersetzt]“ markiert

### Übersetzung

Tätigkeiten werden einmal beim Speichern über die Claude API ins Deutsche übersetzt (ein zentraler
`ANTHROPIC_API_KEY`). Fällt die API aus, wird der Eintrag trotzdem gespeichert; die Übersetzung wird vor
jedem Export/Versand automatisch nachgeholt. Bei einem Trupp wird der Text nur einmal übersetzt.
Ohne API-Schlüssel läuft der Bot weiter, fremdsprachige Tätigkeiten bleiben dann markiert.

### Versand

- Pro Kunde eine E-Mail mit Excel-Anhang an `ZEITFLOW_REPORT_EMAIL`
- Jede Woche wird pro Kunde nur einmal erfolgreich versendet (Protokoll in `report_runs`); fehlgeschlagene
  Versände werden beim nächsten Lauf bzw. per `/versenden` wiederholt
- Die Admins erhalten nach jedem Lauf eine Zusammenfassung in Telegram; was nicht per E-Mail rausging, kommt
  als Datei direkt in den Chat
- Einträge, die nach Sonntag 19:00 für die abgelaufene Woche nachgetragen werden, per `/versenden`
  („Alle erneut senden“) nachliefern

## Konfiguration

Alle Variablen mit Erklärung: [`env.example`](env.example). Minimal nötig:

```
ZEITFLOW_BOT_TOKEN, ZEITFLOW_ADMIN_IDS, ZEITFLOW_DB_PATH=/data/zeitflow.db,
ANTHROPIC_API_KEY, ZEITFLOW_REPORT_EMAIL, ZEITFLOW_SMTP_HOST/USER/PASSWORD/FROM
```

## Deployment (Railway)

1. Repository in Railway verbinden (**New Project → Deploy from GitHub Repo**), Build per `Dockerfile`.
2. **Volume** mit Mount-Pfad `/data` anlegen (dort liegt die SQLite-Datenbank).
3. Variablen aus `env.example` unter **Variables** setzen.
4. Deployen. Im Log erscheint `ZeitFlow gestartet …`.
5. In Telegram `/start` senden (als Admin-ID aus `ZEITFLOW_ADMIN_IDS`), dann Kunden und Projekte anlegen.

Render o. ä.: Start-Befehl `python -m zeitflow`, persistente Disk unter `/data`.

### Update von ZeitFlow 1.x

Beim ersten Start migriert der Bot die bestehende Datenbank automatisch: Benutzer, Rollen, Projekte und
Arbeitseinträge werden übernommen, Kunden aus dem Projektfeld „Kunde“ angelegt. Urlaub/Krank-Einträge werden
nicht übernommen. Die alten Tabellen bleiben als `legacy_*` erhalten. Vorher trotzdem ein Backup der
Datei `/data/zeitflow.db` ziehen.

## Entwicklung

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest            # Tests (inkl. Bot-Dialoge gegen eine Telegram-Attrappe)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI (GitHub Actions) führt Lint und Tests bei jedem Push aus.

### Architektur

```
zeitflow/
  config.py        Einstellungen aus Umgebungsvariablen
  db.py            SQLite-Schema, versionierte Migrationen (inkl. Übernahme 1.x)
  repository.py    Datenzugriff (einzige Stelle mit SQL)
  services.py      Fachlogik: Erfassung, Überschneidungsprüfung, Korrektur, Wochenbericht
  translation.py   Übersetzung (Claude API) hinter einem austauschbaren Protokoll
  export/          Renderer (Excel); weitere Formate über ReportRenderer registrieren
  mailer.py        SMTP-Versand
  reporting.py     Wochenversand je Kunde mit Protokoll
  i18n.py          Bot-Texte je Sprache
  bot/             Telegram-Handler (dünn, rufen nur Services auf)
  app.py           Zusammenbau, Sonntagsjob, Befehlsmenüs
```

### Erweiterungen (vorbereitet)

- **Weitere Sprache:** Eintrag in `i18n.LANGUAGES`, `TEXTS`, `COMMAND_DESCRIPTIONS`; der Test
  `tests/test_i18n.py` prüft Vollständigkeit.
- **PDF-Export:** Renderer mit `render(report) -> bytes` in `export.RENDERERS` eintragen
  (z. B. über LibreOffice headless) und im Reporter zusätzlich anhängen.
- **Urlaub/Krank, interne Stundentabelle:** eigene Tabelle/Services; der Wochenbericht filtert nur Kundeneinträge.
- **Anderer Messenger:** Fachlogik liegt vollständig in `services`/`reporting`; nur `bot/` ist Telegram-spezifisch.
