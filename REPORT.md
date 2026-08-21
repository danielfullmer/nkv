# Fast key/value lookup for native Nix: nkv (binary hash index)

A lookup function for static string→value tables in **pure Nix** (no
`builtins.exec`, no foreign interpreters, no per-eval parse of the data),
backed by a precomputed **nkv** file: one `hashString sha256` per lookup
seeding a linear-probe walk over a power-of-two table at load ≤ 0.8, all
driven by byte reads (`substring`) of the file. The table's
decode alphabet is a format constant stored in one static Nix file
(`nkv-table.nix`), imported once per eval.

For very large tables, or evals doing only a few lookups, the table can be
**sharded** into a directory of independent nkv files (`nkv.nix` reads
one shard per lookup, 46–62 KB each on the 200k table, so a cold single lookup reads ~54 KB
instead of the whole table). Every number in this report is split into the
**Nix load floor** — measured as a cold empty eval
(`nix eval --impure --raw --expr '""'`, 33.0 ms median here) — and the table
**work** on top of it: a sharded single lookup's work is ~1.2 ms, a
single-file one's ~22.9 ms. Headline: a cold single lookup on the 200,000-key
table drops from ~210 ms (`fromJSON`) to **~34 ms (sharded) / ~57 ms
(single file)** total.

## Summary

| | result |
|---|---|
| correctness | 251,005 parent single-file + 200,000 parent sharded + 127,616 multiverse lookups (31,904 × 2 datasets × {single, sharded}): **0 mismatches**, all misses → `null` |
| Cold single lookup, 200k keys | ~34 ms sharded (work 1.2) / ~57 ms single-file nkv (work 22.9) vs ~210 ms `builtins.fromJSON` (work 174.7) — ≈146× / 7.6× on the data work |
| Cold single lookup, 50k keys | 39.7 ms (work 4.9) vs 80.2 ms (work 45.9), 9.4× on work; 1k keys: 34.3 vs 34.9 ms, work 1.1 / 0.1 ms (parity, startup-bound) |
| nixpkgs-multiverse, 31,904 attrs | 8–18× (single file) and 7–149× (256 shards; low query counts; at N = 1 the history sharded work rounds to 0.0) across N = 1–200, on the data work |
| file size | 200k keys: 13,652,092 B = 0.98× the 13.9 MB JSON (3,390,934 B = 0.98× at 50k; 70,750 B = 1.03× at 1k) |
| Nix load floor | Measured as a cold empty eval (`nix eval --impure --raw --expr '""'`, no expression work): 33.0 ms median (parent, 23.2–38.7) / 32.4 ms (multiverse, 23.5–34.7); nkv work on top: ~0–23 ms sharded / ~10–32 ms single-file; `fromJSON` parse work ~123–228 ms |

## Why not `builtins.fromJSON`?

`( builtins.fromJSON (builtins.readFile "large.json") )."some.key"` works —
but the 13.9 MB file is parsed on **every evaluation, for any number of
lookups**: ~207–214 ms wall per eval on the 200k-key table — ~173–181 ms of
work on top of the ~33 ms Nix load floor — flat from 1 to 200 lookups
(the parse is the whole work term). nkv
reads only the 16-byte header, one EW-byte (5–6 byte) index entry per probe
step, and
the one key/value pair needed. The lookup result is identical; the
correctness oracle (`test_correctness3.nix`) checks every key against
`fromJSON`.

## File format: nkv

nkv is a binary format for string→value tables: open addressing with a dense power-of-two table and EW-byte entries
(EW = koffW + klenW + vlenW; 5–6 in the current single-file builds, 4–5 in
the 256-shard builds). Numeric fields are 1–4 **base-255** bytes (one byte per
digit, `byte = digit + 1`, big-endian digits), and the per-field widths are
chosen at build time and stored in the header — so every file byte is
`0x01`–`0xFF` and the file never contains NUL, the one byte Nix's `readFile`
rejects.

```
     offset 0              header, 16 bytes
     offset 16             index region, M × EW bytes (EW = 3–10; 5–6 in the shipped single-file builds)
     offset 16 + M·EW      data region (interleaved, variable)
```

**Header (16 bytes):**

| field | offset | width | meaning |
|---|---:|---:|---|
| magic | 0 | 4 | `NKV3` |
| `N` | 4 | 3 | entry count (base-255) |
| `M` | 7 | 4 | table size, power of two (base-255) |
| revision | 11 | 1 | `6` (0x36) — no data-region totals |
| reserved | 12 | 1 | `0x01` (base-255 digit 0) — the rev-5 `fpW` width byte, kept as a reserved slot |
| `koffW` `klenW` `vlenW` | 13–15 | 3 | per-field base-255 widths — each the smallest that fits the table's max: 1–4 / 1–3 / 1–3 |

**Index region** — one EW-byte entry per table slot `s` at offset `16 + EW·s`:

| field | offset | width | meaning |
|---|---:|---:|---|
| `keyOff` | 0 | 1–4 | absolute file offset of the key; `0` = unused slot |
| `keyLen` | `koffW` | 1–3 | key length |
| `valLen` | `koffW`+`klenW` | 1–3 | value length (value is at `keyOff + keyLen`) |

An unused slot is EW bytes of `0x01` (all fields zero); a miss is a
decoded `keyOff` of 0 (a real key offset is always ≥ `16 + EW·M`). Every
occupied slot is read and byte-compared against the key, so a mismatch
costs one extra key read and a wrong value is impossible by construction.
The data region interleaves key and value bytes in JSON insertion order,
so only the key offset is stored per entry — the value offset is implicit.

**Decode table — static, not in the file.** The base-255 alphabet (255 bytes
`0x01`–`0xFF` → digits 0–254) is a format constant shared by every nkv
file. It lives in `nkv-table.nix` — a 255-entry attrset mapping each byte's
one-character string to its digit — generated by

```sh
python3 build_db3.py --write-table nkv-table.nix
```

`nkv.nix` imports it once per eval (Nix's import cache makes repeat imports
free), so the table costs nothing per lookup and each data file no longer
carries 255 bytes of it (a sharded file no longer carries it per shard).
The file is generated, not hand-edited: it intentionally contains raw
non-UTF-8 bytes in string literals (Nix accepts them; only *invalid*
escapes fail), with the four literal-breaking bytes escaped
(`0x0A → \n`, `0x0D → \r`, `0x22 → \"`, `0x5C → \\`). The `0x0D` escape is
not just hygiene — the Nix lexer normalizes a raw `0x0D` byte in source to
`0x0A` (verified byte-for-byte), so an unescaped raw-CR table would corrupt
the `0x0D` digit.

**Placement and probing.** `s0 = int(h[56:64], 16) AND (M − 1)` (the
probe-seed slice), linear probing, bounded by `M` steps (load < 1
guarantees an unused slot is reached). Every occupied slot is read and
byte-compared against the key — a mismatch costs one extra key read and
can never yield a wrong value.

**Limits** (builder-enforced): `N`, key length, and value length < 254³
(≈16.4 MB); `M` and file offsets < 254⁴ (≈4.16 GB); no NUL anywhere. The
independent `--check` parser re-validates the magic, the revision byte, the
reserved header spaces, and the absence of NUL, and re-probes every key.

Measured sizes (JSON → nkv, with static table):

| dataset | keys | JSON bytes | nkv bytes | index bytes | ratio |
|---|---:|---:|---:|---:|---:|
| small | 1,005 | 68,534 | 70,750 | 10,240 | 1.03× |
| medium | 50,000 | 3,463,238 | 3,390,934 | 327,680 | 0.98× |
| large | 200,000 | 13,941,356 | 13,652,092 | 1,310,720 | 0.98× |
| multiverse versions | 31,904 | 4,833,362 | 5,098,977 | 393,216 | 1.06× |
| multiverse history | 31,904 | 6,876,612 | 7,142,227 | 393,216 | 1.04× |

EW-byte entries, no padding (current builds: EW = 5 = `keyOff` 3 + `keyLen`
1 + `valLen` 1 for the parent tables; EW = 6 = `keyOff` 3 + `keyLen` 1 +
`valLen` 2 for the multiverse single tables; EW = 4 = `keyOff` 2 + `keyLen`
1 + `valLen` 1 for the large shards, EW = 5 with `valLen` 2 for the
multiverse shards — shard-local offsets fit in 2 base-255 digits); the index is
~9.6% of the medium/large files (14.5% of small, where the M = 2,048 table
is proportionally large; 7.7% / 5.5% of the multiverse singles).

**JSON values.** Values are opaque (any UTF-8 minus NUL). String values are
stored raw and returned as-is by `get`; non-string values are stored as
compact JSON documents and decoded back with `builtins.fromJSON` at lookup
time via `getJson`/`getOrJson` (a miss is still `null`).

## Lookup algorithm

```
lookup(key):
  h  = sha256(key) in lowercase hex
  s  = int(h[56:64], 16) AND (M - 1)    # probe-seed slot
  for i in 0..M:                        # bounded walk
    e    = 16 + EW * ((s + i) AND (M - 1)) # EW from the header (bitAnd wrap)
    koff = base-255-decode(entry[e .. e+koffW]) # koffW static-table lookups
    if koff = 0: return null             # unused slot: key absent
    klen = base-255-decode(entry[e+koffW .. e+koffW+klenW])
    k    = substring(raw, koff, klen)
    if k = key:
      vlen = base-255-decode(entry[e+koffW+klenW .. e+EW])
      return substring(raw, koff + klen, vlen)
  unreachable (load < 1)
```

Per probe step: koffW + klenW + vlenW (header values; 5 per step in the
current single-file builds) static-table lookups for the offset and
lengths, one `substring` key read, and one string compare at every
occupied slot; on a hit, one more width-specialized decode for the value
length, then a `substring` value read. One `sha256` per lookup; a
successful walk averages ½(1 + 1/(1−α)) probe steps — ≈ 1.5 at load 0.49,
≈ 2.6 at load 0.76, 3 at the 0.8 cap.

## Sharding

`build_db3.py INPUT.json --shards {16,256,4096} --prefix DIR/ [--check]`
splits the table into independent nkv files, one per slice of the key
hash:

- shard of key `k` = `DIR/<h[24:24+d]>.nkv`, where `h` is the lowercase hex
  of `sha256(k)` and `d` is the digit count (`--shards 16/256/4096` →
  `d = 1/2/3`);
- the slice `[24:24+d)` is disjoint from the probe-seed slice `[56:64)`,
  so sharding does not perturb probe distribution; each shard is a
  standalone nkv table with its own
  `M = next_pow2(max(16, ⌈1.25·N_shard⌉))`;
- **every shard file is always written** — an empty shard is a valid nkv
  file with `N = 0`, `M = 16` — so a key always resolves to an existing
  file;
- `nkv.nix { digits, dir }` is a thin lazy wrapper: per lookup only the
  key's shard file is read (Nix's import cache keeps it for the eval), and
  the static decode table is shared automatically — `nkv.nix`'s single
  `import ./nkv-table.nix` evaluates once per process no matter how many
  shards are imported;
- `db.count` imports every shard file — offline / inspection use only;
- measured shard sizes (256-shard builds): 46,025–62,052 B each on the
  200k-key table (EW = 4), 10,932–34,558 B on versions and 15,294–50,096 B
  on history (EW = 5);
- `--check` in sharded mode re-derives shard membership from the input keys
  and re-probes every key through the shard files.

## Implementation

Files in this repo:

| file | purpose |
|---|---|
| `nkv.nix` | nkv lookup module: `db.get` / `getOr` / `has` / `getJson` / `getOrJson` / `count` / `tableSize`; imports the static decode table once per eval |
| `nkv.nix` | sharded nkv reader: `import ./nkv.nix { digits = 2; dir = ... }`; lazy per-lookup shard import |
| `nkv-table.nix` | the 255-entry base-255 decode table (generated by `build_db3.py --write-table`; a format constant) |
| `build_db3.py` | JSON → nkv builder (single file or `--shards/--prefix`) with independent parser + `--check` |
| `gen_data.py` | deterministic test KV generator (1k / 50k / 200k keys) |
| `test_correctness3.nix` | `fromJSON`-oracle correctness test |
| `bench.py` / `bench_results.json` | 3-method cold-eval benchmark (fromJSON / nkv / nkvs) + raw results |
| `data/` | generated JSON + built `.nkv` files + `large_shards/` (256 shards) |
| `multiverse-faster/` | real-world workload (31,904 attrs): conversion, nkv singles + 256 shards, oracle, 3-method benchmark, own README |

Usage:

```nix
let db = (import ./nkv.nix) ./data/large.nkv;
in {
  db.get "dotted.key.name"   # -> value string, or null if absent
  db.getOr "k" "default"     # -> value or default
  db.has "k"                 # -> true / false
  db.getJson "k"             # -> value parsed as JSON (null on miss)
  db.getOrJson "k" default
  db.count                   # -> N (stored entries)
  db.tableSize               # -> M (slots)
}
```

The module asserts the `NKV3` magic, the revision byte, and the field
widths at import.

```sh
python3 build_db3.py INPUT.json OUTPUT.nkv --check        # single file
python3 build_db3.py INPUT.json --shards 256 --prefix DIR/ --check   # sharded
python3 build_db3.py --write-table nkv-table.nix          # regenerate the static table
```

Builder guarantees: `N` and each key/value length < 254³ bytes, `M` and
file offsets < 254⁴ (raises otherwise); the Python
hash is byte-identical to Nix's (`hashlib.sha256(k).hexdigest()` → same
probe seed `int(h[56:64],16) & (M-1)` and same shard name), which the
cross-language round-trip proves; single-file output is byte-identical
across rebuilds.

Nix 2.34.7+1 workarounds this module relies on:

- **no `builtins.parseInt`** (also no `%`, no `builtins.mod`, no
  `builtins.hasSuffix`, no `or`): every base-255 field decodes one byte at a
  time through the static `nkv-table.nix` attrset, via one
  width-specialized thunk per width (1–4) selected once per file from the
  header (1–4 lookups per field; current builds: 3/1/1–2 for the singles,
  2/1/1–2 for the shards); the only `foldl'` in the module is the 16-radix
  `hexInt` fold;
- **no `%`**: probe wrap is `builtins.bitAnd (s + i) (M - 1)`, exact because
  `M` is a power of two;
- **probe seed**: `hexInt` fold (`* 16 +`) over `sha256(key)`'s hex chars
  `[56:64)`, `bitAnd`-masked to `M - 1` (8 hex chars = 32 bits, masked to
  a power-of-two table index);
- **list-literal gotcha** (Nix 2.34.7+1): `[db.get "k"]` is a two-element
  list — the function `db.get` and the string `"k"`, since an attribute
  path does not absorb a following argument as an application inside a
  list literal — so result lists are built with `builtins.map` (as the
  harnesses and benches do);
- **binary-safe I/O**: bytes from `readFile` pass through `substring`,
  `stringLength`, and `hashString` untouched (verified on 2.34.7); only
  source *literals* are UTF-8-decoded — which is why the decode table is an
  attrset over the raw bytes (with the four literal-breaking bytes escaped)
  rather than a raw string literal;
- **no mutation**: the probe is a tail-recursive walk over integer positions
  of the one file string; header `M`/`N` are forced once at import time,
  not per lookup; the decode table attrset is likewise built once per
  process.

## Correctness

Each dataset is cross-checked against the `fromJSON` oracle in
`test_correctness3.nix` (every key, plus a known miss, plus `count` and
`tableSize`); all three datasets report `ok = true`, `mismatchCount = 0`
(251,005 oracle lookups: 1,005 + 50,000 + 200,000). The builder's own
`--check` independently re-probes every key at build time (Python probe +
known miss).

Sharded correctness: the 256-shard build of the 200k-key table is
re-probed end-to-end through the shard files (200,000 lookups, 0
mismatches, miss → `None`). The multiverse single-file and
sharded indexes (256 shards each) are checked against `fromJSON` in
`multiverse-faster/test_correctness.nix` and
`multiverse-faster/test_correctness_shards.nix`: `mismatches = 0`, misses →
`null`, `count = 31904` for both datasets (127,616 lookups: 31,904 × 2
datasets × {single, sharded}).

## Benchmark

**Environment:** Nix 2.34.7+1 (non-FLAKE native eval), Python 3.13, Linux
x86-64 (Ryzen Threadripper 3970X). Files hot in page cache. Wall clock via
`time.perf_counter` around subprocess `nix eval --impure --raw --expr`
invocations.

**Method.** Nix has no cross-invocation caching, so the realistic unit of
work is one `nix eval`: process start, load data source, look up key(s),
exit. Per point, N fresh evals (N = 7; the multiverse suite runs the same
harness), each doing exactly n lookups of 200 strided keys (or one field
read at n = 0), medians reported. For `fromJSON`, n = 0 forces the whole
parse; on the 200k table it additionally forces all key names (a count), so
that point overstates plain parse cost. Every timed expression is asserted
against the JSON source — every timed run is also a correctness run.
Harness: `bench.py` (parent), `multiverse-faster/bench.py` (real workload);
raw output: `bench_results.json` in each directory.

**Time split.** Nix itself costs time to load before any expression work:
process spawn, runtime + evaluator init, I/O. The harness measures that
separately — a `runs`-long series of cold empty evals with the identical
invocation style (`nix eval --impure --raw --expr '""'`, no file read, no
lookup) — and reports, per method: **total** (full cold-eval wall time),
**work = total − floor** (paired by run index: the i-th method run is
paired with the i-th baseline run), and the baseline series itself. The
measured floor: **33.0 ms median** (23.2–38.7) in the parent run, **32.4 ms**
(23.5–34.7) in the multiverse run. Tables below show `total (work)` ms; the
× multipliers are on work (fromJSON work ÷ method work), so the ~32–33 ms
startup floor drops out of both sides.

### Parent, large (200,000 keys; 13.9 MB JSON / 13.7 MB `.nkv`; 256 shards of 46–62 KB)

Median `total (work)` ms per cold eval (n = 200 row: min-of-7, work paired at
the min-total run); Nix load floor this run: 33.0 ms (23.2–38.7):

| lookups/eval | fromJSON | nkv | nkvs |
|---:|---:|---:|---:|
| 0 | 259.8 (225.0) | 58.6 (25.7) | — (no load point) |
| 1 | 210.0 (174.7) | 56.7 (22.9) (7.6×) | **34.3 (1.2)** (≈146×) |
| 5 | 208.1 (175.2) | 58.0 (25.0) (7.0×) | 34.9 (1.3) (≈135×) |
| 10 | 213.5 (181.4) | 59.4 (26.0) (7.0×) | 35.1 (1.4) (≈130×) |
| 30 | 212.4 (178.0) | 57.7 (24.8) (7.2×) | 34.5 (2.8) (≈64×) |
| 100 | 210.4 (177.7) | 59.1 (25.0) (7.1×) | 43.1 (9.8) (≈18×) |
| 200 | 207.0 (173.3) | 59.3 (26.0) (6.7×) | 46.2 (7.5) (≈23×) |

Single cold lookup by dataset size, median of 7 runs, `total (work)` ms
(fromJSON / nkv; multipliers on work): 1k keys 34.9 (0.1) / 34.3 (1.1) —
parity, both startup-bound; 50k 80.2 (45.9) / 39.7 (4.9), 9.4×; 200k
210.0 (174.7) / 56.7 (22.9), 7.6×, vs 34.3 (1.2) ms sharded, ≈146× (1k/50k
from `bench_marginal.json`; 200k from `bench_results.json`).

### Multiverse (31,904 attrs each; 256 shards)

Median `total (work)` ms per cold eval (n = 200 row: min-of-7, work paired at
the min-total run); Nix load floor this run: 32.4 ms (23.5–34.7); versions:
5.48 MB nested JSON / 5.10 MB `.nkv`, history: 7.84 MB nested JSON /
7.14 MB `.nkv` (`fromJSON` parses the nested `index/*.json`; the flat
files feed the builder):

| versions | 121–127 ms    | 10.1–15.2 | 2.9–16.5   |
| history  | 221–228 ms    | 12.4–19.5 | 0.0–19.2   |
| **versions** | | | |
| 0 | 157.4 (126.8) | 42.9 (10.1) | — (no load point) |
| 1 | 157.2 (123.9) | 43.8 (12.6) (9.8×) | **35.3 (3.1)** (≈40×) |
| 5 | 155.9 (122.6) | 42.7 (10.1) (12.1×) | 34.7 (2.9) (≈42×) |
| 10 | 157.6 (123.1) | 43.5 (11.4) (10.8×) | 35.9 (4.8) (≈26×) |
| 30 (lock file) | 157.4 (125.1) | 43.9 (11.4) (11×) | 37.9 (4.6) (≈27×) |
| 100 | 157.9 (126.3) | 46.1 (15.2) (8.3×) | 42.5 (10.1) (≈13×) |
| 200 | 155.4 (120.7) | 44.2 (11.1) (10.9×) | 50.1 (16.5) (≈7.3×) |
| **history** | | | |
| 0 | 254.9 (226.6) | 45.8 (16.3) | — (no load point) |
| 1 | 256.4 (228.3) | 44.8 (12.4) (18.4×) | **33.3 (0.0)** |
| 5 | 256.0 (223.7) | 46.5 (14.1) (15.9×) | 33.9 (1.5) (≈149×) |
| 10 | 256.2 (224.1) | 46.2 (13.3) (16.8×) | 34.3 (2.0) (≈112×) |
| 30 (lock file) | 255.1 (224.5) | 47.3 (14.9) (15.1×) | 36.9 (3.5) (≈64×) |
| 100 | 259.0 (226.6) | 49.3 (19.5) (11.6×) | 44.0 (10.6) (≈21×) |
| 200 | 253.2 (221.0) | 49.7 (16.2) (13.6×) | 51.7 (19.2) (≈11.5×) |

(n = 0 semantics: `fromJSON` = parse plus one forced field read; `nkv` =
`db.count`, i.e. whole-table readFile + header read (the count is a header
field — no slot walk); `nkvs` has no n = 0 point — its n = 1 row is the
intercept.)

### Analysis

Cost model per `nix eval` doing n lookups (`floor` = the measured Nix load
floor):

    fromJSON(n) ≈ floor + readFile(JSON) + parse(JSON) + sub-ms·n
    nkv(n)     ≈ floor + readFile(.nkv) + probe-cost·n
    nkvs(n)    ≈ floor + Σ readFile(shard) for distinct shards hit + probe-cost·n

The floor is measured, not assumed: the harness runs the same cold
invocation with an empty expression — **33.0 ms median** (23.2–38.7, parent
run), **32.4 ms** (23.5–34.7, multiverse run) — and work is total minus
the per-run floor.

- **The intercept is the whole game.** `fromJSON`'s work term is flat
  (~173–181 ms on the 200k table; ~121–127 ms versions, ~221–228 ms
  history) because it must parse the whole file regardless of how many
  keys are asked for; its per-lookup cost after the parse is negligible.
- **The sharded single-lookup number is essentially the floor.** nkvs's
  work at n = 1 is **1.2 ms** on 200k keys and **0.0–3.1 ms** on the
  multiverse tables: shard selection (`sha256`), one ~46–62 KB /
  ~11–35 KB / ~15–50 KB `readFile`, the header asserts, the static-table
  import, and the probe (a key read + byte compare at every occupied slot)
  all fit in a few ms. The ~34 ms total *is* the ~32–33 ms Nix load; the
  ≈146× (200k) / up to ≈149× (multiverse history, N = 5) data-work speedup
  over `fromJSON` comes entirely from not paying the parse +
  whole-file-read work (at N = 1 on history the sharded work rounds to
  0.0 and the total sits at the floor itself).
- **Single-file work is the readFile.** nkv's work at n = 1: 22.9 ms
  (13.7 MB table), 12.4–12.6 ms (multiverse); it grows slowly with n
  (22.9 → 32.2 median at n = 200 on the 200k table; 26.0 on the min row —
  probes are cheap, the delta is mostly readFile variance).
- **Crossover nkvs vs nkv:** on the 5.1–7.1 MB multiverse tables —
  versions: sharded is ahead through n = 100 (42.5 vs 46.1 total; work 10.1
  vs 15.2) and single-file takes over at n = 200 (50.1 vs 44.2 on the min
  row; work 16.5 vs 11.1) — crossover ~100–200. history: sharded ahead
  through n = 100 (44.0 vs 49.3; work 10.6 vs 19.5); at n = 200
  single-file takes over (51.7 vs 49.7 on the min row, work 19.2 vs 16.2;
  54.1 vs 51.2 median) — crossover ~100–200. On the 13.7 MB 200k-key table
  the single-file readFile keeps nkvs ahead across the whole measured
  range (46.2 vs 59.3 total at n = 200; work 7.5 vs 26.0); the crossover
  is beyond 200 lookups there.
- **The sharded slope.** nkvs's work rises as queries spread across the
  256 shards — 1.2 → 7.5–19.5 ms (200k), 3.1 → 16.5–18.7 ms (versions),
  0.0 → 19.2–23.4 ms (history), n = 1 (median) → n = 200 (min → median),
  ~0.03–0.12 ms/lookup; each new distinct shard pays one readFile + header
  cost per eval.
- **Bulk scans tip back to `fromJSON`** — above the crossover, parsing
  once and indexing the attrset beats per-lookup file slicing: a full
  31,904-key scan with every value serialized takes 0.38 s (`fromJSON`)
  vs 0.89 / 2.49 s (nkv / nkvs); if an eval touches most of the table,
  `fromJSON` wins.

Verdict from the numbers:

- **nkv (single file)** beats `fromJSON` at every measured point on
  50k+ entry tables, on the data work (startup excluded): 9.4× at 50k keys,
  7.6× at n = 1 and 6.7× at n = 200 on the 200k-key table (work ~23–32 ms
  vs ~173–181 ms), 8–12× on versions, 11–18× on history. At 1k keys the
  two are at parity (work 1.1 vs 0.1 ms — startup-bound).
- **Sharded nkv** adds the low-query regime: a single cold lookup
  costs ~34 ms total on a 200k-key table (work 1.2 ms) — ≈146× the data
  work of `fromJSON` — and stays ahead of single-file nkv up to
  ~100–200 lookups on both multiverse tables and across the whole measured
  range on the 13.7 MB table.
- **`fromJSON`** remains the right tool when one evaluation touches a large
  fraction of the table (bulk in-process scan).

## Trade-offs (summary)

1. **Per-lookup cost in-process** is small for both nkv variants
   (probe walk ≈ 1.5 steps at load 0.49, ≈ 2.6 at load 0.76; a few
   `substring`s + table lookups per step), but neither can beat
   `fromJSON`'s sub-ms attrset access when one eval touches most of the
   table — the crossover analysis above quantifies that, and the measured
   bulk scan (0.38 s `fromJSON` vs 0.89 / 2.49 s nkv over 31,904 keys
   with every value serialized) confirms it. The key is read and
   byte-compared at every occupied walk step — the cost of having no
   fingerprint to pre-filter slots; it can never yield a wrong value.
2. **Cold / repeated invocations**: nkv beats `fromJSON` up to the
   crossover (beyond 200 lookups/eval on every table measured; sharded vs
   single-file: ~100–200 on both multiverse tables). At ~1k entries
   all approaches tie: ~34 ms total each, of which ~0–1 ms is data work —
   the rest is Nix itself loading.
3. **File size**: 0.98× the JSON at 50k and 200k keys (the index is
   ~9.6% of the file); 1.03× at 1k keys, where the fixed 16-byte header +
   10,240-byte index (M = 2,048) are 14.5% of the file. Parameterized
   field widths (revision 2) dropped the fixed 15-byte rev-1 entries (no
   padding) and cut the 200k-key file from 16.3 MB to 14.7 MB; moving the
   255-byte decode table out of the files saved 255 B per file (and per
   shard) and made the sharded reader simpler; removing the fingerprint
   field (revision 6) cut the 200k-key single file from 14,700,668 to
   13,652,092 B (−7.1%), the 1k-key table by −10.4%, and the 256-file
   large-shard build from 14,688,364 to 13,492,332 B (−8.1%). Per-table
   minimum widths still pay off on small shards: every shard keeps
   shard-local `koffW` at 2 base-255 digits, and empty shards (M = 16) get the
   minimum widths.
4. **Hashing**: one `sha256` per lookup, used twice — 8 hex chars
   `[56:64)` seed the probe, and (in sharded builds) 1–3 hex chars
   `[24:24+d)` name the shard; with no fingerprint, every occupied slot
   in the walk is key-compared and a wrong value is impossible by
   construction.
5. **Static only**: the database is precomputed and immutable at build
   time; adding keys requires re-running the builder (< 2 s for 200k keys;
   sharded rebuilds parallelize per shard).
6. **No `parseInt` / no `%` / no `or` on this Nix**: base-255 decoding is 1–4
   static-table lookups per field (width-specialized thunks; widths from
   the header) plus one 8-char `* 16 +` hex fold for the probe seed
   (`bitAnd`-masked to `M − 1`); probe wrap is `bitAnd` on a
   power-of-two `M`.
7. **Memory**: each lookup allocates a constant number of heap strings
   (probe-seed and key/value fragments); no table is materialised in Nix
   memory at import.
8. **The static table must sit next to `nkv.nix`** (imported by relative
   path). It is a deterministic function of the format — one correct
   content — regenerated by `build_db3.py --write-table`.

## Reproducing

```sh
python3 gen_data.py                              # data/{small,medium,large}.json
python3 build_db3.py --write-table nkv-table.nix
for s in small medium large; do
  python3 build_db3.py data/$s.json data/$s.nkv --check
done
python3 build_db3.py data/large.json --shards 256 --prefix data/large_shards/ --check
nix eval --impure --json --expr '(import ./test_correctness3.nix) "small"'
nix eval --impure --json --expr '(import ./test_correctness3.nix) "medium"'
nix eval --impure --json --expr '(import ./test_correctness3.nix) "large"'
python3 bench.py 7                               # 3-method cold bench (7 runs) -> bench_results.json
```

Multiverse workload (in `multiverse-faster/`):

```sh
python3 convert.py index/versions.json versions_flat.json
python3 convert.py index/history.json history_flat.json
python3 ../build_db3.py versions_flat.json versions.nkv --check
python3 ../build_db3.py history_flat.json history.nkv --check
python3 ../build_db3.py versions_flat.json --shards 256 --prefix versions_shards/ --check
python3 ../build_db3.py history_flat.json --shards 256 --prefix history_shards/ --check
nix eval --impure --json --expr '(import ./test_correctness.nix) { table = ./versions.nkv; jsonPath = ./index/versions.json; }'
nix eval --impure --json --expr '(import ./test_correctness_shards.nix) { dir = ./versions_shards; jsonPath = ./index/versions.json; }'
python3 bench.py 7
```

Example lookups (same key, both access modes — same value):

```sh
nix eval --impure --raw --expr \
  '((import ./nkv.nix) ./data/large.nkv).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (single file, ~57 ms total / ~23 ms work)

nix eval --impure --raw --expr \
  '(import ./nkv.nix { digits = 2; dir = ./data/large_shards; }).get "pkgs484.env795.nix877.pkgs793"'
# -> chde4cf665ukuewyy-tx        (256 shards, ~34 ms total / ~1.2 ms work — reads one ~46–62 KB shard)
```