# Fast key/value lookup for native Nix: NFK (hash) and NKB (binary search)

A lookup function for static string→string tables in **pure Nix** (no
`builtins.exec`, no `builtins.fromJSON`, no foreign interpreters), backed by a
precomputed database file read with `builtins.readFile` and sliced with
`builtins.substring`. Three formats:

- **NFK v1** — hash-based: sha256 fingerprint + linear probing (open
  addressing), 40-byte index entry, load factor ≤ 0.5.
- **NFK v2** ("dense NFK") — same hash + probe scheme, but a 22-byte index
  entry, load factor ≤ 0.8, and a shorter 32-bit fingerprint: ~1.8× smaller
  than NFK v1 at equal correctness.
- **NKB v1** — byte-sorted keys + binary search, no hashing at all (the
  implementation is literally `readFile` + `substring` + arithmetic).

## Summary

| | result |
|---|---|
| Correctness | 753,015 lookups across all three formats verified against a `fromJSON` oracle: **0 mismatches** (all 3 datasets, every key, plus miss and edge-case checks) |
| Cold single lookup, 200k entries | NKB **79.6 ms** ≈ NFK v2 **81.2 ms** vs NFK v1 **97.8 ms** vs fromJSON **209.2 ms** → 2.63× / 2.58× / 2.14× |
| Cold single lookup, 50k entries | NFK v2 **42.0 ms** ≈ NKB 42.7 vs NFK v1 46.6 vs fromJSON 78.6 ms → 1.87× / 1.84× / 1.69× |
| 200 lookups in one eval, 200k entries | NFK v2 **84.5 ms** vs NKB 97.5 vs NFK v1 105.6 vs fromJSON 217.3 ms → 2.57× / 2.23× / 2.06× |
| Cold single lookup, 1k entries | 33–35 ms all methods → parity (Nix process startup dominates) |
| In-process per-lookup (after load) | fromJSON attrset < 1 µs; NFK v1 ≈ 0–15 µs; NFK v2 ≈ 16–22 µs; NKB ≈ 54–101 µs — see trade-offs |

The headline: every `nix eval` is a cold process that must load its data
source. `builtins.fromJSON` pays a **full parse of the whole file** on every
invocation (≈150–210 ms for the 14 MB table below), regardless of how many
keys you look up. The custom formats replace that parse with a byte read:
NFK pays one `hashString` plus a ≤ M-slot probe; NKB pays a ≤ ⌈log₂(N)⌉
binary search over byte-sorted keys. NFK v2 (dense) keeps the fast hash
lookup while shrinking the index to 22-byte entries at load ≤ 0.8, cutting
the file from 33.3 MB to 18.1 MB at 200k keys — and in this session's
benchmarks it is the best overall choice at 50k+ entries: tied with NKB on
cold lookups, clearly faster than both NKB and NFK v1 when one eval does
hundreds of lookups.

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

## File format: NFK v2 (dense)

NFK v1's file is dominated by the index: at 200k keys the index is 40 B ×
M = 40 × 524,288 = 20.9 MB of a 33.3 MB file (63%), because M is sized for
load factor ≤ 0.5 (M = 2N rounded up to a power of two) and every *slot* —
including the ~194k unused ones — costs 40 bytes. NFK v2 ("dense NFK")
shrinks that region three ways while keeping the identical
sha256-fingerprint + linear-probe scheme:

1. **Denser table**: `M = next_pow2(max(16, ceil(1.25·N)))` → load factor ≤
   0.8 (at 200k: M = 262,144 instead of 524,288 — half the slots).
   Expected probes: hit ≈ 1.9, miss ≈ 0.79 (v1: hit ≈ 1.19, miss ≈ 0.38).
2. **22-byte entry instead of 40**: fingerprint cut from 16 to 8 hex chars
   (32 bits), `keyOff` in 6 base-36 digits (10 decimal), `keyLen`/`valLen`
   in 4 base-36 digits (6/8 decimal).
3. **Data region byte-identical to v1** — all savings come from the index.

```
offset 0                        header, 64 bytes
offset 64                       index region, M × 22 bytes
offset 64 + M·22                data region, variable
```

**Header (64 bytes)**

| field | offset | width | value |
|---|---|---|---|
| magic | 0 | 4 | `NFK2` |
| version | 4 | 2 | `02` |
| algo | 6 | 2 | `sh` (sha256) |
| M | 8 | 10 | table size, zero-padded decimal (power of two) |
| N | 18 | 10 | entry count, zero-padded decimal |
| – | 28 | 36 | reserved (spaces) |

**Index region** — one 22-byte entry per table slot `s`, at offset `64 + 22s`:

| field | offset | width | meaning |
|---|---|---|---|
| `fp` | 0 | 8 | first 8 hex chars of `sha256(key)`; `gggggggg` if slot unused |
| `keyOff` | 8 | 6 | base-36, byte offset of the key in the data region |
| `keyLen` | 14 | 4 | base-36, byte length of the key |
| `valLen` | 18 | 4 | base-36, byte length of the value (value at `keyOff + keyLen`) |

**Data region** — identical to v1: concatenated `key bytes ++ value bytes`
per entry, insertion order, absolute offsets from the region start.

**Limits**: data region < 36⁶ (~2.18 GB); key/value < 36⁴ (~1.68 MB);
M ≤ 10⁹ (header).

**Collision note** — a 32-bit fingerprint means ~N²/2³² ≈ 5 false fp matches
are expected in a 200k table. A false match only costs one extra key read
and compare (the probe continues on mismatch), so lookups stay exactly
correct; it is pure probe-time overhead, not a correctness risk.

Lookup is v1's algorithm verbatim with the shorter fields: `s0 =
int(h[56..64),16) & (M−1)`, then probe fp 8 chars, decode `keyOff`/`keyLen`
base-36 (single-char table `b36` + `foldl'`), compare the key, return the
value slice.

## Implementation

Files in this repo:

| file | purpose |
|---|---|
| `kv.nix` | NFK v1 lookup module (self-contained, no generator) |
| `kv2.nix` | NFK v2 (dense) lookup module (self-contained, no generator) |
| `kv_bin.nix` | NKB lookup module (self-contained, no generator) |
| `build_db.py` | JSON → NFK v1 builder (`--check` round-trips every key) |
| `build_db2.py` | JSON → NFK v2 (dense) builder (`--check`, `--m-factor`) |
| `build_db_bin.py` | JSON → NKB builder (`--check` round-trips every key) |
| `gen_data.py` | test data generation (1k / 50k / 200k keys + edge cases) |
| `gen_kv.py` | emits `kv.nix` (inlines the 100-entry two-digit table to avoid typos) |
| `gen_kv2.py` | emits `kv2.nix` (inlines the 36-entry base-36 digit table) |
| `gen_kv_bin.py` | emits `kv_bin.nix` (inlines the 255-entry base-255 pair table) |
| `test_correctness.nix`, `test_correctness2.nix`, `test_correctness_bin.nix` | Nix-side oracle tests vs `fromJSON` (NFK v1, v2, NKB) |
| `bench.py`, `bench_marginal.py` | benchmark harnesses (parameterized: `--kv/--ext/--label/--out`) |
| `data/` | `small\|medium\|large.{json,nfd,nfd2,nkb}` |

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
python3 build_db.py      input.json out.nfd  --check   # NFK v1
python3 build_db2.py     input.json out.nfd2 --check   # NFK v2 (dense, m-factor 1.25)
python3 build_db_bin.py  input.json out.nkb  --check   # NKB
```

Builder guarantees: keyLen ≤ 6 digits, valLen ≤ 8 digits, keyOff ≤ 10 digits
(raises otherwise); the Python hash is byte-identical to Nix's
(`hashlib.sha256(k).hexdigest()` → same `fp` and same `int(h[-8:],16) & (M-1)`
slot), which the cross-language round-trip proves.

NFK v2 builder guarantees (`build_db2.py`): key/value < 36⁴ bytes, data
region < 36⁶, M ≤ 10⁹ (raises otherwise); same byte-identical hash/slot
computation as v1 (so a v1 lookup on a v2 file would still work if the
entry widths matched — they don't, the formats differ in the index).

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

NFK v2 (`kv2.nix`) additionally relies on:

- base-36 single-char decoding: `to36 = s: foldl' (acc: i: acc*36 +
  b36."${substring i 1 s}")` over the inlined 36-entry table (generated by
  `gen_kv2.py`); the header's 10-digit decimal `M`/`N` still use v1's `toDec`.

NKB v1 (`kv_bin.nix`) additionally relies on:

- no `builtins.parseInt` → base-255 fields decode through the inlined
  255-entry two-char pair table: `pair = s: i: b2."${builtins.substring i 2
  s}"`, folded Horner-style (`p0 + 255*(p1 + 255*(p2 + 255*p3))`).
- integer division truncates (`(lo + hi) / 2`), which is what the binary-
  search midpoint needs.
- `<`/`==` on bytes read from I/O are unsigned byte-lexicographic — the same
  order the builder sorts by (verified on raw 0x01–0xFF bytes).

## Correctness

Method (per format; all three formats run the identical checks):

1. **Python round-trip** (`build_db.py --check` / `build_db2.py --check` /
   `build_db_bin.py --check`): after building each file, re-parse it
   independently and check every key via the Python port of the same
   algorithm (probe for NFK v1/v2, binary search for NKB), plus a
   known-absent key.
2. **Nix oracle** (`test_correctness.nix` / `test_correctness2.nix` /
   `test_correctness_bin.nix`): `fromJSON` is used *only here* as the
   reference. For every key `k` in the JSON: `db.get k == j."${k}"`; a
   missing key returns `null`; `db.has` agrees with `?` for present and
   absent keys; `db.count == length (attrNames j)`.

Results (Nix 2.34.7+1):

| dataset | format | keys | mismatches | miss→null | `has` present/absent | count ok |
|---|---|---:|---|---|---|---|
| small | NFK v1 | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| small | NFK v2 | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| small | NKB | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NFK v1 | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NFK v2 | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NKB | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NFK v1 | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NFK v2 | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NKB | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |

Edge cases (in `small`): empty key → empty value, 1-char key, unicode key and
value (multi-byte UTF-8), keys and values containing spaces, and
`dotted.nested.attr.name`. All three formats pass identically — lookups
compare and return the UTF-8 bytes directly, so multi-byte characters and
spaces are handled by construction.

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

### Four-method results (single session, identical harness)

All four methods re-run in one session (benchmarks are only comparable
within a session). Medians as above.

**Cold single lookup (median of 15, full `nix eval` wall clock):**

| dataset | fromJSON `!` | NFK v1 | NFK v2 | NKB | best vs fromJSON |
|---|---:|---:|---:|---:|---:|
| small (1k) | 34.4 ms | 33.5 ms | 33.9 ms | 34.5 ms | parity (startup-bound) |
| medium (50k) | 78.6 ms | 46.6 ms | **42.0 ms** | 42.7 ms | NFK v2 (1.87×) |
| large (200k) | 209.2 ms | 97.8 ms | 81.2 ms | **79.6 ms** | NKB (2.63×) |

**Cold miss (median of 15):**

| dataset | fromJSON `hasAttr` | NFK v1 | NFK v2 | NKB | best vs fromJSON |
|---|---:|---:|---:|---:|---:|
| small | 34.3 ms | 33.7 ms | 33.2 ms | 34.7 ms | parity |
| medium | 78.6 ms | 45.9 ms | **42.2 ms** | 42.7 ms | NFK v2 (1.86×) |
| large | 208.8 ms | 97.3 ms | 80.8 ms | **80.2 ms** | NKB (2.60×) |

**Warm 200 lookups, single eval (median of 3):**

| dataset | fromJSON | NFK v1 | NFK v2 | NKB | best vs fromJSON |
|---|---:|---:|---:|---:|---:|
| small | 34.8 ms | 37.2 ms | 37.4 ms | 41.5 ms | fromJSON (startup-bound) |
| medium | 77.8 ms | 50.0 ms | **45.6 ms** | 57.2 ms | NFK v2 (1.71×) |
| large | 217.3 ms | 105.6 ms | **84.5 ms** | 97.5 ms | NFK v2 (2.57×) |

**Floors (median):**

| floor | small | medium | large |
|---|---:|---:|---:|
| `nix eval` startup | ~33–34 ms | ~33–34 ms | ~33–34 ms |
| `readFile` JSON | 33.4 ms | 41.1 ms | 55.3 ms |
| `readFile` NFK v1 (.nfd) | 33.0 ms | 45.2 ms | 100.8 ms |
| `readFile` NFK v2 (.nfd2) | 30.6 ms | 40.2 ms | 80.0 ms |
| `readFile` NKB (.nkb) | 32.7 ms | 41.2 ms | 76.6 ms |
| `fromJSON` + `attrNames` (parse) | 33.8 ms | 86.6 ms | 261.7 ms |

**Marginal in-process per-lookup, all methods (least-squares slope):**

| dataset | fromJSON | NFK v1 | NFK v2 | NKB |
|---|---:|---:|---:|---:|
| small | ~4 µs (noise) | ≈ 13 µs | ≈ 18 µs | ≈ 54 µs |
| medium | ~1 µs | ≈ 15 µs | ≈ 22 µs | ≈ 101 µs |
| large | ≈ 0 (noise) | ≈ 0–15 µs (noise) | ≈ 16 µs | ≈ 78 µs |

NFK v1's large slope is session-noisy (−17 µs this run — i.e. at the noise
floor; 11–18 µs in earlier sessions). NFK v2's ~16–22 µs is stable across
sessions and datasets; its probe runs ~2× longer than v1's (load 0.76 vs
0.38) but each step is cheaper (22-byte entry, 8-char fp). NKB's slope is
dominated by ~10–18 index-entry decodes (base-255 pair table) plus one key
`substring` + compare per step.

### Head to head (large, 200k)

| workload | NFK v1 | NFK v2 | NKB | winner |
|---|---:|---:|---:|---|
| Cold single lookup | 97.8 ms | 81.2 ms | **79.6 ms** | NKB (marginal) |
| Cold miss | 97.3 ms | 80.8 ms | **80.2 ms** | NKB (marginal) |
| Warm 200 lookups | 105.6 ms | **84.5 ms** | 97.5 ms | NFK v2 (1.15×) |
| File size | 33.3 MB | 18.1 MB | **17.1 MB** | NKB (1.06× smaller) |
| readFile floor | 100.8 ms | 80.0 ms | **76.6 ms** | NKB (−3.4 ms) |
| Marginal per-lookup | ~0–15 µs | ~16–22 µs | ~78–101 µs | NFK v1/v2 |

Where the numbers come from: NKB's cold edge is exactly its readFile
advantage (76.6 vs 80.0 ms floor). NFK v2's warm win is its per-lookup
speed: ~16–22 µs vs ~78–101 µs saves ~12 ms over 200 lookups — more than
the 3.4 ms floor gap. NFK v1 lost its warm lead in this session (105.6
ms): its 20.8 ms floor disadvantage (33.3 MB file) outweighs its small
per-lookup edge. At medium, NFK v2 wins every workload — its read floor is
already 1.0 ms below NKB's (40.2 vs 41.2) *and* its lookups are ~4× faster.

### Analysis

Cost model, verified against the floors:

```
fromJSON(n)  ≈ 34 + read(JSON)  + parse(JSON) + sub-µs·n
NFK v1(n)    ≈ 34 + read(.nfd)  + import + ~(0–15 µs)·n
NFK v2(n)    ≈ 34 + read(.nfd2) + import + ~(16–22 µs)·n
NKB(n)       ≈ 34 + read(.nkb)  + import + ~(54–101 µs)·n
```

At large: parse ≈ 150 ms vs a ~20–55 ms readFile floor for the custom
formats (100.8 / 80.0 / 76.6 ms); all custom formats buy back the parse
with a per-lookup tax.

Crossover with `fromJSON` (equal total time; load edge ÷ per-lookup tax):

| dataset | NFK v1 | NFK v2 | NKB |
|---|---:|---:|---:|
| small | ≈ tie (startup-bound) | ≈ tie (startup-bound) | ≈ tie (startup-bound) |
| medium | ≈ 2,200 lookups/eval | ≈ 1,700 lookups/eval | ≈ 360 lookups/eval |
| large | ≈ 7,000–60,000 (slope-noisy) | ≈ 6,700 lookups/eval | ≈ 1,700 lookups/eval |

Crossover **between the custom formats**:

- **NKB vs NFK v2** (large): NKB's 3.4 ms read advantage ÷ (78 − 19) µs
  ≈ 58 lookups/eval — NKB below it, NFK v2 above; at medium NFK v2's floor
  is already lower, so NFK v2 wins throughout.
- **NFK v2 vs NFK v1**: v1's per-lookup edge (≤ ~10 µs/lookup) would need
  ~2,000+ lookups to close v2's 20.8 ms floor advantage — v2 dominates
  v1 at 50k+ entries.
- **NKB vs NFK v1** (large): 24.2 ms ÷ (78 − ~10) µs ≈ 350 lookups/eval.

- **Below the `fromJSON` crossovers** (one to a few hundred lookups per
  eval — the common case for Nix tooling, since each `nix eval`/`nix build`
  re-evaluates from scratch) **all custom formats win over `fromJSON`**,
  and the win grows with table size.
- **NFK v2 is the default choice at 50k+ entries**: smallest file among the
  hash formats (18.1 MB), tied with NKB on cold lookups, clearly fastest
  when one eval does hundreds of lookups (84.5 ms warm-200 at 200k vs
  NKB 97.5 and v1 105.6).
- **NKB stays the choice for zero hashing** and the absolute smallest file
  (17.1 MB), with the deterministic worst case (no probe runs, no
  fingerprint collisions).
- **NFK v1** (40-byte entries, load ≤ 0.5) is kept for reference; v2 shows
  the density/entry-size headroom v1 left unused.
- **Above the `fromJSON` crossover** (bulk in-process scans) `fromJSON`'s
  sub-µs attrset access wins per-lookup, since its parse was paid once.
- File sizes vs JSON (13.9 MB at large): NFK v1 2.40×, NFK v2 1.30×,
  NKB 1.23×. A binary index would be smaller still, but its decode tables
  need high-byte attrset keys that `.nix` source cannot express
  (verified), and diffability is lost either way.

## Trade-offs, stated plainly

1. **Per-lookup, warm, in-process: `fromJSON` is fastest** (sub-µs
   attrset), then NFK v1 (~0–15 µs), NFK v2 (~16–22 µs), NKB (~54–101 µs).
   No custom format beats scanning an entire table in one eval.
2. **Cold / repeated invocations: all custom formats beat `fromJSON`** by
   1.87–2.63× at 50k–200k entries (NKB 2.63× / NFK v2 2.58× at 200k); at
   ~1k entries all four tie at startup cost.
3. **File size:** NFK v2 exists precisely because NFK v1's index was
   63% of its file (40 B × M at load ≤ 0.5). v2's 22-byte entries at
   load ≤ 0.8 put the large file at 18.1 MB = 1.30× JSON (index 32%);
   NKB is 1.23× JSON (index 28%).
4. **NKB has no hashing at all** — literally `readFile` + `substring` +
   arithmetic; NFK v1/v2 pay one sha256 per lookup. NFK v2's 32-bit
   fingerprint means ~5 false fp matches expected at 200k — each costs one
   extra key compare, never a wrong answer.
5. **Static only.** Updates require re-running the builder (by design).
6. **No `builtins.parseInt`/`%`/`mod`** on this Nix: v1's decimal decoding
   costs a small fold per field; v2's base-36 decode costs 4–6 table
   lookups per field; NKB's base-255 decode costs 2–4 per field. A Nix
   with `parseInt` would shave a few µs per lookup from all three.
7. **Memory:** the whole file string is held in the evaluator (same as
   `readFile` JSON); the `fromJSON` variant additionally holds a 200k-key
   attrset.

**Use NFK v2** as the default for 50k+ key tables: best file size among
the hash formats, cold lookups tied with NKB, and the fastest warm
multi-lookup numbers. **Use NKB** when zero hashing or the absolute
smallest file matters (and lookups per eval are few). **Use `fromJSON`**
when one evaluation touches a large fraction of the table, or when
simplicity and tooling around JSON outweigh a hundred or two milliseconds.

## Reproducing

```sh
python3 gen_data.py                              # data/{small,medium,large}.json
for s in small medium large; do
  python3 build_db.py      data/$s.json data/$s.nfd  --check   # NFK v1
  python3 build_db2.py     data/$s.json data/$s.nfd2 --check   # NFK v2
  python3 build_db_bin.py  data/$s.json data/$s.nkb  --check   # NKB
done
nix eval --impure --expr '(import ./test_correctness.nix) "large"'
nix eval --impure --expr '(import ./test_correctness2.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin.nix) "large"'
python3 bench.py                                 # NFK v1 cold+warm+floors -> bench_results.json
python3 bench_marginal.py                        # NFK v1 per-lookup slopes
python3 bench.py --kv ./kv2.nix --ext nfd2 --label nfk2 \
    --out bench_nfk2_results.json                # NFK v2, same harness
python3 bench_marginal.py --kv ./kv2.nix --ext nfd2 --label nfk2 \
    --out bench_marginal_nfk2.json
python3 bench.py --kv ./kv_bin.nix --ext nkb --label nkb \
    --out bench_nkb_results.json                 # NKB, same harness
python3 bench_marginal.py --kv ./kv_bin.nix --ext nkb --label nkb \
    --out bench_marginal_nkb.json
```

Example lookups (same key, all three formats — same value):

```sh
nix eval --impure --raw --expr \
  '((import ./kv.nix) ./data/large.nfd).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK v1, ~98 ms)

nix eval --impure --raw --expr \
  '((import ./kv2.nix) ./data/large.nfd2).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK v2, ~81 ms)

nix eval --impure --raw --expr \
  '((import ./kv_bin.nix) ./data/large.nkb).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NKB, ~80 ms)
```
