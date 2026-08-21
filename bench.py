#!/usr/bin/env python3
"""Cold nix-eval benchmark of the three lookup methods on the large
dataset (200,000 keys, data/large.json):

  fromJSON : builtins.fromJSON over the whole 22 MiB JSON file
  nfk3     : single-file NFK v3 (kv3.nix get)
  nfk3s    : sharded NFK v3 (kv3s.nix, 256 shard files, digits = 2) —
             only the key's shard file is read (n=0 not applicable:
             there is no whole-file load; n=1 is the intercept point)

Workload: one cold `nix eval --impure --raw` process answers N queries
(N key lookups; N=0 = load only). Queries are 200 strided samples of the
sorted keys (all present; every method must return the same values).

The sharded dataset is data/large_shards/ (build:
  python3 build_db3.py data/large.json --shards 256 --prefix data/large_shards/ --check)
Time split: the fixed Nix load floor is measured as an empty eval
(`nix eval --impure --raw --expr '""'`, same flags, no expression
work); per method, work = total - baseline, paired by run index.
The JSON carries a top-level baseline plus both totals and work
times for every method.

Usage: bench.py [runs_per_config]
"""
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KV3 = os.path.join(HERE, "kv3.nix")
KV3S = os.path.join(HERE, "kv3s.nix")
JSON = os.environ.get("NFK_JSON", os.path.join(HERE, "data/large.json"))
TABLE = os.environ.get("NFK_TABLE", os.path.join(HERE, "data/large.nfd3"))
SHARDS = os.environ.get("NFK_SHARDS", os.path.join(HERE, "data/large_shards"))
OUT = os.environ.get("NFK_OUT", os.path.join(HERE, "bench_results.json"))
NQS = 200
NS = [0, 1, 5, 10, 30, 100, 200]


def queries_for(json_path, nqs=NQS):
    d = json.load(open(json_path))
    keys = sorted(d)
    step = max(1, len(keys) // nqs)
    return [keys[i] for i in range(0, len(keys), step)][:nqs]


def qlit(qs):
    return "[ " + " ".join(json.dumps(q) for q in qs) + " ]"


def expr_fromjson(qs):
    if not qs:
        return ("let o = builtins.fromJSON (builtins.readFile %s); "
                "in builtins.toJSON (builtins.length (builtins.attrNames o))"
                % json.dumps(JSON))
    return ("let o = builtins.fromJSON (builtins.readFile %s); qs = %s; "
            "in builtins.toJSON (builtins.map (a: o.${a}) qs)"
            % (json.dumps(JSON), qlit(qs)))


def expr_nfk3(qs):
    if not qs:
        return ("let db = import %s %s; in builtins.toJSON db.count"
                % (json.dumps(KV3), json.dumps(TABLE)))
    return ("let db = import %s %s; qs = %s; "
            "in builtins.toJSON (builtins.map (a: db.get a) qs)"
            % (json.dumps(KV3), json.dumps(TABLE), qlit(qs)))


def expr_nfk3s(qs):
    """Sharded NFK v3: only the shard a key hashes to is read."""
    if not qs:
        return None  # no whole-file load point; n=1 is the intercept
    return ("let db = import %s { digits = 2; dir = %s; }; qs = %s; "
            "in builtins.toJSON (builtins.map (a: db.get a) qs)"
            % (json.dumps(KV3S), json.dumps(SHARDS), qlit(qs)))


def run_eval(expr, timeout=120):
    t0 = time.monotonic()
    p = subprocess.run(["nix", "eval", "--impure", "--raw", "--expr", expr],
                       capture_output=True, text=True, timeout=timeout)
    dt = (time.monotonic() - t0) * 1000
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:500])
    return dt, p.stdout


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    qs = queries_for(JSON)
    out = {"runs_per_config": runs, "n_queries": NS, "dataset": "large",
           "note": ("cold process per eval; ms = wall time of nix eval; "
                    "work = total - baseline, paired by run index"),
           "configs": {}}
    # Nix load floor: same flags, empty expression — everything up to
    # the expression itself (process spawn, nix runtime + evaluator
    # init, empty output). Subtracted per run from each method's total.
    base = [run_eval('""')[0] for _ in range(runs)]
    out["baseline"] = {"expr": '""',
                       "label": "nix load floor (empty eval, same flags)",
                       "ms_min": round(min(base), 1),
                       "ms_median": round(statistics.median(base), 1),
                       "ms_all": [round(x, 1) for x in base]}
    for n in NS:
        cfg = f"large/n={n}"
        exprs = {"fromJSON": expr_fromjson(qs[:n]),
                 "nfk3": expr_nfk3(qs[:n])}
        if n > 0:
            exprs["nfk3s"] = expr_nfk3s(qs[:n])
        res = {}
        for m, e in exprs.items():
            dts = []
            outlen = 0
            for r in range(runs):
                dt, outp = run_eval(e)
                dts.append(dt)
                outlen = len(outp)
                assert outp.strip(), "empty output — result not forced?"
            works = [dts[r] - base[r] for r in range(runs)]
            res[m] = {"ms_min": round(min(dts), 1),
                      "ms_median": round(statistics.median(dts), 1),
                      "ms_all": [round(x, 1) for x in dts],
                      "work_ms_min": round(min(works), 1),
                      "work_ms_median": round(statistics.median(works), 1),
                      "out_bytes": outlen}
        # harness invariant: every method answers the same queries
        # (n=0 is a load-only point with different output by design)
        if n > 0:
            lens = {m: res[m]["out_bytes"] for m in res}
            assert len(set(lens.values())) == 1, \
                f"{cfg}: outputs differ: {lens}"
        out["configs"][cfg] = res
        print(cfg + ": " + "  ".join(
            f"{m} min={res[m]['ms_min']} med={res[m]['ms_median']}"
            f" work={res[m]['work_ms_median']}"
            for m in res), flush=True)
    print(f"baseline (nix load): min={out['baseline']['ms_min']} "
          f"med={out['baseline']['ms_median']}", flush=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()