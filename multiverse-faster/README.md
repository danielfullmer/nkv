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
| `versions.nfd3` (5,361,121 B), `history.nfd3` (7,404,371 B) | NFK3 tables, N=31,904, M=65,536, load 0.487 (generated; decode table lives in the static `../nfd3-table.nix`) |
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
file read, no lookup): **34.0 ms median** this run (33.5–36.0). Results
below show `total (work)` ms, where work = total − floor, paired per run;
the × multipliers are on work (fromJSON work ÷ method work), so the ~34 ms
startup drops out of both; the raw baseline series is in `bench_results.json`
(`baseline`).

## Results (median of 7 cold runs; n = 200 row: min-of-7, `total (work)` ms; Nix 2.34.7)

**versions.json** — fromJSON's work term is ~123–127 ms (parse of the
4.8 MB file) no matter how many queries it answers (total ~157–161 ms);
sharded NFK3 (256 files, `kv3s.nix`) only reads the key's shard:

| N queries | fromJSON | NFK3 | NFK3 sharded |
|---:|---:|---:|---:|
| 0 | 157.9 (123.6) | 43.4 (8.9) | — |
| 1 | 160.5 (126.9) | 46.0 (12.5) (10.2×) | **33.5 (−0.9)** |
| 5 | 157.1 (123.5) | 44.0 (9.6) (13×) | 35.1 (0.7) (176×) |
| 10 | 157.8 (124.2) | 44.5 (10.6) (12×) | 35.6 (1.7) (73×) |
| 30 (lock file) | 158.6 (125.0) | 45.4 (11.5) (11×) | 36.7 (3.2) (39×) |
| 100 | 159.9 (125.2) | 47.2 (12.5) (10×) | 44.6 (11.0) (11×) |
| 200 | 158.8 (122.8) | 46.6 (11.1) (11×) | 51.7 (18.1) (6.8×) |

**history.json** (larger file, larger values):

| N queries | fromJSON | NFK3 | NFK3 sharded |
|---:|---:|---:|---:|
| 0 | 254.9 (221.3) | 45.8 (11.9) | — |
| 1 | 261.9 (225.9) | 47.4 (12.8) (18×) | **34.4 (0.9)** (251×) |
| 5 | 265.7 (230.3) | 47.0 (13.0) (18×) | 35.6 (2.0) (115×) |
| 10 | 261.3 (227.7) | 55.9 (20.8) (11×) | 35.5 (1.8) (127×) |
| 30 (lock file) | 254.0 (219.9) | 47.6 (12.8) (17×) | 37.1 (2.7) (81×) |
| 100 | 257.7 (222.8) | 49.9 (15.5) (14×) | 44.3 (9.5) (23×) |
| 200 | 253.1 (217.1) | 51.3 (17.7) (12×) | 44.7 (9.2) (24×) |

## Findings

- **The intercept is the whole game.** fromJSON's work term is flat
  (~123–127 ms versions, ~217–230 ms history regardless of N — total
  ~157–161 / ~253–266 ms; matching the article's flat ~0.29 s curve)
  because it must parse the entire file; each extra query costs ~0
  (attrset lookup). NFK3's intercept is the measured `nix eval` load
  floor (34.0 ms median here) plus one file read (the decode table is
  the static `../nfd3-table.nix`, imported once per eval); each query
  costs a 24-bit fingerprint probe plus `fromJSON` of a ≤11 KB value
  document.
- **NFK3 wins at every N**, including a single query, on the data work
  (startup excluded): 10–13× on versions, 11–18× on history (single
  file); sharded is 6.8–176× (versions) and 23–251× (history, low query
  counts). For a 30-package lock file: ~37/37 ms sharded (work
  3.2/2.7 ms) vs ~159/254 ms total fromJSON.
- **The sharded single-lookup number is essentially the Nix load.** The
  single-lookup total drops from 46.0/47.4 ms (single file) to
  33.5/34.4 ms (sharded) — work −0.9/0.9 ms — against a measured 34.0 ms
  floor: an ~11–51 KB shard read plus one probe nearly vanishes into the
  startup. On the data work, sharded NFK3 is ~251× (history) the fromJSON
  parse + whole-file-read work (at N = 1 the sharded total is the floor).
  The sharded reader imports the static `nfd3-table.nix` once per eval no
  matter how many shards are touched, so a shard import is just a
  readFile + header asserts.
- **Sharding wins from a single lookup to ~100–200 lookups/eval.** The
  query set spans ~N distinct shards, so each new shard costs one
  ~0.1–0.2 ms import and sharded's work climbs −0.9 → 18.1 ms (versions)
  and 0.9 → 9.2 ms (history) by N=200, versus 12.5 → 11.1 / 12.8 → 17.7
  for single-file. versions: sharded ahead through N=100 (44.6 vs 47.2;
  work 11.0 vs 12.5), single-file takes over at N=200 (46.6 vs 51.7 on
  the min row; 49.6 vs 53.9 median) — crossover ~100–200. history:
  sharded ahead through N=100 (44.3 vs 49.9; work 9.5 vs 15.5); at N=200
  the min row favors sharded (44.7 vs 51.3) while the median favors
  single-file (55.0 vs 52.7) — crossover at the top of the measured
  range. Use `kv3s.nix` for lock-file-style workloads (≲ 100
  lookups/eval) and `kv3.nix` for large single-eval lookup sets.
- Table size ≈ input size (5.36 MB vs 4.83 MB `versions_flat.json`,
  1.11×; 7.40 MB vs 6.88 MB `history_flat.json`, 1.08×), unlike the
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
- 31,904 entries / M=65,536 fits the NFK3 limits comfortably (largest value
  7.4/10.9 KB < 16.39 MB per-value limit).
- Cold-process timing only (the article's benchmark is also cold-eval);
  memory not measured.