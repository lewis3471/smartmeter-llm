# Changelog

## 1.6.0

- Akku-Waechter: batt_strings (z.B. "1,4") schuetzt Akku-Strings vor
  Tiefentladung — unter batt_low_v wird das Gesamtlimit adaptiv gesenkt,
  bis die gemessene Entnahme ~0 W ist; ab batt_high_v wieder frei
  (Hysterese). Neue HA-Sensoren: Akku-Spannung, Akku-Schutz aktiv.
  OpenDTU-on-Battery: Dynamic Power Limiter deaktivieren!
- Gemini-Prompt mit Kontext und bekannten Edge-Cases (6-stelliger
  Zaehlerstand — nie trunkieren, Minuszeichen, Segmenttest, Dunkel-Frame)

## 1.5.1

- Gemini-Label-Bug behoben: Gemini trunkiert kWh gelegentlich auf 4 Stellen
  ("3574" statt 35741) — 123 vergiftete Auto-Labels repariert (98 per
  Modell-Konsens) bzw. geloescht; valid_label() verwirft kWh < 10000
- Segmenttest wird lokal auch bei 8er-dominierten Fehl-Lesungen erkannt
  (halbiert die Gemini-Fallback-Calls auf Segmenttest-Frames)
- Modell neu trainiert (829 Samples, inkl. Abend-Evidence bis 16.07.)

## 1.5.0

- NUC trainiert nicht mehr: der Feedback-Sync sammelt und committet nur
  noch Evidence. Gemini-Labels sind fehlerbehaftet — trainiert wird erst
  nach Label-Audit (scripts/ocr/relabel.py: Vorzeichen-Korrektur per
  Geometrie, strittige W-Labels -> kWh-only)
- Option umbenannt: retrain_hour -> auto_train_hour (Default -1 = aus;
  alte Env-Variable RETRAIN_HOUR wird als Fallback noch gelesen)
- Modell neu trainiert auf auditiertem Datensatz (8 Vorzeichen korrigiert,
  26 strittige W-Labels neutralisiert)

## 1.4.15

- state_write_s-Option entfernt: das kWh-Feld wird immer bei Aenderung
  geschrieben (wenige Bytes, wenige Male am Tag) — ein Aus-Schalter
  schuf nur stale-State-Risiko

## 1.4.14

- state.json persistiert nur noch das kWh-Feld und nur bei Aenderung
  (wenige Winz-Writes/Tag, nie wieder stale Zusatz-Felder)
- Re-Baseline: Gemini-Cooldown resettet den Bestaetigungs-Zaehler nicht
  mehr — Heilung eines veralteten kWh-Stands greift im ersten freien Slot

## 1.4.13

- Retraining-Schwelle zaehlt jetzt den RUECKSTAND seit dem letzten
  Training (committeter Marker training-data/.trained-at) statt nur die
  Labels eines Sync-Laufs — vorher konnte sich der Rueckstand unsichtbar
  stapeln und das Modell wurde nie trainiert/gepusht. Erster Sync nach
  diesem Update trainiert sofort (Marker fehlt -> voller Rueckstand).

## 1.4.12

- Minus-Erkennung: Geometrie-Veto in eindeutigen Zonen (Masse nur im
  Mittelband = Minus, ratio>0.75 / <0.3), dazwischen kNN
- Label-Audit beim Training: W-Zeilen, deren Gemini-Label der Minus-
  Geometrie widerspricht (verschlucktes Vorzeichen!), fliegen aus dem
  Training — die Flip-Fehler waren zum Teil antrainierte Label-Fehler
- Modell auf auditiertem Datensatz neu trainiert

## 1.4.11

- Vorzeichen-Flip-Guard: Toleranz auf +-20% (min. 40 W) — faengt auch
  +350/-360-Flips mit Messrauschen dazwischen

## 1.4.10

- Vorzeichen-Flip-Guard: w-Lesungen mit gleichem Betrag und umgekehrtem
  Vorzeichen (+360/-360-Gezappel) werden verworfen; erst 4 konsistente
  Lesungen akzeptieren einen echten Nulldurchgang
- Feedback-Repo migriert sich selbst auf Blobless-Clone (kein manuelles
  Loeschen von /data/feedback-repo noetig)

## 1.4.9

- NUC-Runtime nutzt das git-gesyncte Modell aus dem Feedback-Checkout
  (`MODEL_FILE`) und laedt es bei Aenderung im laufenden Betrieb neu —
  Retraining wirkt sofort, nicht erst beim naechsten Release

## 1.4.8

- NUC-Clone als Blobless-Clone (`--filter=blob:none`): lokale Groesse bleibt
  ~konstant, History-Blobs liegen nur auf GitHub (bestehenden Clone einmal
  loeschen: Add-on stoppen, `/data/feedback-repo` entfernen, starten)
- Retrain-Commits enthalten nur noch EIN Modell (halbierte History-Rate);
  die Add-on-Kopie wird beim Release gebaut

## 1.4.7

- KRITISCH: training-data/ stand in .gitignore — Evidence wurde nie
  committet ("Push ok" ohne Commit), aber lokal geprunt. Gitignore
  bereinigt; Prune läuft jetzt nur noch, wenn training-data nachweislich
  vollständig committet ist. Unkommittete Evidence im /data-Checkout wird
  vom nächsten Sync-Lauf automatisch nachcommittet.

## 1.4.6

- Sync-Intervall default 300s (Commit-Hygiene: keine Mini-Commits alle 30s)

## 1.4.5

- OCR: Shift-Augmentierung — Ziffern generalisieren über alle LCD-Positionen
  (behebt 1→7-Fehllesungen nach Zähler-Rollover, z.B. 35710→35770)
- Feedback-Sync: nur Disagreements/Events + jedes 20. Routine-Sample werden
  committet; lokale Dateien werden erst nach erfolgreichem Push gelöscht
- Deploy-Key: nur noch `git_deploy_key_base64` (Mehrzeilen-Keys brechen im
  HA-Options-UI)
- Sync-Logs mit Zeitstempeln
- Modell als float16 (8,7 MB statt 15,7 MB)

## 1.4.4

- Deploy-Key als Base64-Feld für HAOS

## 1.4.2

- HAOS-nativer Feedback-Worker: Evidence → Git, Retraining, Modell-Push

## 1.4.1

- Positions-bewusstes OCR (Slot-Präferenz mit Fallback), Event-Outbox

## 1.4.0

- `interval_s` als Kommazahl (0,5-s-Takt), `state_write_s`-Schreibdrossel

## 1.3.0

- `log_level` (all/error/none), Samples & Retraining im Add-on default aus

## 1.2.1

- Add-on im Store unsichtbar: ungültiges watchdog-Feld entfernt, build.yaml

## 1.2.0

- Nächtliches Auto-Retraining mit Hot-Reload des Modells

## 1.1.0

- Erstes Add-on-Release: lokales OCR, Hybrid-Modus, Regler v3, MQTT-Discovery
