# fast-nix-lookup

Fast key/value lookup for native Nix eval. Instead of `builtins.fromJSON` + attrset access (which parses the whole file on every `nix eval`), the database is precomputed into a file format that Nix can probe with byte reads, string slicing, and `hashString` — no parse.

Five formats:

- **NFK v1** (`kv.nix`, `.nfd`) — open-addressing hash table (sha256), 40-byte ASCII entries.
- **NFK v2** (`kv2.nix`, `.nfd2`) — same scheme, dense 22-byte base-36 entries (load ≤ 0.8).
- **NKB v1** (`kv_bin.nix`, `.nkb`) — sorted binary search, 24-byte base-255 ASCII entries (diffable).
- **NKB v2** (`kv_bin2.nix`, `.nkb2`) — same sorted scheme, raw binary b254 index (14-byte entries), byte decode table carried in the file; smallest file of all.
- **NFK v3** (`kv3.nix`, `.nfd3`) — NFK v2's hash + probe scheme on NKB v2's binary machinery: 15-byte b254 entries, 24-bit fingerprint, decode table carried in the file: 10% smaller than NFK v2, 7% larger than NKB v2; the fastest multi-lookup eval.

```nix
let db = (import ./kv2.nix) ./data/large.nfd2;  # or kv.nix / data/large.nfd
in db.get "pkgs484.env795.nix877.pkgs793"
# -> chde4cf665ukuewyy-tx
```

(NFK v2 shown; v1, NKB v1, NKB v2, and NFK v3 are the same call with `kv.nix`/`.nfd`, `kv_bin.nix`/`.nkb`, `kv_bin2.nix`/`.nkb2`, and `kv3.nix`/`.nfd3`.)

Same key, same value across all formats. For a 200,000-key table, one cold `nix eval` lookup takes ~60 ms with NKB v2 (60.0) or NFK v3 (60.2), ~80 ms with NKB v1 (79.8) or NFK v2 (81.1), and ~99 ms with NFK v1 (98.7), against ~210 ms for `builtins.fromJSON` + attrset access (2.1×–3.5×; see [REPORT.md](REPORT.md) for the full benchmark and trade-off analysis). For many lookups in one evaluation, NFK v3 leads (72.0 ms warm-200 vs 208.5 ms for `fromJSON`); above ~800 lookups/eval, NFK v2's 9.9 µs/lookup takes over.

## Why not `builtins.fromJSON`?

A single lookup in the JSON version looks like:

```nix
( builtins.fromJSON (builtins.readFile "./large.json") )."some.key"
```

That works — `builtins.fromJSON` returns an attrset of the same shape. The cost: the 14 MB file must be parsed (≈201 ms of `fromJSON` alone, ~209.8 ms total for one lookup on the 200k-key table) *every evaluation, even for a single key*. The custom formats read only the header, one table entry (hash) or ~⌈log₂ N⌉ entries (binary search), and one key/value pair — ~33–35 ms floor plus a few substring/hash operations, measured 60–105 ms wall per eval depending on format. See the [Performance summary](#performance-summary) for the full benchmark.

## Design constraints (this Nix)

The target Nix is a stripped-down build: `builtins.parseInt` does not exist and there is no modulo operator; integer arithmetic is limited to `/` (truncating division), `builtins.div`, and `builtins.bitAnd`, and string primitives to `substring`, `stringLength`, `stringToChars`, `hashString`, `concatStringsSep`. The design works around this:

**Three lookup strategies** — both NFK and NKB are pure string/slice arithmetic over the file bytes, no allocation-heavy structures:

- NFK v1/v2/v3 hash (one `sha256` per lookup; v3 uses NKB v2's binary index machinery with a 24-bit fingerprint);
- NKB v1/v2 are hash-free (sorted keys, binary search).

- **No `builtins.parseInt` / no `%`** — offsets and lengths are fixed-width numeric strings (decimal/hex/base-36/base-255/binary-b254 depending on format); decoding is a per-character table lookup or a small fold (`foldl` over an inline `digit → int` mapping, or the file-embedded 255-byte table for NKB v2 / NFK v3).
- **No in-eval mutation** — the lookup builds one string (the value) from `substring` slices of the file; the index is walked by recursion/`fold` over integer positions, never by re-reading the file.
- **Hash via `builtins.hashString "sha256"`** — stable across processes and platforms, so a fingerprint computed at build time matches at eval time.
- **Binary-safe strings (NKB v2 / NFK v3)** — Nix I/O strings are arbitrary bytes minus NUL; only string *literals in source* are UTF-8-decoded. Raw bytes in a `readFile`-loaded file pass through `substring`, `stringLength`, and the `sha256` digest untouched (verified on Nix 2.34.7, which this project targets). The builders therefore refuse NUL, and the index region of the binary formats is not human-diffable (the data region is unchanged).

## File format: NFK v1 (ASCII hash index)

Header and index are plain ASCII (fixed-width zero-padded decimal/hex fields); the data region is raw UTF-8. Three regions:

```
offset 0                        header, 64 bytes
offset 64                       index region, M × 40 bytes
offset 64 + M·40                data region, variable
```

**Header (64 bytes)** — fixed-width ASCII fields:

| field     | offset | width | meaning                                        |
|-----------|-------:|------:|------------------------------------------------|
| magic     | 0      | 4     | `NFK1`                                         |
| version   | 4      | 2     | `01`                                           |
| algo      | 6      | 2     | `sh` (sha256)                                  |
| `M`       | 8      | 10    | table size, zero-padded decimal (power of two) |
| `N`       | 18     | 10    | entry count, zero-padded decimal               |
| —         | 28     | 36    | reserved (spaces)                              |

**Index region** — one 40-byte entry per table slot `s` at offset `64 + 40·s`:

| field    | offset | width | meaning                                                    |
|----------|-------:|------:|------------------------------------------------------------|
| `fp`     | 0      | 16    | first 16 hex chars of `sha256(key)`; `g`×16 if unused      |
| `keyOff` | 16     | 10    | byte offset of the key in the data region (zero-padded decimal) |
| `keyLen` | 26     | 6     | byte length of the key (zero-padded decimal)               |
| `valLen` | 32     | 8     | byte length of the value (zero-padded decimal)             |

The value offset is not stored: the value is at `keyOff + keyLen`.

**Data region** — per entry, the key bytes immediately followed by the value bytes, in insertion order.

Invariants:

- `M = next_pow2(max(16, F·N))` with `F = --m-factor` (integer, default 2) → load ≤ 0.5 by default; probe walk bounded by `M` slots.
- `s0 = int(h[56:64], 16) AND (M − 1)` (low 32 bits of the digest); linear probing with +1 (mod `M`).
- The fingerprint is 64 bits; `g`×16 marks an unused slot (a hex digest never contains `g`). A fingerprint match is confirmed by a byte-for-byte key compare, so a collision costs one extra probe — never a wrong value.
- All header/index fields are ASCII digits/hex, so the regions are human-readable and diffable.
- Width guards (builder-enforced): `keyOff` < 10¹⁰, `keyLen` < 10⁶, `valLen` < 10⁸.

## File format: NFK v2 (dense ASCII hash index)

Same open-addressing scheme as NFK v1, with the index densified from 40-byte to 22-byte base-36 entries and the load factor raised from ≤ 0.5 to ≤ 0.8. The large file drops from 33.3 MB to 18.1 MB (1.84× smaller); the index from 21 MB to 5.77 MB.

```
offset 0                        header, 64 bytes
offset 64                       index region, M × 22 bytes
offset 64 + M·22                data region, variable (same as v1)
```

**Header (64 bytes)** — fixed-width ASCII fields:

| field     | offset | width | meaning                                        |
|-----------|-------:|------:|------------------------------------------------|
| magic     | 0      | 4     | `NFK2`                                         |
| version   | 4      | 2     | `02`                                           |
| algo      | 6      | 2     | `sh` (sha256)                                  |
| `M`       | 8      | 10    | table size, zero-padded decimal (power of two) |
| `N`       | 18     | 10    | entry count, zero-padded decimal               |
| —         | 28     | 36    | reserved (spaces)                              |

**Index region** — one 22-byte entry per table slot `s` at offset `64 + 22·s`:

| field    | offset | width | meaning                                                   |
|----------|-------:|------:|-----------------------------------------------------------|
| `fp`     | 0      | 8     | first 8 hex chars of `sha256(key)` (32-bit); `g`×8 if unused |
| `keyOff` | 8      | 6     | byte offset of the key in the data region (base-36, big-endian) |
| `keyLen` | 14     | 4     | byte length of the key (base-36)                          |
| `valLen` | 18     | 4     | byte length of the value (base-36)                        |

The value offset is not stored: the value is at `keyOff + keyLen`.

Invariants:

- `M = next_pow2(max(16, ⌈F·N⌉))` with `F = --m-factor` (default 1.25) → load ≤ 0.8; probe walk bounded by `M` slots.
- Base-36 width guards (builder-enforced): `keyOff` < 36⁶ ≈ 2.18 GB; `keyLen`/`valLen` < 36⁴ ≈ 1.68 MB; `M`, `N` < 10¹⁰ (10-digit decimal header fields).
- Fingerprint is 32-bit: ~N²/2³³ expected colliding pairs; a false match costs one extra key compare, never a wrong value (the key string is always verified byte-for-byte).
- All fields are ASCII (digits + `a–z`), so the index stays diffable.

## File format: NKB v1 (ASCII binary-search index)

No hashing at all: keys are sorted bytewise, and lookup is a binary search over the index. The index is pure ASCII — base-255 digits, little-endian, each digit encoded as 2 chars over the alphabet `abcdefghijklmnopqrstuvwxyz234567` (digit = hi·32 + lo, so the high char is always `a`–`h` and digit values are 0–254) — so it stays diffable; the data region is raw UTF-8.

```
offset 0                        header, 64 bytes
offset 64                       key-index region, N × 24 bytes
offset 64 + N·24                data region (sorted, variable)
```

**Header (64 bytes)** — magic `NKB1` [0..4), `N` (4 base-255 digits, 8 chars) [4..12), `keyTotal` (4 digits, 8 chars) [12..20), `valTotal` (4 digits, 8 chars) [20..28), spaces [28..64).

**Key-index region** — one 24-byte entry per *entry* (not per slot) at offset `64 + 24·i`, for sorted position `i`:

| field    | offset | width | meaning                                     |
|----------|-------:|------:|---------------------------------------------|
| `keyOff` | 0      | 8     | file offset of the key (4 base-255 digits)  |
| `keyLen` | 8      | 4     | key length (2 base-255 digits)              |
| `valOff` | 12     | 8     | file offset of the value (4 base-255 digits) |
| `valLen` | 20     | 4     | value length (2 base-255 digits)            |

**Data region** — all keys concatenated in sorted order, then all values concatenated in the same order; all index offsets are absolute file offsets.

Invariants:

- Keys are unique and sorted bytewise (Python `sorted` on the raw byte strings); the binary search compares `substring` slices directly.
- Base-255 width limits (builder-enforced): `N`/`keyTotal`/`valTotal`/`keyOff`/`valOff` < 255⁴ (≈ 4.2 GB); `keyLen`/`valLen` < 255² ≈ 65 KB (2-digit fields).
- No hash, no collisions, no probe chains — worst case is exactly ⌈log₂ N⌉ key reads.

## File format: NKB v2 (binary b254 index)

Same sorted scheme and same layout as NKB v1, with the index region encoded as raw binary b254 digits (`digit = byte − 1`, i.e. bytes `0x01`–`0xFF`) instead of ASCII, and a 255-byte `byte → int` decode table embedded in the file. Entry size drops from 24 to 14 bytes; the large file drops from 17.1 MB to 15.1 MB (1.09× the JSON).

```
offset 0                      header, 64 bytes
offset 64                     byte table T: 0x01 … 0xFF (255 bytes)
offset 319                    key-index region, N × 14 bytes
offset 319 + N·14             data region (sorted, variable — same as NKB v1)
```

**Header (64 bytes)** — magic `NKB2` [0..4), `N` (3 b254 bytes) [4..7), `keyTotal` (3 b254 bytes) [7..10), `valTotal` (3 b254 bytes) [10..13), spaces [13..64). No data-offset field: the data region is at `319 + N·14`.

**Index region** — one 14-byte entry per entry at offset `319 + 14·i`, for sorted position `i`:

| field    | offset | width | meaning                          |
|----------|-------:|------:|----------------------------------|
| `keyOff` | 0      | 4     | file offset of the key (b254)    |
| `keyLen` | 4      | 3     | key length (b254)                |
| `valOff` | 7      | 4     | file offset of the value (b254)  |
| `valLen` | 11     | 3     | value length (b254)              |

Invariants:

- b254 width limits (builder-enforced): `N`/`keyTotal`/`valTotal`/`keyLen`/`valLen` < 254³ (≈ 16.4 MB); `keyOff`/`valOff` — and hence the file size — < 254⁴ (≈ 4.16 GB).
- The byte table `T` maps each byte `0x01`–`0xFF` to its own single-byte encoding; Nix-side field decode is `fold` over `substring` slices table lookups (`T.byte → int`) — no `builtins.parseInt` (see [Nix-side workarounds](#nix-side-workarounds)).
- Keys stay sorted bytewise; worst case is still ⌈log₂ N⌉ key reads.
- The index region and byte table are raw binary (not diffable); the data region (keys block + values block) is raw UTF-8.

## File format: NFK v3 (binary hash index)

NFK v3 combines NFK v2's hash + probe scheme with NKB v2's binary machinery: the same sha256 fingerprint and linear probing, but NKB v2's b254 fields and file-carried byte table, plus a compact 24-bit fingerprint. The data region interleaves key and value bytes (each value immediately follows its key), so only the key offset is stored per entry:

```
offset 0                      header, 64 bytes
offset 64                     byte table T: 0x01 … 0xFF (255 bytes)
offset 319                    index region, M × 15 bytes
offset 319 + M·15             data region (interleaved, variable)
```

**Header (64 bytes)** — magic `NFK3` [0..4), `N` (3 b254 bytes) [4..7), `M` (4 b254 bytes) [7..11), `keyTotal` (3 b254 bytes) [11..14), `valTotal` (3 b254 bytes) [14..17), spaces [17..64).

**Index region** — one 15-byte entry per table slot `s` at offset `319 + 15·s`:

| field    | offset | width | meaning                                                |
|----------|-------:|------:|--------------------------------------------------------|
| `fp`     | 0      | 4     | `int(sha256(key) hex [0:6], 16) + 1` (24-bit); 0 = unused |
| `keyOff` | 4      | 4     | absolute file offset of the key                        |
| `keyLen` | 8      | 3     | key length                                             |
| `valLen` | 11     | 3     | value length (the value is at `keyOff + keyLen`)       |
| —        | 14     | 1     | padding (unused slots are 15 bytes of `0x01`)          |

Invariants:

- `s0 = int(h[56:64], 16) AND (M − 1)`, linear probing, bounded by `M` steps (same as NFK v2); a fingerprint hit is confirmed by a byte-for-byte key compare, so a 24-bit collision (expected ≈0.012 per lookup at 200k keys) costs one extra key read — never a wrong value.
- `M = next_pow2(max(16, ⌈1.25·N⌉))` → load ≤ 0.8 (fixed; no factor flag).
- b254 width limits (builder-enforced): N / `keyTotal` / `valTotal` / key length / value length < 254³ (~16.4 MB); `M` and offsets < 254⁴ (~4.16 GB); no NUL.
- Sizes (200k keys): 16,273,835 bytes = 1.17× the JSON (vs 18.1 MB / 1.30× for NFK v2, 15.1 MB / 1.09× for NKB v2).
- Values are opaque: any UTF-8 minus NUL may be stored. When a value holds a JSON document, `getJson`/`getOrJson` decode it with `builtins.fromJSON` at lookup time — the file format is unchanged (byte-identical to str mode).

## Lookup algorithm (NKB)

```
lookup(key):
  lo, hi = 0, N            # half-open range [lo, hi); N = db.count
  while lo < hi:
    mid = (lo + hi) / 2    # integer division, truncates
    midKey = substring(file, keyOff[mid], keyLen[mid])
    if   key = midKey: return substring(file, valOff[mid], valLen[mid])
    elif key < midKey: hi = mid
    else: lo = mid + 1
  return null
```

- Each step: decode the key offset and length (b254 for NKB v2; base-255 for NKB v1), one `substring` read of the key, one string compare.
- ⌈log₂ N⌉ steps: 10 for N = 1005, 16 for N = 50,000, 18 for N = 200,000.
- Comparison is lexicographic on the raw byte strings, which matches the bytewise sort used by the builder (Python `sorted` on bytes).

## Nix-side workarounds

- **No `builtins.parseInt`** — every numeric field decodes via a table fold: NFK v1/v2 header `M`/`N` via a base-100 fold over 2-digit chunks (inline `d2` table); NFK v2 index fields via a base-36 single-char fold (generated `b36` table); NKB v1 via an inline `b2` table mapping 2-char pairs to base-255 digits (little-endian); NKB v2 / NFK v3 one byte at a time through the file-embedded `byte → int` table (3–4 lookups per field, plus a 6-char hex fold for the NFK v3 24-bit fingerprint).
- **No `%`** — there is no modulo operator, but Nix integer division (`/`, `builtins.div`) and `builtins.bitAnd` are available. The NKB midpoint is plain `(lo + hi) / 2` (truncates); NFK v1/v2 derive the initial slot by folding the last 8 hex chars of the digest (base 16) and masking with `builtins.bitAnd v32 (M − 1)`; probe wrap is `builtins.bitAnd (s + 1) (M − 1)` (M is a power of two).
- **Hash** — `builtins.hashString "sha256" key` returns the hex digest; `substring 0 16` (v1) / `substring 0 8` (v2) gives the fingerprint; the slot uses the last 8 hex chars (low 32 bits of the digest), `substring 56 8`.
- **No mutation** — everything is a pure `let`/recursion over the file string; the only "state" is integer positions.
- **Binary-safe b254 decoding (NKB v2 / NFK v3)** — no `builtins.parseInt` and no integer arithmetic on bytes, so each b254 field decodes one byte at a time through the file-carried `byte → int` table (255 one-byte entries embedded after the header): `intOf byte = table.substring(0,1)` folded left-to-right as `acc * 254 + (intOf byte)`. 3–4 table lookups per field; the table is folded once at import.

Header fields are decoded once at import time (NFK v1/v2: `M`/`N`; NKB v1/v2: `N` and the region totals); NKB v2 and NFK v3 additionally fold their 255-entry byte tables at import. NFK lookups then do one `hashString` and a few `substring`s per probe step, NKB lookups ~log₂(N) key reads.

## Builder

`build_db.py` (NFK v1), `build_db2.py` (NFK v2), `build_db_bin.py` (NKB v1), `build_db_bin2.py` (NKB v2), and `build_db3.py` (NFK v3) read the JSON object and emit the binary/ASCII file. `build_db.py` additionally runs an independent re-parse (`--check`) that validates the header, re-probes every key through a from-scratch decoder, and confirms misses return `None` — so the builder and the Nix module are cross-validated at build time.

`build_db2.py` (NFK v2) takes `--m-factor F` (default 1.25): `M = next_pow2(max(16, ceil(F·N)))`, and `--check` (same independent re-parse + probe of every key + a known miss). Width guards: `keyOff` < 36⁶ ≈ 2.18 GB, key/value lengths < 36⁴ ≈ 1.68 MB, `M`/`N` < 10¹⁰.

`build_db_bin.py` (NKB v1) sorts keys bytewise, writes the header + 24-byte base-255 index + sorted data region, and with `--check` re-parses the file independently (validating magic, no-NUL, exact size) and binary-searches every key plus a known miss.

`build_db_bin2.py` (NKB v2) is the same pipeline with the 14-byte b254 index and the embedded 255-byte table, plus width guards (b254): `N`/`keyTotal`/`valTotal`/lengths < 254³ ≈ 16.4 MB, `keyOff`/`valOff`/file size < 254⁴ ≈ 4.16 GB, no NUL (Nix `readFile` strings cannot carry NUL).

The NFK v3 builder (`build_db3.py`) uses NFK v2's slot assignment and a 24-bit sha256 fingerprint, NKB v2's b254 fields, embeds the 255-byte table after the 64-byte header, and enforces the width limits (N/totals/lengths < 254³, M/offsets < 254⁴). `M = next_pow2(max(16, ⌈1.25·N⌉))` is fixed (load ≤ 0.8); there is no factor flag. Its `--check` re-parses the file independently (validates magic, reserved header spaces, the embedded 255-byte table, absence of NUL, and the exact file size) and probes every key through an independent in-memory probe plus a known miss.

## Usage

Nix 2.34.7+ (uses `hashString` and byte-safe `substring`; no `builtins.parseInt`). Import one of the kv modules with the path to a database file:

```nix
let db = (import ./kv.nix) ./data/large.nfd;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count; db.tableSize }
```

`get` returns `null` on a miss. `kv2.nix` (NFK v2) has the same API and asserts the `NFK2` magic:

```nix
let db = (import ./kv2.nix) ./data/large.nfd2;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count; db.tableSize }
```

NFK v3 (`kv3.nix`) has the same API — plus `getJson`/`getOrJson` for JSON values — and asserts the `NFK3` magic:

```nix
let db = (import ./kv3.nix) ./data/large.nfd3;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count; db.tableSize }
```
Values are opaque; when a value holds a JSON document, `db.getJson "k"` / `db.getOrJson "k" default` return `builtins.fromJSON` of the stored string (a miss still returns `null`).

NKB (`kv_bin.nix`) has the same API (minus `tableSize`, which is the hash-table size and does not apply to a sorted index):

```nix
let db = (import ./kv_bin.nix) ./data/large.nkb;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count }
```

`kv_bin2.nix` (NKB v2) has the same API and asserts the `NKB2` magic:

```nix
let db = (import ./kv_bin2.nix) ./data/large.nkb2;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count }
```

### Building a database

The NFK v1 builder:

```sh
python3 build_db.py INPUT.json OUTPUT.nfd [--m-factor F] [--check]
```

- Input: a JSON object with string keys and string values.
- `--m-factor` (integer, default 2): `M = next_pow2(max(16, F·N))`; the default keeps load ≤ 0.5 for short probe chains.
- `--check`: after writing, re-reads the file with an independent parser and re-probes every key (exits non-zero on mismatch).

The NFK v2 builder:

```sh
python3 build_db2.py INPUT.json OUTPUT.nfd2 [--m-factor F] [--check]
```

- Same input contract; `--m-factor` (default 1.25) as above.
- `--check`: independent re-parse + probe of every key plus a known miss.
- Width guards: `keyOff` < 36⁶ ≈ 2.18 GB, key/value lengths < 36⁴ ≈ 1.68 MB, `M`/`N` < 10¹⁰.

The NKB v1 builder:

```sh
python3 build_db_bin.py INPUT.json OUTPUT.nkb [--check]
```

- Same input contract; keys are sorted bytewise internally (input order is irrelevant).
- `--check`: independent re-parse (magic, no-NUL, exact size) + binary-search of every key plus a known miss.

The NKB v2 builder:

```sh
python3 build_db_bin2.py INPUT.json OUTPUT.nkb2 [--check]
```

- Same input contract; keys are sorted bytewise internally (input order is irrelevant).
- `--check`: independent re-parse (validates magic, the embedded 255-byte table, absence of NUL, and exact size) + binary-search of every key plus a known miss.
- Width guards (b254): `N`/`keyTotal`/`valTotal`/lengths < 254³ ≈ 16.4 MB, `keyOff`/`valOff`/file size < 254⁴ ≈ 4.16 GB, no NUL.

The NFK v3 builder:

```sh
python3 build_db3.py INPUT.json OUTPUT.nfd3 [--check]
```

- Input: a JSON object with string keys and **arbitrary JSON values** — string values are stored raw; non-string values are stored as compact JSON documents, which `getJson`/`getOrJson` decode (all other builders require string values).
- `--check`: independent re-parse (validates magic, reserved header spaces, the embedded 255-byte table, absence of NUL, and the exact file size) + probe of every key plus a known miss.
- Width guards: N/total/length < 254³ bytes, M/offsets < 254⁴, no NUL.

## Correctness

Each format is cross-checked against the `fromJSON` oracle in `test_correctness*.nix`:

```sh
python3 gen_data.py
for s in small medium large; do
  python3 build_db.py      data/$s.json data/$s.nfd  --check
  python3 build_db2.py     data/$s.json data/$s.nfd2 --check
  python3 build_db3.py     data/$s.json data/$s.nfd3 --check
  python3 build_db_bin.py  data/$s.json data/$s.nkb  --check
  python3 build_db_bin2.py data/$s.json data/$s.nkb2 --check
done
# then, per dataset:
nix eval --impure --expr '(import ./test_correctness.nix) "large"'
nix eval --impure --expr '(import ./test_correctness2.nix) "large"'
nix eval --impure --expr '(import ./test_correctness3.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin.nix) "large"'
nix eval --impure --expr '(import ./test_correctness_bin2.nix) "large"'
```

Expected: `ok = true`, `mismatchCount = 0` on all datasets (1,255,025 lookups across the five formats).

## Performance summary

Benchmarked on the 200k-key table (14 MB JSON) with 15 cold `nix eval` runs per point, same session for all five custom formats (median; per-format harness in `bench.py` / `bench_marginal.py`, `fromJSON` via the same harness — see [REPORT.md](REPORT.md)):

| workload | NFK v1 (hash) | NFK v2 (dense) | NKB v1 (sorted) | NKB v2 (binary) | NFK v3 (hybrid) | fromJSON |
|---|---|---|---|---|---|---|
| Cold single `nix eval` lookup, 200k keys | 98.7 ms (2.13×) | 81.1 ms (2.59×) | 79.8 ms (2.63×) | **60.0 ms** (3.50×) | 60.2 ms (3.49×) | 209.8 ms |
| Cold single lookup, 50k keys | 46.3 ms (1.69×) | 41.8 ms (1.88×) | 41.6 ms (1.88×) | 40.4 ms (1.94×) | **39.8 ms** (1.97×) | 78.4 ms |
| Cold single lookup, 1k keys | 34.0 ms | 33.8 ms | 33.6 ms | 34.2 ms | 34.3 ms | 33.7 ms (parity — startup-bound) |
| 200 lookups in one eval, 200k keys | 105.4 ms (1.98×) | 82.1 ms (2.54×) | 97.1 ms (2.15×) | 84.9 ms (2.46×) | **72.0 ms** (2.90×) | 208.5 ms |
| Marginal in-process cost per lookup | ~0–17 µs (noise-limited) | **9.9 µs** (10–16 µs across sizes) | 46–91 µs (≤ 18 steps) | 54–96 µs (≤ 18 steps) | 4–31 µs (slope grows with size) | < 1 µs (attrset) |
| DB file size, 200k keys | 33.3 MB (2.40× JSON) | 18.1 MB (1.30× JSON) | 17.1 MB (1.23× JSON) | **15.1 MB** (1.09× JSON) | 16.3 MB (1.17× JSON) | 13.9 MB |
| `readFile` floor, 200k keys | 97.2 ms | 77.1 ms | 79.6 ms | **57.3 ms** | 60.4 ms | 57.8 ms read + 201 ms parse (259.0 ms total) |

All five custom formats beat `fromJSON` cold at realistic eval sizes (1.69×–3.50× at 50k–200k keys): NKB v2 and NFK v3 split the cold-lookup lead by 0.2 ms (readFile floor 57.3 vs 60.4 ms), NFK v3 wins the warm multi-lookup eval (72.0 ms warm-200 vs 82.1 NFK v2, 84.9 NKB v2, 97.1 NKB v1, 105.4 NFK v1), and NFK v2's 9.9 µs/lookup takes over above ~800 lookups/eval. Bulk scans (thousands of lookups per eval) tip back to `fromJSON`'s sub-µs attrset. Full cost model, crossovers, and trade-offs: [REPORT.md](REPORT.md).

## Repo layout

| path | role |
|---|---|
| `kv.nix` | NFK v1 (hash) lookup module (self-contained) |
| `kv2.nix` | NFK v2 (dense hash) lookup module (self-contained) |
| `kv3.nix` | NFK v3 (binary hash index) lookup module (self-contained; fastest multi-lookup eval) |
| `kv_bin.nix` | NKB v1 (binary search, base-255) lookup module (self-contained) |
| `kv_bin2.nix` | NKB v2 (binary search, b254) lookup module (self-contained; smallest file) |
| `gen_kv.py` / `gen_kv2.py` / `gen_kv_bin.py` | emit the corresponding kv module(s) from the format spec (the modules above are the generated output) |
| `build_db.py` | JSON → NFK v1 builder with independent parser + `--check` |
| `build_db2.py` | JSON → NFK v2 builder with independent parser + `--check` |
| `build_db3.py` | JSON → NFK v3 builder with independent parser + `--check` |
| `build_db_bin.py` | JSON → NKB v1 builder with independent parser + `--check` |
| `build_db_bin2.py` | JSON → NKB v2 builder with independent parser + `--check` |
| `gen_data.py` | deterministic test-data generator (1k / 50k / 200k keys) |
| `test_correctness.nix`, `test_correctness2.nix`, `test_correctness3.nix`, `test_correctness_bin.nix`, `test_correctness_bin2.nix` | `fromJSON`-oracle correctness tests (all five formats) |
| `data/` | `small|medium|large.{json,nfd,nfd2,nfd3,nkb,nkb2}` (1k / 50k / 200k keys; `*.nfd3` = NFK v3) |
| `bench.py`, `bench_marginal.py` | benchmark harnesses, parameterized per format (`--kv/--ext/--label/--out`) |
| `REPORT.md` | full design + benchmark + trade-off write-up |

## Known limitations

- **`builtins.fromJSON` still wins for bulk scans** — if an evaluation touches most of the table, parsing once and indexing the attrset beats per-lookup file slicing. The custom formats target the common case: one or a few lookups per eval.
- **Nix's string model limits the formats** — Nix strings cannot contain NUL, so binary formats (NKB v2, NFK v3) stop at 254-valued digits (b254: digits 0–253 in bytes `0x01`–`0xFF`); the index region of those formats is not human-diffable (the data region is unchanged). A future Nix with `builtins.parseInt` (and a modulo operator) could shrink the index further, and raw-bytes support would lift the NUL limit.
- **sha256 is the only stable hash available** — `hashString`'s other modes (md5, siphash-*) are not stable across Nix versions/platforms in the same documented way; sha256 is ~3× slower than the alternatives but the cost is one hash per lookup, not per entry.
- **NFK (v1/v2/v3): a probe chain of M empty slots in the worst case** — bounded but not O(1) worst case; with load ≤ 0.5 (v1) or ≤ 0.8 (v2/v3) and 64/32/24-bit fingerprints the expected chain is < 1.2 / < 2.1 / < 2.1 slots.
- **Fixed table size** — the table is sized for the input at build time; growing the table requires rebuilding the `.nfd`/`.nfd2`/`.nfd3` file (the builder is cheap: < 2 s for 200k keys).
- **File size** — fixed-width ASCII structural fields make NFK v1 ≈2.4× the equivalent JSON; NFK v2's 22-byte base-36 entries drop it to ≈1.3×; NKB v1's base-255 entries ≈1.23×; NKB v2's binary index (14-byte entries, file-carried byte table) ≈1.09×; NFK v3's 15-byte binary hash entries ≈1.17× (a hash index within 7% of the smallest file). The price of the binary index formats (NKB v2, NFK v3) is diffability of the index region (the data region is unchanged); the embedded table makes them self-describing instead.