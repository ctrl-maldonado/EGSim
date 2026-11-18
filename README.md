# EGSim – Dummy-Energiegemeinschaften für die Handelsplattform

EGSim simuliert Energiegemeinschaften (EGs), die mit eurer Handelsplattform sprechen (**Datenvertrag 0.2**).
Er verhält sich wie echte EGs: Er meldet sich an, reicht Angebote und Bedarfe ein, ändert und storniert sie und holt
nach dem Settlement Zuteilungen und Handelsabschlüsse ab. Dabei prüft er jede Antwort gegen den Vertrag. Dazu kommen
rund 100 Negativtests (Fehlercodes, Rollen, Mandantentrennung, Validierung) und ein **Katalog gezielter Randfälle**.

Bedient wird alles über eine **Web-UI**.

## Schnellstart

```bash
pip install -e ".[dev]"          # Python ≥ 3.10, einzige Abhängigkeit: requests

egsim ui --mock --open           # Demo: eingebauter Mock der Plattform, alles vorkonfiguriert
egsim ui --open                  # gegen eure Plattform; Konfiguration in egsim-ui.json
```

Der Mock setzt den Datenvertrag vollständig um. Damit lassen sich Simulator und Vertrag testen, solange eure
Plattform noch nicht steht. Sobald sie läuft, tragt ihr in der UI ihre Adresse ein, z. B. `http://localhost:8080`.

## Warum die Plattform die EGs kennen muss

Der Simulator schickt Daten, als wäre er eine EG. Laut Datenvertrag braucht aber jede Anfrage einer EG einen API-Key
oder ein Token. Die Plattform erkennt daran, *welche* EG spricht: Jede Position bekommt deren `energyCommunityId`,
Zuteilungen und Trades gehören zu ihr, und keine EG darf fremde Daten sehen. Unbekannte Schlüssel lehnt die Plattform
mit 401 ab. Die EG muss also **auf der Plattform** existieren. Im Simulator selbst wird nichts eingerichtet.

Dafür gibt es drei Wege:

1. **Admin-Key der Test-Plattform in der UI eintragen (empfohlen).** EGSim legt die EGs beim Start selbst an
   (`POST /energy-communities`), schaltet sie frei und holt die Zugangsdaten. Pro EG reichen Name und Lastprofil.
2. **Feste Test-EGs in eurer Plattform** (Seed-Daten im Dev-Modus) und deren API-Keys in EGSim eintragen.
3. **Ohne Admin:** In der UI „Registrieren“ klicken, der Admin schaltet die EG frei und übergibt die Zugangsdaten.

Mit Admin-Key kann EGSim außerdem den Markt selbst schließen, das komplette Tagesergebnis (`/result`) nachrechnen
und die Admin-Checks ausführen. Den Admin-Key nur bei Test-Plattformen eintragen.

## Die UI

| Tab | Inhalt |
|---|---|
| **Lauf** | Plattform-URL, optionaler Admin-Zugang, Liefertage und Verhalten (Seed, Parallelität, Änderungen, Stornos) sowie die Prüfungen. Lauf starten/abbrechen mit Live-Log. „Nur Ergebnisse prüfen“ holt Ergebnisse nach einem späteren Marktschluss ab. |
| **Energiegemeinschaften** | EGs anlegen und konfigurieren: Zugang, Erzeugung und Verbrauch mit Lastprofil, Preisvorstellung, Grafik des Tagesverlaufs. Buttons: „Automatisch einrichten“, „Registrieren“, „Verbindung testen“. Vorschau der Positionen. |
| **Szenarien** | Randfälle auswählen und ausführen, Ergebnis je Szenario mit Positionen und Abweichungen. |
| **Manuell** | Einzelne Requests als gewählte EG, auch freie Requests mit absichtlich kaputtem JSON. Im Demo-Modus: Märkte anlegen und schließen. |
| **Ergebnis** | Fehler, Warnungen, Checks, Szenarien, Märkte, Latenz je Endpoint. Frühere Berichte, Download als JSON. |
| **Requests** | Jeder Request mit Body und Antwort, filterbar. API-Keys, Passwörter und Tokens werden ausgeblendet. |

Änderungen landen sofort in `egsim-ui.json`. Die Datei enthält Zugangsdaten und steht in `.gitignore`.

## Ein Lauf

1. Admin-Zugang prüfen (falls eingetragen), EGs anmelden bzw. automatisch einrichten.
2. `GET /markets/{deliveryDate}`: Auflösung, Preise und `closesAt` lesen.
3. Ist der Markt offen: Positionen aus den Lastprofilen einreichen, einen Teil ändern und stornieren.
4. Negativtests bei offenem Markt.
5. Markt schließen (mit Admin-Key und Option) oder bis zu N Minuten auf den Betreiber warten.
6. Negativtests bei geschlossenem Markt, Ergebnisprüfung, Konto- und Admin-Checks.
7. Bericht in `reports/egsim-<Zeitstempel>.json`.

**Lastprofile**: `solar`, `wind`, `household`, `business`, `night`, `flat`. Im Tagesmarkt eine Position mit der
Tagesmenge, im 15-Minuten-Markt nach Profil auf die Viertelstunden verteilt. Hat eine EG Erzeugung und Verbrauch,
meldet sie den Saldo. Gleicher Seed = gleiche Mengen und Preise.

## Szenarien (Randfälle)

Jedes Szenario legt feste Positionen an und kennt das erwartete Ergebnis. Die Erwartungen sind von Hand aus dem
Vertrag abgeleitet, zusätzlich rechnet EGSim jedes Szenario mit der Referenzlogik nach.

| ID | Szenario | Erwartung |
|---|---|---|
| SZ-01 | Angebot = Bedarf | beide voll, keine Reserve |
| SZ-02 | Preisvorstellung = Ankaufspreis | noch handelbar |
| SZ-03 | Preisvorstellung 0,00001 € über Ankaufspreis | `PRICE_NOT_MET`, Bedarf aus der Reserve |
| SZ-04 | 3 × 5 kWh Angebot, 10 kWh Bedarf | 3,334 / 3,333 / 3,333, Rest an das zuerst eingereichte Angebot |
| SZ-05 | 100 + 60 kWh Angebot, 120 kWh Bedarf | 75 / 45 kWh |
| SZ-06 | nur Bedarf | volle Reserve (oder `RESERVE_SHORTAGE`) |
| SZ-07 | nur Angebot | `NOT_NEEDED`, kein Trade |
| SZ-08 | 0,001 kWh | Trade über 0,001 kWh, Betrag gerundet |
| SZ-09 | 1.000.000 kWh | große Beträge ohne Rundungsfehler |
| SZ-10 | 0,125 kWh | Beträge kaufmännisch gerundet |
| SZ-11 | Ändern und Stornieren | zählt der letzte Stand, Storno ohne Zuteilung |
| SZ-12 | EG nach dem Einreichen gesperrt (Admin) | Position `REJECTED` |
| SZ-20 | jede Viertelstunde des Tages | 96 bzw. 92/100 an Umstellungstagen angenommen |
| SZ-21 | Lasttest | alle EGs gleichzeitig, keine Serverfehler, Latenz p95 |

Im **15-Minuten-Markt** bekommt jedes Szenario eine eigene Viertelstunde ab 10:00 Uhr Wiener Zeit, alle laufen auf
einem Markt. Der Lasttest nutzt 14:00–18:00 Uhr. Im **Tagesmarkt** braucht jedes Szenario einen eigenen Liefertag.
Die genutzten Viertelstunden sollten leer sein, am besten einen eigenen Testmarkt nehmen (Feld „Liefertage für
Szenarien“). Mit Admin-Key legt EGSim fehlende Szenario-EGs selbst an. Ohne Admin-Key braucht es so viele EGs mit
Zugangsdaten, wie die UI anzeigt (maximal vier).

Nicht vom Simulator auslösbar ist alles, was eure Plattform entscheidet: Reserve reicht nicht (Kraftwerk),
Settlement schlägt fehl (`FAILED`), Token läuft nach einer Stunde ab. Das testet ihr mit Testeinstellungen in der
Plattform. EGSim prüft dann, ob Zuteilungen und Trades dazu passen.

## Ergebnisprüfung

- **Immer (EG-Sicht):** Mandantentrennung, Positionsstatus nach dem Settlement, genau eine Zuteilung je Position,
  `reason` passend zu Menge, Typ und Preisvorstellung. Trades passend zur Zuteilung, Preise, Beträge (kaufmännisch gerundet).
  Teilzuteilung pro rata über alle simulierten EGs.
- **Mit Admin-Key:** Alle Positionen des Markts werden komplett nachgerechnet und mit `/allocations`, `/trades` und
  dem Tagesergebnis `/result` verglichen, inklusive Reserve und Marge, bei 15 Minuten auch je Intervall.
- **Ohne Admin, „Nur simulierte EGs am Markt“:** Komplette Nachrechnung aus den Daten der simulierten EGs.

## Negativtests

| Gruppe | Beispiele |
|---|---|
| `AUTH` | fehlende/ungültige Zugangsdaten → 401, falsches Passwort, Logout macht Token ungültig |
| `ROLE` | EG listet alle EGs, ruft Tagesergebnis ab, ändert Status, vergibt sich Zugangsdaten → 403 |
| `TEN` | fremde Positionen abrufen/ändern/stornieren, fremde IDs und Filter → 403 |
| `VAL`, `INT` | Grenzen und Nachkommastellen, Typen, Datumsformat, unbekannte Felder, kaputtes JSON, `intervalStart` |
| `NF`, `STATE`, `POS`, `PROF` | 400/404-Codes, doppelte Position, `MARKET_NOT_SETTLED`, `POSITION_NOT_MODIFIABLE` |
| `CLOSED` | nach Marktschluss: Einreichen/Ändern/Stornieren → 409, Position `PROCESSED` |
| `ADM` (Admin) | Statusübergänge, PENDING/gesperrte EG → 403, neue Zugangsdaten machen alte ungültig, Löschen (204/409), Markt-Schließen idempotent |
| `REG` (optional) | Registrierung mit ungültigen Daten → 400, doppelte E-Mail → 409 |

## Hosten

**Lokal:** `egsim ui` läuft auf `127.0.0.1:8090`. Die Plattform lokal starten und `http://localhost:<port>` eintragen.

**VPS mit Docker Compose** (Simulator und Plattform auf demselben Server):

```bash
cp deploy/.env.example .env      # UI-Passwort und Domain eintragen
docker compose up -d --build
```

- Caddy stellt die UI per HTTPS unter `EGSIM_DOMAIN` bereit (Let's Encrypt, die Domain muss auf den VPS zeigen).
- Die UI verlangt ein Passwort (Benutzer `egsim`, Passwort aus `EGSIM_UI_PASSWORD`).
- Eure Plattform als weiteren Service in `docker-compose.yml` eintragen (Vorlage ist drin). EGSim erreicht sie
  intern unter `http://plattform:8080`, die Plattform-API muss dafür nicht öffentlich sein.
- Ohne Domain: Caddy weglassen und per SSH-Tunnel zugreifen: `ssh -L 8090:localhost:8090 <vps>`.

**Kubernetes:** `deploy/kubernetes/egsim.yaml` enthält Deployment, Service und Volume. Das Image baut GitHub Actions
bei jedem Push nach `ghcr.io/renshredder/egsim`. Das UI-Passwort kommt aus einem Secret, als Basis-URL tragt ihr den
Service der Plattform im Cluster ein.

Ohne Docker: Mit `EGSIM_UI_PASSWORD=… egsim ui --host 0.0.0.0` ist die UI auch direkt passwortgeschützt.

## Ohne UI (z. B. CI)

```bash
egsim run -c egsim-ui.json                     # Lauf mit der gespeicherten UI-Konfiguration
egsim run -c egsim-ui.json --close             # Markt per Admin-Key selbst schließen
egsim run -c egsim-ui.json --scenarios         # ausgewählte Szenarien (oder --scenarios SZ-01,SZ-04)
egsim run -c egsim-ui.json --wait 30           # bis zu 30 min auf das Settlement warten
egsim run -c egsim-ui.json --verify-only       # nur Ergebnisse prüfen
egsim mock --demo-egs 3 --auto-settle          # Mock als eigenen Server (Port 8080, Admin-Key admin-key)
```

Exit-Code `0` = alles vertragskonform, `1` = Befunde, `2` = Konfigurationsfehler. `${VAR}` in der JSON-Datei wird
aus Umgebungsvariablen gelesen, so müssen keine Secrets in der Datei stehen.

## Annahmen zum Datenvertrag (bitte abstimmen)

1. **Markt anlegen:** Dafür gibt es keinen Endpoint. EGSim setzt voraus, dass der Markt existiert.
2. **Liefertag in der Vergangenheit ohne Markt:** Laut Prüfreihenfolge 404 vor 422. `VAL-13` akzeptiert beides.
3. **Unbekannte vs. fremde EG-ID:** 404 für unbekannt, 403 für fremd (Prüfreihenfolge).
4. **`PUT /positions/{id}` ohne Preisvorstellung:** Ob sie entfernt oder behalten wird, ist offen. EGSim schickt sie immer mit.
5. **Rundungsrest:** Er geht in 0,001-kWh-Schritten nach `createdAt` an alle beteiligten Positionen (SZ-04).
6. **Reserveanteil im Trade:** pro rata von der gelieferten Reserve, Anteil aus Angeboten = Zuteilung − Reserveanteil.
7. **Stornierte Position:** Sie blockiert den Slot nicht für eine neue Position (SZ-20, SZ-21).
8. **Zugangsdaten für PENDING-EGs:** Die Vergabe ist erlaubt, eine Anmeldung damit ergibt 403 (`ADM-07` bis `ADM-09`).
9. **`createdAt` nach PUT:** Es bleibt unverändert, weil es die Rundungsreihenfolge bestimmt.

## Projektstruktur

```
egsim/
  ui/server.py, ui/static/index.html   Web-UI (Standardbibliothek + Vanilla-JS), optional Basic Auth
  simulator.py    Ablauf: Anmelden/Einrichten, Einreichen, Ändern, Stornieren, Schließen/Warten, Prüfen
  scenarios.py    Szenario-Katalog
  checks.py       Negativ-, Konto- und Admin-Checks
  verify.py       Ergebnisprüfung (EG-Sicht, vollständig, Admin inkl. /result)
  settlement.py   Referenzrechnung der Handels- und Preislogik (Decimal)
  profiles.py     Lastprofile → Positionen
  config.py       Konfiguration (JSON)
  client.py       HTTP-Client, Request-Log
  mockserver.py   In-Memory-Mock der API
deploy/           Caddyfile, .env-Vorlage, Kubernetes-Manifest
tests/            pytest: Vertragsbeispiele, Profile/Config, Ende-zu-Ende gegen Mock inkl. Szenarien und fehlerhafter Plattformen
```

Gegen den Mock zeigt ein „OK“ nur, dass der Ablauf funktioniert, denn Mock und Prüfung nutzen dieselbe
Referenzlogik. Die Logik selbst sichern die Unit-Tests mit den Beispielen aus dem Vertrag und die handgeschriebenen
Szenario-Erwartungen ab.

```bash
pytest -q
```
