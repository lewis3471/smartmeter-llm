# Changelog

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
