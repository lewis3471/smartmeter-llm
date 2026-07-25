# Einstellungen — was bedeutet was

Kurzreferenz für die Add-on-Konfiguration in Home Assistant.
**Neue Optionen werden von HA automatisch mit ihrem Default ergänzt —
bestehende Einstellungen bleiben erhalten, es geht nie etwas verloren.**

## Die drei Zahlen, die oft verwechselt werden

Sie sitzen an verschiedenen Stellen der Kette:

```
   Stromzähler                        Wechselrichter
   (was das Netz sieht)               (was wir befehlen)
        │                                    │
   target_grid_w  ────► Regler ────►  Limit in Watt
   target_grid_full_w                  ├── sustain_floor_w  (Untergrenze im Normalbetrieb)
                                       └── min_limit_w      (absolute Notbremse)
```

### `target_grid_w` / `target_grid_full_w` — das ZIEL am Zähler

Was am Stromzähler stehen soll. **Negativ = einspeisen, positiv = beziehen.**

Mit Akku wandert das Ziel automatisch mit dem Ladezustand:

| Bus-Spannung | Ziel | warum |
|---|---|---|
| 51,2 V (leer) | `target_grid_w` = **+20 W** | Speicher schonen, lieber ein paar Watt kaufen |
| 52,8 V | −15 W | dazwischen linear |
| 54,4 V (voll) | `target_grid_full_w` = **−50 W** | Akku kann nichts mehr aufnehmen, Überschuss darf raus |

Ohne konfigurierten Akku (`batt_strings` leer) gilt immer `target_grid_w`.

### `sustain_floor_w` — die Untergrenze am Wechselrichter (430 W)

**Keine Wunsch-Einstellung, sondern eine Hardware-Eigenschaft.** Gemessen an
929 Limit-Befehlen: Der HMS folgt einem Limit unter ~500 W nur unzuverlässig
(bei 300 W: 44 %, bei 450 W: 90 %, ab 500 W: 99,7 %). Er *kann* wenig Leistung
liefern — er findet per Befehl nur nicht dorthin, sondern fällt auf ~157 W.

Deshalb: Rechnet der Regler ein Ziel-Limit unter 430 W aus, **sendet er es
nicht**, sondern lässt das Limit stehen. Der Wechselrichter bleibt in Ruhe und
liefert weiter. Preis: etwas Überschuss (bei 390 W Nachtlast ~40 W).

`0` schaltet das ab (dann regelt er wie früher bis `min_limit_w` runter — und
schläft nachts ständig ein).

### `min_limit_w` — die Notbremse (50 W, fest verdrahtet)

Tiefstes Limit, das überhaupt gesendet werden darf. Greift nur noch beim
Akku-Schutz (Tiefentladung) und im Failsafe.

## Beispiel: ein Regelzyklus

Angenommen Ziel −20 W, Wechselrichter liefert 300 W, Zähler zeigt +200 W:

1. Fehler = 200 − (−20) = **220 W zu wenig**
2. Nötiges Limit = 300 + 220 = **520 W** → über dem Floor → wird gesendet.

Nachts dagegen: Ziel +20 W, Wechselrichter liefert 430 W, Zähler zeigt −10 W.

1. Fehler = −10 − 20 = **−30 W zu viel**
2. Nötiges Limit = 430 − 30 = **400 W** → **unter dem Floor** → wird *nicht*
   gesendet, Limit bleibt auf 430. (Ohne diese Regel würde der Befehl den
   MPPT aushebeln und der Wechselrichter fiele auf 157 W — dann zieht das
   Haus 230 W aus dem Netz statt 40 W einzuspeisen.)

## Weitere Optionen

| Option | Bedeutung |
|---|---|
| `deadband_w` (15) | Totzone ums Ziel — darunter wird gar nicht geregelt |
| `latency_s` (0) | zusätzliche Wartezeit vor Runter-Korrekturen; 0 = aus, der Smith-Predictor bremst bereits |
| `max_limit_w` (2000) | maximales Limit |
| `failsafe_limit_w` (51) | Limit, wenn das OCR mehrfach hintereinander ausfällt |
| `batt_strings` | belegte Wechselrichter-Eingänge am Akku-Bus, z. B. `1,2,4` — leer = kein Akku-Schutz |
| `batt_low_v` (51,2) / `batt_high_v` (54,4) | Akku-Schwellen (16S LiFePO4: 3,20 / 3,40 V pro Zelle) |
| `log_level` (error) | `all` / `error` / `none` |
| `save_samples` (true) | Bilder + Labels sammeln (Grundlage fürs Retraining) |
| `git_*` | Evidence-Sync ins Repo |

Alles Weitere (Kamera, OCR-Schwellen, Regler-Feinheiten wie Kick und
Smith-Predictor) ist im Code fest verdrahtet und braucht keine Pflege.
