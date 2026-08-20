# Fast key/value lookup for native Nix: NFK (hash) vs NKB (binary search)

A lookup function for static string→string tables in **pure Nix** (no
`builtins.exec`, no `builtins.fromJSON`, no foreign interpreters), backed by a
precomputed database file read with `builtins.readFile` and sliced with
`builtins.substring`. Two formats:

- **NFK v1** — hash-based: sha256 fingerprint + linear probing (open
  addressing).
- **NKB v1** — byte-sorted keys + binary search, no hashing at all (the
  implementation is literally `readFile` + `substring` + arithmetic).

## Summary

| | result |
|---|---|
| Correctness | 502,010 lookups across both formats verified against a `fromJSON` oracle: **0 mismatches** (all 3 datasets, every key, plus miss and edge-case checks) |
| Cold single lookup, 200k entries | NKB **78.5 ms** vs NFK **98.9 ms** vs fromJSON **208.5 ms** → NKB **2.66×**, NFK 2.11× |
| Cold single lookup, 50k entries | NKB 43.3 vs NFK 46.0 vs fromJSON 77.9 ms → NKB 1.80×, NFK 1.70× |
| Cold single lookup, 1k entries | 33–34 ms all methods → parity (Nix process startup dominates) |
| In-process per-lookup (after load) | fromJSON attrset < 1 µs; NFK ≈ 1–20 µs; NKB ≈ 42–86 µs — see trade-offs |

The headline: every `nix eval` is a cold process that must load its data
source. `builtins.fromJSON` pays a **full parse of the whole file** on every
invocation (≈150–210 ms for the 14 MB table below), regardless of how many
keys you look up. Both custom formats replace that parse with a byte read:
NFK pays one `hashString` plus a ≤ M-slot probe; NKB pays a ≤ ⌈log₂(N)⌉
binary search over byte-sorted keys. For realistic single/few-lookup
workloads both win 1.7–2.7×, and NKB — whose file is half the size of NFK's —
is the better cold-start choice at 50k+ entries.

## File format: NFK v1

Header and index are plain ASCII (fixed-width zero-padded decimal/hex
fields); the data region is raw UTF-8. Three regions:

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

- Text by construction: Nix strings are byte sequences; only NUL is
  forbidden (`readFile` rejects files containing it; verified on 2.34.7).
  Source literals are UTF-8-decoded — an invalid byte becomes U+FFFD — so
  raw high bytes can only come from I/O. Structural fields are therefore
  ASCII (their decode tables need ASCII keys), keeping the file diffable.
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

The 40-byte ASCII index entries are the price of Nix's string-only world. A
binary field encoding would be smaller, but stock Nix has no byte-to-int
builtin, so decoding tables would need raw high-byte keys — which `.nix`
source cannot express (invalid bytes become U+FFFD) — and it would still
lose debuggability.

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

## File format: NKB v1

Same "ASCII structural fields, raw-byte data region" principle as NFK, but no
hashing: keys are sorted by UTF-8 byte order and the index has one
fixed-width entry per key (not per table slot). Three regions:

```
offset 0                        header, 64 bytes
offset 64                       index region, N × 24 bytes
offset 64 + N·24                data region: all keys (sorted), then all values
```

**Header (64 bytes)**

| field | offset | width | value |
|---|---|---|---|
| magic | 0 | 4 | `NKB1` |
| N | 4 | 8 | entry count (4 base-255 digits) |
| keyTotal | 12 | 8 | total key bytes (4 digits) |
| valTotal | 20 | 8 | total value bytes (4 digits) |
| – | 28 | 36 | reserved (spaces) |

**Index region** — one 24-byte entry per key `i` at offset `64 + 24i`; keys
strictly ascending in UTF-8 byte order:

| field | offset | width | meaning |
|---|---|---|---|
| `off_k` | 0 | 8 | file offset of the key (4 digits) |
| `len_k` | 8 | 4 | key length (2 digits) |
| `off_v` | 12 | 8 | file offset of the value (4 digits) |
| `len_v` | 20 | 4 | value length (2 digits) |

Offsets are absolute from file start. Structural fields are **base-255
digits**: one digit = two chars of a 32-char alphabet (`a`–`z` = 0–25,
`2`–`7` = 26–31; digit = `hi*32 + lo`; the pair `77` is unused), low digit
first. Two digits cover < 255² ≈ 65k bytes; four digits < 255⁴ ≈ 4.2 GB — the
builder enforces these widths. Raw binary would be denser, but Nix source
cannot express raw high bytes as attrset keys (invalid bytes in a literal
decode to U+FFFD), so a byte→int decode table must key on ASCII; base-255
over a 32-char alphabet packs two chars per digit.

Measured sizes (JSON → NKB):

| dataset | keys | JSON bytes | NKB bytes | index bytes | ratio |
|---|---|---:|---:|---:|---:|
| small | 1,005 | 68,534 | 84,678 | 24,120 | 1.24× |
| medium | 50,000 | 3,463,238 | 4,263,302 | 1,200,000 | 1.23× |
| large | 200,000 | 13,941,356 | 17,141,420 | 4,800,000 | 1.23× |

1.9–2.0× smaller than NFK (one 24-byte entry per key vs 40-byte entries per
2×-load-factor table slot).

## Lookup algorithm (NKB)

```
lo, hi = 0, N
while lo < hi:
    m = (lo + hi) / 2            # integer division truncates
    key_m = index[m].key         # raw bytes
    key_m < key  ? lo = m + 1    # Nix < is unsigned byte-lexicographic
    key_m == key ? return index[m].value
    key_m > key  ? hi = m
return null
```

- No hashing: ≤ ⌈log₂(N)⌉ key comparisons (10 at 1k, 16 at 50k, 18 at 200k
  keys), each a `substring` + byte compare.
- The builder sorts by UTF-8 byte sequence (`sorted(key.encode("utf-8"))`),
  exactly the order Nix's `<` compares (verified on raw 0x01–0xFF bytes), so
  the search is well-defined.
- A miss is the search running to exhaustion — same cost as a hit.
- Correctness: the invariant "if the key exists it lies in [lo, hi)" is
  preserved by every branch; termination with `lo ≥ hi` means the interval
  is empty → `null`.

## Implementation

Files in this repo:

| file | purpose |
|---|---|
| `kv.nix` | NFK lookup module (self-contained, no generator) |
| `kv_bin.nix` | NKB lookup module (self-contained, no generator) |
| `build_db.py` | JSON → NFK builder (`--check` round-trips every key) |
| `build_db_bin.py` | JSON → NKB builder (`--check` round-trips every key) |
| `gen_data.py` | test data generation (1k / 50k / 200k keys + edge cases) |
| `gen_kv.py` | emits `kv.nix` (inlines the 100-entry two-digit table to avoid typos) |
| `gen_kv_bin.py` | emits `kv_bin.nix` (inlines the 255-entry base-255 pair table) |
| `test_correctness.nix`, `test_correctness_bin.nix` | Nix-side oracle tests vs `fromJSON` (NFK, NKB) |
| `bench.py`, `bench_marginal.py` | benchmark harnesses (parameterized: `--kv/--ext/--label/--out`) |
| `data/` | `small|medium|large.{json,nfd,nkb}` |

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

NKB (`kv_bin.nix`) has the same API minus `tableSize` (no slots — one index
entry per key):

```nix
let db = (import ./kv_bin.nix) ./data/large.nkb;
in {
  db.get "dotted.key.name"   # -> value string, or null if absent
  db.getOr "k" "default"     # -> value or default
  db.has "k"                 # -> true / false
  db.count                   # -> N (stored entries)
}
```

```sh
python3 build_db.py input.json output.nfd            # build NFK (m-factor 2)
python3 build_db.py input.json output.nfd --check    # build + verify every key
python3 build_db_bin.py input.json output.nkb --check  # build NKB + verify
```

Builder guarantees: keyLen ≤ 6 digits, valLen ≤ 8 digits, keyOff ≤ 10 digits
(raises otherwise); the Python hash is byte-identical to Nix's
(`hashlib.sha256(k).hexdigest()` → same `fp` and same `int(h[-8:],16) & (M-1)`
slot), which the cross-language round-trip proves.

NKB builder guarantees: key/value < 255² bytes and each total < 255⁴ (raises
otherwise); keys are sorted by UTF-8 byte order, exactly the order Nix's `<`
compares — which is what the binary search relies on.

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

NKB v1 (`kv_bin.nix`) additionally relies on:

- no `builtins.parseInt` → base-255 fields decode through the inlined
  255-entry two-char pair table: `pair = s: i: b2."${builtins.substring i 2
  s}"`, folded Horner-style (`p0 + 255*(p1 + 255*(p2 + 255*p3))`).
- integer division truncates (`(lo + hi) / 2`), which is what the binary-
  search midpoint needs.
- `<`/`==` on bytes read from I/O are unsigned byte-lexicographic — the same
  order the builder sorts by (verified on raw 0x01–0xFF bytes).

## Correctness

Method (per format; NFK and NKB run the identical checks):

1. **Python round-trip** (`build_db.py --check` / `build_db_bin.py --check`):
   after building each file, re-parse it independently and check every key
   via the Python port of the same algorithm (probe for NFK, binary search
   for NKB), plus a known-absent key.
2. **Nix oracle** (`test_correctness.nix` / `test_correctness_bin.nix`):
   `fromJSON` is used *only here* as the reference. For every key `k` in the
   JSON: `db.get k == j."${k}"`; a missing key returns `null`; `db.has` agrees
   with `?` for present and absent keys; `db.count == length (attrNames j)`.

Results (Nix 2.34.7+1):

| dataset | format | keys | mismatches | miss→null | `has` present/absent | count ok |
|---|---|---:|---|---|---|---|
| small | NFK | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| small | NKB | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NFK | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NKB | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NFK | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NKB | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |

Edge cases (in `small`): empty key → empty value, 1-char key, unicode key and
value (multi-byte UTF-8), keys and values containing spaces, and
`dotted.nested.attr.name`. Both formats pass identically — lookups compare
and return the UTF-8 bytes directly, so multi-byte characters and spaces are
handled by construction.

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

### NKB v1 results (same session, same method)

**Cold single lookup (median of 15, full `nix eval` wall clock):**

| dataset | fromJSON + `!` | NFK `get` | NKB `get` | NFK vs fj | NKB vs fj |
|---|---:|---:|---:|---:|---:|
| small (1k) | 34.0 ms | 33.1 ms | 33.3 ms | 1.03× | 1.02× |
| medium (50k) | 77.9 ms | 46.0 ms | 43.3 ms | 1.70× | **1.80×** |
| large (200k) | 208.5 ms | 98.9 ms | 78.5 ms | 2.11× | **2.66×** |

**Cold miss (median of 15):**

| dataset | fromJSON `hasAttr` | NFK `has` | NKB `has` | NFK vs fj | NKB vs fj |
|---|---:|---:|---:|---:|---:|
| small | 34.1 ms | 33.8 ms | 34.2 ms | 1.01× | 0.99× |
| medium | 77.8 ms | 46.3 ms | 43.1 ms | 1.68× | **1.80×** |
| large | 209.4 ms | 101.6 ms | 79.7 ms | 2.06× | **2.63×** |

**Warm 200 lookups, single eval (median of 3):**

| dataset | fromJSON | NFK `get` | NKB `get` | NFK vs fj | NKB vs fj |
|---|---:|---:|---:|---:|---:|
| small | 34.9 ms | 37.8 ms | 43.0 ms | 0.92× | 0.81× |
| medium | 77.2 ms | 49.7 ms | 56.8 ms | 1.55× | 1.36× |
| large | 212.4 ms | 101.7 ms | 86.4 ms | 2.09× | **2.45×** |

**Marginal in-process per-lookup, NKB (least-squares slope):**

| dataset | NKB `get` |
|---|---:|
| small | ≈ 42 µs |
| medium | ≈ 86 µs |
| large | ≈ 77 µs |

vs NFK ≈ 18 / 10 / 1–20 µs (the NFK large slope is session-noisy: 1.2 µs this
run, 11.4 µs in the earlier session — true per-lookup cost is somewhere in
that range, dominated by `hashString` + 1–2 slot reads). NKB's slope is
dominated by ~10–18 index-entry decodes (each: 6 pair-table lookups + 6
`substring`s) plus one key `substring` + compare per step.

### NKB vs NFK, head to head (large, 200k)

| workload | NFK | NKB | winner |
|---|---:|---:|---|
| Cold single lookup | 98.9 ms | 78.5 ms | NKB (1.26×) |
| Cold miss | 101.6 ms | 79.7 ms | NKB (1.27×) |
| Warm 200 lookups | 101.7 ms | 86.4 ms | NKB (1.18×) |
| File size | 33.3 MB | 17.1 MB | NKB (1.94× smaller) |
| readFile floor | 98.1 ms | 76.6 ms | NKB (−21.5 ms) |
| Marginal per-lookup | ~1–20 µs | ~77 µs | NFK |
| Hashing | sha256 per lookup | none | NKB |

Where the numbers come from: NKB's cold win is exactly its readFile advantage
(17.1 vs 33.3 MB → 21.5 ms floor gap at large), which its ~15 ms extra
200-lookup cost doesn't quite eat at large — but does at medium (NFK wins
warm-200 there: 49.7 vs 56.8 ms, since the floor gap is only ~4 ms).

### Analysis

Cost model, verified against the floors:

```
fromJSON(n) ≈ 34 + read(JSON) + parse(JSON) + sub-µs·n
NFK(n)      ≈ 34 + read(NFK)  + import + ~(1–20 µs)·n
NKB(n)      ≈ 34 + read(NKB)  + import + ~(42–86 µs)·n
```

At large: parse ≈ 150 ms vs NFK's extra ~21 MB of reads ≈ 21.5 ms; NKB reads
another 16.2 MB less. Both custom formats buy back the parse with a
per-lookup tax.

Crossover with `fromJSON` (equal total time; load edge ÷ per-lookup tax):

| dataset | NFK | NKB |
|---|---:|---:|
| small | ≈ tie (startup-bound) | ≈ tie (startup-bound) |
| medium | ≈ 3,000 lookups/eval | ≈ 440 lookups/eval |
| large | ≈ 5,500–90,000 (slope-noisy: 11.4 µs → 5,500; 1.2 µs → 90,000) | ≈ 1,700 lookups/eval |

Crossover **between the two custom formats** (NKB's read advantage vs NFK's
lookup advantage): at large, 21.5 ms ÷ (77 − ~10) µs ≈ 330 lookups/eval —
NKB wins below it, NFK above; at medium the read gap is only ~4 ms → NFK
wins above ≈ 55 lookups.

- **Below the crossovers** (one to a few hundred lookups per eval — the
  common case for Nix tooling, since each `nix eval`/`nix build` re-evaluates
  from scratch) **both custom formats win over `fromJSON`**, and the win
  grows with table size.
- **NKB is the better cold choice** at 50k+ entries: half the file bytes, no
  hashing, and a smaller worst-case (no probe runs, no fingerprint
  collisions).
- **NFK is the better warm choice**: O(1) lookups beat NKB's per-step table
  decoding once a single eval does hundreds of lookups on a large table.
- **Above the `fromJSON` crossover** (bulk in-process scans) `fromJSON`'s
  sub-µs attrset access wins per-lookup, since its parse was paid once.
- NFK's file is 2.4× JSON; NKB's is 1.23×. A binary index would be smaller
  still, but its decode tables need high-byte attrset keys that `.nix` source
  cannot express (verified), and diffability is lost either way.

## Trade-offs, stated plainly

1. **Per-lookup, warm, in-process: `fromJSON` is fastest** (sub-µs attrset),
   then NFK (~1–20 µs), then NKB (~42–86 µs). Neither custom format is a
   speed win for scanning an entire table in one eval.
2. **Cold / repeated invocations: both custom formats beat `fromJSON`** by
   1.7–2.7× at 50k–200k entries (NKB's 2.66× at 200k is the best cold number
   in the report); at ~1k entries all three tie at startup cost.
3. **File size:** NKB ≈ 1.23× JSON (4.8 MB index = 28% of the large file);
   NFK ≈ 2.4× JSON (21 MB index = 63%). A binary index would shrink both but
   needs byte→int decode tables whose high-byte keys `.nix` source cannot
   express (verified), and would lose debuggability.
4. **NKB has no hashing at all** — the Nix implementation is literally
   `readFile` + `substring` + arithmetic (plus the generated decode table);
   NFK pays one sha256 per lookup.
5. **Static only.** Updates require re-running the builder (by design).
6. **No `builtins.parseInt`/`%`/`mod`** on this Nix: NFK's decimal decoding
   costs a small fold per field; NKB's base-255 decode costs 2–4 table
   lookups per field; a Nix with `parseInt` would shave a few µs per lookup
   from both.
7. **Memory:** the whole file string is held in the evaluator (same as
   `readFile` JSON); the `fromJSON` variant additionally holds a 200k-key
   attrset.

**Use NKB** for cold single/few-lookup evaluations over 50k+ key tables, or
when you want the smallest possible DB file with zero hashing. **Use NFK**
when one evaluation does many (hundreds to thousands) lookups. **Use
`fromJSON`** when one evaluation touches a large fraction of the table, or
when simplicity and tooling around JSON outweigh a hundred or two
milliseconds.

## Reproducing

```sh
python3 gen_data.py                              # data/{small,medium,large}.json
for s in small medium large; do
  python3 build_db.py data/$s.json data/$s.nfd --check
  python3 build_db_bin.py data/$s.json data/$s.nkb --check
done
nix eval --impure --expr '(import ./test_correctness.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin.nix) "large"'
python3 bench.py                                 # NFK cold+warm+floors -> bench_results.json
python3 bench_marginal.py                        # NFK per-lookup slopes -> bench_marginal.json
python3 bench.py --kv ./kv_bin.nix --ext nkb --label nkb \
    --out bench_nkb_results.json                 # NKB, same harness
python3 bench_marginal.py --kv ./kv_bin.nix --ext nkb --label nkb \
    --out bench_marginal_nkb.json
```

Example lookups (same key, both formats — same value):

```sh
nix eval --impure --raw --expr \
  '((import ./kv.nix) ./data/large.nfd).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK, ~99 ms)

nix eval --impure --raw --expr \
  '((import ./kv_bin.nix) ./data/large.nkb).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NKB, ~103 ms wall in one sample)
```
