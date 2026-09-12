# AC-Tiefentladeschutz — eine Steckdose, ein Schalter

## Das Problem

Ein Limit ist keine Abschaltung. Der HMS-2000-4T hat ein Mindestlimit
von 50 W, und selbst damit zieht er weiter aus dem Akku. Gemessen in der
Nacht vom 11. auf den 12.09.2026 (HA-Historie):

| Zeit | Was passierte |
|---|---|
| 18:07 | Akku-Wächter greift bei 48 V, Limit → 50 W |
| 18:07–03:36 | HMS speist trotzdem durchgehend ~35 W ein, Bus sinkt 48 → 41,5 V |
| 03:36 | BMS schaltet hart ab (2,6 V/Zelle). Victron-Log: „min. 1,9 V“ |
| 07:08–08:47 | Morgens flattert der HMS im 20-s-Takt: BMS gibt frei, HMS startet, Bus bricht ein, BMS trennt … |
| 14:00–18:07 | Nachmittags pumpt der Wächter die Nachladung ins Haus: Freigabe bei 49,5 V, 700 W Entnahme, 30 min später wieder 48 V |

Das war die vierte Nacht in Folge. Die Victron-Historie zeigt seit dem
28.08. an 10 von 15 Nächten „min. 1,9 V“ — Akku vom BMS abgeklemmt.
Das ist für LiFePO4 die schädlichste Betriebsart überhaupt.

Still ist der HMS nur **stromlos**.

## Warum ein Schalter genügt

Der HMS versorgt seine Elektronik aus der DC-Seite. Bei stromloser
AC-Seite bleibt er über die DTU erreichbar und meldet weiter die
Akkuspannung. Beleg vom 12.09.2026, Steckdose 15:22–16:58 von Hand aus:

| | AC-Spannung | DC-Spannung (String 1) | `reachable` |
|---|---|---|---|
| vorher | 230 V | 50,3 V | on |
| Dose aus | **1,4 V** | 50,7 → **52,1 V** (Victron lädt) | **on** |
| nachher | 230 V | 51,9 V | on |

Der Packspannungs-Wächter (`battery_guard`, Option `batt_strings`) sieht
also auch im Aus weiter. Eine zweite Spannungsquelle (BMS, Victron per
MQTT), auf der Version 1.8 aufbaute, war nie nötig — und die 19 Optionen
dazu auch nicht.

## Einrichten

Genau eine Option:

```yaml
ac_switch_entity: switch.umnaia_rozetka_1
```

Voraussetzung ist der vorhandene Wächter (`batt_strings`, `batt_low_v`).
Das Add-on hat `homeassistant_api: true` und ruft ausschließlich
`switch.turn_on` / `switch.turn_off` und liest den Zustand der Entität.

## Was dann passiert

- **Aus**, sobald der Wächter hält: Bus unter `batt_low_v` für 15 s.
  Vorher setzt das Add-on das Limit *persistent* auf 50 W, damit der
  HMS nach dem Einschalten sanft hochkommt statt mit dem letzten
  Tageswert.
- **Ein**, sobald der Wächter freigibt: Bus ≥ `batt_low_v` + 2,0 V für
  10 min — frühestens aber **30 min** nach dem Aus. Beides gegen
  Klappern: der Bus erholt sich ohne Last binnen 20 min um ~1 V (bei
  +1,5 V hätte das nachts fast für die Freigabe gereicht, ohne dass eine
  Wattstunde nachgekommen wäre), und ein Wolkenloch hebt ihn für
  Minuten. Die Sperre zählt ab dem Aus-*Befehl*, nicht ab der Antwort
  der Dose — die P100 öffnet das Relais auch dann, wenn ihre Antwort
  erst nach dem Timeout kommt.
- Nach dem Einschalten beobachtet der HMS ~60 s das Netz, bevor er
  einspeist. Solange OpenDTU `producing: false` meldet, regelt der
  Regler nicht — sonst hielte er den Tracker für verklemmt und schickte
  Kicks an einen Wechselrichter, der noch gar nicht angefangen hat.
- Solange die Dose aus ist — oder ihr Zustand nach dem Start noch nicht
  gelesen wurde, oder die DTU den HMS nicht erreicht —, regelt der
  Regler nicht (keine Limits an einen stillen Wechselrichter, auch nicht
  im Failsafe), der Wächter liest aber weiter. Eine Ausnahme: Ist die
  Dose unlesbar **und** der Wächter hält, geht wenigstens das 50-W-Limit
  an den HMS — beide Schutzebenen dürfen nie gleichzeitig schweigen.
  Beim Wiederanlauf fragt der Regler die DTU nach dem Limit, das der HMS
  wirklich fährt (`/api/limit/status`, bis zu drei Versuche), statt das
  persistierte Minimum anzunehmen.
- Das Minimum wird je Halte-Episode einmal in den HMS-Flash geschrieben
  und bei weiteren Aus frühestens nach 10 min wiederholt — auch wenn
  eine Automation die Dose immer wieder einschaltet, bleibt es bei
  höchstens sechs Schreibzyklen pro Stunde.
- Sensor `Wechselrichter-Steckdose` in HA: `ein`, `aus`,
  `aus (frei in N min)` oder `unbekannt`; bei WLAN-Aussetzer der Dose
  mit Zusatz `, Dose nicht lesbar`.
- Nie mehr als ein Schaltbefehl pro Minute. Ist die Dose in HA
  `unavailable` (WLAN-Aussetzer der P100, ~5× täglich für 5–20 s),
  behält der Schalter den zuletzt gelesenen Zustand und schaltet nicht;
  dauert es länger als 5 min, steht es im Log.

## Von Hand eingreifen

Die Dose gehört dem Wächter. Schaltet jemand von Hand **ein**, während er
hält, nimmt er das nach spätestens einer Minute zurück. Schaltet jemand
von Hand **aus**, gilt das wie ein eigenes Aus: die 30-min-Sperre läuft
ab diesem Moment, danach schaltet der Wächter wieder ein, sobald er
freigibt. Dasselbe gilt nach einem Neustart, wenn die Dose aus vorgefunden
wird und kein Aus-Zeitpunkt gespeichert ist. Wer den Wechselrichter
länger vom Netz haben will: `ac_switch_entity` leeren oder das Add-on
stoppen — dann bleibt die Dose, wie sie ist.

## Grenzen

- **Kein Totmann.** Stirbt das Add-on, während die Dose EIN ist,
  schaltet niemand ab. `batt_hold` überlebt Neustarts in `state.json`,
  der HA-Watchdog startet das Add-on neu — mehr Sicherung gibt es nicht.
  Das ist der Preis der Einfachheit.
- Der Wächter urteilt nach der Bus-Spannung, nicht nach der schwächsten
  Zelle. Bei einem driftenden Pack schützt das BMS weiterhin zuerst.
- Eingefrorene DTU-Werte (Inverter nicht erreichbar, `data_age` > 60 s)
  gelten als „keine Messung“: der Wächter hält dann seinen Zustand.

## Interna (nur per Env, keine Add-on-Optionen)

| Env | Standard | Bedeutung |
|---|---|---|
| `AC_OFF_MIN_S` | 1800 | Mindest-Aus-Zeit |
| `BATT_TRIP_S` | 15 | Entprellung vor dem Aus |
| `BATT_RECOVER_V` | 2.0 | Freigabe bei `batt_low_v` + dieser Wert |
| `BATT_RELEASE_S` | 600 | so lange muss die Freigabespannung stehen |
