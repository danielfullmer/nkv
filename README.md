# fast-nix-lookup

Fast key/value lookup for native Nix eval. Instead of `builtins.fromJSON` + attrset access (which parses the whole file on every `nix eval`), the table is precomputed into an **NFK v3** file that Nix can probe with byte reads, string slicing, and `hashString` — no parse.

```nix
let db = (import ./kv3.nix) ./data/large.nfd3;
in db.get "pkgs484.env795.nix877.pkgs793"
# -> chde4cf665ukuewyy-tx
```

For very large tables, or evals that do only a few lookups, the same format can be sharded so a lookup reads one small shard file instead of the whole table (`kv3s.nix`):

```nix
let db = import ./kv3s.nix { digits = 2; dir = ./data/large_shards; };
in db.get "pkgs484.env795.nix877.pkgs793"   # only the key's 1-of-256 shard is read
```

A single cold lookup (N = 1) costs ~35 ms sharded / ~62 ms single-file NFK v3
vs ~212 ms for `builtins.fromJSON`. On the data work — total minus the ~34 ms
`nix eval` startup floor both sides pay — that is ~110× sharded / 6.3×
single-file: the ~35 ms sharded total is almost entirely the startup
(~34 ms measured as an empty eval), and its data work is ~1.6 ms (one ~80 KB
shard read + one probe) against `fromJSON`'s ~178 ms `readFile`+parse of the
16.3 MB file. NFK v3 beats `fromJSON` on the data work at every query count
on the nixpkgs-multiverse workload (31,904 attrs): 8.2–18× single file,
7.7–250× sharded (low query counts); details in REPORT.md and
multiverse-faster/.

## Why not `builtins.fromJSON`?

A single lookup in the JSON version looks like:

```nix
( builtins.fromJSON (builtins.readFile "./large.json") )."some.key"
```

`builtins.fromJSON` costs ~210–215 ms wall per eval on the 200k-key table —
~175–186 ms of `readFile`+parse work on top of the ~34 ms `nix eval` floor —
and the cost is essentially flat from 1 to 200 lookups per eval: the JSON
parse is the whole bill and lookup adds nothing measurable. NFK v3 is
6.3× faster single-file and ~110× with 256 shards on the data work (N = 1), and stays ahead
at every N measured (up to 200 lookups/eval). Single-lookup cost scales with
file size — measured 35–67 ms wall per eval (work 1–30 ms) depending on
size and shard count — and with shard count at higher N, since each distinct
shard file is read once per eval.

## Design constraints (this Nix)

The target is Nix 2.34.7 with a stripped-down builtin set. What is missing shapes the format:

- **No `builtins.parseInt` (and no `%`, no `builtins.mod`, no `builtins.hasSuffix`, no `or`)** — integer arithmetic is limited to `/` (truncating division), `builtins.div`, `builtins.bitAnd`; the design works around it with fixed-width fields decoded by table folds and power-of-two tables masked with `bitAnd`.
- **Hash via `builtins.hashString "sha256"`** — stable across processes and platforms, so a fingerprint computed at build time matches at eval time.
- **Binary-safe strings** — Nix I/O strings are arbitrary bytes minus NUL; only string *literals in source* are UTF-8-decoded. Raw bytes from `readFile` pass through `substring`, `stringLength`, and `sha256` untouched (verified on 2.34.7). The builder therefore refuses NUL, and every file byte is in `0x01`–`0xFF`.
- **One source-literal gotcha** — the lexer normalizes a raw `0x0D` byte in `.nix` *source* to `0x0A` (verified byte-for-byte). Raw-bytes data files are unaffected (`readFile` output is raw), but any table material emitted into Nix source must escape `0x0D` as `\r` — which is exactly how the generated decode table (below) is written.
- **No in-eval mutation** — the lookup builds one string (the value) from `substring` slices of the file; the index is walked by recursion over integer positions, never by re-reading the file.

## File format: NFK v3 (binary hash index)

Open addressing: one `sha256` per lookup, a 24-bit fingerprint, and linear probing over a power-of-two table at load ≤ 0.8. Numeric fields are 3–4 **b254** bytes (one byte per digit, `byte = digit + 1`, big-endian digits), so every file byte is `0x01`–`0xFF` and the file never contains the one byte Nix's `readFile` rejects.

```
offset 0              header, 64 bytes
offset 64             index region, M × 15 bytes
offset 64 + M·15      data region (interleaved, variable)
```

**Header (64 bytes):**

| field      | offset | width | meaning                                          |
|------------|-------:|------:|--------------------------------------------------|
| magic      | 0      | 4     | `NFK3`                                           |
| `N`        | 4      | 3     | entry count (b254)                               |
| `M`        | 7      | 4     | table size, power of two (b254)                  |
| `keyTotal` | 11     | 3     | total key bytes (b254)                           |
| `valTotal` | 14     | 3     | total value bytes (b254)                         |
| revision   | 17     | 1     | `1` (0x31) — no table in file                    |
| —          | 18     | 46    | reserved (spaces)                                |

**Index region** — one 15-byte entry per table slot `s` at offset `64 + 15·s`:

| field    | offset | width | meaning                                             |
|----------|-------:|------:|-----------------------------------------------------|
| `fp`     | 0      | 4     | `int(sha256(key) hex [0:6], 16) + 1` (24-bit); 0 = unused |
| `keyOff` | 4      | 4     | absolute file offset of the key                     |
| `keyLen` | 8      | 3     | key length                                          |
| `valLen` | 11     | 3     | value length (the value is at `keyOff + keyLen`)    |

An unused slot is 15 bytes of `0x01` (all fields zero). The data region interleaves key and value bytes in JSON insertion order, so only the key offset is stored per entry.

**Decode table — static, not in the file.** The b254 alphabet (255 bytes `0x01`–`0xFF` → digits 0–254) is a format constant shared by every NFK v3 file, so it is stored in exactly one place: `nfd3-table.nix`, a 255-entry attrset (`byte 1-char string → digit`) generated by

```sh
python3 build_db3.py --write-table nfd3-table.nix
```

`kv3.nix` imports it once per eval (the Nix import cache makes repeat imports free), so the table costs nothing per lookup and no per-file 255 bytes. The file is generated, not hand-edited; it intentionally contains raw non-UTF-8 bytes (Nix accepts them in string literals) with only the four literal-breaking bytes escaped (`0x0A → \n`, `0x0D → \r`, `0x22 → \"`, `0x5C → \\`).

Invariants:

- `s0 = int(h[56:64], 16) AND (M − 1)`; linear probing, bounded by `M` steps. A fingerprint hit is confirmed by a byte-for-byte key compare, so a 24-bit collision (expected ≈ N²/2²⁴ false pairs ≈ 2 at 200k keys) costs one extra key read — never a wrong value.
- `M = next_pow2(max(16, ⌈1.25·N⌉))` → load ≤ 0.8 (fixed; no factor flag).
- b254 width limits (builder-enforced): `N` / `keyTotal` / `valTotal` / key length / value length < 254³ (~16.4 MB); `M` and offsets < 254⁴ (~4.16 GB); no NUL.
- Sizes: 1,005 keys → 91,278 B; 50,000 → 4,046,342 B; 200,000 → 16,273,580 B = 1.17× the 13.9 MB JSON.
- Values are opaque: any UTF-8 minus NUL may be stored. String values are returned as-is by `get`; when a value holds a JSON document, `getJson`/`getOrJson` decode it with `builtins.fromJSON` at lookup time (a miss is still `null`).

### Optional file sharding

For very large tables, or evals that do only a few lookups, `build_db3.py` can split the table into a directory of independent NFK v3 files, one per slice of the key hash:

```sh
python3 build_db3.py INPUT.json --shards 256 --prefix sharded/ --check
```

- Shard of key `k` = `sharded/<h[24:24+d]>.nfd3`, where `h` is the lowercase hex of `sha256(k)` and `d` is the number of digits: `--shards 16/256/4096` → `d = 1/2/3`.
- **Every shard file is always written** — an empty shard is a valid NFK v3 file with `N = 0`, `M = 16` — so a key always resolves to an existing file.
- The slice `[24:24+d)` is disjoint from the fingerprint slice `[0:6)` and the probe-seed slice `[56:64)` the probing algorithm uses, so sharding does not perturb probe distribution; each shard is a standalone NFK v3 table with its own `M`.
- Reader: `kv3s.nix` — per lookup only the shard the key hashes to is read, and Nix's import cache keeps it for the rest of the eval. The static decode table is shared automatically: `kv3.nix`'s single `import ./nfd3-table.nix` is evaluated once per process no matter how many shard files are imported.
- `--check` in sharded mode re-derives shard membership from the input keys and re-probes every key through the shard files.

## Lookup algorithm

```
lookup(key):
  h  = sha256(key) in lowercase hex
  fp = int(h[0:6], 16) + 1              # 24-bit fingerprint
  s  = int(h[56:64], 16) AND (M - 1)    # initial slot
  for i in 0..M:                        # bounded walk
    e    = 64 + 15 * (s + i) AND (M - 1)  # (bitAnd wrap, M a power of two)
    efp  = b254-decode(entry[e .. e+4])  # 4 static-table lookups
    if efp = 0: return null              # unused slot: key absent
    if efp ≠ fp: continue                # fingerprint miss: probe on
    k    = substring(raw, keyOff, keyLen)
    if k = key: return substring(raw, keyOff + keyLen, valLen)
  unreachable (load < 1 guarantees an unused slot)
```

- Per probe step: 4–3 table lookups (fingerprint, offsets, lengths) via the static decode attrset, one `substring` key read, one string compare.
- One `sha256` per lookup; expected ~1–2 probe steps at load 0.8.
- The fingerprint is compared as an int and every hit is confirmed byte-for-byte, so the 24-bit fingerprint can only add a key read, never a wrong value.

## Nix-side workarounds

- **No `builtins.parseInt`** — every b254 field decodes one byte at a time through the static `nfd3-table.nix` attrset (3–4 lookups per field: `acc * 254 + table."${substring p 1 raw}"`); the 24-bit fingerprint and the probe seed fold the 6/8 hex chars of the digest through a 16-entry inline table (no `%`, just `* 16 +` and a final `bitAnd` mask).
- **No `%`** — probe wrap is `builtins.bitAnd (s + i) (M - 1)` (M is a power of two); the midpoint-free design (hash probing, no binary search) means no other modulo is needed.
- **No `or`** — the modules use `if x == null then a else b` and plain booleans throughout.
- **No mutation** — everything is a pure `let`/recursion over the file string; the only "state" is integer positions.
- **Binary-safe b254 decoding** — raw `readFile` bytes pass through `substring` untouched; the decode table (an attrset in source) is the only place where bytes meet the lexer, and it escapes the four literal-breaking bytes (see the format section).

Header fields and the table import happen once at import time; each lookup then costs one `hashString`, one 6-char and one 8-char hex fold, and a few `substring`s per probe step.

## Builder

`build_db3.py` reads a JSON object and emits NFK v3 files (single file or sharded), with an independent re-parser and `--check`:

```sh
python3 build_db3.py INPUT.json OUTPUT.nfd3 [--check]
python3 build_db3.py INPUT.json --shards {16,256,4096} --prefix DIR/ [--check]
python3 build_db3.py --write-table nfd3-table.nix
```

- Input: a JSON object with string keys and **arbitrary JSON values** — string values are stored raw; non-string values are stored as compact JSON documents, which `getJson`/`getOrJson` decode back.
- Sharded mode writes `DIR/<h[24:24+d]>.nfd3` for every shard, including empty ones (see the sharding subsection); single-file mode is unchanged.
- `--check` re-parses the file(s) independently — validates magic, revision byte, reserved header spaces, absence of NUL, and the exact file size — and re-probes every key through an independent Python probe plus a known miss. In sharded mode, shard membership is re-derived and every key is re-probed through the shard files.
- Width guards: `N` / totals / lengths < 254³ bytes, `M` / offsets < 254⁴, no NUL.
- Single-file output is byte-identical across rebuilds (no timestamps, insertion-order data region).

`gen_data.py` generates the deterministic test datasets (1k / 50k / 200k keys, seeded RNG).

## Usage

Nix 2.34.7+ (uses `hashString` and byte-safe `substring`; no `builtins.parseInt`).

```nix
let db = (import ./kv3.nix) ./data/large.nfd3;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count; db.tableSize }
```

`get` returns `null` on a miss. `db.getJson "k"` / `db.getOrJson "k" default` return `builtins.fromJSON` of the stored string when the value holds a JSON document (a miss still returns `null`). The module asserts the `NFK3` magic, the revision byte, and the exact file size at import.

Sharded NFK v3 (`kv3s.nix`) takes a directory built with `--shards` (`digits` must match the shard count):

```nix
let db = import ./kv3s.nix { digits = 2; dir = ./data/large_shards; };
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.getJson "k"; db.count }
```

Importing the module is lazy — no shard file is read until a lookup is forced. Per lookup only the key's shard is read. `db.count` imports every shard file — offline / inspection use only.

## Correctness

Each dataset is cross-checked against the `fromJSON` oracle in `test_correctness3.nix` (every key, plus a known miss, plus `count`/`tableSize`):

```sh
python3 gen_data.py
python3 build_db3.py --write-table nfd3-table.nix
for s in small medium large; do
  python3 build_db3.py data/$s.json data/$s.nfd3 --check
  nix eval --impure --json --expr '(import ./test_correctness3.nix) "$s"'
done
```

Expected: `ok = true`, `mismatchCount = 0` on all three datasets (251,005 oracle lookups total). The builder's own `--check` independently re-probes every key at build time.

Sharded correctness: `python3 build_db3.py data/large.json --shards 256 --prefix data/large_shards/ --check` (and 16/4096-shard regression runs) re-derive shard membership and re-probe all 200,000 keys through the shard files (0 mismatches, miss → `None`). The multiverse sharded indexes (256 shards each) are checked against `fromJSON` in `multiverse-faster/test_correctness_shards.nix`: `mismatches = 0`, misses → `null`, `count = 31904` for both datasets.

## Performance summary

One cold `nix eval` per point (median of 3), all methods in the same
session; harness in `bench.py`, raw results in `bench_results.json`. The
harness first measures the **Nix load floor** — a cold empty eval with the
same flags (`nix eval --impure --raw --expr '""'`, no expression work:
process spawn + runtime/evaluator init + output), 33.7 ms median this run —
and then reports each method's **total** and **work = total − floor**
(paired per run). Cells show `total (work) ms`; the × multiplier is on work (fromJSON work ÷ method work), so the ~34 ms startup floor drops out of both. The n=0 point for `fromJSON`
on the 200k table additionally forces all key names (a count), so it
overstates plain parse cost; n=0 for the multiverse tables reads one field.

**Parent, large (200,000 keys; 13.9 MB JSON / 16.3 MB `.nfd3`; 256 shards of 57–85 KB):**

| lookups/eval | fromJSON | nfk3 | nfk3s |
|---:|---:|---:|---:|
| 0 | 260.0 (223.4) | 59.7 (25.9) | — |
| 1 | 211.8 (178.1) | 62.1 (28.4) (6.3×) | **34.6 (1.6)** (110×) |
| 5 | 211.1 (181.6) | 58.2 (26.4) (6.9×) | 35.1 (1.6) (110×) |
| 10 | 209.7 (175.5) | 60.1 (28.6) (6.1×) | 34.8 (1.1) (160×) |
| 30 | 211.4 (178.1) | 62.9 (29.2) (6.1×) | 39.0 (6.7) (27×) |
| 100 | 215.3 (185.8) | 62.9 (33.4) (5.6×) | 44.9 (11.2) (17×) |
| 200 | 208.9 (179.0) | 66.5 (34.6) (5.2×) | 55.4 (21.7) (8.2×) |

Single cold lookup by dataset size, `total (work) ms` (fromJSON / nfk3;
multipliers on work): 1k keys 34.6 (1.2) / 34.2 (1.0) — parity, both
startup-bound; 50k 81.4 (57.5) / 40.6 (7.6), 7.6×; 200k 211.8 (178.1) /
62.1 (28.4), 6.3×, vs 34.6 (1.6) ms sharded, 110×.

**Multiverse (31,904 attrs each; multipliers on work):** versions 5.5 MB
JSON / 5.7 MB `.nfd3` — fromJSON 156.7–168.4 ms total (work 124.2–136.8) vs
nfk3 43.4–49.2 ms (8.2–11×) and nfk3s 33.8–50.8 ms (7.7–69×); history 7.8 MB
JSON / 7.7 MB `.nfd3` — fromJSON 255.5–267.1 ms (work 222.2–233.4) vs nfk3
45.6–55.0 ms (10–18×) and nfk3s 34.6–53.2 ms (11–250×). Full per-point tables in
[REPORT.md](REPORT.md) and
[`multiverse-faster/README.md`](multiverse-faster/README.md).

**Reading the numbers:**

- **The intercept is the game, and the split shows what it is.** `fromJSON`
  is flat (work ~175–186 ms on 200k, ~124–137 ms versions / ~222–233 ms
  history) because it must parse the whole file regardless of how many keys
  are asked for; its per-lookup cost after the parse is negligible.
- **Sharding wins the low-query regime.** nfk3s pays one small-shard
  readFile per *new* shard (~0.1–0.2 ms, import-cached for the eval)
  instead of a 16.3 MB readFile: at 1 lookup its **work is ~1.6 ms**
  (multiverse: ~2 ms) — the ~35 ms total is essentially the measured 33.7 ms
  Nix load floor — ~110× the data work of `fromJSON` on 200k keys (1.6 vs
  178.1 ms).
- **Crossover with single-file nfk3:** on the 5.7–7.7 MB multiverse tables
  nfk3s is ahead through ~30–100 lookups (versions) / ~100–200 (history) and
  single-file takes over by ~200; on the 16.3 MB 200k-key table the
  single-file readFile (work ~26–35 ms) keeps nfk3s ahead across the whole
  measured range (1–200: work 21.7 vs 34.6 ms at n=200).
- **Bulk scans tip back to `fromJSON`** — if an eval touches most of the table, parsing once and indexing the attrset beats per-lookup file slicing.

## Repo layout

| path | role |
|---|---|
| `kv3.nix` | NFK v3 lookup module: `db` is a path (or string path) to one `.nfd3` file; imports the static decode table once per eval |
| `kv3s.nix` | sharded NFK v3 reader: takes a `--shards` directory + `digits`, reads only the key's hash shard (lazy; `count` reads all shards — offline use) |
| `nfd3-table.nix` | the 255-entry b254 decode table (static format constant; generated, not hand-edited) |
| `build_db3.py` | JSON → NFK v3 builder (single file or `--shards/--prefix`) with independent parser + `--check`; `--write-table` regenerates `nfd3-table.nix` |
| `gen_data.py` | deterministic test-data generator (1k / 50k / 200k keys) |
| `test_correctness3.nix` | `fromJSON`-oracle correctness test (every key + miss + count) |
| `data/` | `small|medium|large.{json,nfd3}` (1k / 50k / 200k keys) + `large_shards/` (256-shard NFK v3 of `large.json`) |
| `bench.py`, `bench_results.json` | 3-method cold-eval benchmark (fromJSON / nfk3 / nfk3s on the 200k table) + raw results |
| `REPORT.md` | full design + benchmark + trade-off write-up |
| `multiverse-faster/` | real-world workload: fkzakaria's nixpkgs-multiverse index (31,904 attrs) converted to NFK v3, 3-method cold-eval benchmark (single file and 256 shards), oracle, harness, and its own README |
| `suggestions.md` | NFK v3 improvement ideas and what was rejected, 2026-08-20 |

## Known limitations

- **`builtins.fromJSON` still wins for bulk scans** — if an evaluation touches most of the table, parsing once and indexing the attrset beats per-lookup file slicing. NFK v3 targets the common case: one or a few lookups per eval.
- **Nix's string model caps the alphabet at 254** — Nix strings cannot contain NUL, so the numeric fields stop at 254-valued digits (b254: digits 0–253 in bytes `0x01`–`0xFF`). The index region is not human-diffable (the data region is raw UTF-8); a future Nix with `builtins.parseInt` could shrink the index further, and raw-bytes support would lift the NUL limit.
- **The decode table must exist where `kv3.nix` sits** — it is imported by path relative to `kv3.nix`; if you copy the module elsewhere, regenerate/copy `nfd3-table.nix` alongside it (`build_db3.py --write-table`). The table is a deterministic function of the format, so there is exactly one correct content.
- **sha256 is the only stable hash available** — `hashString`'s other modes are not stable across Nix versions/platforms in the same documented way; sha256 is ~3× slower than the alternatives but the cost is one hash per lookup, not per entry.
- **A probe walk of up to M empty slots in the worst case** — bounded but not O(1); at load ≤ 0.8 the expected chain is < 2.1 slots.
- **Fixed table size** — the table is sized for the input at build time; growing it requires a rebuild (cheap: < 2 s for 200k keys; sharded rebuilds are parallelizable per shard).
- **File size ≈ 1.17× the JSON** (200k keys) — the 15-byte hash index costs ~17% over the raw data; in exchange a single-lookup cold eval reads and decodes only a few hundred bytes of the 16.3 MB file.