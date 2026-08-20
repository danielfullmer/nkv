# Fast key/value lookup for native Nix: NFK (hash) and NKB (binary search)

A lookup function for static string→string tables in **pure Nix** (no
`builtins.exec`, no `builtins.fromJSON`, no foreign interpreters), backed by a
precomputed database file read with `builtins.readFile` and sliced with
`builtins.substring`. Four formats:
- **NFK v1** — hash-based: sha256 fingerprint + linear probing (open
  addressing), 40-byte index entry, load factor ≤ 0.5.
- **NFK v2** ("dense NFK") — same hash + probe scheme, but a 22-byte index
  entry, load factor ≤ 0.8, and a shorter 32-bit fingerprint: ~1.8× smaller
  than NFK v1 at equal correctness.
- **NKB v1** — byte-sorted keys + binary search, no hashing at all (the
  implementation is literally `readFile` + `substring` + arithmetic).
- **NKB v2** ("binary NKB") — same sorted keys + binary search, but a raw
  binary index (14-byte entries, no NUL anywhere); the byte→int decode
  table is embedded in the file and built at import time.

## Summary

| | result |
|---|---|
| Correctness | 1,004,020 lookups across all four formats verified against a `fromJSON` oracle: **0 mismatches** (all 3 datasets, every key, plus miss and edge-case checks) |
| Cold single lookup, 200k entries | NKB v2 **61.6 ms** vs NFK v2 79.5 ≈ NKB v1 80.7 vs NFK v1 98.0 vs fromJSON **211.5 ms** → 3.43× / 2.66× / 2.62× / 2.15× |
| Cold single lookup, 50k entries | NKB v2 **40.0 ms** vs NKB v1 41.7 ≈ NFK v2 42.5 vs NFK v1 46.5 vs fromJSON **78.4 ms** → 1.96× / 1.88× / 1.84× / 1.69× |
| 200 lookups in one eval, 200k entries | NFK v2 **83.6 ms** vs NKB v2 85.8 vs NKB v1 96.1 vs NFK v1 99.7 vs fromJSON **210.7 ms** → 2.52× / 2.46× / 2.19× / 2.11× |
| Cold single lookup, 1k entries | 33–35 ms all methods → parity (Nix process startup dominates) |
| In-process per-lookup (after load) | fromJSON attrset < 1 µs; NFK v1 ≈ 0–17 µs; NFK v2 ≈ 15–22 µs; NKB v1 ≈ 40–92 µs; NKB v2 ≈ 46–105 µs — see trade-offs |

The headline: every `nix eval` is a cold process that must load its data
source. `builtins.fromJSON` pays a **full parse of the whole file** on every
invocation (≈150–210 ms for the 14 MB table below), regardless of how many
keys you look up. The custom formats replace that parse with a byte read:
NFK pays one `hashString` plus a ≤ M-slot probe; NKB pays a ≤ ⌈log₂(N)⌉
binary search over byte-sorted keys. NFK v2 (dense) keeps the fast hash
lookup while shrinking the index to 22-byte entries at load ≤ 0.8, cutting
the file from 33.3 MB to 18.1 MB at 200k keys. NKB v2 is the smallest file
of all (15.1 MB, 1.09× JSON) and the fastest cold single lookup in this
session (61.6 ms at 200k, 3.4× vs fromJSON); NFK v2 remains the fastest
when one eval does hundreds of lookups (83.6 ms warm-200 at 200k vs
NKB v2 85.8).

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

The 40-byte ASCII index entries are the price of Nix's string-only world —
a constraint NKB v2 later removes by carrying its byte→int decode table in
the file itself (see File format: NKB v2).

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
builder enforces these widths. Raw binary would be denser; the obstacle was
that `.nix` source cannot express high bytes in string literals (they
decode to U+FFFD), so a byte→int decode table had to key on ASCII pairs.
NKB v2 removes that obstacle by reading the byte table from the file
itself — see below.

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

## File format: NKB v2 (binary index)

NKB v1 uses base-255 ASCII structural fields because `.nix` source cannot
express raw high bytes — a string literal with invalid UTF-8 decodes to
U+FFFD — so a byte→int decode table had to key on ASCII pairs. NKB v2
sidesteps that: the decode table does not live in the `.nix` source at all.
The file carries it (255 bytes `0x01 … 0xFF` at offset 64), and the module
builds the `byte → int` attrset **at import time from the file's own
bytes**: byte strings produced at runtime (here, via `substring` on a
`readFile` result) serve as attrset keys unmangled. That unlocks a raw
binary index — every byte in the file is `0x01..0xFF`, so it never contains
NUL, the only byte `readFile` rejects.

Structural fields are **b254**: one byte per digit, `byte = digit + 1`,
big-endian, so digits 0–253 map to bytes `0x01`–`0xFF`. 3 digits cover
< 254³ ≈ 16.4 MB (lengths, totals); 4 digits cover < 254⁴ ≈ 4.16 GB
(offsets). The lookup is NKB v1's binary search verbatim.

```
offset 0                        header, 64 bytes
offset 64                       byte table T: 0x01 … 0xFF (255 bytes)
offset 319                      index region, N × 14 bytes
offset 319 + N·14               data region: all keys (sorted), then all values
```

**Header (64 bytes)**

| field | offset | width | value |
|---|---|---|---|
| magic | 0 | 4 | `NKB2` |
| N | 4 | 3 | entry count (3 b254 bytes) |
| keyTotal | 7 | 3 | total key bytes (3 bytes) |
| valTotal | 10 | 3 | total value bytes (3 bytes) |
| – | 13 | 51 | reserved (spaces) |

**Index region** — one 14-byte entry per key `i` at offset `319 + 14i`;
keys strictly ascending in UTF-8 byte order (same order as NKB v1):

| field | offset | width | meaning |
|---|---|---|---|
| `off_k` | 0 | 4 | file offset of the key |
| `len_k` | 4 | 3 | key length |
| `off_v` | 7 | 4 | file offset of the value |
| `len_v` | 11 | 3 | value length |

Offsets are absolute from file start. **Limits**: key/value < 254³ bytes
(≈ 16.4 MB each), each total < 254³, file < 254⁴ (builder enforces; the
independent `--check` parser re-validates all of it, including the embedded
table byte-for-byte and the exact file size).

Measured sizes (JSON → NKB v2):

| dataset | keys | JSON bytes | NKB v2 bytes | index bytes | ratio |
|---|---|---:|---:|---:|---:|
| small | 1,005 | 68,534 | 74,883 | 14,070 | 1.09× |
| medium | 50,000 | 3,463,238 | 3,763,557 | 700,000 | 1.09× |
| large | 200,000 | 13,941,356 | 15,141,675 | 2,800,000 | 1.09× |

14-byte entries vs v1's 24-byte entries (1.71× denser); the large file is
2.0 MB (11.7%) smaller than NKB v1, and the whole table is 1.09× the JSON
size vs 1.23×. The 255-byte table is 0.2% of the large file.

## Implementation

Files in this repo:

| file | purpose |
|---|---|
| `kv.nix` | NFK v1 lookup module (self-contained, no generator) |
| `kv2.nix` | NFK v2 (dense) lookup module (self-contained, no generator) |
| `kv_bin.nix` | NKB lookup module (self-contained, no generator) |
| `kv_bin2.nix` | NKB v2 (binary index) lookup module (byte table read from file) |
| `build_db.py` | JSON → NFK v1 builder (`--check` round-trips every key) |
| `build_db2.py` | JSON → NFK v2 (dense) builder (`--check`, `--m-factor`) |
| `build_db_bin.py` | JSON → NKB builder (`--check` round-trips every key) |
| `build_db_bin2.py` | JSON → NKB v2 builder (`--check` round-trips every key) |
| `gen_data.py` | test data generation (1k / 50k / 200k keys + edge cases) |
| `gen_kv.py` | emits `kv.nix` (inlines the 100-entry two-digit table to avoid typos) |
| `gen_kv2.py` | emits `kv2.nix` (inlines the 36-entry base-36 digit table) |
| `gen_kv_bin.py` | emits `kv_bin.nix` (inlines the 255-entry base-255 pair table) |
| `test_correctness.nix`, `test_correctness2.nix`, `test_correctness_bin.nix`, `test_correctness_bin2.nix` | Nix-side oracle tests vs `fromJSON` (NFK v1/v2, NKB v1/v2) |
| `bench.py`, `bench_marginal.py` | benchmark harnesses (parameterized: `--kv/--ext/--label/--out`) |
| `data/` | `small\|medium\|large.{json,nfd,nfd2,nkb,nkb2}` |

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
NKB v2 (`kv_bin2.nix`, `*.nkb2` files) exposes the identical API.

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

NKB v2 builder guarantees: key/value < 254³ bytes, each total < 254³, file
< 254⁴ (raises otherwise); `--check` re-validates the embedded 255-byte
table and the exact file size; keys sorted in the same byte order as NKB v1.

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

NKB v2 (`kv_bin2.nix`) additionally relies on:

- runtime byte strings as attrset keys: the 255-byte table embedded at file
  offset 64 is folded at import into `{ byte → int }` via `substring` +
  `foldl'` — no high byte ever appears in `.nix` source (only source
  *literals* are UTF-8-decoded to U+FFFD; bytes from I/O are not).
- b254 decoding: one `byte` lookup per digit, Horner-style `dec3`/`dec4`.

## Correctness

Method (per format; all four formats run the identical checks):

1. **Python round-trip** (`build_db.py --check` / `build_db2.py --check` /
   `build_db_bin.py --check` / `build_db_bin2.py --check`): after building each file, re-parse it
   independently and check every key via the Python port of the same
   algorithm (probe for NFK v1/v2, binary search for NKB), plus a
   known-absent key.
2. **Nix oracle** (`test_correctness.nix` / `test_correctness2.nix` /
   `test_correctness_bin.nix` / `test_correctness_bin2.nix`): `fromJSON` is used *only here* as the
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
| small | NKB v2 | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NKB v2 | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NKB v2 | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |

Edge cases (in `small`): empty key → empty value, 1-char key, unicode key and
value (multi-byte UTF-8), keys and values containing spaces, and
`dotted.nested.attr.name`. All four formats pass identically — lookups
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

The original report's per-format (two-method) tables — cold lookup, cold
miss, warm 200, floors, marginal slopes — are superseded by the five-method
same-session results below; their numbers came from earlier sessions.

### Five-method results (single session, identical harness)

All five methods re-run in one session (benchmarks are only comparable
within a session); `fromJSON` medians are taken across the four runs
(per-run spread ≤ 3.4 ms at 200k).

**Cold single lookup (median of 15, full `nix eval` wall clock):**

| dataset | fromJSON `!` | NFK v1 | NFK v2 | NKB v1 | NKB v2 | best vs fromJSON |
|---|---:|---:|---:|---:|---:|---:|
| small (1k) | 33.7 ms | 33.7 ms | 34.1 ms | 33.0 ms | 34.2 ms | parity (startup-bound) |
| medium (50k) | 78.4 ms | 46.5 ms | 42.5 ms | 41.7 ms | **40.0 ms** | NKB v2 (1.96×) |
| large (200k) | 211.5 ms | 98.0 ms | 79.5 ms | 80.7 ms | **61.6 ms** | NKB v2 (3.43×) |

**Cold miss (median of 15):**

| dataset | fromJSON `hasAttr` | NFK v1 | NFK v2 | NKB v1 | NKB v2 | best vs fromJSON |
|---|---:|---:|---:|---:|---:|---:|
| small | 34.3 ms | 33.1 ms | 33.7 ms | 33.4 ms | 33.1 ms | parity |
| medium | 77.9 ms | 46.0 ms | 42.2 ms | 42.3 ms | **39.8 ms** | NKB v2 (1.96×) |
| large | 209.7 ms | 98.2 ms | 79.5 ms | 79.6 ms | **60.9 ms** | NKB v2 (3.44×) |

**Warm 200 lookups, single eval (median of 3):**

| dataset | fromJSON | NFK v1 | NFK v2 | NKB v1 | NKB v2 | best vs fromJSON |
|---|---:|---:|---:|---:|---:|---:|
| small | 34.0 ms | 38.1 ms | 37.3 ms | 43.2 ms | 43.9 ms | fromJSON (startup-bound) |
| medium | 78.9 ms | 48.7 ms | **46.0 ms** | 55.9 ms | 56.1 ms | NFK v2 (1.71×) |
| large | 210.7 ms | 99.7 ms | **83.6 ms** | 96.1 ms | 85.8 ms | NFK v2 (2.52×) |

**Floors (median):**

| floor | small | medium | large |
|---|---:|---:|---:|
| `nix eval` startup | ~33–34 ms | ~33–34 ms | ~33–34 ms |
| `readFile` JSON | 33.1 ms | 39.9 ms | 56.2 ms |
| `readFile` NFK v1 (.nfd) | 32.0 ms | 46.3 ms | 98.7 ms |
| `readFile` NFK v2 (.nfd2) | 32.5 ms | 41.2 ms | 77.8 ms |
| `readFile` NKB v1 (.nkb) | 31.4 ms | 42.2 ms | 81.7 ms |
| `readFile` NKB v2 (.nkb2) | 33.6 ms | 39.5 ms | 59.5 ms |
| `fromJSON` + `attrNames` (parse) | 34.3 ms | 86.9 ms | 263.4 ms |

**Marginal in-process per-lookup, all methods (least-squares slope):**

| dataset | fromJSON | NFK v1 | NFK v2 | NKB v1 | NKB v2 |
|---|---:|---:|---:|---:|---:|
| small | ≈ 3 µs (noise) | ≈ 12 µs | ≈ 15 µs | ≈ 40 µs | ≈ 46 µs |
| medium | ≈ 6–12 µs (noise) | ≈ 17 µs | ≈ 20 µs | ≈ 92 µs | ≈ 89 µs |
| large | ≈ 0–5 µs (noise) | ≈ −22 µs (noise floor) | ≈ 22 µs | ≈ 92 µs | ≈ 105 µs |

NFK v1's large slope is again at/below the noise floor (−22 µs this run —
i.e. sub-µs). NFK v2's ~15–22 µs is stable across sessions and datasets;
its probe runs ~2× longer than v1's (load 0.76 vs 0.38) but each step is
cheaper (22-byte entry, 8-char fp). NKB v1's slope (~92 µs) is ~10–18
index-entry decodes (base-255 pair table) plus one key `substring` + compare
per step. NKB v2's (~105 µs) is the same search with b254 entries (7 one-byte
table lookups per entry vs 6 pair lookups) — slightly pricier per step, but
its readFile floor is 22 ms lower at 200k (59.5 vs 81.7 ms), which is what
makes it the cold-lookup winner.

### Head to head (large, 200k)

| workload | NFK v1 | NFK v2 | NKB v1 | NKB v2 | winner |
|---|---:|---:|---:|---:|---|
| Cold single lookup | 98.0 ms | 79.5 ms | 80.7 ms | **61.6 ms** | NKB v2 (1.29×) |
| Cold miss | 98.2 ms | 79.5 ms | 79.6 ms | **60.9 ms** | NKB v2 (1.30×) |
| Warm 200 lookups | 99.7 ms | **83.6 ms** | 96.1 ms | 85.8 ms | NFK v2 (1.03× over NKB v2) |
| File size | 33.3 MB | 18.1 MB | 17.1 MB | **15.1 MB** | NKB v2 (1.13× smaller than NKB v1) |
| readFile floor | 98.7 ms | 77.8 ms | 81.7 ms | **59.5 ms** | NKB v2 (−18.3 ms vs NFK v2) |
| Marginal per-lookup | ~0–17 µs | ~15–22 µs | ~40–92 µs | ~46–105 µs | NFK v2 |

Where the numbers come from: NKB v2's cold edge is exactly its readFile
advantage (59.5 vs 77.8/81.7 ms floor) — the binary index made the file
11.7% smaller than NKB v1 and the floor dropped 22 ms with it. NFK v2's
warm win is its per-lookup speed: ~22 µs vs ~105 µs saves ~16.6 ms over
200 lookups — just enough to beat NKB v2's 18.3 ms floor advantage by a
2.2 ms margin (83.6 vs 85.8; the crossover sits at ~220 lookups/eval).
NKB v1 is now dominated by NKB v2 on every workload; NFK v1's 33.3 MB file
keeps its floor 19–40 ms above the rest, which it can't buy back at this
session's per-lookup rates.

### Analysis

Cost model, verified against the floors:

```
fromJSON(n)  ≈ 34 + read(JSON)  + parse(JSON) + sub-µs·n
NFK v1(n)    ≈ 34 + read(.nfd)  + import + ~(0–17 µs)·n
NFK v2(n)    ≈ 34 + read(.nfd2) + import + ~(15–22 µs)·n
NKB v1(n)    ≈ 34 + read(.nkb)  + import + ~(40–92 µs)·n
NKB v2(n)    ≈ 34 + read(.nkb2) + import + ~(46–105 µs)·n
```

At large: parse ≈ 207 ms (263.4 − 56.2) vs readFile floors of 98.7 / 77.8 /
81.7 / 59.5 ms for the four custom formats; all custom formats buy back the
parse with a per-lookup tax.

Crossover with `fromJSON` (equal total time; load edge ÷ per-lookup tax):

| dataset | NFK v1 | NFK v2 | NKB v1 | NKB v2 |
|---|---:|---:|---:|---:|
| small | ≈ tie (startup-bound) | ≈ tie (startup-bound) | ≈ tie (startup-bound) | ≈ tie (startup-bound) |
| medium | ≈ 1,700 lookups/eval | ≈ 1,700 lookups/eval | ≈ 430 lookups/eval | ≈ 410 lookups/eval |
| large | slope-noisy (≤0 this run) | ≈ 6,000 lookups/eval | ≈ 1,500 lookups/eval | ≈ 1,400 lookups/eval |

Crossover **between the custom formats** (large; floor gap ÷ per-lookup gap):

- **NKB v2 vs NFK v2**: 18.3 ms ÷ (105 − 22) µs ≈ 220 lookups/eval — NKB
  v2 below it, NFK v2 above; at 200 lookups they are within 2.2 ms
  (85.8 vs 83.6).
- **NKB v2 vs NKB v1**: 22.2 ms ÷ (105 − 92) µs ≈ 1,700 lookups/eval —
  NKB v2 wins below that; at 200 lookups by 10.3 ms (85.8 vs 96.1).
- **NFK v2 vs NFK v1**: v1's slope is at the noise floor this run, but v2's
  floor is 26.5 ms lower (load 75.3 vs 101.8 ms) and its per-lookup cost is
  at most ~22 µs — v2 dominates v1 at 50k+ entries.
- **NKB v2 vs NFK v1**: 44.8 ms ÷ (105 − ~0) µs ≈ 400 lookups/eval.

- **Below the `fromJSON` crossovers** (one to a few hundred lookups per
  eval — the common case for Nix tooling, since each `nix eval`/`nix build`
  re-evaluates from scratch) **all custom formats win over `fromJSON`**,
  and the win grows with table size.
- **NKB v2 is the default choice for cold single/few-lookup evals at 50k+
  entries**: smallest file of all (15.1 MB, 1.09× JSON), lowest readFile
  floor (59.5 ms), fastest cold lookups (61.6 ms at 200k — 3.4× vs
  fromJSON), still zero hashing with the deterministic worst case.
- **NFK v2 is the choice when one eval does hundreds of lookups**: its
  ~22 µs/lookup beats NKB v2's ~105 µs/lookup above ~220 lookups/eval
  (83.6 ms warm-200 at 200k vs NKB v2 85.8 and NKB v1 96.1).
- **NKB v1 and NFK v1 are kept for reference**: NKB v1 for the diffable
  ASCII index (superseded on every workload by NKB v2), NFK v1 to show the
  density headroom its successors exploit.
- **Above the `fromJSON` crossover** (bulk in-process scans) `fromJSON`'s
  sub-µs attrset access wins per-lookup, since its parse was paid once.
- File sizes vs JSON (13.9 MB at large): NFK v1 2.40×, NFK v2 1.30×,
  NKB v1 1.23×, NKB v2 1.09×. NKB v2's binary index is the smallest file
  of all — possible because the 255-byte decode table is carried by the
  file itself (built at import) rather than written in `.nix` source; the
  price is diffability of the index region (data region unchanged).

## Trade-offs, stated plainly

1. **Per-lookup, warm, in-process: `fromJSON` is fastest** (sub-µs
   attrset), then NFK v1 (~0–17 µs), NFK v2 (~15–22 µs), NKB v1
   (~40–92 µs), NKB v2 (~46–105 µs). No custom format beats scanning an
   entire table in one eval.
2. **Cold / repeated invocations: all custom formats beat `fromJSON`** by
   1.96–3.44× at 50k–200k entries (NKB v2 3.43× cold at 200k); at ~1k
   entries all five tie at startup cost.
3. **File size:** NFK v2 exists precisely because NFK v1's index was
   63% of its file (40 B × M at load ≤ 0.5). v2's 22-byte entries at
   load ≤ 0.8 put the large file at 18.1 MB = 1.30× JSON (index 32%);
   NKB v1 is 1.23× JSON (index 28%); NKB v2's 14-byte binary entries put
   it at 15.1 MB = 1.09× JSON (index 18.5%).
4. **NKB v1/v2 has no hashing at all** — literally `readFile` +
   `substring` + arithmetic; NFK v1/v2 pay one sha256 per lookup. NFK v2's
   32-bit fingerprint means ~5 false fp matches expected at 200k — each
   costs one extra key compare, never a wrong answer.
5. **Static only.** Updates require re-running the builder (by design).
6. **No `builtins.parseInt`/`%`/`mod`** on this Nix: v1's decimal decoding
   costs a small fold per field; v2's base-36 decode costs 4–6 table
   lookups per field; NKB v1's base-255 decode costs 2–4 per field; NKB
   v2's b254 decode costs 3–4 one-byte table lookups per field. A Nix
   with `parseInt` would shave a few µs per lookup from all four.
7. **Memory:** the whole file string is held in the evaluator (same as
   `readFile` JSON); the `fromJSON` variant additionally holds a 200k-key
   attrset.

**Use NKB v2** as the default for 50k+ key tables: smallest file, fastest
cold lookups, zero hashing. **Use NFK v2** when a single evaluation does
hundreds of lookups (its ~22 µs/lookup beats NKB v2's ~105 µs/lookup).
**Use `fromJSON`** when one evaluation touches a large fraction of the
table, or when simplicity and tooling around JSON outweigh a hundred or two
milliseconds.

## Reproducing

```sh
python3 gen_data.py                              # data/{small,medium,large}.json
for s in small medium large; do
  python3 build_db.py      data/$s.json data/$s.nfd  --check   # NFK v1
  python3 build_db2.py     data/$s.json data/$s.nfd2 --check   # NFK v2
  python3 build_db_bin.py  data/$s.json data/$s.nkb  --check   # NKB
  python3 build_db_bin2.py data/$s.json data/$s.nkb2 --check   # NKB v2
done
nix eval --impure --expr '(import ./test_correctness.nix) "large"'
nix eval --impure --expr '(import ./test_correctness2.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin2.nix) "large"'
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
python3 bench.py --kv ./kv_bin2.nix --ext nkb2 --label nkb2 \
    --out bench_nkb2_results.json            # NKB v2, same harness
python3 bench_marginal.py --kv ./kv_bin2.nix --ext nkb2 --label nkb2 \
    --out bench_marginal_nkb2.json
```

Example lookups (same key, all four formats — same value):

```sh
nix eval --impure --raw --expr \
  '((import ./kv.nix) ./data/large.nfd).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK v1, ~98 ms)

nix eval --impure --raw --expr \
  '((import ./kv2.nix) ./data/large.nfd2).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK v2, ~80 ms)

nix eval --impure --raw --expr \
  '((import ./kv_bin.nix) ./data/large.nkb).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NKB, ~80 ms)

nix eval --impure --raw --expr \
  '((import ./kv_bin2.nix) ./data/large.nkb2).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NKB v2, ~61 ms)
```
