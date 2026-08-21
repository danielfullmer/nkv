#!/usr/bin/env python3
"""Marginal cost of one lookup at 1k/50k keys (small/medium).

Same measurement style as bench.py: cold `nix eval --impure --raw`
process per run; the fixed Nix load floor is measured as `runs` empty
evals (same flags, no expression work); per method, work = total -
floor, paired by run index. One lookup per run: the smallest (sorted)
key of each dataset.

Writes bench_marginal.json:
  { <size>: { key, <method>: { total, work, all } } }
total/work = median (ms); all = totals of the runs (ms).

Usage: bench_marginal.py [runs]   (default 7)
"""
import json
import os
import statistics
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "bench_marginal.json")
SETS = [("small", "data/small.json", "data/small.nkv"),
        ("medium", "data/medium.json", "data/medium.nkv")]


def run_eval(expr, timeout=120):
    t0 = time.monotonic()
    p = subprocess.run(["nix", "eval", "--impure", "--raw", "--expr", expr],
                       capture_output=True, text=True, timeout=timeout)
    dt = (time.monotonic() - t0) * 1000
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:500])
    return dt, p.stdout


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    floor = [run_eval('""')[0] for _ in range(runs)]
    print(f"floor: min={min(floor):.1f} med={statistics.median(floor):.1f}")
    out = {}
    for name, jrel, trel in SETS:
        jpath = os.path.join(BASE, jrel)
        tpath = os.path.join(BASE, trel)
        with open(jpath, encoding="utf-8") as f:
            key = sorted(json.load(f))[0]
        q = json.dumps([key])
        exprs = {
            "fromJSON": ("let o = builtins.fromJSON (builtins.readFile %s); "
                         "qs = %s; "
                         "in builtins.toJSON (builtins.map (a: o.${a}) qs)"
                         % (json.dumps(jpath), q)),
            "nkv": ("let db = import %s %s; qs = %s; "
                    "in builtins.toJSON (builtins.map (a: db.get a) qs)"
                    % (json.dumps(os.path.join(BASE, "nkv.nix")),
                       json.dumps(tpath), q)),
        }
        row = {"key": key}
        for m, e in exprs.items():
            dts = [run_eval(e)[0] for _ in range(runs)]
            works = [dts[r] - floor[r] for r in range(runs)]
            row[m] = {"total": round(statistics.median(dts), 1),
                      "work": round(statistics.median(works), 1),
                      "all": [round(x, 1) for x in dts]}
            print(f"{name} {m}: med={row[m]['total']} work={row[m]['work']}",
                  flush=True)
        out[name] = row
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()