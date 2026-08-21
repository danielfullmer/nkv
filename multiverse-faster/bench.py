#!/usr/bin/env python3
"""Cold nix-eval benchmark replicating the fkzakaria 2026-08-19 workload
("three ways to smuggle sqlite into nix") on nixpkgs-multiverse index data.

Workload: one cold `nix eval --impure --raw` process answers N queries of
"which revisions shipped this package?" (N attribute lookups; N=0 = load
only). Three methods compared:

  fromJSON : builtins.fromJSON over the whole 5.3/7.5 MiB index file
  nfk3     : our NFK3 table (kv3.nix getJson) — per-attr compact JSON
  nfk3s    : sharded NFK3 (kv3s.nix, 256 shard files) — per-attr compact
             JSON, only the key's shard file is read (n=0 not applicable:
             there is no whole-file load; n=1 is the intercept point)

Queries: "hello" + 199 strided samples of the sorted attr names, per file.
Time split: the fixed Nix load floor is measured once as an empty eval
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
PARENT = os.path.dirname(HERE)
KV3 = os.path.join(PARENT, "kv3.nix")
KV3S = os.path.join(PARENT, "kv3s.nix")
NQS = 200
NS = [0, 1, 5, 10, 30, 100, 200]


def queries_for(flat_path):
    d = json.load(open(flat_path))
    attrs = sorted(d)
    assert "hello" in d, "hello missing from attr set"
    qs = ["hello"]
    i = 0
    while len(qs) < NQS:
        a = attrs[(i * len(attrs)) // NQS]
        if a not in qs:
            qs.append(a)
        i += 1
    return qs[:NQS]


def qlit(qs):
    return "[ " + " ".join(json.dumps(q) for q in qs) + " ]"


def expr_fromjson(j, qs):
    if not qs:
        return ("let o = builtins.fromJSON (builtins.readFile %s); "
                "in builtins.toJSON o.revisionCount" % json.dumps(j))
    return ("let o = builtins.fromJSON (builtins.readFile %s); qs = %s; "
            "in builtins.toJSON (builtins.map (a: o.attrs.${a}) qs)"
            % (json.dumps(j), qlit(qs)))


def expr_nfkc3(t, qs):
    if not qs:
        return ("let db = import %s %s; in builtins.toJSON db.count"
                % (json.dumps(KV3), json.dumps(t)))
    return ("let db = import %s %s; qs = %s; "
            "in builtins.toJSON (builtins.map (a: db.getJson a) qs)"
            % (json.dumps(KV3), json.dumps(t), qlit(qs)))


def expr_nfkc3s(d, qs):
    """Sharded NFK3: only the shard a key hashes to is read."""
    if not qs:
        return None  # no whole-file load point; n=1 is the intercept
    return ("let db = import %s { digits = 2; dir = %s; }; qs = %s; "
            "in builtins.toJSON (builtins.map (a: db.getJson a) qs)"
            % (json.dumps(KV3S), json.dumps(d), qlit(qs)))


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
    files = [
        ("versions", os.path.join(HERE, "index/versions.json"),
         os.path.join(HERE, "versions.nfd3"),
         os.path.join(HERE, "versions_flat.json"),
         os.path.join(HERE, "versions_shards")),
        ("history", os.path.join(HERE, "index/history.json"),
         os.path.join(HERE, "history.nfd3"),
         os.path.join(HERE, "history_flat.json"),
         os.path.join(HERE, "history_shards")),
    ]
    out = {"runs_per_config": runs, "n_queries": NS,
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
    for name, j, t, flat, sd in files:
        qs = queries_for(flat)
        for n in NS:
            cfg = f"{name}/n={n}"
            exprs = {"fromJSON": expr_fromjson(j, qs[:n]),
                     "nfk3": expr_nfkc3(t, qs[:n])}
            if n > 0:
                exprs["nfk3s"] = expr_nfkc3s(sd, qs[:n])
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
    with open(os.path.join(HERE, "bench_results.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("wrote bench_results.json")


if __name__ == "__main__":
    main()