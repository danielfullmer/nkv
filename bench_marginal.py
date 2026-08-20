#!/usr/bin/env python3
"""Marginal per-lookup cost: slope of eval time vs number of in-process lookups.

For n in N_LIST, time one `nix eval` that performs exactly n lookups (file
already loaded once inside the eval).  Least-squares slope over (n, time)
gives the marginal cost per lookup, free of load/startup cost.
"""
import json
import os
import subprocess
import time
def lsq(xs, ys):
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    intercept = (sy - slope * sx) / n
    return slope, intercept

BASE = os.path.dirname(os.path.abspath(__file__))
KV = f"{BASE}/kv.nix"
N_LIST = [10, 50, 100, 200, 400]
REPS = 5
SIZES = ["small", "medium", "large"]


def nix_eval(expr):
    args = ["nix", "eval", "--impure", "--expr", expr]
    t0 = time.perf_counter()
    r = subprocess.run(args, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:3000])
    return dt, r.stdout


def main():
    out = {}
    for size in SIZES:
        J = f"{BASE}/data/{size}.json"
        N = f"{BASE}/data/{size}.nfd"
        obj = json.load(open(J))
        names = list(obj)
        out[size] = {}
        for method in ["fromJSON", "kvl"]:
            xs, ys = [], []
            for n in N_LIST:
                keys = names[:n]
                keylist = "[" + " ".join(f'"{k}"' for k in keys) + "]"
                exp = sum(len(obj[k]) for k in keys)
                if method == "fromJSON":
                    e = (
                        f'let j = builtins.fromJSON (builtins.readFile {J}); '
                        f"in builtins.foldl' (acc: k: acc + builtins.stringLength "
                        f'(j."${{k}}")) 0 {keylist}'
                    )
                else:
                    e = (
                        f'let db = (import {KV}) {N}; '
                        f"in builtins.foldl' (acc: k: acc + builtins.stringLength "
                        f"(db.get k)) 0 {keylist}"
                    )
                ts = []
                for _ in range(REPS):
                    dt, o = nix_eval(e)
                    assert int(o.strip()) == exp, (method, n, o)
                    ts.append(dt)
                xs.append(n)
                ys.append(min(ts))  # min over reps: least noise
                print(f"{size:7s} {method:9s} n={n:4d} min={min(ts)*1000:8.1f} ms", flush=True)
            slope, intercept = lsq(xs, ys)
            out[size][method] = {
                "per_lookup_ms": round(slope * 1000, 4),
                "load_ms": round(intercept * 1000, 1),
                "points": list(zip(xs, [round(t * 1000, 1) for t in ys])),
            }
    with open(f"{BASE}/bench_marginal.json", "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()