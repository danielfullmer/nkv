# multiverse-faster: NFK3 vs `builtins.fromJSON` on the nixpkgs-multiverse workload

Replicates the workload of ["Three ways to smuggle SQLite into Nix"](https://fzakaria.com/2026-08-19/three-ways-to-smuggle-sqlite-into-nix)
(fzakaria, 2026-08-19) using the NFK3 tables from the parent project
(`../kv3.nix`, `../build_db3.py`) instead of the article's
`builtins.fromJSON` approach. Only the `fromJSON` comparison is made; the
article's `builtins.exec` / `importNative` / `wasm-sqlite` alternatives are
out of scope.

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

## Rebuild

```sh
python3 convert.py index/versions.json versions_flat.json
python3 convert.py index/history.json history_flat.json
python3 ../build_db3.py versions_flat.json versions.nfd3 --check
python3 ../build_db3.py history_flat.json history.nfd3 --check
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

## Workload

The article's benchmark question: *"which revisions shipped this package?"*
— N attribute lookups per run. Replicated as: one **cold**
`nix eval --impure --raw` process (fresh evaluator, no warm cache) answers
N queries, N ∈ {0, 1, 5, 10, 30, 100, 200} (N=0 = load only; 30 = the
article's "lock file pinning thirty packages" point). Queries are
deterministic: `hello` + 199 strided samples of the sorted attr names.
Both methods return `toJSON` of the N answer maps, so the result is fully
forced (outputs are byte-identical between methods — checked).

```sh
python3 bench.py [runs_per_config]   # default 3; writes bench_results.json
```

## Results (median of 3 cold runs; Nix 2.34.7)

**versions.json** — fromJSON pays ~155 ms to parse the 5.3 MiB file no
matter how many queries it answers:

| N queries | fromJSON (ms) | NFK3 (ms) | speedup |
|---:|---:|---:|---:|
| 0 | 157.6 | 46.2 | 3.4× |
| 1 | 157.5 | 40.8 | 3.9× |
| 5 | 154.8 | 44.7 | 3.5× |
| 10 | 157.2 | 44.8 | 3.5× |
| 30 (lock file) | 158.0 | 43.4 | 3.6× |
| 100 | 167.0 | 46.5 | 3.6× |
| 200 | 158.9 | 50.2 | 3.2× |

**history.json** (larger file, larger values):

| N queries | fromJSON (ms) | NFK3 (ms) | speedup |
|---:|---:|---:|---:|
| 0 | 260.9 | 48.4 | 5.4× |
| 1 | 254.6 | 46.7 | 5.5× |
| 5 | 261.4 | 47.1 | 5.6× |
| 10 | 259.8 | 48.2 | 5.4× |
| 30 (lock file) | 256.6 | 50.1 | 5.1× |
| 100 | 260.7 | 51.4 | 5.1× |
| 200 | 260.0 | 55.0 | 4.7× |

## Findings

- **The intercept is the whole game.** fromJSON is flat (~155/260 ms
  regardless of N — matching the article's flat ~0.29 s curve) because it
  must parse the entire file; each extra query costs ~0 (attrset lookup).
  NFK3's intercept is the `nix eval` startup floor (~32–36 ms) plus a
  file read and hash-table setup; each query costs ~50 µs (a 24-bit
  fingerprint probe + `fromJSON` of a ≤11 KB value document).
- **NFK3 wins at every N**, including a single query: 3.2–3.9× on
  versions, 4.7–5.6× on history. For a 30-package lock file: ~43 ms vs
  ~158 ms (versions) and ~50 ms vs ~257 ms (history).
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