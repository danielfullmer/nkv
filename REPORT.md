# Fast hash-based key/value lookup for native Nix

A lookup function for static string→string tables in **pure Nix** (no
`builtins.exec`, no `builtins.fromJSON`, no foreign interpreters), backed by a
precomputed database file read with `builtins.readFile` and sliced with
`builtins.substring`.

## Summary

| | result |
|---|---|
| Correctness | 251,005 lookups verified against a `fromJSON` oracle: **0 mismatches** (all 3 datasets, every key, plus miss and edge-case checks) |
| Cold single lookup, 200k entries | **97 ms vs 206 ms** → **2.1× faster** than `fromJSON` + attrset access |
| Cold single lookup, 50k entries | 47 ms vs 78 ms → **1.7× faster** |
| Cold single lookup, 1k entries | 34 ms vs 34 ms → parity (Nix process startup dominates) |
| In-process per-lookup (after load) | ~11–21 µs for `db.get` vs < 1 µs for attrset `!` — `fromJSON` wins *this* sub-benchmark, see trade-offs |

The headline: every `nix eval` is a cold process that must load its data
source. `builtins.fromJSON` pays a **full parse of the whole file** on every
invocation (≈150 ms for the 14 MB table below), regardless of how many
keys you look up. The NFK format replaces that parse with a byte read plus one
`hashString` call and a ≤ M-slot probe, and wins by 1.7–2.1× for realistic
single/few-lookup workloads.

## File format: NFK v1

Plain ASCII, fixed-width zero-padded decimal fields, three regions:

```
offset 0                        header, 64 bytes
offset 64                       index region, M × 40 bytes
offset 64 + M·40                data region, variable
```

**Header (64 bytes)**

| field | offset | width | value |
|---|---|---|---|
| magic | 0 | 4 | `NFK1` |
| version | 4 | 2 | `01` |
| algo | 6 | 2 | `sh` (sha256) |
| M | 8 | 10 | table size (power of two) |
| N | 18 | 10 | entry count |
| – | 28 | 36 | reserved (spaces) |

**Index region** — one 40-byte entry per table slot `s`, at offset `64 + 40s`:

| field | offset | width | meaning |
|---|---|---|---|
| `fp` | 0 | 16 | first 16 hex chars of `sha256(key)`; `gggg…` (16×`g`) if slot unused |
| `keyOff` | 16 | 10 | byte offset of the key in the data region |
| `keyLen` | 26 | 6 | byte length of the key |
| `valLen` | 32 | 8 | byte length of the value (value is at `keyOff + keyLen`) |

**Data region** — concatenated `key bytes ++ value bytes`, one entry per key,
insertion order. Offsets are absolute from the data-region start.

Properties:

- ASCII throughout: the file is diffable, printable, and safe to pass through
  Nix string builtins (which are the only "binary" I/O Nix has).
- `g` is not a hex digit, so the empty marker can never collide with a real
  fingerprint.
- M is a power of two with load factor ≤ 0.5 (`m_factor = 2`), so slot
  selection is a `bitAnd` and expected probe runs are short.

Measured sizes (JSON → NFK):

| dataset | keys | JSON bytes | NFK bytes | index bytes | ratio |
|---|---|---:|---:|---:|---:|
| small | 1,005 | 68,534 | 142,478 | 81,920 | 2.08× |
| medium | 50,000 | 3,463,238 | 8,306,182 | 5,242,880 | 2.40× |
| large | 200,000 | 13,941,356 | 33,312,940 | 20,971,520 | 2.39× |

The 40-byte ASCII index entries are the price of Nix's string-only world; a
binary encoding would roughly halve the file but lose debuggability.

## Lookup algorithm

```
h   = builtins.hashString "sha256" key        # 64 lowercase hex chars
fp  = h[0:16]                                 # 64-bit fingerprint
s0  = int(h[56:64], 16) AND (M − 1)           # initial slot (low 32 bits)
for s in s0, s0+1, s0+2, … (mod M, at most M steps):
    if slot[s].fp == EMPTY:                  return null   # miss
    if slot[s].fp != fp:                     continue
    if data[slot[s].keyOff .. +keyLen] != key: continue    # fp collision
    return data[slot[s].keyOff+keyLen .. +valLen]          # hit
```

- One `hashString` call serves both the fingerprint and the slot (the two 32-bit
  halves are independent).
- A fingerprint match must be confirmed by a byte-for-byte key comparison, so
  fp collisions (probabilistically ~N²/2⁶⁵ for N ≤ 10⁶) can only add a probe,
  never a wrong answer.
- Worst case O(M) probes (bounded by a counter, so a corrupt file cannot loop);
  expected O(1) at load factor ≤ 0.5.
- A miss costs the same as a hit's probe — it walks to the first empty slot.

## Implementation

Files in this repo:

| file | purpose |
|---|---|
| `kv.nix` | the lookup module (self-contained, no generator) |
| `build_db.py` | JSON → NFK builder (`--check` round-trips every key) |
| `gen_data.py` | test data generation (1k / 50k / 200k keys + edge cases) |
| `gen_kv.py` | emits `kv.nix` (inlines the 100-entry two-digit table to avoid typos) |
| `test_correctness.nix` | Nix-side oracle test vs `fromJSON` |
| `bench.py`, `bench_marginal.py` | benchmark harnesses |
| `data/` | `small|medium|large.{json,nfd}` |

Usage:

```nix
let db = (import ./kv.nix) ./data/large.nfd;
in {
  db.get "dotted.key.name"   # -> value string, or null if absent
  db.getOr "k" "default"     # -> value or default
  db.has "k"                 # -> true / false
  db.count                   # -> N (stored entries)
  db.tableSize               # -> M (slots)
}
```

```sh
python3 build_db.py input.json output.nfd            # build (m-factor 2)
python3 build_db.py input.json output.nfd --check    # build + verify every key
```

Builder guarantees: keyLen ≤ 6 digits, valLen ≤ 8 digits, keyOff ≤ 10 digits
(raises otherwise); the Python hash is byte-identical to Nix's
(`hashlib.sha256(k).hexdigest()` → same `fp` and same `int(h[-8:],16) & (M-1)`
slot), which the cross-language round-trip proves.

Nix-build constraints handled (Nix 2.34.7+1 here):

- no `builtins.parseInt` → `toDec` decodes two decimal digits at a time via a
  100-entry table and `foldl'` (`acc*100 + d2."${s[p:p+2]}"`).
- no `%` operator and no `builtins.mod` → slot wrap-around is
  `builtins.bitAnd (s + 1) (M - 1)`, exact because M is a power of two.
- no `builtins.hash` → `hashString "sha256"` (the hash-based lookup the
  task requires).
- `builtins.genList` is function-first on this build.
- The probe is a tail-recursive walk (`foldl'`-friendly); header `M`/`N` are
  forced once at import time, not per lookup.

## Correctness

Method:

1. **Python round-trip** (`build_db.py --check`): after building each `.nfd`,
   re-parse the file independently and check every key via the Python port of
   the same probe algorithm, plus a known-absent key.
2. **Nix oracle** (`test_correctness.nix`): `fromJSON` is used *only here* as
   the reference. For every key `k` in the JSON: `db.get k == j."${k}"`; a
   missing key returns `null`; `db.has` agrees with `?` for present and absent
   keys; `db.count == length (attrNames j)`.

Results (Nix 2.34.7+1):

| dataset | keys | mismatches | miss→null | `has` present/absent | count ok |
|---|---:|---:|---|---|---|
| small | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |

Edge cases (in `small`): empty key → empty value, 1-char key, unicode key and
value (multi-byte UTF-8), keys and values containing spaces, and
`dotted.nested.attr.name`. All pass — the format stores raw bytes, so
UTF-8 and spaces are handled by construction.

## Benchmark

**Environment:** Nix 2.34.7+1 (non-FLAKE native eval), Python 3.13.13, Linux
x86-64 (Ryzen Threadripper 3970X). Files hot in page cache. Wall clock via
`time.perf_counter` around subprocess `nix eval --impure` invocations.

**Method.** Nix has no cross-invocation caching, so the realistic unit of work
is one `nix eval`: process start, load data source, look up key(s), exit.

- **Cold single lookup** — 15 fresh `nix eval` runs, each doing exactly one
  lookup of the same present key (miss variant uses an absent key; `hasAttr`
  vs `db.has`). Medians reported.
- **Warm 200-lookup** — one `nix eval` performing 200 lookups in-process over
  a *literal* key list shared by both methods, so each method pays only its
  own load cost plus 200 lookups (no shared contamination). Sum of value
  lengths asserted equal across methods.
- **Marginal per-lookup cost** — least-squares slope of eval time vs lookup
  count n ∈ {10, 50, 100, 200, 400}, 5 reps per point, min-of-reps; this
  isolates per-lookup cost from load/startup.
- **Floors** — bare `nix eval` startup, `readFile` only, and
  `fromJSON`+`attrNames` (parse) only.

All benchmark expressions asserted their outputs against the JSON source, so
every timed run is also a correctness run.

### Cold single lookup (median of 15, full `nix eval` wall clock)

| dataset | fromJSON + `!` | NFK `get` | speedup |
|---|---:|---:|---:|
| small (1k) | 34.2 ms | 34.0 ms | 1.01× |
| medium (50k) | 78.4 ms | 47.2 ms | **1.66×** |
| large (200k) | 205.9 ms | 97.0 ms | **2.12×** |

### Cold miss (median of 15)

| dataset | fromJSON `hasAttr` | NFK `has` | speedup |
|---|---:|---:|---:|
| small | 33.9 ms | 34.3 ms | 0.99× |
| medium | 78.7 ms | 46.2 ms | **1.70×** |
| large | 207.0 ms | 99.2 ms | **2.09×** |

### Warm 200 lookups, single eval (median of 3)

| dataset | fromJSON | NFK `get` | ratio |
|---|---:|---:|---:|
| small | 35.6 ms | 38.6 ms | 0.92× |
| medium | 77.3 ms | 50.4 ms | **1.53×** |
| large | 202.9 ms | 98.0 ms | **2.07×** |

### Floors (median)

| floor | small | medium | large |
|---|---:|---:|---:|
| `nix eval` startup (no I/O) | 34.0 ms | 34.0 ms | 34.0 ms |
| `readFile` JSON only | 34.1 ms | 39.3 ms | 57.2 ms |
| `readFile` NFK only | 32.3 ms | 46.2 ms | 98.1 ms |
| `fromJSON` + `attrNames` (parse) | 33.3 ms | 89.0 ms | 257.2 ms |

### Marginal in-process per-lookup cost (least-squares slope)

| dataset | fromJSON attrset `!` | NFK `get` |
|---|---:|---:|
| small | < 1 µs (noise) | ≈ 18 µs |
| medium | < 1 µs (noise) | ≈ 20.5 µs |
| large | < 1 µs (noise) | ≈ 11–20 µs |

(`fromJSON`'s attrset access is so cheap that its least-squares slope lands at
or below zero — i.e. sub-µs, under this benchmark's noise floor. NFK's slope is
dominated by the `hashString` call plus ~3–4 `substring` forces per probe step.)

### Analysis

Cost model, verified against the floors:

```
fromJSON(n) ≈ 34 + read(JSON) + parse(JSON) + sub-µs·n
NFK(n)      ≈ 34 + read(NFK)  + import kv.nix + ~20µs·n
```

For `large`: parse ≈ 150 ms vs NFK's extra ~20 MB of reads ≈ 40 ms. NFK
buys back the parse with a small per-lookup tax. Crossover (equal total time):

| dataset | NFK load edge | per-lookup tax | crossover |
|---|---:|---:|---:|
| small | ~0 ms | ~18 µs | ≈ 40 lookups/eval |
| medium | ~31 ms | ~20 µs | ≈ 1,500 lookups/eval |
| large | ~111 ms | ~20 µs | ≈ 5,500 lookups/eval |

- **Below the crossover** (one to a few thousand lookups per eval — the common
  case for Nix tooling, since each `nix eval`/`nix build` re-evaluates from
  scratch) **NFK wins**, and the win grows with table size (1.7–2.1×).
- **Above the crossover** (bulk in-process scans, e.g. rendering a whole
  table in one eval) `fromJSON`'s O(1) attrset access wins per-lookup.
- NFK's file is 2.4× the JSON size and its `readFile` transfers ~2.4× the
  bytes; that I/O cost is still far below JSON parse cost. (Nix has no
  partial-read builtin — `readFile` is eager — so the design trades parse
  time for bytes, which is the right trade on a warm page cache and on cold
  reads alike up to very large files.)

## Trade-offs, stated plainly

1. **Per-lookup, warm, in-process: `fromJSON` is faster** (sub-µs vs ~11–20 µs).
   NFK is not a drop-in speed win for scanning an entire table in one eval.
2. **Cold / repeated invocations: NFK wins 1.7–2.1×** at 50k–200k entries
   because it never parses JSON; at ~1k entries both are startup-bound and tie.
3. **File size +2.4×.** Fixed-width ASCII index dominates (63% of the large
   file). Binary fields would shrink it ~2× at the cost of Nix-string
   debuggability.
4. **Static only.** Updates require re-running the builder (by design).
5. **No `builtins.parseInt`/`%`/`mod`** on this Nix: decimal decoding costs a
   small fold per field; a Nix with `parseInt` would shave a few µs per lookup.
6. **Memory:** the whole file string is held in the evaluator (same as
   `readFile` JSON); the `fromJSON` variant additionally holds a 200k-key
   attrset.

**Use NFK** when a Nix program looks up a few values per evaluation from a
large static string table, or when `fromJSON` is off-limits. **Use
`fromJSON`** when one evaluation touches a large fraction of the table, or
when simplicity and tooling around JSON outweigh a hundred or two
milliseconds.

## Reproducing

```sh
python3 gen_data.py                              # data/{small,medium,large}.json
for s in small medium large; do
  python3 build_db.py data/$s.json data/$s.nfd --check
done
nix eval --impure --expr '(import ./test_correctness.nix) "large"'
python3 bench.py             # cold + warm + floors  -> bench_results.json
python3 bench_marginal.py    # per-lookup slopes     -> bench_marginal.json
```

Example lookup:

```sh
nix eval --impure --raw --expr \
  '((import ./kv.nix) ./data/large.nfd).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx
```