# Fast key/value lookup for native Nix: NFK (hash) and NKB (binary search)

A lookup function for static string→string tables in **pure Nix** (no
`builtins.exec`, no `builtins.fromJSON`, no foreign interpreters), backed by a
precomputed database file read with `builtins.readFile` and sliced with
`builtins.substring` (+ `hashString` for the hash formats). Five formats:

- **NFK v1** — open-addressing hash table (sha256), 40-byte ASCII entries.
- **NFK v2** — same scheme, dense 22-byte base-36 entries (load ≤ 0.8).
- **NKB v1** — sorted binary search over keys, 24-byte base-255 ASCII
  entries (diffable).
- **NKB v2** — same sorted scheme, raw binary b254 index (14-byte entries),
  with the byte→int decode table embedded in the file; smallest file of
  all and lowest readFile floor (byte-string premise: Nix I/O strings are
  arbitrary bytes minus NUL, and only source *literals* are
  UTF-8-decoded — verified on Nix 2.34.7).
- **NFK v3** — NFK v2's hash + probe scheme on NKB v2's binary machinery:
  15-byte entries, 24-bit fingerprint, decode table carried in the file.
  10% smaller than NFK v2, 7% larger than NKB v2; the fastest multi-lookup
  eval in this session.

## Summary

| | result |
|---|---|
| Correctness | 1,255,025 lookups verified against the `fromJSON` oracle across all five formats: **0 mismatches** (every key, all 3 datasets, plus miss and edge cases) |
| Cold single lookup, 200k entries | NKB v2 **60.0 ms** ≈ NFK v3 60.2 vs NKB v1 79.8 ≈ NFK v2 81.1 vs NFK v1 98.7 vs `fromJSON` **209.8 ms** → 3.50× / 3.49× / 2.63× / 2.59× / 2.13× |
| Cold single lookup, 50k entries | NFK v3 **39.8 ms** vs NKB v2 40.4 vs NKB v1 41.6 ≈ NFK v2 41.8 vs NFK v1 46.3 vs `fromJSON` **78.4 ms** → 1.97× / 1.94× / 1.88× / 1.88× / 1.69× |
| 200 lookups in one eval, 200k entries | NFK v3 **72.0 ms** vs NFK v2 82.1 vs NKB v2 84.9 vs NKB v1 97.1 vs NFK v1 105.4 vs `fromJSON` **208.5 ms** → 2.90× / 2.54× / 2.46× / 2.15× / 1.98× |
| Cold single lookup, 1k entries | 33–35 ms across all methods → parity (Nix process startup dominates) |
| In-process per-lookup (after load) | `fromJSON` attrset < 1 µs; NFK v3 ≈ 4–31 µs; NFK v2 ≈ 10–16 µs; NFK v1 ≈ 0–17 µs (noise-limited at 200k); NKB v1 ≈ 46–91 µs; NKB v2 ≈ 54–96 µs — see trade-offs |

**Headline.** Every `nix eval` is a cold process: startup (~33 ms) + reading
the file + (for `fromJSON`) a full parse of the whole file — ≈201 ms for
the 14 MB table below — *regardless of how many keys are looked up*.
The custom formats replace that parse with a byte read: NFK pays one
`hashString` and a probe of ≤ M slots; NKB pays a binary search of ≤
⌈log₂(N)⌉ over byte-sorted keys. The v2 line densified both: NFK v2
shrinks the hash index to 22-byte entries (load ≤ 0.8), and NKB v2 moves
the structural fields to raw b254 binary with the decode table carried in
the file. **NFK v3 — NFK v2's hash table on NKB v2's binary machinery
(15-byte entries, 16.3 MB = 1.17× JSON) — is the overall champion for
typical workloads: 72.0 ms warm-200 at 200k (2.9× over `fromJSON`) and a
cold near-tie with NKB v2 (60.2 vs 60.0; NFK v3 wins at 50k, 39.8 vs
40.4). NKB v2 stays the smallest file of all (15.1 MB, 1.09× JSON) and the
lowest-floor format (57.3 ms); NFK v2 (9.9 µs/lookup at 200k) takes over
  above ~800 lookups/eval.

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
M, N < 10¹⁰ (10-digit decimal header).

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

## File format: NFK v3 (binary hash index)

NFK v3 combines the two v2-line improvements: NFK v2's dense hash table
(`M = next_pow2(max(16, ⌈1.25·N⌉))`, load ≤ 0.8) and NKB v2's binary index
machinery (b254 fields, byte table embedded in the file). The result is a
hash index nearly as dense as a sorted one — 15-byte entries — with O(1)
expected probes:

```
offset 0                        header, 64 bytes
offset 64                       byte table T: 0x01 … 0xFF (255 bytes)
offset 319                      index region, M × 15 bytes
offset 319 + M·15               data region: for each entry (insertion
                                order), the key bytes immediately followed
                                by the value bytes
```

**Header (64 bytes)**

| field | offset | width | value |
|---|---|---|---|
| magic | 0 | 4 | `NFK3` |
| N | 4 | 3 | entry count (3 b254 bytes) |
| M | 7 | 4 | table size (4 b254 bytes, power of two) |
| keyTotal | 11 | 3 | total key bytes (3 b254 bytes) |
| valTotal | 14 | 3 | total value bytes (3 b254 bytes) |
| – | 17 | 47 | reserved (spaces) |

**Index region** — one 15-byte entry per table slot `s` at offset
`319 + 15s`:

| field | offset | width | meaning |
|---|---|---|---|
| `fp` | 0 | 4 | `int(sha256(key) hex [0:6], 16) + 1` (24-bit); 0 = unused slot |
| `keyOff` | 4 | 4 | absolute file offset of the key |
| `keyLen` | 8 | 3 | key length (bytes) |
| `valLen` | 11 | 3 | value length (bytes); the value is at `keyOff + keyLen` |
| – | 14 | 1 | padding |

An unused slot is all zeros, i.e. 15 bytes of `0x01`. Because the data
region is interleaved (key bytes immediately followed by value bytes), the
value offset is implicit — one 4-byte field saved versus NKB v2's entry, at
the cost of 1 padding byte.

**Placement** — `s0 = int(h[56:64], 16) AND (M − 1)` with linear probing
(the probe at lookup time walks the same slots; identical to NFK v2). The
24-bit fingerprint (NFK v2 uses 32 bits) implies ~N/2²⁴ ≈ 0.012 false fp
matches expected per lookup at 200k keys (≈2,400 in total) — each costs one
extra key read + compare and can never yield a wrong value.

**Limits** (builder-enforced): N, keyTotal, valTotal, and each key/value
length < 254³ (≈16.4 MB); M and file offsets < 254⁴ (≈4.16 GB); no NUL
anywhere. The independent `--check` parser re-validates the magic, the
embedded table, and the exact file size, and re-probes every key.

Measured sizes (JSON → NFK v3):

| dataset | keys | JSON bytes | NFK v3 bytes | index bytes | ratio |
|---|---|---:|---:|---:|---:|
| small | 1,005 | 68,534 | 91,533 | 30,720 | 1.34× |
| medium | 50,000 | 3,463,238 | 4,046,597 | 983,040 | 1.17× |
| large | 200,000 | 13,941,356 | 16,273,835 | 3,932,160 | 1.17× |

15-byte entries vs NFK v2's 22-byte entries (1.47× denser) and NKB v2's
14-byte entries (1 byte more, the fingerprint); the large file is 1.8 MB
(10.1%) smaller than NFK v2 and 1.1 MB (7.5%) larger than NKB v2 — a hash
index within 7% of the smallest file of all.

## Implementation

Files in this repo:

| file | purpose |
|---|---|
| `kv.nix` | NFK v1 lookup module (self-contained, no generator) |
| `kv2.nix` | NFK v2 (compact 22-byte entry) lookup module |
| `kv3.nix` | NFK v3 (binary hash) lookup module (self-contained, no generator) |
| `kv_bin.nix` | NKB v1 lookup module (base-255 ASCII index) |
| `kv_bin2.nix` | NKB v2 (binary index) lookup module (byte table read from file) |
| `build_db.py` | JSON → NFK v1 builder (`--check` round-trips all keys) |
| `build_db2.py` | JSON → NFK v2 builder (`--check` round-trips all keys) |
| `build_db3.py` | JSON → NFK v3 builder (`--check` round-trips all keys) |
| `build_db_bin.py` | JSON → NKB v1 builder (with independent parser + `--check`) |
| `build_db_bin2.py` | JSON → NKB v2 builder (with independent parser + `--check`) |
| `gen_data.py` | Deterministic test KV generator |
| `gen_kv.py` | Raw byte-table → NFK v1 file (test helper; not used in mainline) |
| `gen_kv_bin.py` | Raw byte-table → NKB v1 file (test helper; not used in mainline) |
| `bench.py` / `bench_marginal.py` | Timers (see Benchmark) |
| `test_correctness*.nix` | Per-format Nix-vs-JSON oracle tests (NFK v1/v2/v3, NKB v1/v2) |
| `bench*.json` | Raw benchmark output |
| `data/` | Generated JSON and built `{nfd,nfd2,nfd3,nkb,nkb2}` files |

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
NFK v3 (`kv3.nix`, `*.nfd3` files) has the same API as v1/v2 (including
`tableSize`), plus `getJson`/`getOrJson` — values that hold a JSON document
come back parsed via `builtins.fromJSON` (a miss is still null) — and asserts
the `NFK3` magic. The builder (`build_db3.py`) accepts arbitrary JSON
values: non-string values are stored as compact JSON documents and come
back parsed via `getJson`/`getOrJson`; string values are stored raw (all
other builders require string values).

```sh
python3 build_db.py      input.json out.nfd  --check   # NFK v1
python3 build_db2.py     input.json out.nfd2 --check   # NFK v2 (dense, m-factor 1.25)
python3 build_db3.py     input.json out.nfd3 --check   # NFK v3 (binary hash index)
python3 build_db_bin.py  input.json out.nkb  --check   # NKB
python3 build_db_bin2.py input.json out.nkb2 --check   # NKB v2 (binary index)
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

NFK v3 builder guarantees (`build_db3.py`): N / keyTotal / valTotal and
each key/value length < 254³ bytes, M and file offsets < 254⁴ (raises
otherwise); the hash/slot computation is NFK v2's (`fp = int(h[0:6],16)+1`,
slot `int(h[-8:],16) & (M-1)`); `--check` re-validates the embedded
255-byte table and the exact file size and re-probes every key with an
independent open-addressing walk.

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

NFK v3 (`kv3.nix`) additionally relies on:

- the same file-carried byte table as NKB v2 (255 bytes at offset 64, folded
  into `{ byte → int }` at import via `foldl'`).
- b254 decoding: `dec3`/`dec4` — 3–4 one-byte table lookups per field.
- 24-bit fingerprint decoding: a 6-char hex fold
  (`int(h[0:6],16)+1`); the value offset is implicit (`keyOff + keyLen`),
  so the data region is interleaved.

## Correctness

Method (per format; all five formats run the identical checks):

1. Python round-trip (`build_db.py --check` / `build_db2.py --check` /
   `build_db3.py --check` / `build_db_bin.py --check` /
   `build_db_bin2.py --check`): independent re-parse of the entire file
   (structure + all keys + all values) compared against the source JSON,
   followed by a full probe (NFK v1/v2/v3) or binary search (NKB v1/v2)
   of every key in the file.
2. Nix oracle (`test_correctness.nix` / `test_correctness2.nix` /
   `test_correctness3.nix` / `test_correctness_bin.nix` /
   `test_correctness_bin2.nix`): for every key in the JSON source,
   compare the db's `get`, `getOr`, and `has` against the JSON source;
   plus an absent key (expected miss), an empty key (the NFK formats
   handle it via the fingerprint of the empty hash, NKB v1/v2 via binary
   search), and a `tableSize` match (NFK formats only).
3. Miss behavior: `get` of an absent key returns null / `getOr` returns
   the default; a `has` probe of an absent key returns false.

| dataset | format | keys | mismatches | `tableSize` | present/miss checks | empty key |
|---|---|---:|---:|---|---|---|
| small | NFK v1 | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NFK v1 | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NFK v1 | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| small | NFK v2 | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NFK v2 | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NFK v2 | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| small | NFK v3 | 1,005 | 0 | ✓ | ✓ / ✓ | ✓ |
| medium | NFK v3 | 50,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| large | NFK v3 | 200,000 | 0 | ✓ | ✓ / ✓ | ✓ |
| small | NKB v1 | 1,005 | 0 | n/a | ✓ / ✓ | ✓ |
| medium | NKB v1 | 50,000 | 0 | n/a | ✓ / ✓ | ✓ |
| large | NKB v1 | 200,000 | 0 | n/a | ✓ / ✓ | ✓ |
| small | NKB v2 | 1,005 | 0 | n/a | ✓ / ✓ | ✓ |
| medium | NKB v2 | 50,000 | 0 | n/a | ✓ / ✓ | ✓ |
| large | NKB v2 | 200,000 | 0 | n/a | ✓ / ✓ | ✓ |

All five formats pass identically — 1,255,025 lookups across the three
datasets (every key probed through both the Python checker and the Nix
oracle), zero mismatches.

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

Per-format (two-method) results — cold lookup, cold miss, warm 200, floors,
marginal slope — and the earlier five-method section are superseded by the
six-method same-session results below; the older numbers came from earlier
sessions.

### Six-method results (single session, identical harness)

All six methods re-run in one session (benchmarks are only comparable
within a session); `fromJSON` medians are taken across the five runs
(per-run spread ≤ 2.2 ms cold / ≤ 4.0 ms warm at 200k).

### Cold single lookup (median of 15 reps)

| dataset | fromJSON | NFK v1 | NFK v2 | NKB v1 | NKB v2 | NFK v3 | winner |
|---|---|---|---|---|---|---|---|
| small (1k) | 33.7 ms | 34.0 ms | 33.8 ms | 33.6 ms | 34.2 ms | 34.3 ms | parity (startup-bound) |
| medium (50k) | 78.4 ms | 46.3 ms | 41.8 ms | 41.6 ms | 40.4 ms | **39.8 ms** | NFK v3 (1.97×) |
| large (200k) | 209.8 ms | 98.7 ms | 81.1 ms | 79.8 ms | **60.0 ms** | 60.2 ms | NKB v2 ≈ NFK v3 (3.50× / 3.49×) |

### Cold miss (median of 15 reps)

| dataset | fromJSON | NFK v1 | NFK v2 | NKB v1 | NKB v2 | NFK v3 | winner |
|---|---|---|---|---|---|---|---|
| small (1k) | 33.8 ms | 34.4 ms | 33.5 ms | 33.9 ms | 33.5 ms | 34.5 ms | parity (startup-bound) |
| medium (50k) | 77.8 ms | 45.6 ms | 43.1 ms | 43.0 ms | 39.6 ms | **39.5 ms** | NFK v3 (1.97×) |
| large (200k) | 210.5 ms | 97.6 ms | 80.9 ms | 79.7 ms | **60.9 ms** | 61.7 ms | NKB v2 (3.46×) |

### Warm 200 lookups per eval (median of 3 reps)

| dataset | fromJSON | NFK v1 | NFK v2 | NKB v1 | NKB v2 | NFK v3 | winner |
|---|---|---|---|---|---|---|---|
| small (1k) | 34.6 ms | 36.5 ms | 36.8 ms | 43.6 ms | 42.6 ms | 38.0 ms | fromJSON (startup-bound) |
| medium (50k) | 77.5 ms | 51.4 ms | 47.4 ms | 55.2 ms | 54.9 ms | **43.8 ms** | NFK v3 (1.77×) |
| large (200k) | 208.5 ms | 105.4 ms | 82.1 ms | 97.1 ms | 84.9 ms | **72.0 ms** | NFK v3 (2.90×) |

**Floors (median):**

| floor | small | medium | large |
|---|---|---|---|
| `nix eval` startup (empty expr) | ~32–36 ms (32.1–35.6 across runs) |
| `readFile` JSON | 33.4 ms | 38.8 ms | 57.8 ms |
| `readFile` NFK v1 | 32.8 ms | 45.4 ms | 97.2 ms |
| `readFile` NFK v2 | 33.0 ms | 42.1 ms | 77.1 ms |
| `readFile` NKB v1 | 33.9 ms | 41.8 ms | 79.6 ms |
| `readFile` NKB v2 | 34.0 ms | 40.0 ms | 57.3 ms |
| `readFile` NFK v3 | 32.5 ms | 39.3 ms | 60.4 ms |
| `fromJSON` + `attrNames` (parse) | 34.2 ms | 87.8 ms | 259.0 ms |

### Marginal (in-process) cost per lookup

Least-squares slope of one-eval-time vs number of lookups (10–400 lookups),
per lookup:

| dataset | fromJSON | NFK v1 | NFK v2 | NKB v1 | NKB v2 | NFK v3 |
|---|---|---|---|---|---|---|
| small | ≈ 0 µs (noise) | 16.9 µs | 16.3 µs | 45.9 µs | 53.5 µs | **3.5 µs** |
| medium | ≈ 0 µs (noise) | 14.3 µs | 16.4 µs | 90.5 µs | 63.1 µs | **20.8 µs** |
| large | ≈ 0–5 µs (noise) | −6.3 µs (noise floor) | **9.9 µs** | 78.6 µs | 96.4 µs | 30.5 µs |

NFK v3's slope grows with table size (3.5 → 20.8 → 30.5 µs): expected probe
count rises with load factor (0.49 → 0.76) and each step decodes a 4-byte
b254 fingerprint. At 200k it is ~3× NFK v2's 9.9 µs (cheapest stable
slope) and ~3× cheaper than NKB v2's 96.4 µs. NFK v1's large slope is again
at/below the noise floor (−6.3 µs this run; `fromJSON`'s large slope is
equally noisy); its real per-lookup is ~14–17 µs. The binary search's step
cost, not the file, is what keeps NKB v2's floor (57.3 ms) the lowest of all.

### Head-to-head at large (200k keys, ms)

| workload | NFK v1 | NFK v2 | NKB v1 | NKB v2 | NFK v3 | winner |
|---|---:|---:|---:|---:|---:|---|
| cold single lookup | 98.7 | 81.1 | 79.8 | **60.0** | 60.2 | NKB v2 (1.33× over NKB v1) |
| cold miss | 97.6 | 80.9 | 79.7 | **60.9** | 61.7 | NKB v2 (1.31× over NKB v1) |
| warm 200 lookups | 105.4 | 82.1 | 97.1 | 84.9 | **72.0** | NFK v3 (1.14× over NFK v2) |
| file size (MB) | 33.3 | 18.1 | 17.1 | **15.1** | 16.3 | NKB v2 (7% smaller than NFK v3) |
| readFile floor | 97.2 | 77.1 | 79.6 | **57.3** | 60.4 | NKB v2 (−3.1 ms over NFK v3) |
| marginal per-lookup | ~0–17 µs (noise) | 9.9 µs | 78.6 µs | 96.4 µs | 30.5 µs | NFK v2 |

NFK v3 is the union of the two v2-line strengths: within 3.1 ms of NKB v2's
floor with a file only 7% larger, but with NFK-class per-lookup cost. NKB
v2's cold win has narrowed to a 0.2 ms coin-flip (60.0 vs 60.2); its
remaining edges are file size (−1.13 MB) and floor (−3.1 ms), which decide
below ≈ 47 lookups/eval. NFK v3's warm win over NFK v2 (72.0 vs 82.1) comes
from the 16.7 ms floor gap outweighing the 20.6 µs/lookup slope gap up to
≈ 810 lookups/eval.

### Analysis

Cost model per `nix eval` doing n lookups on the 200k table:

    fromJSON(n) ≈ 33 + read(JSON) + parse(JSON) + sub-µs·n
    NFK v1(n)   ≈ 33 + read(.nfd)  + import + ~(0–17 µs)·n
    NFK v2(n)   ≈ 33 + read(.nfd2) + import + ~(10–16 µs)·n
    NKB v1(n)   ≈ 33 + read(.nkb)  + import + ~(46–91 µs)·n
    NKB v2(n)   ≈ 33 + read(.nkb2) + import + ~(54–96 µs)·n
    NFK v3(n)   ≈ 33 + read(.nfd3) + import + ~(4–31 µs)·n

At large, `parse(JSON)` ≈ 201 ms (259.0 − 57.8), vs the readFile floors of
97.2 / 77.1 / 79.6 / 57.3 / 60.4 ms — the parse is the whole story for cold
evals.

Crossover with fromJSON (where the custom format's higher floor + slope
catches fromJSON's lower floor + steeper effective slope):

| dataset | NFK v1 | NFK v2 | NKB v1 | NKB v2 | NFK v3 |
|---|---|---|---|---|---|
| small | ≈ parity (startup-bound) | ≈ parity | ≈ parity | ≈ parity | ≈ parity |
| medium | ≈ 2,200 | ≈ 2,100 | ≈ 410 | ≈ 600 | ≈ 1,800 |
| large | slope-noisy (≤0 this run) | ≈ 12,400 | ≈ 1,600 | ≈ 1,500 | ≈ 4,700 |

Crossover between custom formats (large, 200k):

- **NFK v3 vs NKB v2**: 3.1 ms ÷ (96.4 − 30.5) µs ≈ **47 lookups/eval** —
  NKB v2 below (its 3.1 ms floor edge), NFK v3 above; at 200 lookups NFK v3
  wins by 12.9 ms (72.0 vs 84.9).
- **NFK v3 vs NFK v2**: 16.7 ms ÷ (30.5 − 9.9) µs ≈ **810 lookups/eval** —
  NFK v3 below, NFK v2 above (its 9.9 µs is the cheapest stable slope).
- **NKB v2 vs NFK v2**: 19.8 ms ÷ (96.4 − 9.9) µs ≈ 230 lookups/eval.
- **NKB v2 vs NKB v1**: 22.3 ms ÷ (96.4 − 78.6) µs ≈ 1,250 lookups/eval —
  NKB v1 only wins above ~1,250 lookups/eval; NKB v1 is dominated.
- **NKB v2 vs NFK v1**: 39.9 ms ÷ (96.4 − ~0) µs ≈ 410 lookups/eval.

Verdict from the numbers:

- Every custom format beats `fromJSON` up to its crossover (≈ 410
  lookups/eval for NKB v1 at medium to ≈ 12,400 for NFK v2 at large) — i.e.
  for essentially all realistic single-eval workloads.
- **NFK v3 is the overall champion for typical workloads (few to a few
  hundred lookups/eval) at 50k+ entries**: fastest warm-200 at medium and
  large (43.8 / 72.0 ms), wins cold at medium (39.8), and ties the fastest
  cold at large within 0.2 ms (60.2 vs 60.0).
- **NKB v2 is the smallest-file / few-lookup choice**: smallest file of all
  (15.1 MB, 1.09× JSON), lowest floor (57.3 ms); it stays ahead of NFK v3
  below ≈ 47 lookups/eval at 200k keys and wins cold large by 0.2 ms.
- **NFK v2 is the 800+-lookups/eval specialist**: its 9.9 µs/lookup beats
  NFK v3's 30.5 µs above ≈ 810 lookups despite the 16.7 ms higher floor.
- NKB v1 and NFK v1 are retained for reference (diffable ASCII index; the
  density baseline).
- Above the fromJSON crossover (bulk in-process scan), `fromJSON`'s sub-µs
  attrset access wins per-lookup.
- File sizes vs JSON (13.9 MB at large): NFK v1 2.40×, NFK v2 1.30×,
  NKB v1 1.23×, NFK v3 1.17×, NKB v2 1.09×. NFK v3 shows a hash index can
  sit within 7% of the smallest file of all while keeping O(1) probing.

## Trade-offs (summary)

1. **Per-lookup warm in-process**: `fromJSON` is fastest (sub-µs attrset
   lookup after load); NFK v2 ≈ 10–16 µs/lookup (hash + dense-table probe);
   NFK v3 ≈ 4–31 µs (hash + binary-decoded probe, slope grows with table
   size); NFK v1 ≈ 0–17 µs (noise-limited at 200k); NKB v1 ≈ 46–91 µs;
   NKB v2 ≈ 54–96 µs (≤ 18 binary-search steps of byte-slice decoding).
   None of the custom formats can beat scanning a large fraction of the
   table in one evaluation — the crossover table above quantifies that.
2. **Cold / repeated invocations**: every custom format beats `fromJSON`
   up to its crossover, which ranges from a few lookups (small,
   startup-bound) to ≈ 4,700 (NFK v3, large) and ≈ 12,400 (NFK v2, large).
   At 50k–200k entries the custom formats take 39.8–105.4 ms
   (NFK v3 / NFK v1 bounds) vs ~209 ms for `fromJSON`; at ~1k entries all
   six tie at startup cost (33–35 ms).
3. **File size**: NFK v1 ≈ 2.4× the equivalent JSON (index is 63% of the
   file); NFK v2's 22-byte base-36 entries bring it to 18.1 MB = 1.30× (index
   32%); NKB v1's base-255 entries 17.1 MB = 1.23× (index 28%); NFK v3's
   15-byte binary entries 16.3 MB = 1.17× (index 24%); NKB v2's binary
   index 15.1 MB = 1.09× (index 18.5%).
4. **Hashing**: NKB v1/v2 have none; NFK v1/v2/v3 pay one `sha256` per
   lookup. Fingerprint sizes: v1 64-bit, v2 32-bit (~5 false fp matches
   expected at 200k), v3 24-bit (~2,400 expected at 200k, ≈0.012 per
   lookup). A false match costs one extra key read + compare — it can
   never produce a wrong value.
5. **Static only**: the database is precomputed and immutable at build
   time; adding keys requires re-running the builder (the file is a
   snapshot; there is no in-eval mutation).
6. **No `parseInt` / no `%` / no mod on this Nix**: v1's decimal decode
   is a small fold per field; v2's base-36 decode is 4–6 table lookups per
   field; NKB v1's base-255 decode is 2–4 table lookups per field; NKB v2's
   and NFK v3's b254 decodes are 3–4 one-byte table lookups per field
   (NFK v3 additionally pays a 6-char hex fold for the 24-bit fingerprint).
7. **Memory**: each lookup is a constant number of heap strings
   (fingerprint/key/value fragments); no table is materialised in Nix
   memory at import.

**Verdict.** **Use NFK v3** as the default for 50k+ entry tables: it is the
fastest multi-lookup format (72.0 ms warm-200 at 200k, 2.9× over fromJSON
and 14% over NFK v2), a cold near-tie with NKB v2 (60.2 vs 60.0; NFK v3
actually wins cold at 50k, 39.8 vs 40.4), its file is 1.17× JSON, and hash
probing keeps the worst case deterministic. **Use NKB v2** when the
smallest file or a few-lookup cold eval matters: smallest file of all
(15.1 MB, 1.09× JSON), lowest floor (57.3 ms), zero hashing — it stays
ahead of NFK v3 below ≈ 47 lookups/eval at 200k keys. **Use NFK v2** when a
single evaluation does 800+ lookups: its 9.9 µs/lookup (cheapest stable
slope) beats NFK v3's 30.5 µs above ≈ 810 lookups despite the 16.7 ms
higher floor. **Use `fromJSON`** when one evaluation touches a large
fraction of the table (bulk in-process scan), or when JSON's simplicity and
tooling outweigh a hundred-plus milliseconds of parse time. NKB v1 and NFK
v1 are retained for reference (diffable ASCII index; the density baseline).

## Reproducing

```sh
python3 gen_data.py                              # data/{small,medium,large}.json
for s in small medium large; do
  python3 build_db.py      data/$s.json data/$s.nfd  --check   # NFK v1
  python3 build_db2.py     data/$s.json data/$s.nfd2 --check   # NFK v2
  python3 build_db3.py     data/$s.json data/$s.nfd3 --check   # NFK v3
  python3 build_db_bin.py  data/$s.json data/$s.nkb  --check   # NKB
  python3 build_db_bin2.py data/$s.json data/$s.nkb2 --check   # NKB v2
done
nix eval --impure --expr '(import ./test_correctness.nix) "large"'
nix eval --impure --expr '(import ./test_correctness2.nix) "large"'
nix eval --impure --expr '(import ./test_correctness3.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin2.nix) "large"'
python3 bench.py                                 # NFK v1 cold+warm+floors -> bench_results.json
python3 bench_marginal.py                        # NFK v1 per-lookup slopes
python3 bench.py --kv ./kv2.nix --ext nfd2 --label nfk2 \
    --out bench_nfk2_results.json                # NFK v2, same harness
python3 bench_marginal.py --kv ./kv2.nix --ext nfd2 --label nfk2 \
    --out bench_marginal_nfk2.json
python3 bench.py --kv ./kv3.nix --ext nfd3 --label nfk3 \
    --out bench_nfk3_results.json                # NFK v3, same harness
python3 bench_marginal.py --kv ./kv3.nix --ext nfd3 --label nfk3 \
    --out bench_marginal_nfk3.json
python3 bench.py --kv ./kv_bin.nix --ext nkb --label nkb \
    --out bench_nkb_results.json                 # NKB, same harness
python3 bench_marginal.py --kv ./kv_bin.nix --ext nkb --label nkb \
    --out bench_marginal_nkb.json
python3 bench.py --kv ./kv_bin2.nix --ext nkb2 --label nkb2 \
    --out bench_nkb2_results.json                # NKB v2, same harness
python3 bench_marginal.py --kv ./kv_bin2.nix --ext nkb2 --label nkb2 \
    --out bench_marginal_nkb2.json
```

Example lookups (same key, all five formats — same value):

```sh
nix eval --impure --raw --expr \
  '((import ./kv.nix) ./data/large.nfd).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK v1, ~98 ms)

nix eval --impure --raw --expr \
  '((import ./kv2.nix) ./data/large.nfd2).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK v2, ~81 ms)

nix eval --impure --raw --expr \
  '((import ./kv3.nix) ./data/large.nfd3).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NFK v3, ~60 ms)

nix eval --impure --raw --expr \
  '((import ./kv_bin.nix) ./data/large.nkb).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NKB, ~80 ms)

nix eval --impure --raw --expr \
  '((import ./kv_bin2.nix) ./data/large.nkb2).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (NKB v2, ~60 ms)
```
