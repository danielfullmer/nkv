# fast-nix-lookup

A fast key/value lookup function for **native Nix**, backed by a precomputed
static database file. The implementation uses only `builtins.readFile` and
`builtins.substring` (plus `builtins.hashString` and arithmetic for the
hash-based format) — no `builtins.exec`, no `builtins.fromJSON`, no foreign
interpreters.

Given a JSON file of string→string pairs, Python builders emit compact
database files; self-contained Nix modules read them and answer lookups in a
single `nix eval`. Three formats:

- **NFK v1** (`.nfd`) — hash-based open addressing: 16-hex sha256 fingerprint,
  40-byte index entries, O(1) expected.
- **NFK v2** (`.nfd2`, dense) — same scheme with 22-byte entries, 8-hex
  fingerprint, load ≤ 0.8: 1.84× smaller file, same speed class.
- **NKB v1** (`.nkb`) — keys sorted by byte order + binary search, O(log N),
  no hashing.

(NFK v2 shown; v1 and NKB are the same call with `kv.nix`/`.nfd` and
`kv_bin.nix`/`.nkb`).

```sh
$ python3 build_db2.py data/large.json data/large.nfd2
built data/large.nfd2: n=200000 M=262144 data=12341356B total=18108588B load=0.76
$ nix eval --impure --raw \
    --expr '((import ./kv2.nix) ./data/large.nfd2).get "pkgs484.env795.nix877.pkgs793"'
chde4cf665ukuewyy-tx
```

For a 200,000-key table, one cold `nix eval` lookup takes ~79.6 ms with NKB
and ~81.2 ms with NFK v2 (vs ~98 ms with NFK v1), against ~209 ms for
`builtins.fromJSON` + attrset access (2.63× / 2.58× — see
[REPORT.md](REPORT.md) for the full benchmark and trade-off analysis).

## How it works

### Design constraints

- Keys and values are **strings only**, stored **statically** in a precomputed
  file (rebuild the file to change data).
- The Nix side must work with the builtins available on a stock Nix: no
  `fromJSON`, no `exec`, no `parseInt`/`%`/`mod`/`hash` on this build
  (Nix 2.34.7+1) — see the workarounds below.
- Nix strings are a sequence of bytes; the only forbidden byte is NUL —
  `readFile` rejects any file containing one (verified on 2.34.7). All other
  bytes, including invalid UTF-8, pass through byte-for-byte. One quirk:
  source *literals* are UTF-8-decoded (an invalid byte becomes U+FFFD), so
  raw high bytes can only come from I/O, never from `.nix` source.
- Two lookup strategies: NFK hashes (one sha256 per probe); NKB is
  hash-free (sorted keys, binary search).

### File format: NFK v1

The header and index are pure ASCII (fixed-width zero-padded decimal and hex
fields); the data region holds the raw bytes of keys and values (UTF-8, as
JSON input guarantees). Structural fields must be ASCII-decodable — Nix
source cannot express raw high bytes as table keys (see the design
constraints) — and this keeps the index diffable. Three regions:

```
offset 0                      header, 64 bytes
offset 64                     index region, M × 40 bytes
offset 64 + M·40              data region, variable length
```

**Header (64 bytes)**

| field   | offset | width | value                        |
|---------|-------:|------:|------------------------------|
| magic   | 0      | 4     | `NFK1`                       |
| version | 4      | 2     | `01`                         |
| algo    | 6      | 2     | `sh` (sha256)                |
| M       | 8      | 10    | table size, power of two     |
| N       | 18     | 10    | number of stored entries     |
| —       | 28     | 36    | reserved (spaces)            |

**Index region** — one 40-byte entry per table slot `s`, at offset
`64 + 40·s`:

| field    | offset | width | meaning                                              |
|----------|-------:|------:|------------------------------------------------------|
| `fp`     | 0      | 16    | first 16 hex chars of `sha256(key)`; `g`×16 if unused |
| `keyOff` | 16     | 10    | byte offset of the key in the data region            |
| `keyLen` | 26     | 6     | byte length of the key                               |
| `valLen` | 32     | 8     | byte length of the value (at `keyOff + keyLen`)      |

**Data region** — for each key, in insertion order: the key's UTF-8 bytes
followed by the value's UTF-8 bytes. Offsets are absolute from the start of
the data region; the value offset is implied, not stored.

### Lookup algorithm (NFK)

```
h    = sha256(key)                     # 64 lowercase hex chars, one hashString call
fp   = h[0:16]                         # 64-bit fingerprint
s0   = int(h[56:64], 16) AND (M − 1)   # initial slot from the low 32 bits
for s = s0, s0+1, s0+2, … (mod M, at most M steps):
    slot fp == g×16 ?                  → null            (empty slot: miss)
    slot fp ≠ fp ?                     → continue
    stored key ≠ key (byte-for-byte)?  → continue        (fingerprint collision)
    → return stored value
```

- One `hashString` call serves both the fingerprint and the slot, because the
  two 32-bit halves of the digest are independent.
- A fingerprint hit must be confirmed by comparing the stored key bytes, so a
  collision (expected ~N²/2⁶⁵ pairs for N ≤ 10⁶ keys) can only add a probe —
  it can never return a wrong value.
- `M` is a power of two with load factor ≤ 0.5 (builder default
  `m_factor = 2`), so slot wrap-around is a single `bitAnd` and probe runs are
  short. The probe is bounded by a counter (at most M steps) so a corrupt
  file cannot loop forever.
- A miss costs the same walk as a hit (to the first empty slot) and returns
  `null`.

### File format: NFK v2 (dense)

Same algorithm as NFK v1 (sha256 fingerprint + linear probe, byte-identical
data region) with a denser index: 22-byte entries at load ≤ 0.8 instead of
40-byte entries at load ≤ 0.5. The large file drops from 33.3 MB to
18.1 MB (1.84× smaller); the index from 21 MB to 5.77 MB. Structural fields
are base-36 digits (one ASCII char per digit, big-endian, alphabet
`0123456789abcdefghijklmnopqrstuvwxyz`) instead of two-digit decimal; the
header keeps the v1 layout with magic `NFK2`.

```
offset 0                      header, 64 bytes
offset 64                     index region, M × 22 bytes
offset 64 + M·22              data region, byte-identical to NFK v1
```

**Index region** — one 22-byte entry per table slot `s` at offset
`64 + 22·s` (header: same fields as v1, `M`/`N` as 10-digit decimals):

| field    | offset | width | meaning                                              |
|----------|-------:|------:|------------------------------------------------------|
| `fp`     | 0      | 8     | first 8 hex chars of `sha256(key)`; `g`×8 if unused |
| `keyOff` | 8      | 6     | byte offset of the key in the data region (base 36)  |
| `keyLen` | 14     | 4     | byte length of the key (base 36)                     |
| `valLen` | 18     | 4     | byte length of the value (at `keyOff + keyLen`)      |

- The 32-bit fingerprint only *adds a key read* on a false match — the
  stored key is still compared byte-for-byte, so a collision (expected ~5 at
  200k keys) can never return a wrong value.
- `M = next_pow2(max(16, ⌈1.25·N⌉))` (builder default `m_factor = 1.25`),
  load ≤ 0.8; slot wrap-around is the same `bitAnd (M − 1)`.
- Limits (builder-enforced): key/value < 36⁴ bytes (~1.68 MB), data region
  < 36⁶ bytes (~2.18 GB).

### File format: NKB v1

Sorted-key binary search, no hashing. Structural fields are base-255 digits
over a 32-char alphabet (`a`–`z` = 0–25, `2`–`7` = 26–31; one digit is two
chars, `hi*32 + lo`; the pair `77` is unused) rather than raw binary, because
the byte→int decode table must live in `.nix` source, which cannot express
raw high bytes as table keys (see the design constraints). The data region
holds raw bytes. Three regions:

```
offset 0                      header, 64 bytes
offset 64                     index region, N·24 bytes
offset 64 + N·24              data region: all keys (sorted), then all values
```

**Header (64 bytes)**

| field    | offset | width | value                            |
|----------|-------:|------:|----------------------------------|
| magic    | 0      | 4     | `NKB1`                           |
| N        | 4      | 8     | entry count (4 base-255 digits)  |
| keyTotal | 12     | 8     | total key bytes (4 digits)       |
| valTotal | 20     | 8     | total value bytes (4 digits)     |
| —        | 28     | 36    | reserved (spaces)                |

**Index region** — one 24-byte entry per key `i` at offset `64 + 24·i`; keys
are strictly ascending by UTF-8 byte order:

| field   | offset | width | meaning                           |
|---------|-------:|------:|-----------------------------------|
| `off_k` | 0      | 8     | file offset of the key (4 digits) |
| `len_k` | 8      | 4     | key length (2 digits)             |
| `off_v` | 12     | 8     | file offset of the value (4 digits) |
| `len_v` | 20     | 4     | value length (2 digits)           |

All offsets are absolute from the file start. Limits (builder-enforced):
key/value < 255² bytes, each total < 255⁴.

### Lookup algorithm (NKB)

```
lo, hi = 0, N
while lo < hi:
    m = (lo + hi) / 2                # truncated integer division
    key_m = index[m].key             # raw bytes
    key_m < key  ? lo = m + 1        # Nix < is unsigned byte-lexicographic
    key_m == key ? return index[m].value
    key_m > key  ? hi = m
return null
```

- No hashing: ⌈log₂(N)⌉ key comparisons (≤ 18 at 200k keys), each a
  `substring` + byte compare.
- The builder sorts by UTF-8 byte sequence (`sorted(key.encode("utf-8"))`),
  which is exactly the order Nix's `<` compares (verified on raw bytes), so
  the search is well-defined.
- A miss is the search running to exhaustion; `kv_bin.nix` asserts the `NKB1`
  magic on import.

### Nix-side workarounds

Both modules target a stock Nix builtin surface:

- **No `builtins.parseInt` (NFK)** — decimal fields are decoded two digits at a
  time through a 100-entry lookup table and a `foldl'`
  (`acc = acc * 100 + d2."${s[p:p+2]}"`).
- **No `builtins.parseInt` (NFK v2)** — base-36 fields decode one char at a
  time through an inlined 36-entry table and a 4–6-step Horner fold.
- **No `builtins.parseInt` (NKB)** — base-255 fields decode through an
  inlined 255-entry two-char pair table and a Horner fold; the table is
  machine-generated (`gen_kv_bin.py`) so it can't be mistyped.
- **No `%` / `builtins.mod`** (NFK) — slot wrap-around is
  `builtins.bitAnd (s + 1) (M − 1)`, exact because M is a power of two.
- **No `builtins.hash`** (NFK) — `hashString "sha256"` is used (the hash the format
  is built around).
- `builtins.genList` is called function-first (`genList (i: i) n`) on this
  build.
- Header fields are decoded once at import time (NFK v1/v2: `M`/`N`; NKB:
  `N` and the region totals); NFK lookups then do one `hashString` and a
  few `substring`s per probe step, NKB lookups ~log₂(N) key reads.

The Python builder (`build_db.py`) computes the identical hash/slot/fingerprint
(`hashlib.sha256(key).hexdigest()` → `h[:16]`, `int(h[-8:], 16) & (M-1)`),
which is why the two implementations agree on every key.

The NFK v2 builder (`build_db2.py`) computes the same sha256 and takes the
slot from the same low 32 bits, but stores an 8-hex fingerprint and
base-36 offsets/lengths — so every key lands in the same slot in v1 and v2
files; only the entry encoding differs.

The NKB builder (`build_db_bin.py`) instead sorts keys by their UTF-8 byte
sequence, which is exactly the order Nix's `<` compares (verified), so the
index order and the Nix search agree on every key.

## Usage

### Nix API

```nix
let db = (import ./kv.nix) ./data/large.nfd;
in {
  db.get "some.key"         # value string, or null if absent
  db.getOr "some.key" "dflt" # value string, or "dflt" if absent
  db.has "some.key"         # true / false
  db.count                  # number of stored entries (N)
  db.tableSize              # number of table slots (M)
}
```

`kv.nix` asserts the file magic on import — a non-NFK file fails evaluation
with a clear assertion error rather than a silent wrong answer.

The NKB module (`kv_bin.nix`) has the same API minus `tableSize` (no slots:
one index entry per key) and asserts the `NKB1` magic:

```nix
let db = (import ./kv_bin.nix) ./data/large.nkb;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count }
```

NFK v2 (`kv2.nix`) has the same API as v1 and asserts the `NFK2` magic:

```nix
let db = (import ./kv2.nix) ./data/large.nfd2;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count; db.tableSize }
```

### Builder

```sh
python3 build_db.py INPUT.json OUTPUT.nfd [--m-factor N] [--check]
```

- Input is a JSON object of string→string pairs (values must be strings).
- `--m-factor` (default 2): table size = `next_pow2(m_factor * n)`, giving a
  load factor ≤ `1/m_factor`. Larger → faster probes, bigger file.
- `--check`: re-parses the produced file independently and verifies every key
  round-trips plus one known-miss key; exits non-zero on any mismatch.
- Field-width guards: keys ≤ 999,999 bytes, values ≤ 99,999,999 bytes,
  data region < 10 GB (the fixed field widths above); violations raise.

The NFK v2 builder:

```sh
python3 build_db2.py INPUT.json OUTPUT.nfd2 [--m-factor F] [--check]
```

- Same input contract (JSON object of string→string pairs; `--check`
  semantics as above).
- `--m-factor` (default 1.25): `M = next_pow2(max(16, ⌈F·n⌉))`; the default
  gives load ≤ 0.8.
- Width guards: key/value < 36⁴ bytes, data region < 36⁶ bytes.

The NKB builder:

```sh
python3 build_db_bin.py INPUT.json OUTPUT.nkb [--check]
```

- Same input contract (JSON object of string→string pairs; duplicate keys:
  last value wins).
- Sorts keys by UTF-8 byte order.
- `--check`: independent re-parse + binary-search round-trip of every key
  plus a known miss.
- Width guards: key/value < 255² bytes, totals < 255⁴.

### Test data

```sh
python3 gen_data.py            # data/{small,medium,large}.json (deterministic, seeded)
```

`small` (1,005 keys) also carries explicit edge cases: empty key→empty value,
1-char key, unicode keys/values, keys/values with spaces, dotted names.

### Correctness

`fromJSON` is used **only in the test harness**, never in the implementation:

```sh
for s in small medium large; do
  python3 build_db.py      data/$s.json data/$s.nfd  --check
  nix eval --impure --expr "(import ./test_correctness.nix) \"$s\""
  python3 build_db2.py     data/$s.json data/$s.nfd2 --check
  nix eval --impure --expr "(import ./test_correctness2.nix) \"$s\""
  python3 build_db_bin.py  data/$s.json data/$s.nkb  --check
  nix eval --impure --expr "(import ./test_correctness_bin.nix) \"$s\""
done
```

`test_correctness.nix` / `test_correctness2.nix` / `test_correctness_bin.nix`
check, for every key: `db.get k == (fromJSON json)."${k}"`, plus miss→`null`,
`has` on a present and an absent key, and `db.count == n`. Expected:
`ok = true`, `mismatchCount = 0` on all datasets (753,015 lookups across the
three formats).

## Performance (summary)

| workload | NFK v1 (hash) | NFK v2 (dense) | NKB (binary search) | fromJSON |
|---|---|---|---|---|
| Cold single `nix eval` lookup, 200k keys | 97.8 ms (2.14×) | 81.2 ms (2.58×) | **79.6 ms** (2.63×) | 209.2 ms |
| Cold single lookup, 50k keys | 46.6 ms (1.69×) | **42.0 ms** (1.87×) | 42.7 ms (1.84×) | 78.6 ms |
| Cold single lookup, 1k keys | 33.5 ms | 33.2 ms | 34.5 ms | 34.0 ms (parity — startup-bound) |
| Warm 200 lookups per eval, 200k keys | 105.6 ms | **84.5 ms** | 97.5 ms | 217.3 ms |
| Marginal in-process cost per lookup | ~0–15 µs (noise-limited) | ~16–22 µs (hash + probe) | ~54–101 µs (≤18 steps) | < 1 µs (attrset) |
| DB file size, 200k keys | 33.3 MB (2.40× JSON) | 18.1 MB (1.30× JSON) | 17.1 MB (1.23× JSON) | 13.9 MB |
| `readFile` floor, 200k keys | 100.8 ms | 80.0 ms | 76.6 ms | 58.2 ms read (+ parse) |

All three custom formats beat `fromJSON` cold (2.63× / 2.58× / 2.14× at
200k keys), because `fromJSON` pays a full file parse (~262 ms at 200k keys)
on every eval regardless of how many keys are touched. NKB's cold edge comes
from the smallest file (readFile floor 76.6 vs 80.0 ms at 200k); NFK v2's
per-lookup cost is ~4–5× lower than NKB's, so one evaluation doing hundreds
of lookups on a large table tips to NFK v2 (84.5 ms warm-200 vs 97.5), and
bulk scans (thousands of lookups/eval) tip to `fromJSON`'s sub-µs attrset.
Crossovers and the full cost model: [REPORT.md](REPORT.md).

## Repository layout

| path | purpose |
|---|---|
| `kv.nix` | NFK v1 lookup module (self-contained) |
| `kv2.nix` | NFK v2 (dense) lookup module (self-contained; recommended) |
| `kv_bin.nix` | NKB lookup module (self-contained) |
| `build_db.py` | JSON → NFK v1 builder with independent parser + `--check` |
| `build_db2.py` | JSON → NFK v2 builder with independent parser + `--check` |
| `build_db_bin.py` | JSON → NKB builder with independent parser + `--check` |
| `gen_data.py` | deterministic test-data generator |
| `gen_kv.py`, `gen_kv2.py` | emit `kv.nix` / `kv2.nix` (inline the digit decode tables so they can't be mistyped) |
| `gen_kv_bin.py` | emits `kv_bin.nix` (inlines the 255-entry base-255 pair table) |
| `test_correctness.nix`, `test_correctness2.nix`, `test_correctness_bin.nix` | `fromJSON`-oracle correctness tests (NFK v1, v2, NKB) |
| `bench.py`, `bench_marginal.py` | benchmark harnesses, parameterized per format (`--kv/--ext/--label/--out`) |
| `data/` | `small\|medium\|large.{json,nfd,nfd2,nkb}` test datasets |
| `REPORT.md` | benchmark report and trade-off analysis |

## Known limitations

- **Static**: data changes require re-running the builder.
- **Strings only; arbitrary bytes except NUL** — the full range of a Nix
  string (NUL is the one byte `readFile` rejects; verified on 2.34.7). JSON
  input provides UTF-8, so values never hit the limit. Lookups are
  byte-exact: `stringLength`/`substring`/comparison all operate on bytes
  (`stringLength "héllo"` = 6), and `<` is unsigned byte-lexicographic
  (verified on raw 0x01–0xFF bytes).
- **No partial reads**: Nix's `readFile` reads the whole file, so the cost
  floor is a full file transfer plus one hash — the format's win is skipping
  JSON parsing, not skipping I/O.
- **File size**: fixed-width ASCII structural fields make NFK v1 ≈2.4× the
  equivalent JSON; NFK v2's 22-byte base-36 entries drop it to ≈1.3× (NKB
  ≈1.2×). A binary index would be smaller, but the byte→int decode tables it
  needs can't be written in `.nix` source (high-byte keys mangle to U+FFFD;
  verified) — and diffability would be lost either way.
- On a Nix that has `builtins.parseInt`, NFK v1's two-digit decimal folds
  would collapse to a handful of `parseInt` calls, shaving a few µs per
  lookup.