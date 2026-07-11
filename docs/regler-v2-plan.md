# Regler v2: Sägezahn eliminieren, Ziel ±0 W (Plan)

**Branch:** `feature/regler-v2` (Build dort, nicht auf feature/ocr)
**Stand:** 2026-07-11, Analyse aus 987 Zyklen Sekundentakt-Log

## Diagnose (belegt)

Das Sägezahn-Muster im Netzbezug (langsamer Anstieg, scharfer Abriss,
Amplitude ~40–60 W) ist ein **Limit-Cycle des eigenen Reglers**:

- 8/10 scharfe W-Abrisse folgen 0–15 s auf eine eigene Limit-Erhöhung
- Sägezahn-Periode median 66 s = Korrektur-Rhythmus des Reglers
- Regelkreis-Latenz (Limit-Befehl → Wirkung am Zähler): **median 6 s**
  (OpenDTU-Funk + Inverter-Rampe + LCD-Update + Median-3-Filter)

Ursache: „Warte bis Fehler > Hysterese (15 W), dann voller Ausgleichsschritt"
+ 6 s Totzeit = Drift → Großkorrektur → Drift. Wolken-Prädiktion ist NICHT
nötig: wenn PV unter dem Limit liegt, regelt ohnehin niemand — der Sägezahn
existiert nur im limitierten Betrieb.

## Phase 1 — Telemetrie (bereits aktiv auf feature/ocr)

- PV-Leistung jede Sekunde im Log (statt nur bei CONTROL_EVERY)
- Analyse-Skript wertet Log aus: Abriss/Limit-Korrelation, Periode, Latenz

## Phase 2 — Inkrementeller PI-Regler (Kern der Arbeit)

Prinzip: **viele kleine Schritte statt seltener großer.**

- Jede Regelperiode: `limit += Kp * error + Ki * integral(error)`
  Start: Kp≈0.4, Ki≈0.05/s, Anti-Windup an MIN/MAX_LIMIT
- Hysterese abschaffen (non-persistente Limits kosten nichts); nur
  Mini-Deadband ±5 W gegen Funk-Spam
- **In-Flight-Guard** (wichtigster Teil): nach einer Limit-Änderung
  Korrekturen aussetzen, bis PV die Änderung reflektiert oder 8 s
  (Latenz + Puffer) vergangen sind. Verhindert Doppel-Korrekturen
  in die Totzeit hinein — die Hauptquelle des Überschwingens.
- Lastsprung-Feedforward bleibt: |ΔW| > 200 W in 2 s → sofort voller
  Schritt, PI übernimmt danach das Feintuning

Erwartung: Amplitude von ±50 W auf ±10–15 W, dann `TARGET_GRID_W` von
−50 schrittweise Richtung −10…0 W.

## Phase 3 — Prädiktion (nur falls Phase 2 nicht reicht)

- Periodische Lasten (Kühlschrank-Zyklen) per Autokorrelation über mehrere
  Tage lernen und vorhalten. Erwarteter Zusatznutzen klein (±15 → ±8 W) —
  erst angehen, wenn PI ausgereizt und der Bedarf belegt ist.
- Explizit NICHT geplant: Wolken-/PV-Prognose (siehe Diagnose).

## Messkriterien

- Amplitude (P95−P5 des W-Signals über 10-min-Fenster) vorher/nachher
- Anteil Zeit in [TARGET−25, TARGET+25]
- Anzahl Limit-Befehle/Stunde (Funklast) darf nicht explodieren
