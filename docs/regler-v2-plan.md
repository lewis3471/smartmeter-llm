# Regler v2: Sägezahn eliminieren, Ziel ±0 W (Plan)

**Branch:** `feature/regler-v2` (Build dort, nicht auf feature/ocr)
**Stand:** 2026-07-11, Analyse aus 987 Zyklen Sekundentakt-Log

## Diagnose (korrigiert 2026-07-11 ~07:40)

**Erste Hypothese (Regler-Limit-Cycle) war falsch** — vom User widerlegt:
Der Inverter lief ungedrosselt (PV ≪ Limit), Limit-Befehle konnten den
Output gar nicht beeinflussen. Die Limit↔Abriss-Korrelation (8/10) war
umgekehrte Kausalität — der Regler folgt dem W-Anstieg.

**Belegte Diagnose** (Sekundentakt-Telemetrie, PV pro Zyklus):

- PV konstant 94–96 W (σ=0,6 W) durch alle W-Abrisse hindurch,
  Korrelation dW/dPV = 0,04 → Inverter unbeteiligt
- Der Sägezahn (Rampe +40–60 W über ~50–66 s, scharfer Reset) ist eine
  **echte periodische Haushaltslast**. Kandidaten: Inverter-Kompressor
  (Kühl-/Gefrierschrank), geregelte Umwälzpumpe. Identifikation: Geräte
  einzeln trennen und 1-s-Graph in HA beobachten.
- Regelkreis-Latenz Limit→Zähler bleibt als Messwert nützlich: **~6 s**

Konsequenz: Der Regler muss diese wandernde Last **verfolgen** (und darf
dabei selbst keinen Limit-Cycle produzieren, sobald das Limit greift).
Rechnung: Rampe ~1 W/s × Schleifenverzögerung ~8 s → Folgefehler ±8 W mit
sauberem PI. Die Prädiktion (Phase 3) ist damit aufgewertet: Der Sägezahn
ist deterministisch und phasen-trackbar → Feedforward drückt den Rest.

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

> **Update 2026-07-11 ~08:15 — Quelle identifiziert: Eight Sleep Pod**
> (eight-pod.fritz.box, .99). Nach dem Ausschalten: W-Signal glatt
> (σ=3,6 W statt ±25-W-Sägezahn). Phase 3 wird damit **konditional**:
> nur bauen, wenn der Pod während PV-Stunden läuft und der Sägezahn im
> gedrosselten Betrieb tatsächlich stört. Das Design unten bleibt gültig.

## Phase 3 — Wellenpaket-Feedforward (konkretisiert 2026-07-11)

Befund verfeinert: Die Störlast ist eine **Wellenpaketsteuerung mit festem
~10-s-Raster** (Tal-Abstände 9/10/19/21/30/31 s = Vielfache von 10;
Paketleistung ~40–60 W, Duty-Cycle langsam moduliert). Ein festes Raster
ist prädizierbar — Design:

1. **Raster-Sync (Software-PLL):** Schaltflanken im 1-s-W-Signal erkennen
   (|ΔW| 30–80 W binnen 1–2 s), daraus die Phase des 10-s-Gitters gleitend
   schätzen. Flanken kommen nur auf dem Gitter → Phase rastet schnell ein.
2. **Amplituden-Schätzer:** Pakethöhe als EMA über die letzten Flanken
   (hier ist EMA das richtige Werkzeug: Schätzung, nicht Regelung).
3. **Duty-Vorhersage:** An/Aus-Muster der letzten N Fenster; Vorhersage =
   Persistenz (nächstes Fenster wie das letzte) — bei langsamer
   Duty-Modulation fast immer richtig.
4. **Vorhalt:** Limit-Korrektur ~6 s (gemessene Kreis-Latenz) VOR der
   erwarteten Flanke senden, damit die Inverter-Reaktion mit der Flanke
   zusammenfällt. Ohne Raster-Sync unmoeglich, mit trivial.
5. **Aktivierung nur wenn das Limit bindet** (sonst wirkungslos) und die
   PLL eingerastet ist (Konfidenz-Gate); sonst reiner PI-Betrieb.

Realistisches Ziel: Sägezahn-Restamplitude halbieren (±25 → ±10–12 W);
die 6-s-Latenz mit Jitter setzt die Untergrenze.

Explizit NICHT geplant: Wolken-/PV-Prognose (PV war während der Analyse
konstant; im ungedrosselten Betrieb regelt ohnehin niemand).

## Messkriterien

- Amplitude (P95−P5 des W-Signals über 10-min-Fenster) vorher/nachher
- Anteil Zeit in [TARGET−25, TARGET+25]
- Anzahl Limit-Befehle/Stunde (Funklast) darf nicht explodieren
