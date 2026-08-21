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
| `versions.nfd3` (5,689,104 B), `history.nfd3` (7,732,354 B) | NFK3 tables, N=31,904, M=65,536, load 0.487 (generated) |
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

## Results (median of 3 cold runs; Nix 2.34.7)

**versions.json** — fromJSON pays ~155–160 ms to parse the 5.3 MiB file no
matter how many queries it answers; sharded NFK3 (256 files, `kv3s.nix`)
only reads the key's shard:

| N queries | fromJSON (ms) | NFK3 (ms) | NFK3 sharded (ms) |
|---:|---:|---:|---:|
| 0 | 157.4 | 44.7 | — |
| 1 | 152.6 | 43.2 | **33.9** |
| 5 | 157.6 | 42.2 | 35.6 |
| 10 | 153.2 | 43.4 | 35.8 |
| 30 (lock file) | 158.6 | 43.8 | 35.7 |
| 100 | 156.9 | 47.8 | **43.4** |
| 200 | 157.5 | 45.3 | 53.0 |

**history.json** (larger file, larger values):

| N queries | fromJSON (ms) | NFK3 (ms) | NFK3 sharded (ms) |
|---:|---:|---:|---:|
| 0 | 250.4 | 46.0 | — |
| 1 | 251.2 | 48.3 | **34.6** |
| 5 | 251.6 | 47.2 | 35.2 |
| 10 | 253.6 | 47.5 | 35.4 |
| 30 (lock file) | 257.5 | 48.6 | 38.0 |
| 100 | 255.2 | 49.5 | **43.7** |
| 200 | 257.1 | 52.7 | 55.0 |

## Findings

- **The intercept is the whole game.** fromJSON is flat (~155/260 ms
  regardless of N — matching the article's flat ~0.29 s curve) because it
  must parse the entire file; each extra query costs ~0 (attrset lookup).
  NFK3's intercept is the `nix eval` startup floor (~32–36 ms) plus a
  file read and hash-table setup; each query costs ~50 µs (a 24-bit
  fingerprint probe + `fromJSON` of a ≤11 KB value document).
- **NFK3 wins at every N**, including a single query: 3.3–3.7× on
  versions, 4.9–5.4× on history. For a 30-package lock file: ~44 ms vs
  ~159 ms (versions) and ~49 ms vs ~258 ms (history).
- **Sharding wins from a single lookup up to ~100 lookups/eval.** The
  single-lookup intercept drops from 43.2/48.3 ms (single file) to
  33.9/34.6 ms — ≈ the ~32–35 ms `nix eval` startup floor, since a 15–30 KB
  shard read nearly vanishes into it. Sharded NFK3 is 4.5× (versions) /
  7.3× (history) faster than fromJSON at N=1. Per shard, `kv3s.nix` builds
  the 255-byte decode table **once per eval** (from the all-zero shard,
  which the builder always writes) and shares it into every shard import
  via kv3.nix's `{ file, table }` call form, so a shard import is just a
  readFile + header asserts (~0.2 ms measured; ~0.8 ms before the table
  was shared, when each import rebuilt the table). The query set spans ~N
  distinct shards, so sharded stays ahead of single-file through N=100
  (43.4/43.7 ms sharded vs 47.8/49.5 ms single-file) and the crossover is
  ~100–200 lookups/eval (at N=200 single-file is marginally ahead,
  53.0/55.0 vs 45.3/52.7). Use `kv3s.nix` for lock-file-style workloads
  (≲ 100 lookups/eval) and `kv3.nix` for huge single-eval lookup sets.
- Table size ≈ JSON size (5.69 MB vs 5.48 MB; 7.73 MB vs 7.84 MB),
  unlike the article's giant-.nix alternative (6.0 MiB, 1.13×, and 1.7×
  memory per the article).
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