# EGSim – Dummy-Energiegemeinschaften für die Handelsplattform

EGSim simuliert Energiegemeinschaften (EGs), die mit eurer Handelsplattform sprechen (**Datenvertrag 0.2**).
Er verhält sich wie eine echte EG: Er meldet sich mit den Zugangsdaten der EG an, reicht Angebote und Bedarfe ein,
ändert und storniert sie und holt nach dem Settlement Zuteilungen und Handelsabschlüsse ab. Dabei prüft er,
ob jede Antwort zum Vertrag passt, und führt rund 75 Negativtests aus (Fehlercodes, Rollen, Mandantentrennung,
Validierung).

EGSim hat **keinen Admin-Zugang** und nutzt nur Endpoints, die die Rolle `ENERGY_COMMUNITY` darf.
Märkte schließen und abwickeln macht euer Betreiber.

Konfiguriert und gestartet wird alles über eine **Web-UI**.

## Schnellstart

```bash
pip install -e ".[dev]"          # Python ≥ 3.10, einzige Abhängigkeit: requests

egsim ui --open                  # UI auf http://localhost:8090, Konfiguration in egsim-ui.json
```

Ohne eure Plattform ausprobieren:

```bash
egsim ui --mock --open           # eingebauter Mock mit 3 freigeschalteten Demo-EGs
```

Im Demo-Modus schließt der Mock den Markt für morgen nach 3 Minuten selbst. Der Lauf wartet darauf und prüft
danach die Ergebnisse. Im Tab „Manuell“ lassen sich im Demo-Modus weitere Märkte anlegen und sofort schließen.

## Die UI

| Tab | Inhalt |
|---|---|
| **Lauf** | Plattform-URL, Liefertage, Verhalten (Seed, Parallelität, Änderungen, Stornos, 15-Min-Intervalle), Prüfungen. Lauf starten/abbrechen mit Live-Log. „Nur Ergebnisse prüfen“ holt nach einem späteren Marktschluss die Ergebnisse ab. |
| **Energiegemeinschaften** | EGs anlegen, duplizieren, (de)aktivieren. Zugang (API-Key oder E-Mail/Passwort), Erzeugung und Verbrauch mit Lastprofil und Tagesmenge, Preisvorstellung. Tagesverlauf als Grafik. Buttons **Registrieren** und **Verbindung testen**. Vorschau der Positionen, die ein Lauf einreichen würde. |
| **Manuell** | Einzelne Requests als gewählte EG senden: vorgefertigte Aktionen für alle EG-Endpoints oder ein freier Request (auch mit absichtlich kaputtem JSON). |
| **Ergebnis** | Fehler, Warnungen, Checks, Märkte, Ergebnisprüfung, Latenz je Endpoint. Frühere Berichte auswählen und als JSON herunterladen. |
| **Requests** | Jeder Request mit Body und Antwort, filterbar (z. B. nur ≥ 400). API-Keys, Passwörter und Tokens werden ausgeblendet. |

Jede Änderung wird sofort in `egsim-ui.json` gespeichert. Die Datei enthält die Zugangsdaten der EGs und steht in
`.gitignore`. Die UI lauscht standardmäßig nur auf `127.0.0.1`.

### Eine neue EG einrichten

1. Im Tab **Energiegemeinschaften** „+ EG hinzufügen“, Name und E-Mail eintragen.
2. **Registrieren** → `POST /energy-communities`, die EG hat dann den Status `PENDING`.
3. Der Admin eurer Plattform schaltet sie frei und übergibt API-Key und Passwort.
4. Beides eintragen, Anmeldeart wählen, **Verbindung testen** (`GET /me`).

Bestehende EGs: einfach Zugangsdaten eintragen. Für die Tests zur Mandantentrennung braucht es mindestens zwei
aktive EGs.

## Was ein Lauf macht

1. Alle aktiven EGs melden sich an (`X-API-Key` oder `POST /auth/login` → Bearer-Token).
2. `GET /markets/{deliveryDate}`: Auflösung (`DAY`/`QUARTER_HOUR`), Preise und `closesAt` lesen.
3. Ist der Markt offen: Positionen aus den Lastprofilen erzeugen und einreichen, einen Teil ändern und stornieren.
   Jede Antwort wird gegen das Schema geprüft.
4. Negativtests bei offenem Markt.
5. Optional bis zu N Minuten warten, bis der Betreiber den Markt geschlossen und abgewickelt hat
   (Status-Abfrage alle 10 s), dann Negativtests bei geschlossenem Markt.
6. Ist der Markt `SETTLED`: Ergebnisse prüfen (siehe unten).
7. Bericht in `reports/egsim-<Zeitstempel>.json`.

**Lastprofile**: `solar`, `wind`, `household` (Morgen- und Abendspitze), `business` (8–18 Uhr), `night`
(Wärmepumpe/E-Auto), `flat`. Im Tagesmarkt reicht jede EG eine Position mit ihrer Tagesmenge ein, im
15-Minuten-Markt wird die Tagesmenge nach Profil auf die Intervalle verteilt (96, an Umstellungstagen 92/100).
Hat eine EG Erzeugung und Verbrauch, meldet sie je Intervall den Saldo. Gleicher Seed = gleiche Mengen und Preise.

## Ergebnisprüfung ohne Admin

Jede EG sieht nur ihre eigenen Daten (`/result` darf nur der Admin abrufen). EGSim prüft deshalb aus EG-Sicht:

- **Mandantentrennung**: In Positionen, Zuteilungen und Trades tauchen keine fremden Datensätze auf.
- **Positionen**: Nach dem Settlement `PROCESSED` bzw. `CANCELLED`, Mengen unverändert.
- **Zuteilungen**: Genau eine je Position, auch mit 0 kWh. `requestedKwh` stimmt, Zuteilung liegt zwischen 0 und Anfrage.
  `reason` passt zu Menge und Typ, z. B. `PRICE_NOT_MET` genau dann, wenn die Preisvorstellung über dem Ankaufspreis liegt.
- **Trades**: Richtung passt zum Typ, Preis = Ankaufs- bzw. Verkaufspreis des Markts. Die Trade-Mengen ergeben die
  Zuteilung, `amountEur` = kaufmännisch gerundete Menge × Preis, keine 0-kWh-Trades.
- **Konsistenz über alle simulierten EGs**: Teilzuteilung pro rata mit gleichem Anteil. Keine Reserve, wenn Angebote
  gekürzt wurden. Kein Kürzen, wenn Reserve genutzt wurde.

**Option „Nur simulierte EGs am Markt“**: Sind auf einem Testmarkt nur EGSim-EGs aktiv, kennt EGSim alle Positionen
und rechnet das komplette Matching mit der Referenzlogik nach: Teilzuteilung, Rundungsrest, Reserve-Aufteilung und Beträge.
Als gelieferte Reserve dient die Summe der Reserve-Trades.

## Negativtests

| Gruppe | Beispiele |
|---|---|
| `AUTH` | ohne/mit ungültigem API-Key oder Token → 401; Login mit falschem Passwort; Logout macht das Token ungültig |
| `ROLE` | EG listet alle EGs, ruft Tagesergebnis ab, ändert eigenen Status, vergibt sich Zugangsdaten → 403 |
| `TEN` | fremde Position abrufen/ändern/stornieren, fremde `energyCommunityId`, fremde Filter, fremdes Profil → 403 |
| `VAL`, `INT` | Mengen/Preise (Grenzen, Nachkommastellen, Typ), `type`, Datumsformat, unbekannte/fehlende Felder, kaputtes JSON, `intervalStart`-Regeln |
| `NF`, `STATE`, `POS`, `PROF` | 404- und 400-Codes, `POSITION_ALREADY_EXISTS`, `MARKET_NOT_SETTLED`, `POSITION_NOT_MODIFIABLE`, Profiländerung |
| `CLOSED` | nach Marktschluss: Einreichen/Ändern/Stornieren → 409, EG schließt Markt → 403, Position `PROCESSED` |
| `REG` (optional) | Registrierung mit ungültigen Daten → 400, doppelte E-Mail → 409. Legt eine PENDING-Test-EG an, deshalb standardmäßig aus. |

Validierungstests laufen mit einer EG, die im selben Intervall schon eine Position hat. Ein Server, der die
Validierung auslässt, antwortet dann mit 409, statt eine falsche Position anzulegen. Wird trotzdem eine
ungültige Position angelegt, storniert EGSim sie wieder und meldet das.

## Ohne UI (z. B. CI)

```bash
egsim run -c egsim-ui.json                  # Lauf mit der gespeicherten UI-Konfiguration
egsim run -c egsim-ui.json --wait 30        # bis zu 30 min auf das Settlement warten
egsim run -c egsim-ui.json --verify-only    # nur Ergebnisse prüfen
egsim plan -c egsim-ui.json --resolution QUARTER_HOUR
egsim mock --demo-egs 3 --auto-settle       # Mock als eigenen Server (Port 8080)
```

Exit-Code `0` = alles vertragskonform, `1` = Befunde, `2` = Konfigurationsfehler. Werte wie `${EG_API_KEY}` in der
JSON-Datei werden aus Umgebungsvariablen gelesen. So müssen in CI keine Secrets in der Datei stehen.

## Annahmen zum Datenvertrag (bitte abstimmen)

1. **Markt anlegen**: Dafür gibt es keinen Endpoint. EGSim setzt voraus, dass der Markt existiert.
2. **Liefertag in der Vergangenheit ohne Markt**: Laut Prüfreihenfolge 404 vor 422. `VAL-13` akzeptiert beides und
   vermerkt 404 als Hinweis.
3. **Fremde vs. unbekannte EG-ID**: Nach der Prüfreihenfolge 404 für eine unbekannte und 403 für eine fremde EG (`NF-02`, `TEN-07`).
4. **`PUT /positions/{id}` ohne Preisvorstellung**: Ob die Preisvorstellung dann entfernt oder behalten wird, ist offen.
   EGSim schickt sie beim Ändern immer mit.
5. **Rundungsrest** (vollständige Nachrechnung): Er wird in 0,001-kWh-Schritten nach `createdAt` an alle beteiligten
   Positionen verteilt, nicht nur an die, die beim Abrunden etwas verloren haben.
6. **Reserveanteil im Trade**: pro rata von der gelieferten Reserve, Anteil aus Angeboten = Zuteilung − Reserveanteil.
   Abweichungen um 0,001 kWh fängt die einstellbare Toleranz ab.
7. **Logout**: Laut Vertrag nur mit Bearer-Token. EGSim testet Logout nur so.
8. **`createdAt` nach PUT**: Sollte unverändert bleiben, weil es die Rundungsreihenfolge bestimmt (EGSim warnt sonst).

## Projektstruktur

```
egsim/
  ui/server.py, ui/static/index.html   Web-UI (nur Standardbibliothek + Vanilla-JS)
  config.py       Konfiguration (JSON), Validierung
  simulator.py    Ablauf aus EG-Sicht: Anmelden, Einreichen, Ändern, Stornieren, Warten, Prüfen
  checks.py       Negativ- und Vertragstests
  verify.py       Ergebnisprüfung aus EG-Sicht, optional vollständige Nachrechnung
  settlement.py   Referenzrechnung der Handels- und Preislogik (Decimal)
  profiles.py     Lastprofile → Positionen
  client.py       HTTP-Client, Request-Log
  mockserver.py   In-Memory-Mock der API (nur für Demo und Tests)
tests/            pytest: Vertragsbeispiele, Profile/Config, Ende-zu-Ende gegen Mock inkl. UI-API und fehlerhafter Plattform
```

Der Mock rechnet mit derselben Referenzlogik wie die Prüfung. Gegen den Mock zeigt ein „OK“ also nur, dass der
Ablauf funktioniert. Die Richtigkeit der Logik sichern die Unit-Tests mit den Beispielen aus dem Vertrag
(`tests/test_settlement.py`).

```bash
pytest -q
```
