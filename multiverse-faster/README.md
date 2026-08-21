# multiverse-faster: NFK3 (single + sharded) vs `builtins.fromJSON` on the nixpkgs-multiverse workload

Replicates the workload of ["Three ways to smuggle SQLite into Nix"](https://fzakaria.com/2026-08-19/three-ways-to-smuggle-sqlite-into-nix)
(fzakaria, 2026-08-19) using the NFK3 tables from the parent project
(`../kv3.nix`, `../kv3s.nix`, `../build_db3.py`) instead of the article's
`builtins.fromJSON` approach. Compared: `fromJSON`, single-file NFK3, and
256-shard sharded NFK3 (`kv3s.nix`); the article's `builtins.exec` /
`importNative` / `wasm-sqlite` alternatives are out of scope.

## Data

Downloaded from the pinned commit of the article's dataset
(`fkzakaria/nixpkgs-multiverse` @ `9cc02098e177f784f822c57973ebfc3c02c21bed`):

| file | bytes | content |
|---|---:|---|
| `index/versions.json` | 5,476,283 (article: 5.3 MiB) | `attr → date → revision index` (int or `null`) |
| `index/history.json` | 7,835,496 (article: 7.5 MiB) | `attr → date → [package names]` |

Both files: `{ "revisionCount": 1534, "attrs": { … } }` with **31,904
attributes** and 305,492 inner entries (the article's "305,492 package
versions"). Inner keys contain dots/dates, and `revisionCount` is not an
attr — so the JSON is flattened exactly one level for NFK3:

```
NFK3 key   = attribute name (exact match — no delimiter ambiguity)
NFK3 value = the inner map, stored as a compact JSON document,
             decoded by kv3.nix's getJson / getOrJson
```

## Files

| file | what |
|---|---|
| `convert.py` | `index/*.json` → `{ attr: innerMap }` flat JSON |
| `versions_flat.json`, `history_flat.json` | flattened inputs (generated) |
| `versions.nfd3` (5,688,849 B), `history.nfd3` (7,732,099 B) | NFK3 tables, N=31,904, M=65,536, load 0.487 (generated; decode table lives in the static `../nfd3-table.nix`) |
| `test_correctness.nix` | every-attr `getJson` vs `fromJSON` oracle |
| `bench.py`, `bench_results.json` | cold-eval benchmark harness + results |
| `versions_shards/`, `history_shards/` | 256 sharded NFK3 tables each (`<h[24:26]>.nfd3`; generated) |
| `test_correctness_shards.nix` | every-attr `getJson` vs `fromJSON` oracle over the sharded tables |

## Rebuild

```sh
python3 convert.py index/versions.json versions_flat.json
python3 convert.py index/history.json history_flat.json
python3 ../build_db3.py versions_flat.json versions.nfd3 --check
python3 ../build_db3.py history_flat.json history.nfd3 --check
python3 ../build_db3.py versions_flat.json --shards 256 --prefix versions_shards/ --check
python3 ../build_db3.py history_flat.json --shards 256 --prefix history_shards/ --check
```

## Correctness

- `build_db3.py --check` (independent Python re-parse of the `.nfd3`
  bytes): **31,904/31,904 ok** for both tables, miss → `null`.
- Nix-side oracle, one cold `nix eval` per file comparing every attribute
  `db.getJson k == (fromJSON index).attrs.${k}`:

```
$ nix eval --impure --json --expr "(import ./test_correctness.nix) { table = ./versions.nfd3; jsonPath = ./index/versions.json; }"
{"total":31904,"mismatches":0,"firstBad":null,"missNull":true}
```

Same result for `history.nfd3`. The 31,904-lookup full scan completes in
~1.0–1.2 s wall in a single eval.

- Same test against the 256-shard directories
  (`test_correctness_shards.nix`):
  `{"count":31904,"firstBad":null,"mismatches":0,"missNull":true,"total":31904}`
  for both — shard routing matches the builder's on every key.

## Workload

The article's benchmark question: *"which revisions shipped this package?"*
— N attribute lookups per run. Replicated as: one **cold**
`nix eval --impure --raw` process (fresh evaluator, no warm cache) answers
N queries, N ∈ {0, 1, 5, 10, 30, 100, 200} (N=0 = load only; 30 = the
article's "lock file pinning thirty packages" point). Queries are
deterministic: `hello` + 199 strided samples of the sorted attr names.
All methods return `toJSON` of the N answer maps, so the result is fully
forced (outputs are byte-identical between methods for N ≥ 1 — checked; N=0
is a load-only point with different outputs by design).

```sh
python3 bench.py [runs_per_config]   # default 3; writes bench_results.json
```

The harness also measures the **Nix load floor** — a cold empty eval with
the identical invocation style (`nix eval --impure --raw --expr '""'`, no
file read, no lookup): **32.9 ms median** this run (min 31.6). Results
below show `total (work)` ms, where work = total − floor, paired per run;
the × multipliers are on work (fromJSON work ÷ method work), so the ~33 ms
startup drops out of both; the raw baseline series is in `bench_results.json`
(`baseline`).

## Results (median of 3 cold runs, `total (work)` ms; Nix 2.34.7)

**versions.json** — fromJSON's work term is ~124–137 ms (parse of the
5.3 MiB file) no matter how many queries it answers (total ~157–168 ms);
sharded NFK3 (256 files, `kv3s.nix`) only reads the key's shard:

| N queries | fromJSON | NFK3 | NFK3 sharded |
|---:|---:|---:|---:|
| 0 | 157.1 (125.5) | 43.6 (9.9) | — |
| 1 | 159.9 (126.2) | 45.3 (13.1) (9.6×) | **34.4 (2.7)** (47×) |
| 5 | 157.4 (124.6) | 46.0 (12.8) (9.7×) | 34.6 (1.8) (69×) |
| 10 | 156.7 (124.2) | 43.4 (11.8) (11×) | 33.8 (2.2) (56×) |
| 30 (lock file) | 168.4 (136.8) | 45.3 (12.5) (11×) | **39.5 (6.6)** (21×) |
| 100 | 158.3 (125.9) | 47.9 (15.0) (8.4×) | 49.1 (16.3) (7.7×) |
| 200 | 167.4 (134.5) | 49.2 (16.4) (8.2×) | 50.8 (17.2) (7.8×) |

**history.json** (larger file, larger values):

| N queries | fromJSON | NFK3 | NFK3 sharded |
|---:|---:|---:|---:|
| 0 | 255.5 (222.2) | 46.7 (14.5) | — |
| 1 | 257.9 (226.3) | 46.1 (12.5) (18×) | **35.2 (2.1)** (110×) |
| 5 | 257.0 (225.4) | 45.6 (14.0) (16×) | 34.6 (0.9) (250×) |
| 10 | 259.9 (228.3) | 51.0 (18.2) (13×) | 36.0 (3.9) (59×) |
| 30 (lock file) | 265.5 (231.8) | 47.4 (13.8) (17×) | **36.5 (3.8)** (61×) |
| 100 | 262.2 (228.5) | 52.3 (18.7) (12×) | **45.5 (11.8)** (19×) |
| 200 | 267.1 (233.4) | 55.0 (22.6) (10×) | 53.2 (20.3) (11×) |

## Findings

- **The intercept is the whole game.** fromJSON's work term is flat
  (~124–137 ms versions, ~222–233 ms history regardless of N — total
  ~157–168 / ~256–267 ms; matching the article's flat ~0.29 s curve)
  because it must parse the entire file; each extra query costs ~0
  (attrset lookup). NFK3's intercept is the measured `nix eval` load
  floor (32.9 ms median here) plus one file read (the decode table is
  the static `../nfd3-table.nix`, imported once per eval); each query
  costs a 24-bit fingerprint probe plus `fromJSON` of a ≤11 KB value
  document.
- **NFK3 wins at every N**, including a single query, on the data work
  (startup excluded): 8.2–11× on versions, 10–18× on history (single
  file); sharded is 7.7–69× (versions) and 11–250× (history, low query
  counts). For a 30-package lock file: ~39.5/36.5 ms sharded (work
  6.6/3.8 ms) vs ~168/266 ms total fromJSON.
- **The sharded single-lookup number is essentially the Nix load.** The
  single-lookup total drops from 45.3/46.1 ms (single file) to
  34.4/35.2 ms (sharded) — work 2.7/2.1 ms — against a measured 32.9 ms
  floor: a 15–30 KB shard read plus one probe nearly vanishes into the
  startup. On the data work, sharded NFK3 is ~47× (versions) / ~110×
  (history) the fromJSON cost at N=1. `kv3.nix` imports the static
  `nfd3-table.nix` once per eval no matter how many shards are touched,
  so a shard import is just a readFile + header asserts.
- **Sharding wins from a single lookup to ~30–100 lookups/eval.** The
  query set spans ~N distinct shards, so each new shard costs one
  ~0.1–0.2 ms import and sharded's work climbs 2.7 → 17.2 ms (versions)
  and 2.1 → 20.3 ms (history) by N=200, versus 13.1 → 16.4 / 12.5 → 22.6
  for single-file. versions: sharded ahead through N=30 (39.5 vs 45.3;
  work 6.6 vs 12.5), single-file takes over by N=100 (49.1 vs 47.9; work
  16.3 vs 15.0) — crossover ~30–100. history: sharded ahead through
  N=100 (45.5 vs 52.3; work 11.8 vs 18.7) and still marginally ahead at
  N=200 (53.2 vs 55.0; work 20.3 vs 22.6) — crossover beyond 200. Use
  `kv3s.nix` for lock-file-style workloads (≲ 100 lookups/eval) and
  `kv3.nix` for large single-eval lookup sets.
- Table size ≈ input size (5.69 MB vs 4.83 MB `versions_flat.json`,
  1.18×; 7.73 MB vs 6.88 MB `history_flat.json`, 1.12×), unlike the
  article's giant-.nix alternative (6.0 MiB, 1.13×, and 1.7× memory per
  the article).
- Absolute numbers differ from the article's ~0.29 s fromJSON baseline
  (different machine and Nix version; its chart also started at N=1),
  but the shape — flat fromJSON, small-intercept NFK3 — is reproduced.

## Caveats

- `versions.json` inner keys are dates (matching the article's giant-.nix
  section, which used this exact file); the article's SQLite `versions`
  table is keyed by package version instead. The benchmarked workload is
  the JSON-file path, which is the one being replicated here.
- 31,904 entries / M=65,536 fits the NFK3 limits comfortably (value total
  4.39/6.43 MB < 16.39 MB dec3 limit).
- Cold-process timing only (the article's benchmark is also cold-eval);
  memory not measured.