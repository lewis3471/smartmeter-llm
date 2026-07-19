#!/usr/bin/env python3
"""FOPDT-Analyse der HMS-Limit-Sprungantwort aus den Regler-Telemetrie-Logs.

Fuer jeden isolierten Limit-Sprung (kein weiterer Send im Fenster) wird die
Inverter-AC-Leistung (pv) als Sprungantwort gefittet:

    y(t) = y0                                   fuer t <= theta
    y(t) = y0 + K * (1 - exp(-(t - theta)/tau)) fuer t >  theta

K = tatsaechliche Leistungsaenderung (aus dem Endwert, nicht dem Limit —
der HMS liefert ja evtl. weniger als angefragt). theta = Totzeit,
tau = Zeitkonstante; Grid-Search minimiert SSE, R^2 pro Sprung.

Empfehlung am Ende: LATENCY_S ~= median(theta) + 2*median(tau).

Aufruf:  analyze_latency.py [training-data/control]
"""

import json
import math
import sys
from pathlib import Path


def load(dirp: Path) -> list[dict]:
    recs = []
    for f in sorted(dirp.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass
    return sorted(recs, key=lambda r: r["t"])


def isolated_steps(recs, quiet_s=25.0, window_s=40.0):
    """(send, pre_ticks, post_ticks) fuer Sends ohne Folge-Send im Fenster."""
    sends = [r for r in recs if r["ev"] == "limit"]
    ticks = [r for r in recs if r["ev"] == "tick"]
    for i, s in enumerate(sends):
        nxt = sends[i + 1]["t"] if i + 1 < len(sends) else float("inf")
        if nxt - s["t"] < quiet_s:
            continue
        hi = min(s["t"] + window_s, nxt)
        w = [t for t in ticks if s["t"] - 10 <= t["t"] <= hi]
        pre = [t for t in w if t["t"] < s["t"]]
        post = [t for t in w if t["t"] >= s["t"]]
        if len(pre) >= 3 and len(post) >= 8:
            yield s, pre, post


def fit_step(s, pre, post):
    y0 = sorted(t["pv"] for t in pre)[len(pre) // 2]  # Median-Vorwert
    tail = [t["pv"] for t in post if t["t"] - s["t"] > 25]
    if not tail:
        return None
    K = sum(tail) / len(tail) - y0
    if abs(K) < 30:  # zu kleiner Sprung — im Rauschen nicht fitbar
        return None
    ybar = sum(t["pv"] for t in post) / len(post)
    sst = sum((t["pv"] - ybar) ** 2 for t in post) or 1e-9
    best = None
    for th10 in range(0, 200):          # theta: 0 .. 20 s
        th = th10 / 10
        for tau10 in range(5, 150, 2):  # tau: 0.5 .. 15 s
            tau = tau10 / 10
            sse = 0.0
            for t in post:
                dt = t["t"] - s["t"]
                yhat = y0 if dt <= th else \
                    y0 + K * (1 - math.exp(-(dt - th) / tau))
                sse += (t["pv"] - yhat) ** 2
            if best is None or sse < best[0]:
                best = (sse, th, tau)
    return {"K": round(K), "theta": best[1], "tau": best[2],
            "r2": round(1 - best[0] / sst, 3), "tag": s.get("tag", "?")}


def median(xs):
    return sorted(xs)[len(xs) // 2]


def main():
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "training-data/control")
    recs = load(d)
    fits = [f for f in (fit_step(*st) for st in isolated_steps(recs))
            if f and f["r2"] >= 0.5]
    n_all = sum(1 for _ in isolated_steps(recs))
    if not fits:
        print(f"Keine fitbaren Spruenge ({n_all} isolierte Sends gefunden — "
              "Logs zu kurz oder Spruenge zu klein)")
        return
    print(f"{len(fits)}/{n_all} isolierte Spruenge gefittet "
          f"(R2-Median {median([f['r2'] for f in fits]):.3f})")
    for name, grp in (("hoch  ", [f for f in fits if f["K"] > 0]),
                      ("runter", [f for f in fits if f["K"] < 0])):
        if grp:
            print(f"  {name}: n={len(grp):3d}  "
                  f"theta={median([f['theta'] for f in grp]):4.1f}s  "
                  f"tau={median([f['tau'] for f in grp]):4.1f}s  "
                  f"R2={median([f['r2'] for f in grp]):.3f}")
    th = median([f["theta"] for f in fits])
    ta = median([f["tau"] for f in fits])
    print(f"\nEmpfehlung: LATENCY_S ≈ {th + 2 * ta:.0f}  "
          f"(theta {th:.1f} + 2·tau {ta:.1f})")


if __name__ == "__main__":
    main()
