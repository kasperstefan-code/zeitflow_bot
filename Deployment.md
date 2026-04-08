# ZeitFlow – Deployment

## Enthaltene Dateien

- `zeitflow.py` – Telegram-Bot
- `SMF_2026_KW13.xlsx` – Vorlage für `/exportkunde`
- `Stundentabelle_Vadim.xlsx` – Vorlage für `/exportintern`
- `requirements.txt`
- `Dockerfile`
- `railway.json`
- `Procfile`

Die beiden Excel-Dateien dienen **als Beispielvorlagen**.  
Der Bot kann den **internen Export auch für Folgejahre** erzeugen. Dabei werden Blattnamen, Jahresblatt und Formelbezüge beim Export dynamisch auf das gewünschte Jahr umgestellt.

---

## Funktionen des Bots

### Rollen
- **Mitarbeiter** – eigene Zeiten und Abwesenheiten erfassen
- **Vorarbeiter** – zusätzlich für Kollegen erfassen und exportieren
- **Admin** – zusätzlich Rollen und Stammdaten verwalten

### Erfassung
- `/zeit` – Arbeit, Urlaub oder Krank erfassen
- `/korrektur` – vorhandene Einträge ändern oder löschen
- `/heute` – heutige Einträge
- `/woche` – Wochensicht
- `/projekte` – aktive Projekte

### Exporte
- `/exportkunde` – Kundennachweis im SMF-Format
- `/exportintern` – interne Stunden-/Abwesenheitstabelle im Stil der Vadim-Vorlage

---

## Benötigte Umgebungsvariablen

### Pflicht
- `ZEITFLOW_BOT_TOKEN` = Telegram-Bot-Token von BotFather

### Empfohlen
- `ZEITFLOW_DB_PATH=/data/zeitflow.db`

### Optional
Wenn die Vorlagen an einem anderen Ort liegen:
- `ZEITFLOW_TEMPLATE_KUNDE=/app/SMF_2026_KW13.xlsx`
- `ZEITFLOW_TEMPLATE_INTERN=/app/Stundentabelle_Vadim.xlsx`

---

## Railway

### 1. Dateien ins GitHub-Repo legen
Lege diese Dateien in dein Repo:
- `zeitflow.py`
- `requirements.txt`
- `Dockerfile`
- `railway.json`
- `Procfile`
- `SMF_2026_KW13.xlsx`
- `Stundentabelle_Vadim.xlsx`

### 2. In Railway deployen
1. Railway öffnen
2. **New Project**
3. **Deploy from GitHub Repo**
4. Repo auswählen

### 3. Variable setzen
Im Service unter **Variables**:
- `ZEITFLOW_BOT_TOKEN`
- `ZEITFLOW_DB_PATH=/data/zeitflow.db`

### 4. Volume anlegen
Im Service unter **Volumes**:
- **Add Volume**
- Mount Path: `/data`

### 5. Neustarten / redeployen
Danach den Service neu starten oder neu deployen.

---

## Render

### Build / Start
- Build Command: `pip install -r requirements.txt`
- Start Command: `python zeitflow.py`

### Disk
Eine persistente Disk mit Mount Path `/data` anlegen.

### Variablen
- `ZEITFLOW_BOT_TOKEN`
- `ZEITFLOW_DB_PATH=/data/zeitflow.db`

---

## Hinweise zur Nutzung

### Rollenmenü
Der Bot setzt das Telegram-Befehlsmenü rollenabhängig:
- Mitarbeiter sehen nur Mitarbeiter-Befehle
- Vorarbeiter zusätzlich Export- und Team-Befehle
- Admins zusätzlich Rollen- und Projektverwaltung

### Kundenexport
Der Kundenexport nutzt die SMF-Vorlage.  
Wenn an einem Tag mehr Einträge anfallen als im Tagesblock Platz haben, erstellt der Bot ein zusätzliches Blatt **Anhang_KWxx**, damit keine Daten verloren gehen.

### Interner Export
Der interne Export nutzt die Vadim-Vorlage als Muster.
Beim Export in Folgejahre werden:
- Jahresblatt umbenannt
- Monatsblätter auf das Zieljahr umgestellt
- Formelbezüge angepasst
- definierte Namen wie `Urlaub` und `Resturlaub` mitgeführt

---

## Start in Telegram

1. Bot bei BotFather anlegen
2. Token setzen
3. Bot starten
4. In Telegram `/start` senden
5. Sprache wählen
6. Namen eingeben

Der erste registrierte Nutzer wird automatisch **Admin**.

---

## Empfohlener erster Test

1. `/start`
2. `/zeit`
3. einen Arbeitseintrag anlegen
4. `/exportkunde`
5. `/exportintern`

So siehst du sofort, ob Token, Datenbank und Vorlagen korrekt eingebunden sind.
