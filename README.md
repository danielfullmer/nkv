# fast-nix-lookup

A fast, hash-based key/value lookup function for **native Nix**, backed by a
precomputed static database file. The implementation uses only
`builtins.readFile` and `builtins.substring` (plus `builtins.hashString` and
arithmetic) — no `builtins.exec`, no `builtins.fromJSON`, no foreign
interpreters.

Given a JSON file of string→string pairs, a Python builder emits a compact
`.nfd` database; a ~130-line Nix module reads it and answers lookups in
O(1) expected time:

```sh
$ python3 build_db.py data/large.json data/large.nfd
built data/large.nfd: n=200000 M=524288 data=12341356B total=33312940B load=0.38
$ nix eval --impure --raw \
    --expr '((import ./kv.nix) ./data/large.nfd).get "pkgs484.env795.nix877.pkgs793"'
chde4cf665ukuewyy-tx
```

For a 200,000-key table, one cold `nix eval` lookup takes ~97 ms end-to-end
vs ~206 ms for `builtins.fromJSON` + attrset access (2.1× — see
[REPORT.md](REPORT.md) for the full benchmark and trade-off analysis).

## How it works

### Design constraints

- Keys and values are **strings only**, stored **statically** in a precomputed
  file (rebuild the file to change data).
- The Nix side must work with the builtins available on a stock Nix: no
  `fromJSON`, no `exec`, no `parseInt`/`%`/`mod`/`hash` on this build
  (Nix 2.34.7+1) — see the workarounds below.
- Hash-based lookup: one `sha256` per probe.

### File format: NFK v1

Plain ASCII with fixed-width zero-padded decimal fields, so the file is
diffable and safe to pass through Nix's string builtins (the only I/O Nix
has). Three regions:

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

**Data region** — for each key, in insertion order: raw key bytes followed by
raw value bytes. Offsets are absolute from the start of the data region; the
value offset is implied, not stored.

### Lookup algorithm

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

### Nix-side workarounds

This module targets a stock Nix builtin surface:

- **No `builtins.parseInt`** — decimal fields are decoded two digits at a
  time through a 100-entry lookup table and a `foldl'`
  (`acc = acc * 100 + d2."${s[p:p+2]}"`).
- **No `%` / `builtins.mod`** — slot wrap-around is
  `builtins.bitAnd (s + 1) (M − 1)`, exact because M is a power of two.
- **No `builtins.hash`** — `hashString "sha256"` is used (the hash the format
  is built around).
- `builtins.genList` is called function-first (`genList (i: i) n`) on this
  build.
- Header fields `M`/`N` are forced once at import time; every lookup does one
  `hashString`, a few `substring`s per probe step, and a byte compare.

The Python builder (`build_db.py`) computes the identical hash/slot/fingerprint
(`hashlib.sha256(key).hexdigest()` → `h[:16]`, `int(h[-8:], 16) & (M-1)`),
which is why the two implementations agree on every key.

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
  python3 build_db.py data/$s.json data/$s.nfd --check
  nix eval --impure --expr "(import ./test_correctness.nix) \"$s\""
done
```

`test_correctness.nix` checks, for every key: `db.get k == (fromJSON json)."${k}"`,
plus miss→`null`, `has` on a present and an absent key, and `db.count == n`.
Expected: `ok = true`, `mismatchCount = 0` on all datasets.

## Performance (summary)

Full numbers, method, and cost model in [REPORT.md](REPORT.md):

| workload | result |
|---|---|
| Cold single `nix eval` lookup, 200k keys | **2.1× faster** than `fromJSON` + attrset (97 ms vs 206 ms) |
| Cold single lookup, 50k keys | **1.7× faster** (47 ms vs 78 ms) |
| Cold single lookup, 1k keys | parity (both startup-bound at ~34 ms) |
| Marginal in-process cost per lookup | NFK ~11–20 µs (hash + probe) vs attrset `< 1 µs` |

The split: `fromJSON` pays a full file parse (≈150 ms at 200k keys) on every
evaluation regardless of how many keys are touched, while NFK pays a small
per-lookup tax. Crossover ≈ 5,500 lookups per evaluation on the 200k table —
below it NFK wins, above it `fromJSON`'s O(1) attrset wins.

## Repository layout

| path | purpose |
|---|---|
| `kv.nix` | the lookup module (self-contained; the only Nix file you need) |
| `build_db.py` | JSON → NFK builder with independent parser + `--check` |
| `gen_data.py` | deterministic test-data generator |
| `gen_kv.py` | emits `kv.nix` (inlines the 100-entry two-digit decode table so it can't be mistyped) |
| `test_correctness.nix` | `fromJSON`-oracle correctness test |
| `bench.py`, `bench_marginal.py` | benchmark harnesses (results in `bench_results.json`, `bench_marginal.json`) |
| `data/` | `small\|medium\|large.{json,nfd}` test datasets |
| `REPORT.md` | benchmark report and trade-off analysis |

## Known limitations

- **Static**: data changes require re-running the builder.
- **Strings only**; NFK stores raw bytes, so UTF-8 is handled as bytes.
- **No partial reads**: Nix's `readFile` reads the whole file, so the cost
  floor is a full file transfer plus one hash — the format's win is skipping
  JSON parsing, not skipping I/O.
- **File size**: fixed-width ASCII index entries make the NFK file ≈2.4× the
  equivalent JSON; a binary index would roughly halve it at the cost of
  diffability.
- On a Nix that has `builtins.parseInt`, the decimal decode folds could be
  replaced by `parseInt` to shave a few µs per lookup.