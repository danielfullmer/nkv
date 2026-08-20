#!/usr/bin/env python3
"""Benchmark NFK lookups (kv.nix) against builtins.fromJSON attrset lookups.

Scenarios (per dataset size):
  cold_present  K fresh `nix eval` runs, each doing exactly one lookup of a
                present key.  This is the realistic case: every `nix eval`
                is a cold process that must load its data source.
  cold_miss     same, but for a missing key (hasAttr vs db.has).
  warm_200      single `nix eval` performing 200 lookups in-process
                (literal key list shared by both methods, so each method
                pays only its own load cost + 200 lookups).
  floors        nix startup floor / readFile floor / fromJSON-parse floor.

Every scenario asserts output correctness against the JSON source.
"""
import json
import os
import statistics
import subprocess
import time

BASE = os.path.dirname(os.path.abspath(__file__))
KV = f"{BASE}/kv.nix"
SIZES = ["small", "medium", "large"]
COLD_REPS = 15
WARM_REPS = 3
WARM_KEYS = 200
MISS_KEY = "zzz_missing_key_zzz"


def nix_eval(expr, raw=False):
    args = ["nix", "eval", "--impure"]
    if raw:
        args.append("--raw")
    args += ["--expr", expr]
    t0 = time.perf_counter()
    r = subprocess.run(args, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError(f"nix eval failed:\n{r.stderr[:3000]}")
    return dt, r.stdout


def timeit(expr, reps, raw=False):
    ts, outs = [], []
    for _ in range(reps):
        dt, out = nix_eval(expr, raw)
        ts.append(dt)
        outs.append(out)
    return ts, outs


def stats(ts):
    s = sorted(ts)
    n = len(s)
    return {
        "min_ms": round(s[0] * 1000, 1),
        "med_ms": round(statistics.median(s) * 1000, 1),
        "mean_ms": round(statistics.fmean(s) * 1000, 1),
        "max_ms": round(s[-1] * 1000, 1),
    }


def main():
    # one-time startup floor (no file access at all)
    ts, _ = timeit('"hello"', 5)
    startup = stats(ts)

    results = {"startup_ms": startup}
    for size in SIZES:
        J = f"{BASE}/data/{size}.json"
        N = f"{BASE}/data/{size}.nfd"
        obj = json.load(open(J))
        names = list(obj)
        key0 = names[0]
        expected = obj[key0]
        assert MISS_KEY not in obj
        warm_keys = names[:WARM_KEYS]
        keylist = "[" + " ".join(f'"{k}"' for k in warm_keys) + "]"
        exp_sum = sum(len(obj[k]) for k in warm_keys)

        r = {}

        # ---- 1. cold single lookup, present key --------------------------
        e_fj = f'(builtins.fromJSON (builtins.readFile {J}))."{key0}"'
        e_kv = f'((import {KV}) {N}).get "{key0}"'
        ts_fj, out_fj = timeit(e_fj, COLD_REPS, raw=True)
        ts_kv, out_kv = timeit(e_kv, COLD_REPS, raw=True)
        assert all(o.rstrip("\n") == expected for o in out_fj), "fromJSON cold mismatch"
        assert all(o.rstrip("\n") == expected for o in out_kv), "kvl cold mismatch"
        r["cold_present"] = {"fromJSON": stats(ts_fj), "kvl": stats(ts_kv)}

        # ---- 2. cold miss --------------------------------------------------
        e_fj2 = f'builtins.hasAttr "{MISS_KEY}" (builtins.fromJSON (builtins.readFile {J}))'
        e_kv2 = f'((import {KV}) {N}).has "{MISS_KEY}"'
        ts_fj, out_fj = timeit(e_fj2, COLD_REPS)
        ts_kv, out_kv = timeit(e_kv2, COLD_REPS)
        assert all(o.strip() == "false" for o in out_fj), "fromJSON miss mismatch"
        assert all(o.strip() == "false" for o in out_kv), "kvl miss mismatch"
        r["cold_miss"] = {"fromJSON": stats(ts_fj), "kvl": stats(ts_kv)}

        # ---- 3. warm batch: WARM_KEYS lookups in a single eval ------------
        e_fj3 = (
            f'let j = builtins.fromJSON (builtins.readFile {J}); '
            f'in builtins.foldl\' (acc: k: acc + builtins.stringLength (j."${{k}}")) '
            f"0 {keylist}"
        )
        e_kv3 = (
            f'let db = (import {KV}) {N}; '
            f"in builtins.foldl' (acc: k: acc + builtins.stringLength (db.get k)) "
            f"0 {keylist}"
        )
        ts_fj, out_fj = timeit(e_fj3, WARM_REPS)
        ts_kv, out_kv = timeit(e_kv3, WARM_REPS)
        assert all(int(o.strip()) == exp_sum for o in out_fj), "fromJSON warm sum mismatch"
        assert all(int(o.strip()) == exp_sum for o in out_kv), "kvl warm sum mismatch"
        r["warm_200"] = {
            "fromJSON": stats(ts_fj),
            "kvl": stats(ts_kv),
            "per_lookup_us": {
                "fromJSON": round(statistics.median(ts_fj) / WARM_KEYS * 1e6, 2),
                "kvl": round(statistics.median(ts_kv) / WARM_KEYS * 1e6, 2),
            },
        }

        # ---- 4. floors ------------------------------------------------------
        ts, _ = timeit(f'builtins.stringLength (builtins.readFile {J})', WARM_REPS)
        r["floor_readfile_json_ms"] = stats(ts)
        ts, _ = timeit(f'builtins.stringLength (builtins.readFile {N})', WARM_REPS)
        r["floor_readfile_nfd_ms"] = stats(ts)
        ts, out_ = timeit(
            f'builtins.length (builtins.attrNames (builtins.fromJSON (builtins.readFile {J})))',
            WARM_REPS,
        )
        assert all(int(o.strip()) == len(obj) for o in out_), "parse floor count mismatch"
        r["floor_fromjson_parse_ms"] = stats(ts)

        results[size] = r
        print(f"== {size} done", flush=True)

    out_path = f"{BASE}/bench_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=1)
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()