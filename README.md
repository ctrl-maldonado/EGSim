# EGSim – Dummy-Energiegemeinschaften für die Handelsplattform

EGSim simuliert beliebig viele Energiegemeinschaften (EGs), die gegen eure Handelsplattform-API
(**Datenvertrag 0.2**) Angebote und Bedarfe einreichen, ändern und stornieren. Danach kann EGSim den Markt
schließen, die Ergebnisse der Plattform mit einer eigenen Referenzrechnung vergleichen und rund 90 Negativtests
auf Statuscodes, Fehlercodes, Rollen und Mandantentrennung ausführen.

```
EGs registrieren → freischalten → Zugangsdaten → Positionen einreichen → ändern / stornieren
      → Negativtests (Markt offen) → Markt schließen → Negativtests (Markt zu)
      → Zuteilungen, Trades, Tagesergebnis nachrechnen → Bericht (JSON + Konsole, Exit-Code)
```

## Schnellstart

```bash
pip install -e ".[dev]"          # Python ≥ 3.10, Abhängigkeiten: requests, PyYAML

# 1) Ohne eure Plattform ausprobieren: Mock starten (Markt für morgen, Tagesmarkt)
egsim mock --port 8080 --market tomorrow:DAY

# 2) In einem zweiten Terminal simulieren
cp config.example.yaml config.yaml
export EGSIM_ADMIN_API_KEY=admin-key
egsim run -c config.yaml --close
```

Gegen eure Plattform: `platform.baseUrl` bzw. `EGSIM_BASE_URL` setzen und den Admin-API-Key aus dem
Kubernetes-Secret als `EGSIM_ADMIN_API_KEY` übergeben. Ohne Installation geht auch `python -m egsim …`.

### Befehle

| Befehl | Zweck |
|---|---|
| `egsim run -c config.yaml` | Simulation ausführen |
| `egsim run … --close` | zusätzlich Markt schließen und Ergebnisse prüfen (nur auf Testsystemen) |
| `egsim run … --date 2026-11-20 --date +2` | Liefertage überschreiben |
| `egsim run … --no-checks --seed 7 --report ''` | ohne Negativtests, anderer Seed, ohne Berichtsdatei |
| `egsim plan -c config.yaml --resolution QUARTER_HOUR` | zeigt die Positionen, die erzeugt würden (ohne Plattform) |
| `egsim mock --help` | Mock-Optionen (Märkte, Reserve-Kapazität, asynchrones Settlement) |
| `-vv` | Debug-Ausgabe inkl. jedes Requests |

Exit-Code `0` = alles wie im Vertrag, `1` = Abweichungen oder fehlgeschlagene Checks (abschaltbar mit
`run.failOnFindings: false`), `2` = Konfigurationsfehler. Damit lässt sich EGSim direkt in CI einhängen.

## Konfiguration

Alles steht kommentiert in [`config.example.yaml`](config.example.yaml). Die wichtigsten Stellschrauben:

**EGs** (`communities`): Jede EG hat `generation` und/oder `consumption` mit Lastprofil und Tagesmenge.
Hat sie beides, meldet sie den Saldo (Prosumer). `count: 5` erzeugt fünf gleichartige EGs für Lasttests.

| Profil | Verlauf über den Tag (Wiener Zeit) |
|---|---|
| `solar` | Sinusbogen ca. 6:30–17:30 |
| `wind` | gleichmäßig, nachts etwas mehr |
| `household` | Morgen- und Abendspitze |
| `business` | 8–18 Uhr hoch |
| `night` | 22–6 Uhr hoch (Wärmepumpe, E-Auto) |
| `flat` | konstant |

Im **Tagesmarkt** (`DAY`) reicht jede EG eine Position mit ihrer Tagesmenge ein. Im **15-Minuten-Markt**
(`QUARTER_HOUR`) wird die Tagesmenge nach Profil auf die Intervalle verteilt (96, an Umstellungstagen 92/100)
und mit `noise` verrauscht. `behaviour.quarterHour.intervals` begrenzt die Anzahl (`all`, `daylight` oder eine Zahl).
Die Auflösung liest EGSim aus `GET /markets/{deliveryDate}`.

**Preisvorstellungen** (`priceExpectation`): Wahrscheinlichkeit, Bereich und Nachkommastellen. Liegt der Bereich
teils über dem Ankaufspreis, entstehen Angebote mit `PRICE_NOT_MET` und mehr Reservebedarf.

**Verhalten** (`behaviour`): Anteil geänderter (`updateProbability`) und stornierter (`cancelProbability`)
Positionen. Mit `run.seed` sind Mengen, Preise und Änderungen reproduzierbar.

**Bereitstellung** (`provisioning`):
- `mode: register` – EGSim registriert die EGs, schaltet sie per Admin frei und holt API-Key und Passwort.
  Die Zugangsdaten landen in `stateFile`, damit der nächste Lauf dieselben EGs wiederverwendet
  (die Datei enthält Secrets und ist in `.gitignore`).
- `mode: existing` – vorhandene EGs mit `apiKey` oder `email`/`password` je EG.
- `auth: apiKey | bearer` – EGs nutzen `X-API-Key` oder melden sich per `POST /auth/login` an.

Ohne Admin-Zugang funktioniert nur `mode: existing`; die Ergebnisprüfung beschränkt sich dann auf die eigene
Sicht der EGs.

## Was geprüft wird

**Antworten im normalen Ablauf**: Status und Felder nach Registrierung, Freischaltung, Einreichen, Ändern,
Stornieren und Marktschluss (`200` + `SETTLED` bzw. `202` + `CLOSED`, danach Polling).

**Ergebnisprüfung** (wenn der Markt `SETTLED` ist): EGSim lädt als Admin alle Positionen des Liefertags,
rechnet Matching, Teilzuteilung, Reserve, Rundung und Beträge nach dem Vertrag mit `Decimal` nach und vergleicht
jede Zuteilung, jeden Trade und das Tagesergebnis (bei `QUARTER_HOUR` auch je Intervall). Die tatsächlich gelieferte
Reserve (`dispatchedKwh`) übernimmt EGSim aus dem Plattformergebnis, weil das Kraftwerk extern ist.
Zusätzlich prüft jede simulierte EG, dass sie nur ihre eigenen Zuteilungen und Trades sieht.
Fremde Positionen echter EGs auf demselben Markt stören nicht – sie fließen in die Referenzrechnung mit ein.

**Negativtests** (`run.checks`), vollständige Liste mit Ergebnis im JSON-Bericht:

| Gruppe | Beispiele |
|---|---|
| `AUTH` | ohne/mit ungültigem API-Key oder Token → 401 |
| `ROLE` | EG listet EGs, ruft Tagesergebnis ab, ändert Status → 403; Admin reicht Position ein → 403 |
| `TEN` | fremde Position abrufen/ändern/stornieren, fremde `energyCommunityId`, fremde Filter → 403 |
| `VAL`, `INT` | Mengen/Preise (Grenzen, Nachkommastellen), Typen, Datumsformat, unbekannte Felder, kaputtes JSON, `intervalStart`-Regeln |
| `NF`, `STATE` | 404-Codes, `POSITION_ALREADY_EXISTS`, `MARKET_NOT_SETTLED`, `POSITION_NOT_MODIFIABLE` |
| `CLOSED` | nach Marktschluss: Einreichen/Ändern/Stornieren → 409, `close` idempotent, Positionen `PROCESSED` |
| `LC` | Wegwerf-EG: Registrierung, doppelte E-Mail, Statusübergänge, Login/Logout, neue Zugangsdaten machen alte ungültig, Löschen |

Validierungstests laufen mit einer EG, die im selben Intervall schon eine Position hat. Ein Server, der die
Validierung auslässt, antwortet dann mit 409 statt eine falsche Position anzulegen. Wird trotzdem eine ungültige
Position angelegt, storniert EGSim sie wieder und meldet das.

## Bericht

`reports/egsim-<Zeitstempel>.json` enthält Märkte, Mengen, Prüfergebnis mit Referenzwerten, jeden Check
(erwartet/erhalten), alle Befunde und eine Request-Statistik je Endpoint (Anzahl, Statuscodes, p50/p95-Latenz).

## Annahmen zum Datenvertrag (bitte abstimmen)

An diesen Stellen lässt Version 0.2 Spielraum. EGSim (und der Mock) handhaben sie so:

1. **Markt anlegen**: Es gibt keinen Endpoint dafür („legt der Betreiber an“). EGSim setzt voraus, dass der Markt
   existiert, und meldet sonst einen Fehler. Der Mock hat dafür `POST /_mock/markets` (nicht im Vertrag).
2. **Liefertag in der Vergangenheit ohne Markt**: Laut Prüfreihenfolge kommt 404 vor 422. EGSim akzeptiert bei
   `VAL-13` beides und vermerkt 404 als Hinweis.
3. **`PUT /positions/{id}` ohne `priceExpectationEurPerKwh`**: unklar, ob die Preisvorstellung entfernt oder
   behalten wird. EGSim schickt sie beim Ändern immer mit; der Mock entfernt sie (PUT = ersetzen).
4. **Rundungsrest**: Der Rest wird in 0,001-kWh-Schritten an die Positionen in `createdAt`-Reihenfolge verteilt,
   und zwar an alle beteiligten Positionen, nicht nur an die, die beim Abrunden etwas verloren haben.
5. **Reserveanteil im Trade**: Reserveanteil je Bedarf = pro rata von `dispatchedKwh` (mit derselben Rundungsregel),
   Anteil aus Angeboten = Zuteilung − Reserveanteil. Andere Aufteilungen können um 0,001 kWh abweichen;
   dafür gibt es `run.tolerance.kwh`.
6. **Reserve-Status im Tagesergebnis bei `QUARTER_HOUR`**: `NOT_NEEDED`, wenn nirgends Reserve nötig war;
   `COVERED`, wenn alle Intervalle gedeckt sind; `UNAVAILABLE` bei 0 kWh geliefert; sonst `PARTIAL`.
   `reserve.costEur` = Summe `dispatchedKwh` × Reservepreis, gerundet. `priceEurPerKwh` ist `null`, wenn keine Reserve nötig war.
7. **Stornierte Position und neuer Eintrag**: Eine `CANCELLED`-Position blockiert den Slot nicht (Mock).
8. **Zugangsdaten für `PENDING`-EGs**: Vergabe ist erlaubt (201), Anmeldung damit ergibt 403
   `ENERGY_COMMUNITY_NOT_ACTIVE` (`LC-08` bis `LC-10`).
9. **`createdAt` nach PUT**: bleibt unverändert, sonst verschiebt sich die Rundungsreihenfolge (EGSim warnt).

## Projektstruktur

```
egsim/
  cli.py          Kommandozeile (run, plan, mock)
  config.py       YAML laden, Umgebungsvariablen, Validierung
  client.py       HTTP-Client + Request-Statistik
  profiles.py     Lastprofile → Positionen
  simulator.py    Ablauf: Bereitstellung, Einreichen, Ändern, Stornieren, Marktschluss
  checks.py       Negativ- und Vertragstests
  settlement.py   Referenzrechnung der Handels- und Preislogik
  verify.py       Vergleich Plattform ↔ Referenz
  mockserver.py   In-Memory-Mock der API
tests/            pytest: Vertragsbeispiele, Profile, Ende-zu-Ende gegen Mock (korrekt + mit eingebauten Fehlern)
```

Der Mock rechnet mit derselben Referenzlogik wie die Prüfung. Gegen den Mock zeigt ein „OK“ also nur, dass der
Ablauf funktioniert. Die Richtigkeit der Logik sichern die Unit-Tests mit den Beispielen aus dem Vertrag
(`tests/test_settlement.py`: Beispielablauf 10,00 / 18,20 / 6,00 / 2,20 €, Teilzuteilung 75/45 kWh,
15-Minuten-Beispiel).

```bash
pytest -q
```
