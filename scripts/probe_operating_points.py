#!/usr/bin/env python3
"""Arbeitspunkte des Wechselrichters ausmessen (Kalibrierung fuer LOW_POINTS).

Die Regel "unter 430 W folgt der HMS nicht" stammt aus Kommandos, die der
Regler nebenbei erzeugt hat — nie aus einem Versuch, der einen niedrigen
Wert auch STEHEN liess. Genau das macht dieses Skript: es faehrt eine
Treppe von Limits ab, laesst jede Stufe mehrere Minuten stehen und misst,
wo der Inverter wirklich landet.

    python3 scripts/probe_operating_points.py --yes
    python3 scripts/probe_operating_points.py --yes --steps 250,300,350,400 \
        --hold 300 --repeat 3

Ergebnis ist eine Tabelle + die fertige Zeile fuer die Add-on-Option
`low_points`. Am besten NACHTS laufen lassen: konstante Last, keine Wolken,
und der Fehlbetrag geht in den Akku statt in eine Fehlmessung.

Sicherheit: das Add-on regelt waehrenddessen weiter und wuerde gegen die
Messung arbeiten — es MUSS vorher gestoppt werden (HA: Add-on stoppen).
Das Skript prueft das und bricht ab, wenn ihm jemand ins Limit funkt.
Am Ende (auch bei Strg-C) wird das urspruengliche Limit wiederhergestellt.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import sys
import time

import requests

OPENDTU_URL = os.environ.get("OPENDTU_URL", "").rstrip("/")
AUTH = (os.environ.get("OPENDTU_USER", "admin"), os.environ.get("OPENDTU_PASS", ""))
SERIAL = os.environ.get("INVERTER_SERIAL", "")


def livedata() -> tuple[float, float, dict]:
    """(AC-Leistung, min. DC-Spannung, Rohdaten)."""
    url = f"{OPENDTU_URL}/api/livedata/status?inv={SERIAL}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()
    inv = d["inverters"][0]
    try:
        ac = float(d["total"]["Power"]["v"])
    except (KeyError, IndexError):
        ac = float(inv["AC"]["0"]["Power"]["v"])
    volts = []
    for ch in inv.get("DC", {}).values():
        try:
            v = float(ch["Voltage"]["v"])
        except (KeyError, TypeError, ValueError):
            continue
        if v > 5.0:
            volts.append(v)
    return ac, (min(volts) if volts else 0.0), inv


def set_limit(watts: int) -> None:
    payload = {"serial": SERIAL, "limit_type": 0, "limit_value": watts}
    r = requests.post(f"{OPENDTU_URL}/api/limit/config", auth=AUTH,
                      data={"data": json.dumps(payload)}, timeout=10)
    r.raise_for_status()


def read_limit() -> int | None:
    try:
        r = requests.get(f"{OPENDTU_URL}/api/limit/status", timeout=10)
        r.raise_for_status()
        d = r.json().get(SERIAL, {})
        v = d.get("limit_absolute")
        return int(v) if v is not None else None
    except Exception:
        return None


def messung(cmd: int, hold: float, settle: float, log) -> dict:
    """Eine Stufe: Limit setzen, einschwingen lassen, dann mitteln."""
    set_limit(cmd)
    t0 = time.time()
    proben: list[tuple[float, float, float]] = []
    while time.time() - t0 < hold:
        time.sleep(2.0)
        try:
            ac, v, _ = livedata()
        except Exception as e:
            print(f"    OpenDTU-Fehler: {e}", file=sys.stderr)
            continue
        proben.append((time.time() - t0, ac, v))
        log({"t": round(time.time(), 2), "ev": "probe", "cmd": cmd,
             "dt": round(time.time() - t0, 1), "ac": round(ac, 1),
             "v": round(v, 2)})
    ist = read_limit()
    if ist is not None and abs(ist - cmd) > 5:
        raise RuntimeError(
            f"Limit steht auf {ist} W statt {cmd} W — es regelt noch jemand "
            f"mit (Add-on oder OpenDTU-DPL). Erst stoppen, dann messen.")
    stabil = [ac for dt_, ac, _ in proben if dt_ >= settle]
    if not stabil:
        return {"cmd": cmd, "n": 0}
    return {
        "cmd": cmd,
        "n": len(stabil),
        "ac": round(statistics.median(stabil), 1),
        "min": round(min(stabil), 1),
        "max": round(max(stabil), 1),
        "sd": round(statistics.pstdev(stabil), 1) if len(stabil) > 1 else 0.0,
        "v": round(statistics.median(v for _, _, v in proben), 2),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--steps", default="50,150,200,250,300,350,400,430,500",
                   help="Limit-Treppe in Watt")
    p.add_argument("--hold", type=float, default=240,
                   help="Sekunden pro Stufe (Plateaus brauchen Minuten)")
    p.add_argument("--settle", type=float, default=60,
                   help="Sekunden, die vor der Mittelung verworfen werden")
    p.add_argument("--repeat", type=int, default=2,
                   help="Durchlaeufe — die Treffer QUOTE ist die eigentliche "
                        "Zahl, nicht der Einzelwert")
    p.add_argument("--from-below", action="store_true",
                   help="vor jeder Stufe erst schlafen legen (misst, ob der "
                        "Anfahrweg den Landeplatz bestimmt)")
    p.add_argument("--out", default="", help="JSONL-Protokoll (optional)")
    p.add_argument("--yes", action="store_true",
                   help="Bestaetigung: Add-on ist gestoppt, Messung darf laufen")
    args = p.parse_args()

    if not OPENDTU_URL or not SERIAL:
        print("OPENDTU_URL und INVERTER_SERIAL muessen gesetzt sein "
              "(z.B. `set -a; . .env; set +a`)", file=sys.stderr)
        return 2
    if not args.yes:
        print(__doc__)
        print("Ohne --yes wird nichts gesendet.")
        return 1

    fh = open(args.out, "a") if args.out else None

    def log(rec):
        if fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()

    try:
        ac0, v0, _ = livedata()
    except Exception as e:
        print(f"OpenDTU nicht erreichbar ({e}) — Messung abgebrochen",
              file=sys.stderr)
        return 2
    vorher = read_limit()
    print(f"Inverter liefert gerade {ac0:.0f} W bei {v0:.1f} V Bus.")
    print(f"Limit vorher: {vorher} W — wird am Ende wiederhergestellt")

    def restore(*_):
        if vorher:
            try:
                set_limit(int(vorher))
                print(f"\nLimit auf {vorher} W zurueckgesetzt.")
            except Exception as e:
                print(f"\nLimit-Restore fehlgeschlagen: {e}", file=sys.stderr)
        if fh:
            fh.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, restore)
    signal.signal(signal.SIGTERM, restore)

    steps = [int(s) for s in args.steps.split(",") if s.strip()]
    dauer = len(steps) * args.repeat * (args.hold + (30 if args.from_below else 0))
    print(f"{len(steps)} Stufen x {args.repeat} Durchlaeufe x {args.hold:.0f}s "
          f"= ~{dauer / 60:.0f} min\n")

    ergebnisse: list[dict] = []
    try:
        for runde in range(args.repeat):
            for cmd in steps:
                if args.from_below and cmd > 50:
                    set_limit(50)
                    time.sleep(30)
                try:
                    r = messung(cmd, args.hold, args.settle, log)
                except Exception as e:
                    print(f"  Befehl {cmd:4d} W: Fehler {e}", file=sys.stderr)
                    continue
                r["runde"] = runde + 1
                ergebnisse.append(r)
                print(f"  Befehl {cmd:4d} W (Runde {runde + 1}) -> "
                      f"AC {r.get('ac', float('nan')):6.1f} W "
                      f"(min {r.get('min', 0):.0f} / max {r.get('max', 0):.0f}, "
                      f"sd {r.get('sd', 0):.1f}, Bus {r.get('v', 0):.1f} V)")
    finally:
        if vorher:
            try:
                set_limit(int(vorher))
            except Exception:
                pass

    print("\n== Ergebnis ==")
    print(f"{'Befehl':>7s} {'AC Median':>10s} {'Streuung':>9s} {'Runden':>7s} "
          f"{'stabil?':>8s}")
    punkte = []
    for cmd in steps:
        rs = [r for r in ergebnisse if r["cmd"] == cmd and r.get("n")]
        if not rs:
            print(f"{cmd:7d}        keine Messung")
            continue
        acs = [r["ac"] for r in rs]
        med = statistics.median(acs)
        spread = max(acs) - min(acs)
        stabil = spread <= 25 and max(r["sd"] for r in rs) <= 15
        print(f"{cmd:7d} {med:10.1f} {spread:9.1f} {len(rs):7d} "
              f"{'ja' if stabil else 'nein':>8s}")
        if stabil and 50 < cmd and med > 60:
            punkte.append((cmd, int(round(med))))

    # Nur Punkte behalten, die sich in der gelieferten Leistung wirklich
    # unterscheiden — zwei Befehle auf denselben Attraktor sind ein Punkt.
    gefiltert: list[tuple[int, int]] = []
    for cmd, ac in punkte:
        if gefiltert and abs(gefiltert[-1][1] - ac) < 40:
            continue
        gefiltert.append((cmd, ac))
    print("\nVorschlag fuer die Add-on-Option `low_points` "
          "(nur Punkte unterhalb des Sustain-Floors eintragen):")
    print("  low_points: \"" +
          ",".join(f"{c}:{a}" for c, a in gefiltert) + "\"")
    if fh:
        fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
