# AC-seitiger Tiefentladeschutz — Einrichtung und Inbetriebnahme

## Warum

Am 28.08. lief der Akku bis zur BMS-Abschaltung leer, obwohl der Wächter
auf 47/48 V stand. Der Grund ist strukturell und nicht mit anderen Zahlen
zu beheben:

**Ein Limit ist keine Abschaltung.** Das kleinste ansteuerbare Limit des
HMS-2000-4T ist 50 W, und unterhalb von ~500 W folgt er einem Limitbefehl
ohnehin nur unzuverlässig (eigene Messung an 929 Kommandos: 250–300 W zu
25 %, 350–400 W zu 67 %). Er fällt stattdessen in einen Attraktor bei
~157 W und speist die Nacht durch. Am Solarstrang war das folgenlos, am
Akkubus entlädt es den Pack bis das BMS abschaltet.

**Die Packspannung ist die falsche Messgröße.** Die LFP-Kurve ist im
Betriebsbereich flach, der Victron zieht den Bus beim Laden sofort hoch
(volle Spannung bei leerem Akku), und das BMS schützt auf die *schwächste
Zelle*: bei 200 mV Drift steht die bei 2,6 V, während der Pack noch 48 V
anzeigt. Der 47-V-Schutz konnte deshalb gar nicht vor dem BMS auslösen.

Der neue Schutz trennt die **AC-Seite über eine schaltbare Steckdose** und
entscheidet anhand der **Zellspannung und des BMS-SoC**.

## Voraussetzungen — vor der ersten Zeile Konfiguration prüfen

| # | Punkt | Warum |
|---|---|---|
| V1 | **OpenDTU Fusion und NUC hängen NICHT an der geschalteten Dose** | Sonst verschwindet mit dem AC genau die Datenquelle, die das Wiedereinschalten erlauben müsste |
| V2 | Dauerlast der Dose beachten: P100 = 10 A/2300 W, P110/P115 = 16 A/3680 W | Bei `max_limit_w: 1450` sind es ~6,3 A — für den P100 in Ordnung |
| V3 | Tapo-App: **Third-Party Compatibility EIN**, Firmware-Auto-Update AUS, **Default State auf AUS** (nicht „letzter Zustand") | Nach einem Netzausfall darf nur eine Instanz einschalten, die frische BMS-Daten gesehen hat |
| V4 | Steckerlage mit Phasenprüfer bestimmen und markieren | Das Relais ist einpolig; trennt es N statt L, bleibt der Wechselrichter phasenseitig am Netz |
| V5 | Aufkleber „NUR WECHSELRICHTER — nichts dazustecken", keine Mehrfachsteckdose | Eine Fremdlast verfälscht jede Leistungsprüfung |
| V6 | Einbauort ganzjährig ≥ 0 °C (TP-Link spezifiziert 0–35 °C) | Sonst gehört an die Stelle ein Installationsschütz im Verteiler |
| V7 | In OpenDTU-oB unter „Battery" und „Solar Charger" die Option *publish updates only* abschalten | Sonst kommen Alarme und Ladezustand nach einem Broker-Reconnect nie wieder |

> „Steckdose aus" ist **nicht** „spannungsfrei": der P100 trägt die
> Kennzeichnung *Micro-gap switch µ* (Kontaktabstand < 3 mm). Für den
> Zweck reicht das völlig — der Wechselrichter geht über seinen NA-Schutz
> aus. Wer am Gerät arbeitet, zieht trotzdem den Stecker.

## Inbetriebnahme in sieben Phasen

Zwischen den Phasen wird nichts übersprungen. Der Akku bleibt in jeder
Phase außer der letzten unangetastet.

### Phase 1 — Topics inventarisieren (ohne Code)

```bash
mosquitto_sub -h 192.168.178.64 -v -t 'solar/battery/#' -t 'solar/victron/#'
```

Notieren: die **echten** Namen für Zellspannung, Zell-Drift, Alarme und
`BatteryOnline`, das **Vorzeichen** von `battery/current` beim Laden, und
ob `retain` gesetzt ist. Das Add-on protokolliert dieselbe Liste eine
Minute nach dem Start selbst und warnt für jedes konfigurierte Topic, das
nie ankam.

### Phase 2 — die Kernannahme prüfen (der entscheidende Test)

Wechselrichter **von Hand** ausstecken, zwei Minuten warten.

- `solar/battery/dataAge` muss klein bleiben, `solar/victron/<SN>/V` weiterlaufen.
  Wenn nicht, wird der JK-RS485-Port mit dem Inverter stromlos — dann
  fehlt die Freigabequelle und der Aufbau muss geändert werden.
- `curl -s http://192.168.178.42/api/livedata/status | jq '.inverters[0] | {reachable, data_age}'`
  → `reachable` muss auf 0 gehen. Das ist der physikalische Zeuge der
  Abschalt-Quittung.

### Phase 3 — Trockentest mit einer Tischlampe

Dose in Betrieb nehmen, **eine Lampe** einstecken, nicht den Inverter.
`ac_switch_entity`, `ac_power_entity` und die drei Totmann-Entitäten
eintragen, `ac_off_cell_mv` auf 2700 (löst nie aus).

Erwartet: `ac_state = normal`, `ac_on = ON`, `ac_deadman = ok`,
`ac_deadman_at` springt alle 5 min ~15 min in die Zukunft.

**Totmann-Test:** Add-on stoppen. Nach spätestens 15 min muss die Lampe
von selbst ausgehen. Tut sie das nicht, steht `ac_deadman` in Wahrheit auf
`unbestaetigt` — dann ist der Schutz auf die Zustellung eines Befehls
angewiesen, und die BMS-Schwellen (unten) sind zwingend.

### Phase 4 — Auslösung erzwingen, immer noch mit der Lampe

`ac_off_cell_mv` vorübergehend **über** den aktuellen `batt_cell_min_mv`
setzen. Erwartet: `normal → drossel → aus_angefordert → ac_aus`,
`ac_switches_today` steigt um 1, `ac_reason` enthält Zahlen.
Danach Schwelle zurücksetzen und `AC-Hand-Freigabe = 5 min` testen.

Ebenfalls hier: Add-on-Neustart in jedem Zustand, Mosquitto-Neustart,
Stecker der Dose ziehen (→ `getrennt`, **kein** Fehleralarm).

### Phase 5 — mitschreiben, ohne Schaltlogik (mindestens 7 Tage)

Inverter an die Dose, `ac_automatik = AUS`, Abschaltschwellen unerreichbar.
Aufzeichnen: `cell_min` und `cell_diff` in Ruhe und unter Last, `soc_bms`,
`battery/current`, WLAN-Pegel der Dose. Daraus `ac_on_diff_max_mv` und
`batt_capacity_ah` festlegen. **RSSI schlechter als −70 dBm → erst die
Funkstrecke verbessern.**

### Phase 6 — persistentes Limit verifizieren

Dose aus, 60 s warten, Dose an, die ersten 120 s die AC-Leistung
mitschreiben. Stehen dort 1450 W statt 430 W, hat das persistente Limit
nicht gegriffen → `ac_start_blind_s` kürzen und Freigabeschwellen
konservativer setzen.

### Phase 7 — scharfschalten

Schwellen auf die Zielwerte, `ac_automatik` zunächst **AUS** (der Automat
schaltet dann nur ab, nie ein). Von Hand einschalten, drei Zyklen
beobachten, dann `ac_automatik` auf EIN.

**Die Watchdog-Automation unten wird vor Phase 7 angelegt, nicht danach.**

## Zweiter Pfad: HA-Automation, unabhängig vom Add-on

```yaml
alias: Akku-Notabschaltung (unabhaengig vom Add-on)
mode: single
trigger:
  - platform: numeric_state
    entity_id: sensor.smartmeter_llm_zellspannung_minimum
    below: 2950
    for: "00:00:30"
  - platform: state
    entity_id: sensor.smartmeter_llm_zellspannung_minimum
    to: ["unavailable", "unknown"]
    for: "00:05:00"
action:
  - service: switch.turn_off
    target: {entity_id: switch.wechselrichter_ac}
  - service: notify.persistent_notification
    data: {message: "Akku-Notabschaltung ausgeloest"}
```

Der zweite Trigger ist der wichtigere: **ein fehlender Sensor ist der
gefährlichere Zustand als ein niedriger Messwert.** Er greift, weil das
Add-on einen letzten Willen (`availability`) setzt und die Schutzsensoren
mit `expire_after: 90` laufen.

## BMS-Schwellen nachziehen (in der JK-App, nicht im Code)

Das BMS ist die einzige Instanz, die auch bei totem Netzwerk noch
abschaltet — und seine Werkseinstellungen sind der eigentliche Grund für
die Tiefe der Entladung:

| Einstellung | Werk | empfohlen |
|---|---|---|
| Zell-UVP | 2,60 V | **2,80 V** |
| UVP-Recovery | 2,65 V | **3,00 V** |
| Auto-Shutdown | 2,50 V | **2,60 V** |
| SOC-0-%-Spannung | 2,60 V | **2,90 V** |
| Balance-Start | 3,00 V | **3,40 V** |

## Zustände des Automaten

`normal` → `drossel` (Limit auf Minimum) → `aus_angefordert` → `ac_aus` →
`freigabe_beobachtung` → `ein_angefordert` → `anlauf` → `normal`.

Quer dazu: `manuell_ein`/`manuell_aus` (ein Mensch hat geschaltet — der
Automat hält sich heraus), `getrennt` (Dose stromlos, kein Fehler),
`unbekannt` (HA antwortet nicht), `stoerung` (Schaltkette defekt),
`ac_aus_unbestaetigt` (**die Dose meldet „aus", der Akku wird trotzdem
entladen** — klebendes Relais, einpolig N getrennt oder schlicht die
falsche Dose).
