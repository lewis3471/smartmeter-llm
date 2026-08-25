#!/usr/bin/env python3
"""Eigenverbrauchs-Analyse aus der Regler-Telemetrie (training-data/control).

Beantwortet drei Fragen aus den eigenen Logs, ohne Zusatzmessung:

  1. WO geht Eigenverbrauch verloren? Zeit und Energie, aufgeteilt nach
     Regler-Zustand (schlafend / am Floor / regelnd).
  2. WELCHE Arbeitspunkte kann der Inverter ueberhaupt? Landeplatz-Tabelle:
     zu welchem Kommando liefert der HMS hinterher welche AC-Leistung.
  3. WAS BRINGT die Arbeitspunkt-Leiter? Monte-Carlo ueber die gemessene
     Trefferverteilung, inklusive Fehlschlaegen und Sperrzeiten.

    python3 scripts/analyze_selfuse.py --days 7
    python3 scripts/analyze_selfuse.py --dir /data/samples/control --json

WICHTIG zur Datenlage: `ctl_tick` schreibt nur rund um Limit-Befehle
(Ringpuffer + 45 s danach), nicht durchgehend. Zeitanteile werden deshalb
aus der LUECKENLOSEN Kette der Limit-Ereignisse rekonstruiert, Leistungen
aus den Ticks innerhalb des jeweiligen Abschnitts. Abschnitte ohne Ticks
oder laenger als --max-seg (Add-on-Ausfall) werden ausgewiesen, nicht
stillschweigend hochgerechnet.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import glob
import json
import os
import random
import statistics
import sys

W2KWH = 1 / 3600 / 1000


def load(directory: str, days: float) -> tuple[list[dict], list[dict]]:
    ev: list[dict] = []
    for f in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ev") in ("tick", "limit"):
                    ev.append(r)
    if not ev:
        sys.exit(f"keine Telemetrie in {directory}")
    ev.sort(key=lambda r: r["t"])
    cut = ev[-1]["t"] - days * 86400
    ev = [r for r in ev if r["t"] >= cut]
    return ([r for r in ev if r["ev"] == "tick"],
            [r for r in ev if r["ev"] == "limit"])


def state_of(limit: int | None, floor: int, sleep_max: int) -> str:
    if limit is None:
        return "?"
    if limit <= sleep_max:
        return "schlaf"
    if floor - 30 <= limit <= floor + 30:
        return "floor"
    return "regelnd"


def section_1(ticks, lims, args):
    tt = [r["t"] for r in ticks]
    agg = collections.Counter()
    segs = []
    lang = []
    for a, b in zip(lims, lims[1:]):
        dur = b["t"] - a["t"]
        if dur <= 0:
            continue
        st = state_of(a["to"], args.floor, args.sleep_max)
        # Nur das ZUVERLAESSIG geloggte Fenster direkt nach dem Befehl
        # verwenden (ctl_tick schreibt 45 s lang durch). Die paar Ticks am
        # ENDE eines langen Abschnitts stammen aus dem Ringpuffer, also aus
        # dem Moment, in dem die Last wieder stieg — sie als Mittelwert des
        # Abschnitts zu nehmen, verzerrt die Bilanz nach oben.
        i = bisect.bisect_left(tt, a["t"] + 10)
        j = bisect.bisect_right(tt, min(b["t"], a["t"] + 45))
        inner = ticks[i:j]
        if not inner:
            agg[st + "_blind_h"] += dur / 3600
            continue
        g = statistics.median(r["grid"] for r in inner)
        pv = statistics.median(r.get("pv", 0.0) or 0.0 for r in inner)
        if dur > args.max_seg:
            # Lange Abschnitte haben nur den Ringpuffer VOR dem naechsten
            # Befehl als Beleg — also genau den Moment, in dem die Last
            # wieder stieg. Ihre Netzleistung ist damit nach oben verzerrt
            # und wird getrennt ausgewiesen, nicht in die Bilanz gemischt.
            lang.append((st, dur, g, pv, len(inner)))
            continue
        agg[st + "_h"] += dur / 3600
        agg[st + "_gridkwh"] += g * dur * W2KWH
        agg[st + "_pvkwh"] += pv * dur * W2KWH
        segs.append((st, dur, g, pv))

    print("== 1. Wo bleibt der Eigenverbrauch? ==")
    print(f"{'Zustand':10s} {'Zeit h':>8s} {'Netz kWh':>9s} {'Mittel W':>9s} "
          f"{'HMS kWh':>8s} {'blind h':>8s}")
    for st in ("schlaf", "floor", "regelnd"):
        h = agg[st + "_h"]
        if not h:
            continue
        print(f"{st:10s} {h:8.1f} {agg[st + '_gridkwh']:+9.2f} "
              f"{agg[st + '_gridkwh'] * 1000 / h:+9.0f} {agg[st + '_pvkwh']:8.2f} "
              f"{agg[st + '_blind_h']:8.1f}")
    if lang:
        print(f"\n  Lange Abschnitte (> {int(args.max_seg / 60)} min, ein einziger "
              f"Zustand am Stueck):")
        byst = collections.Counter()
        for st, dur, g, pv, n in lang:
            byst[st] += dur / 3600
            byst[st + "_grid"] += g * dur * W2KWH
        for st in ("schlaf", "floor", "regelnd"):
            if byst[st]:
                print(f"    {st:8s} {byst[st]:6.1f} h, Netzleistung am Ende der "
                      f"Abschnitte im Mittel {byst[st + '_grid'] * 1000 / byst[st]:+.0f} W")
        print("    (nur der Ringpuffer vor dem naechsten Befehl belegt sie — "
              "die Zahl ist eine Obergrenze, kein Mittelwert)")

    # Der eigentliche Befund: WIE VIEL Zeit verbringt der Regler in einem
    # Zustand, den ein niedriger Arbeitspunkt besser bedient haette?
    tage = max((lims[-1]["t"] - lims[0]["t"]) / 86400, 1e-9)
    kauf = sum(max(g, 0) * d * W2KWH for st, d, g, _ in segs if st == "schlaf")
    schenk = sum(max(-g, 0) * d * W2KWH for st, d, g, _ in segs if st == "floor")
    print(f"\n  gekauft, waehrend der Inverter schlief : {kauf:6.2f} kWh "
          f"({kauf / tage:.2f} kWh/Tag)")
    print(f"  verschenkt, waehrend er am Floor stand : {schenk:6.2f} kWh "
          f"({schenk / tage:.2f} kWh/Tag)")
    unten, oben = args.point_ac / 2, (args.point_ac + args.floor - 5) / 2
    band = collections.Counter()
    for st, d, g, pv in segs:
        if st not in ("schlaf", "floor"):
            continue
        bedarf = g + pv
        key = ("Fenster" if unten <= bedarf <= oben else
               "zu klein" if bedarf < unten else "zu gross")
        band[key] += d / 3600
        if key == "Fenster":
            band["luecke_kwh"] += abs(bedarf - (0.0 if st == "schlaf"
                                                else args.floor - 5.0)) \
                * d * W2KWH
    ges = sum(band[k] for k in ("Fenster", "zu klein", "zu gross")) or 1e-9
    print(f"\n  Bedarf in den Schlaf-/Floor-Abschnitten (belegte Fenster, "
          f"{ges:.0f} h):")
    print(f"    < {unten:.0f} W (Schlafen richtig)     : {band['zu klein']:5.1f} h "
          f"({band['zu klein'] / ges:4.0%})")
    print(f"    {unten:.0f}-{oben:.0f} W (Arbeitspunkt waere besser): "
          f"{band['Fenster']:5.1f} h ({band['Fenster'] / ges:4.0%})")
    print(f"    > {oben:.0f} W (Floor richtig)          : {band['zu gross']:5.1f} h "
          f"({band['zu gross'] / ges:4.0%})")
    print(f"  => im Fenster falsch bedient: {band['luecke_kwh']:.2f} kWh "
          f"({band['luecke_kwh'] / tage:.2f} kWh/Tag)")
    return segs, [(st, d, g, pv) for st, d, g, pv, _ in lang], tage


def section_2(ticks, lims, args):
    """Landeplatz-Tabelle: Kommando -> tatsaechliche AC-Leistung."""
    tt = [r["t"] for r in ticks]
    lt = [r["t"] for r in lims]
    print("\n== 2. Welche Arbeitspunkte kann der Inverter? ==")
    print(f"(Fenster {args.settle_from}-{args.settle_to} s nach dem Befehl, "
          f"nur Befehle die >= {args.settle_to} s stehen blieben)")
    zones = [(0, 60), (60, 120), (120, 210), (210, 350), (350, 460), (460, 10 ** 9)]
    TOP_BUCKET = 1000   # alles darueber faellt in einen offenen Sammel-Eimer
    b = collections.defaultdict(list)
    for e in lims:
        i = bisect.bisect_right(lt, e["t"])
        if i < len(lims) and lims[i]["t"] - e["t"] < args.settle_to:
            continue
        i = bisect.bisect_left(tt, e["t"] + args.settle_from)
        j = bisect.bisect_right(tt, e["t"] + args.settle_to)
        w = [ticks[k].get("pv") for k in range(i, j)
             if ticks[k].get("pv") is not None]
        if len(w) < 5:
            continue
        b[min(e["to"], TOP_BUCKET) // 50 * 50].append(statistics.median(w))
    head = "".join(f"{lo}-{hi if hi < 10 ** 9 else '':>4}".rjust(10)
                   for lo, hi in zones)
    print(f"{'Befehl':>10s} {'n':>5s}" + head + f"{'Median':>8s}")
    table = {}
    for k in sorted(b):
        v = b[k]
        if len(v) < args.min_n:
            continue
        cells = "".join(f"{sum(1 for x in v if lo <= x < hi) / len(v):9.0%} "
                        for lo, hi in zones)
        label = f"{k:6d}+   " if k >= TOP_BUCKET else f"{k:6d}-{k + 49:3d}"
        print(f"{label} {len(v):5d} " + cells +
              f"{statistics.median(v):8.0f}")
        table[k] = v
    return table


def section_3(segs, lang, table, tage, args):
    """Monte-Carlo: was haette die Leiter in denselben Abschnitten gebracht?"""
    print("\n== 3. Was bringt die Arbeitspunkt-Leiter? ==")
    cmd_bucket = args.point_cmd // 50 * 50
    landings = table.get(cmd_bucket)
    if not landings and args.p_hit is None:
        print(f"  keine Messdaten fuer Befehl ~{args.point_cmd} W in diesem "
              f"Zeitraum — mit --days ueber die ganze Historie rechnen, "
              f"--p-hit setzen oder scripts/probe_operating_points.py laufen "
              f"lassen")
        return
    if landings:
        hit = [x for x in landings if abs(x - args.point_ac) <= args.tol]
        p_hit = len(hit) / len(landings)
        ac_hit = statistics.median(hit) if hit else args.point_ac
    else:
        p_hit, ac_hit = args.p_hit, args.point_ac
    if args.p_hit is not None:
        p_hit = args.p_hit
    print(f"  Befehl {args.point_cmd} W: trifft {p_hit:.0%} der Versuche das "
          f"{args.point_ac}-W-Plateau (dann im Mittel {ac_hit:.0f} W), "
          f"sonst faellt er weg")
    # Der Arbeitsbereich des Punkts folgt direkt aus den Kosten:
    # er schlaegt den Schlaf ab Bedarf > ac/2 und den Floor bis
    # Bedarf < (ac + floor_ac)/2.
    floor_ac = args.floor - 5.0
    unten, oben = args.point_ac / 2, (args.point_ac + floor_ac) / 2
    print(f"  Er lohnt sich fuer einen Bedarf zwischen {unten:.0f} und "
          f"{oben:.0f} W (darunter ist Schlafen billiger, darueber der Floor)")

    def kosten(ac, bedarf):
        return max(0.0, bedarf - ac) + max(0.0, ac - bedarf)

    rng = random.Random(20260824)
    gain = []
    for _ in range(args.runs):
        kauf = schenk = 0.0
        gesperrt_bis = 0.0
        uhr = 0.0
        for st, dur, g, pv in segs:
            uhr += dur
            if st not in ("schlaf", "floor"):
                continue
            bedarf = g + pv                  # was das Haus vom HMS wollte
            alt = (max(g, 0) * dur * W2KWH, max(-g, 0) * dur * W2KWH)
            wahl = min((0.0, args.point_ac, floor_ac),
                       key=lambda ac: kosten(ac, bedarf))
            if wahl != args.point_ac or uhr < gesperrt_bis:
                kauf += alt[0]
                schenk += alt[1]
                continue
            ok = rng.random() < p_hit or rng.random() < p_hit  # zwei Anlaeufe
            if ok:
                rest = bedarf - ac_hit
            else:
                gesperrt_bis = uhr + args.cooldown
                rest = bedarf                # Fallback: Inverter bleibt aus
            kauf += max(rest, 0) * dur * W2KWH
            schenk += max(-rest, 0) * dur * W2KWH
        gain.append((kauf, schenk))
    k = statistics.mean(x[0] for x in gain)
    s = statistics.mean(x[1] for x in gain)
    kauf0 = sum(max(g, 0) * d * W2KWH for st, d, g, _ in segs
                if st in ("schlaf", "floor"))
    schenk0 = sum(max(-g, 0) * d * W2KWH for st, d, g, _ in segs
                  if st in ("schlaf", "floor"))
    print(f"  heute     : {kauf0:6.2f} kWh gekauft, {schenk0:6.2f} kWh verschenkt")
    print(f"  mit Leiter: {k:6.2f} kWh gekauft, {s:6.2f} kWh verschenkt "
          f"({args.runs} Simulationen)")
    print(f"  Ersparnis : {kauf0 - k:+.2f} kWh Netzbezug, "
          f"{schenk0 - s:+.2f} kWh Akku-Energie "
          f"=> {((kauf0 - k) + (schenk0 - s)) / tage:.2f} kWh/Tag")
    return (kauf0 - k) + (schenk0 - s)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default="training-data/control",
                   help="Verzeichnis mit den JSONL-Dateien")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--floor", type=int, default=430)
    p.add_argument("--sleep-max", type=int, default=60)
    p.add_argument("--max-seg", type=float, default=3600,
                   help="Abschnitte laenger als das gelten als Ausfall")
    p.add_argument("--settle-from", type=float, default=25)
    p.add_argument("--settle-to", type=float, default=60)
    p.add_argument("--min-n", type=int, default=10)
    p.add_argument("--point-cmd", type=int, default=300,
                   help="Kandidat fuer den niedrigen Arbeitspunkt (Befehl)")
    p.add_argument("--point-ac", type=float, default=160,
                   help="erwartete AC-Leistung dieses Punkts")
    p.add_argument("--tol", type=float, default=45)
    p.add_argument("--p-hit", type=float, default=None,
                   help="Trefferwahrscheinlichkeit des Punkts erzwingen "
                        "(sonst aus der Landeplatz-Tabelle)")
    p.add_argument("--cooldown", type=float, default=900)
    p.add_argument("--runs", type=int, default=200)
    args = p.parse_args()

    ticks, lims = load(args.dir, args.days)
    span = (lims[-1]["t"] - lims[0]["t"]) / 86400 if len(lims) > 1 else 0
    print(f"{len(ticks)} Ticks, {len(lims)} Limit-Befehle ueber {span:.1f} Tage "
          f"({dt.datetime.fromtimestamp(lims[0]['t']):%d.%m. %H:%M} - "
          f"{dt.datetime.fromtimestamp(lims[-1]['t']):%d.%m. %H:%M})")
    print(f"Befehlsrate: {len(lims) / max(span, 1e-9):.0f}/Tag\n")
    segs, lang, tage = section_1(ticks, lims, args)
    table = section_2(ticks, lims, args)
    g1 = section_3(segs, lang, table, tage, args)
    if lang and g1 is not None:
        print("\n  Dieselbe Rechnung MIT den langen Abschnitten (deren "
              "Netzleistung nur\n  am Ende belegt ist — das ist die "
              "optimistische Grenze des Korridors):")
        g2 = section_3(segs + lang, [], table, tage, args)
        if g2 is not None:
            print(f"\n  KORRIDOR: {g1 / tage:.2f} - {g2 / tage:.2f} kWh/Tag "
                  f"mehr Eigenverbrauch")


if __name__ == "__main__":
    main()
