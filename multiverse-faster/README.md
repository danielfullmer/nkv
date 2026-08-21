# multiverse-faster: nkv (single + sharded) vs `builtins.fromJSON` on the nixpkgs-multiverse workload

Replicates the workload of ["Three ways to smuggle SQLite into Nix"](https://fzakaria.com/2026-08-19/three-ways-to-smuggle-sqlite-into-nix)
(fzakaria, 2026-08-19) using the nkv tables from the parent project
(`../nkv.nix`, `../build_nkv.py`) instead of the article's
`builtins.fromJSON` approach. Compared: `fromJSON`, single-file nkv, and
256-shard sharded nkv (`nkv.nix`); the article's `builtins.exec` /
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
attr — so the JSON is flattened exactly one level for nkv:

```
nkv key   = attribute name (exact match — no delimiter ambiguity)
nkv value = the inner map, stored as a compact JSON document,
             decoded by nkv.nix's getJson / getOrJson
```

## Files

| file | what |
|---|---|
| `convert.py` | `index/*.json` → `{ attr: innerMap }` flat JSON |
| `versions_flat.json`, `history_flat.json` | flattened inputs (generated) |
| `versions.nkv` (5,098,975 B), `history.nkv` (7,142,225 B) | nkv tables, N=31,904, M=65,536, load 0.487 (generated; decode table lives in the static `../nkv-table.nix`) |
| `test_correctness.nix` | every-attr `getJson` vs `fromJSON` oracle |
| `bench.py`, `bench_results.json` | cold-eval benchmark harness + results |
| `versions_shards/`, `history_shards/` | 256 sharded nkv tables each (`<h[24:26]>.nkv`; generated) |
| `test_correctness_shards.nix` | every-attr `getJson` vs `fromJSON` oracle over the sharded tables |

## Rebuild

```sh
python3 convert.py index/versions.json versions_flat.json
python3 convert.py index/history.json history_flat.json
python3 ../build_nkv.py versions_flat.json versions.nkv --check
python3 ../build_nkv.py history_flat.json history.nkv --check
python3 ../build_nkv.py versions_flat.json --shards 256 --prefix versions_shards/ --check
python3 ../build_nkv.py history_flat.json --shards 256 --prefix history_shards/ --check
```

## Correctness

- `build_nkv.py --check` (independent Python re-parse of the `.nkv`
  bytes): **31,904/31,904 ok** for both tables, miss → `null`.
- Nix-side oracle, one cold `nix eval` per file comparing every attribute
  `db.getJson k == (fromJSON index).attrs.${k}`:

```
$ nix eval --impure --json --expr "(import ./test_correctness.nix) { table = ./versions.nkv; jsonPath = ./index/versions.json; }"
{"total":31904,"mismatches":0,"firstBad":null,"missNull":true}
```

Same result for `history.nkv`. The 31,904-lookup full scan completes in
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
file read, no lookup): **32.4 ms median** this run (23.5–34.7). Results
below show `total (work)` ms, where work = total − floor, paired per run;
the × multipliers are on work (fromJSON work ÷ method work), so the ~32–33 ms
startup drops out of both; the raw baseline series is in `bench_results.json`
(`baseline`).

## Results (median of 7 cold runs; n = 200 row: min-of-7, `total (work)` ms; Nix 2.34.7)

**versions.json** — fromJSON's work term is ~121–127 ms (parse of the
5.48 MB nested `index/versions.json`) no matter how many queries it answers
(total ~155–158 ms); sharded nkv (256 files, `nkv.nix`) only reads the key's shard:

| N queries | fromJSON | nkv | nkv sharded |
|---:|---:|---:|---:|
| 0 | 157.4 (126.8) | 42.9 (10.1) | — |
| 1 | 157.2 (123.9) | 43.8 (12.6) (9.8×) | **35.3 (3.1)** (≈40×) |
| 5 | 155.9 (122.6) | 42.7 (10.1) (12.1×) | 34.7 (2.9) (≈42×) |
| 10 | 157.6 (123.1) | 43.5 (11.4) (10.8×) | 35.9 (4.8) (≈26×) |
| 30 (lock file) | 157.4 (125.1) | 43.9 (11.4) (11×) | 37.9 (4.6) (≈27×) |
| 100 | 157.9 (126.3) | 46.1 (15.2) (8.3×) | 42.5 (10.1) (≈13×) |
| 200 | 155.4 (120.7) | 44.2 (11.1) (10.9×) | 50.1 (16.5) (≈7.3×) |

**history.json** (larger file, larger values):

| N queries | fromJSON | nkv | nkv sharded |
|---:|---:|---:|---:|
| 0 | 254.9 (226.6) | 45.8 (16.3) | — |
| 1 | 256.4 (228.3) | 44.8 (12.4) (18.4×) | **33.3 (0.0)** |
| 5 | 256.0 (223.7) | 46.5 (14.1) (15.9×) | 33.9 (1.5) (≈149×) |
| 10 | 256.2 (224.1) | 46.2 (13.3) (16.8×) | 34.3 (2.0) (≈112×) |
| 30 (lock file) | 255.1 (224.5) | 47.3 (14.9) (15.1×) | 36.9 (3.5) (≈64×) |
| 100 | 259.0 (226.6) | 49.3 (19.5) (11.6×) | 44.0 (10.6) (≈21×) |
| 200 | 253.2 (221.0) | 49.7 (16.2) (13.6×) | 51.7 (19.2) (≈11.5×) |

## Findings

- **The intercept is the whole game.** fromJSON's work term is flat
  (~121–127 ms versions, ~221–228 ms history regardless of N — total
  ~155–158 / ~253–259 ms; the article's flat ~0.29 s),
  because it must parse the entire file; each extra query costs ~0
  (attrset lookup). nkv's intercept is the measured `nix eval` load
  floor (32.4 ms median here) plus one file read (the decode table is
  the static `../nkv-table.nix`, imported once per eval); each query
  costs a few key-read probe steps — key bytes compared at every
  occupied slot, so a wrong value is impossible by construction — plus
  `fromJSON` of a ≤11 KB value document.
- **nkv wins at every N**, including a single query, on the data work
  (startup excluded): 8–12× on versions, 11–18× on history (single
  file); sharded is 7–42× (versions) and 11–149× (history, N ≥ 5, low
  query counts; at N = 1 the history sharded work rounds to 0.0). For a
  30-package lock file: ~37.9/36.9 ms sharded (work 4.6/3.5 ms) vs
  ~157/255 ms total fromJSON.
- **The sharded single-lookup number is essentially the Nix load.** The
  single-lookup total drops from 43.8/44.8 ms (single file) to
  35.3/33.3 ms (sharded) — work 3.1/0.0 ms — against a measured 32.4 ms
  floor: an ~11–50 KB shard read plus a few probe steps nearly vanishes
  into the startup. On the data work, sharded nkv is ~149× (history) /
  ≈40× (versions) the fromJSON parse + whole-file-read work (at N = 1 the
  sharded work rounds to 0.0). Only one or a few distinct shards are
  touched, so a shard import is just a readFile + header asserts.
- **Sharding wins from a single lookup to ~100–200 lookups/eval.** The
  query set spans ~N distinct shards, so each new shard costs one
  ~0.1–0.2 ms import and sharded's work climbs 3.1 → 16.5 ms (versions)
  and 0.0 → 19.2 ms (history) by N=200 (min row; medians reach
  18.7/23.4), versus 12.6 → 11.1 / 12.4 → 16.2 for single-file.
  versions: sharded ahead through N=100 (42.5 vs 46.1; work 10.1 vs
  15.2), single-file takes over at N=200 (50.1 vs 44.2 on the min row;
  50.7 vs 46.5 median) — crossover ~100–200. history: sharded ahead
  through N=100 (44.0 vs 49.3; work 10.6 vs 19.5); at N=200 single-file
  takes over (51.7 vs 49.7 on the min row; 54.1 vs 51.2 median) —
  crossover at the top of the measured range. Use `nkv.nix` for
  lock-file-style workloads (≲ 100 lookups/eval) and `nkv.nix` for
  large single-eval lookup sets.
- Table size ≈ input size (5.10 MB vs 4.83 MB `versions_flat.json`,
  1.06×; 7.14 MB vs 6.88 MB `history_flat.json`, 1.04×), unlike the
  article's giant-.nix alternative (6.0 MiB, 1.13×, and 1.7× memory per
  the article).
- Absolute numbers differ from the article's ~0.29 s fromJSON baseline
  (different machine and Nix version; its chart also started at N=1),
  but the shape — flat fromJSON, small-intercept nkv — is reproduced.

## Caveats

- `versions.json` inner keys are dates (matching the article's giant-.nix
  section, which used this exact file); the article's SQLite `versions`
  table is keyed by package version instead. The benchmarked workload is
  the JSON-file path, which is the one being replicated here.
- 31,904 entries / M=65,536 fits the nkv limits comfortably (largest value
  7.4/10.9 KB < 16.39 MB per-value limit).
- Cold-process timing only (the article's benchmark is also cold-eval);
  memory not measured.
