# Akku-Anbindung: JK-BMS + OpenDTU Fusion + Victron — Schritt für Schritt

Setup: JK-PB2A16S30P (V19) am Akku-Bus, Victron SmartSolar MPPT 150/45,
OpenDTU Fusion (ESP32-S3) mit OpenDTU-on-Battery-Firmware, HMS-2000-4T an
String 1+4. Unser Add-on regelt die Nulleinspeisung — OpenDTU-oB liefert
nur Monitoring (SoC, Ströme, MPPT-Daten) nach HA.

> **Sicherheit zuerst:** Am Akku-Bus können mehrere hundert Ampere fließen.
> Immer zuerst Sicherung ziehen / Trennschalter öffnen, Aderendhülsen
> verwenden, jede Litze mit dem Multimeter GEGENPRÜFEN, bevor sie an ein
> Board geht. Bei den VE.Direct-Kabeln sind die Farben NICHT verlässlich
> (Cross-over-Kabel: Rot kann GND sein!).

---

## 1. JK-BMS an den Akku (Leistungsseite)

1. **Balancer-Leitungen** (schwarzer Stecker, 16 Zellabgriffe + 1):
   Litze `0` (bzw. die erste) an **Zelle-1-Minus** (= Pack-Minus), dann
   aufsteigend je Litze an den Plus-Pol der Zelle 1, 2, 3, … Bei weniger
   als 16S bleiben die obersten Litzen frei — **von oben her** unbelegt
   lassen, nicht dazwischen. Stecker erst einstecken, wenn alle Litzen
   verschraubt und gemessen sind (jede Litze gegen Pack-Minus: muss
   n × Zellspannung zeigen).
2. **B-** (dickes Kabel, BMS-Anschluss „B-") an **Pack-Minus**.
3. **P-** (BMS „P-") ist der Lastausgang-Minus: Hieran kommen **Victron
   BATTERY-Minus** und (über den Precharge, s. Schritt 6) **HMS-Minus**.
   Niemals eine Last direkt an Pack-Minus — dann misst/schützt das BMS
   nichts.
4. **Plus** geht ungeschaltet vom Pack über die **Hauptsicherung** (nach
   Akku-Datenblatt, z. B. 100 A NH/Mega) zu Victron+ und HMS+.

## 2. JK-BMS Grundkonfiguration (App)

JK-App (Bluetooth) → Einstellungen, Passwort-Default `1234`:

- **Zellenzahl** exakt auf euren Pack setzen, **Kapazität** in Ah.
- Zell-Schutzgrenzen nach Chemie (LiFePO4: OVP 3,65 V, UVP 2,7 V;
  Li-Ion/NMC: OVP 4,2 V, UVP 3,0 V).
- Balance-Start ~3,4 V (LiFePO4), Balance-Strom moderat (0,5–1 A).
- **Entlade-/Lade-Überstrom** auf eure Verkabelung auslegen, nicht aufs
  BMS-Maximum.
- Prüfen, ob es unter den Entlade-Einstellungen eine
  **Precharge-/Soft-Start-Zeit** gibt (firmwareabhängig) → wenn ja: 2–3 s.
  Wenn nein: externer Precharge in Schritt 6 ist ohnehin eingeplant.

## 3. JK-BMS → OpenDTU Fusion (RS485)

Das Fusion-Board hat einen **eigenen RS485-Header mit fest verdrahtetem
Transceiver** — kein Zusatzmodul nötig.

**Am BMS:** Der **linke der beiden RJ45-Ports** (RS485-1). Pinbelegung
(RJ45-Pin-Zählung: Nase unten, Kontakte oben, Pin 1 links):

| RJ45-Pin (BMS, linker Port) | Signal    |
|-----------------------------|-----------|
| 1 und 8                     | RS485-B   |
| 2 und 7                     | RS485-A   |
| 3 und 6                     | GND       |

Praktisch: Patchkabel abschneiden, mit Multimeter/Aderfarben Pin 1, 2, 3
identifizieren (Standard T568B: Pin1 = weiß-orange, Pin2 = orange,
Pin3 = weiß-grün — trotzdem messen!).

**Am Fusion:** RS485-Header, beschriftet A / B / GND:

| BMS RJ45         | Fusion RS485-Header |
|------------------|---------------------|
| Pin 2 (RS485-A)  | A                   |
| Pin 1 (RS485-B)  | B                   |
| Pin 3 (GND)      | GND                 |

(Wenn später keine Daten kommen: zuerst A und B tauschen — die
Beschriftung A/B ist zwischen Herstellern notorisch uneinheitlich.)

**Im BMS konfigurieren:** JK-App → System → **UART1-Protokoll** auf
`000 – 4G-GPS Remote module Common protocol V4.2` stellen. (UART2/CAN
bleibt frei; DIP/Adresse auf Default 1 lassen.)

## 4. OpenDTU-on-Battery konfigurieren (BMS)

1. Firmware: Fusion muss **OpenDTU-on-Battery** (hoylabs) laufen, nicht
   das Standard-OpenDTU — sonst fehlen die Battery/Victron-Menüs.
2. **Einstellungen → Hardware (Device Manager) → Pin-Zuordnung**, dem
   aktiven Profil hinzufügen (GPIOs des fest verdrahteten Fusion-
   Transceivers):

   ```json
   "battery": {
       "rx": 16,
       "rxen": 15,
       "tx": 45,
       "txen": 46
   }
   ```

3. **Einstellungen → Batterie**: aktivieren, Provider
   **JK BMS**, Schnittstellen-Typ **RS485-Transceiver on MCU**,
   Polling-Intervall ~5 s. Speichern, Neustart.
4. Check: Live-Ansicht zeigt eine Batterie-Kachel mit SoC,
   Zellspannungen, Temperatur. Kommt nichts: A/B tauschen (s. o.), dann
   UART1-Protokoll im BMS prüfen.

## 5. Victron SmartSolar 150/45 → Fusion (VE.Direct)

Der VE.Direct-Port des MPPT ist eine **JST-PH-2.0-Buchse, 4-polig**:

| VE.Direct-Pin | Signal                      |
|---------------|-----------------------------|
| 1             | GND                         |
| 2             | RX (Eingang in den Victron) |
| 3             | **TX (Daten vom Victron)**  |
| 4             | 5 V — **NIEMALS anschließen** |

Wir lesen nur (Monitoring), also brauchen wir **nur Pin 1 (GND) und
Pin 3 (TX)**.

**Achtung Pegel:** VE.Direct sendet mit **5-V-Logik**, der ESP32-S3 kann
offiziell nur 3,3 V. Zwei saubere Wege:

- **Empfohlen:** das offizielle **Fusion CAN/Iso-Shield** aufstecken — es
  hat zwei galvanisch getrennte VE.Direct-Eingänge (ADUM1201). Victron-TX
  und GND an „Victron 1", Pin-Zuordnung im Profil:

  ```json
  "victron": { "rx": 10, "tx": 9 }
  ```

- **Ohne Shield:** einfacher Pegelwandler (bidirektionales
  MOSFET-Level-Shifter-Modul, HV-Seite 5 V, LV-Seite 3,3 V) zwischen
  Victron-TX und einem freien Fusion-GPIO, z. B. **GPIO 1 oder 2**
  (I2C-Header) — GND beider Seiten verbinden. Profil dann z. B.:

  ```json
  "victron": { "rx": 1, "tx": -1 }
  ```

Dann **Einstellungen → Victron (VE.Direct)** aktivieren → Live-Ansicht
zeigt die MPPT-Kachel (PV-Leistung, Ladestrom, Zustand).

Fertige VE.Direct-Kabel mit offenem Ende gibt es von Victron
(ASS030532xxx) — **Adern vor dem Anschluss durchmessen**, die Farben sind
je nach Kabelende vertauscht (Cross-over!).

## 6. Precharge + HMS an den Bus

1. In die **Plus-Leitung Bus → HMS**: Leistungswiderstand **33–47 Ω /
   ≥ 25 W** fest **parallel zum Hauptschütz/Trennschalter**.
2. Hauptschütz mit **Einschaltverzögerungs-Zeitrelais (3–5 s)** ansteuern,
   oder von Hand: Trennschalter 5 s nach Anlegen der Busspannung
   schließen. (Hat das JK-BMS die Precharge-Option aus Schritt 2, ersetzt
   sie das Ganze.)
3. HMS-Eingänge **String 1 + 4** an den Bus, jeweils über eine flinke
   **DC-Sicherung** passend zum Eingangsstrom (HMS-2000: max ~14 A pro
   Eingang → 15-A-Sicherung).

## 7. Zusammenspiel mit unserem Regler

1. **OpenDTU-oB: Dynamic Power Limiter AUS** (Einstellungen →
   Leistungsbegrenzer deaktivieren) — unser Add-on regelt das Limit,
   zwei Regler am selben Limit pendeln gegeneinander.
2. Add-on-Optionen (HA): `batt_strings: "1,4"`, `batt_low_v`/`batt_high_v`
   nach Chemie/Zellenzahl (Default 36/38), `batt_release_s: 300`.
3. In HA erscheinen „Akku-Spannung" und „Akku-Schutz aktiv" automatisch
   (MQTT-Discovery); SoC/Zellen liefert OpenDTU-oB zusätzlich über dessen
   eigene MQTT-Topics.

## 8. Inbetriebnahme-Checkliste

- [ ] Balancer-Stecker gemessen (aufsteigende Spannungen), dann gesteckt
- [ ] BMS-App zeigt alle Zellen plausibel
- [ ] UART1 = Protokoll 000 (V4.2), RS485 an Fusion, Batterie-Kachel in
      OpenDTU sichtbar
- [ ] VE.Direct nur GND+TX, über Isolator/Level-Shifter, MPPT-Kachel
      sichtbar
- [ ] DPL in OpenDTU-oB deaktiviert
- [ ] Precharge getestet: HMS zuschalten ohne Funken, startet von allein
- [ ] Add-on: `batt_strings` gesetzt, HA-Sensor „Akku-Spannung" liefert
- [ ] Hold-Test: `batt_low_v` testweise über die aktuelle Busspannung
      setzen → Limit fällt, „Akku-Schutz aktiv" = ON → Wert zurücksetzen

## Quellen

- OpenDTU-oB: JK-PB-Anbindung — https://opendtu-onbattery.net/hardware/jkbms/models_pb/
- OpenDTU-oB: VE.Direct-Verkabelung — https://opendtu-onbattery.net/hardware/vedirect/
- OpenDTU-oB: Device Profiles — https://opendtu-onbattery.net/firmware/device_profiles/
- Fusion CAN/Iso-Shield — https://github.com/markusdd/OpenDTUFusionDocs/blob/main/CANIso.md
- JK PB-Serie Handbuch — https://www.jkbms.com/wp-content/uploads/2024/06/JK-BMS-User-Manual-for-PB-series-jkbms.com_.pdf
