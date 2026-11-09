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
| Admin | `/testmail adresse` | Beispiel-Stundennachweis an beliebige Adresse – wird nicht als Versand verbucht |

Sprachen der Monteur-Oberfläche: Deutsch, Russisch, Ukrainisch, Polnisch, Litauisch.
Büro-Befehle sind deutsch.

### Erfassung

- Nachtschichten: Ende vor Beginn (z. B. 20:00–06:00) wird als Schicht bis zum Folgetag gebucht, am Starttag
- Pause als Minuten (30), `1:00`, `60 min` oder Stunden mit Komma (`0,5`)
- Überschneidet sich bei einer Trupp-Erfassung ein Monteur, bleibt der Entwurf erhalten: „Ohne … speichern“
  oder Zeiten ändern
- Abgelaufene Sitzungen (30 Min. bei `/zeit`, 15 Min. sonst) melden sich beim nächsten Tippen, statt Eingaben
  stillschweigend zu verwerfen

### Stundennachweis

Layout nach `docs/vorlagen/SMF_2026_KW13.xlsx` mit diesen Änderungen:

- Spalte **Kostenstelle** zwischen „Ausgeführte Arbeiten“ und „Beginn“ (aus dem Projekt)
- **jeder Monteur eine eigene Zeile** mit eigener Tätigkeit; Tagesblöcke wachsen bei großen Trupps
- **Anfahrten** direkt unter „Normal-Std.“ – bleibt leer, bis es per `/anfahrt` oder in der Datei
  nachgetragen wird
- KW, Jahr und Tagesdaten werden für jede Woche aus dem ISO-Kalender berechnet (auch über den Jahreswechsel)
- Tätigkeiten erscheinen auf Deutsch; nicht übersetzbare Texte werden mit „[nicht übersetzt]“ markiert

### Übersetzung

Tätigkeiten werden direkt nach dem Speichern im Hintergrund über die Claude API ins Deutsche übersetzt
(ein zentraler `ANTHROPIC_API_KEY`); der Monteur bekommt die Übersetzung als kurze Folgenachricht. Fällt die
API aus, ist der Eintrag trotzdem gespeichert; die Übersetzung wird vor jedem Export/Versand automatisch
nachgeholt. Bei einem Trupp wird der Text nur einmal übersetzt. Ohne API-Schlüssel läuft der Bot weiter,
fremdsprachige Tätigkeiten bleiben dann markiert.

### Versand

- Pro Kunde eine E-Mail mit Excel-Anhang an `ZEITFLOW_REPORT_EMAIL`
- Jede Woche geht pro Kunde einmal raus. Ändert sich eine bereits versendete Woche danach (Nachtrag nach
  Sonntag 19:00, Korrektur, Löschung, Anfahrten nachgetragen), sendet der nächste Sonntagslauf automatisch
  eine Fassung mit „(korrigiert)“ im Betreff – nur für die betroffenen Kunden. Sofort geht das über
  `/versenden` → „Neue und geänderte“.
- `/versenden` → „Alle erneut senden“ verschickt unveränderte Wochen mit „(erneut gesendet)“, nicht „(korrigiert)“
- Ändert das Büro Bezeichnung oder Kostenstelle eines Projekts (`/projekt`), werden die betroffenen Wochen der
  letzten 70 Tage als Korrektur nachversendet; der Bot nennt die Anzahl
- Ohne `ZEITFLOW_REPORT_EMAIL` gehen nur die Nachweise der laufenden Woche per Telegram an die Admins
- Fehlgeschlagene Versände werden nach 30 Minuten automatisch wiederholt (bis zu 3 Versuche), danach beim
  nächsten Sonntagslauf oder per `/versenden`
- Die Admins erhalten nach jedem Lauf eine Zusammenfassung in Telegram; was nicht per E-Mail rausging, kommt
  als Datei direkt in den Chat. Bricht der Lauf ab, gibt es eine Warnung.

### Rechte und Zeitfenster

- Monteure korrigieren eigene Einträge der letzten 14 Tage; Vorarbeiter zusätzlich die von Mitarbeitern und
  selbst erfasste; das Büro alle Einträge der letzten 62 Tage
- Neue Nutzer sind gesperrt, bis ein Admin sie freischaltet. Admins werden ausschließlich über
  `ZEITFLOW_ADMIN_IDS` oder durch einen bestehenden Admin (`/mitarbeiter`) bestimmt.

## Konfiguration

Alle Variablen mit Erklärung: [`env.example`](env.example). Minimal nötig:

```
ZEITFLOW_BOT_TOKEN, ZEITFLOW_ADMIN_IDS, ZEITFLOW_DB_PATH=/data/zeitflow.db,
ANTHROPIC_API_KEY, ZEITFLOW_REPORT_EMAIL, ZEITFLOW_SMTP_HOST/USER/PASSWORD/FROM
```

### Mailversand über Brevo (nötig auf Railway Hobby)

Railway sperrt ausgehendes SMTP (Ports 25/465/587/2525) im Hobby-Tarif. Der Bot versendet deshalb über die
HTTPS-API von [Brevo](https://www.brevo.com) (EU-Anbieter, Gratis-Tarif für kleine Mengen):

1. Kostenloses Brevo-Konto anlegen.
2. **Senders, Domains & Dedicated IPs → Senders → Add a sender**: Absenderadresse eintragen (eine Firmenadresse,
   keine gmx-/gmail-Adresse – deren DMARC-Richtlinie verhindert den Versand über Brevo) und den Link in der
   Bestätigungsmail anklicken.
3. **SMTP & API → API Keys → Generate a new API key**.
4. In Railway setzen: `ZEITFLOW_BREVO_API_KEY` (Schlüssel) und `ZEITFLOW_MAIL_FROM` (bestätigte Adresse).
5. Im Bot `/testmail adresse` senden. Fehlermeldungen von Brevo werden im Klartext angezeigt.

## Deployment (Railway)

1. Repository in Railway verbinden (**New Project → Deploy from GitHub Repo**), Build per `Dockerfile`.
2. **Volume** mit Mount-Pfad `/data` anlegen (dort liegt die SQLite-Datenbank).
3. Variablen aus `env.example` unter **Variables** setzen.
4. Deployen. Im Log erscheint `ZeitFlow gestartet …`. Fehlt `ZEITFLOW_ADMIN_IDS` und gibt es noch keinen
   Admin, bricht der Start bewusst ab (sonst könnte ein Fremder Admin werden).
5. In Telegram `/start` senden (als Admin-ID aus `ZEITFLOW_ADMIN_IDS`), dann Kunden und Projekte anlegen.
6. Mailversand prüfen: `/testmail name@firma.de`. Bei GMX muss im Postfach unter *Einstellungen → POP3 & IMAP*
   der externe Zugriff erlaubt sein, und `ZEITFLOW_SMTP_FROM` muss die GMX-Adresse sein.

Render o. ä.: Start-Befehl `python -m zeitflow`, persistente Disk unter `/data`.

### Update von ZeitFlow 1.x

Beim ersten Start migriert der Bot die bestehende Datenbank automatisch: Benutzer, Rollen, Projekte und
Arbeitseinträge werden übernommen, Kunden aus dem Projektfeld „Kunde“ angelegt. Urlaub/Krank-Einträge werden
nicht übernommen. Nutzer mit Lettisch als Sprache werden auf Deutsch umgestellt (Lettisch wird nicht mehr
angeboten). Die alten Tabellen bleiben als `legacy_*` erhalten. Vorher trotzdem ein Backup der
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
