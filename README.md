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

A single cold lookup (N = 1) costs ~34 ms sharded / ~60 ms single-file NFK v3
vs ~213 ms for `builtins.fromJSON`. On the data work — total minus the ~34 ms
`nix eval` startup floor both sides pay — that is ~300× sharded / 6.9×
single-file: the ~34 ms sharded total is at the startup floor
(31.9–34.8 ms measured as an empty eval), and its data work is ~0.6 ms (one
~50–72 KB shard read + one probe) against `fromJSON`'s ~179 ms
`readFile`+parse of the 14.7 MB file. NFK v3 beats `fromJSON` on the data
work at every query count on the nixpkgs-multiverse workload (31,904
attrs): 10–18× single file, 6.8–251× sharded (low query counts); details in
REPORT.md and multiverse-faster/.

## Why not `builtins.fromJSON`?

A single lookup in the JSON version looks like:

```nix
( builtins.fromJSON (builtins.readFile "./large.json") )."some.key"
```

`builtins.fromJSON` costs ~209–214 ms wall per eval on the 200k-key table —
~174–180 ms of `readFile`+parse work on top of the ~34 ms `nix eval` floor —
and the cost is essentially flat from 1 to 200 lookups per eval: the JSON
parse is the whole bill and lookup adds nothing measurable. NFK v3 is
6.9× faster single-file and ~300× with 256 shards on the data work (N = 1), and stays ahead
at every N measured (up to 200 lookups/eval). Single-lookup cost scales with
file size — measured 34–60 ms wall per eval (work 0.6–26 ms) depending on
size and shard count — and with shard count at higher N, since each distinct
shard file is read once per eval.

## Design constraints (this Nix)

The target is Nix 2.34.7 with a stripped-down builtin set. What is missing shapes the format:

- **No `builtins.parseInt` (and no `%`, no `builtins.mod`, no `builtins.hasSuffix`, no `or`)** — integer arithmetic is limited to `/` (truncating division), `builtins.div`, `builtins.bitAnd`; the design works around it with fixed-width fields decoded by per-byte table lookups (one width-specialized thunk per file) and power-of-two tables masked with `bitAnd`.
- **Hash via `builtins.hashString "sha256"`** — stable across processes and platforms, so a fingerprint computed at build time matches at eval time.
- **Binary-safe strings** — Nix I/O strings are arbitrary bytes minus NUL; only string *literals in source* are UTF-8-decoded. Raw bytes from `readFile` pass through `substring`, `stringLength`, and `sha256` untouched (verified on 2.34.7). The builder therefore refuses NUL, and every file byte is in `0x01`–`0xFF`.
- **One source-literal gotcha** — the lexer normalizes a raw `0x0D` byte in `.nix` *source* to `0x0A` (verified byte-for-byte). Raw-bytes data files are unaffected (`readFile` output is raw), but any table material emitted into Nix source must escape `0x0D` as `\r` — which is exactly how the generated decode table (below) is written.
- **No in-eval mutation** — the lookup builds one string (the value) from `substring` slices of the file; the index is walked by recursion over integer positions, never by re-reading the file.

## File format: NFK v3 (binary hash index)

Open addressing: one `sha256` per lookup, a 24-bit fingerprint, and linear probing over a power-of-two table at load ≤ 0.8. Numeric fields are **b254** bytes, 1–4 wide — per-field widths are chosen at build time and stored in the header (one byte per digit, `byte = digit + 1`, big-endian digits) — so every file byte is `0x01`–`0xFF` and the file never contains the one byte Nix's `readFile` rejects.

```
offset 0              header, 64 bytes
offset 64             index region, M × W bytes (W = 9–10 per file)
offset 64 + M·W       data region (interleaved, variable)
```

**Header (64 bytes):**

| field      | offset | width | meaning                                          |
|------------|-------:|------:|--------------------------------------------------|
| magic      | 0      | 4     | `NFK3`                                           |
| `N`        | 4      | 3     | entry count (b254)                               |
| `M`        | 7      | 4     | table size, power of two (b254)                  |
| `keyTotal` | 11     | 3     | total key bytes (b254)                           |
| `valTotal` | 14     | 3     | total value bytes (b254)                         |
| revision   | 17     | 1     | `2` (0x32) — parameterized field widths          |
| `fpW`      | 18     | 1     | width of `fp` in b254 bytes (1–4)                |
| `koffW`    | 19     | 1     | width of `keyOff` (1–4)                          |
| `klenW`    | 20     | 1     | width of `keyLen` (1–3)                          |
| `vlenW`    | 21     | 1     | width of `valLen` (1–3)                          |
| —          | 22     | 42    | reserved (spaces)                                |

**Index region** — one `W`-byte entry per table slot `s` at offset `64 + W·s` (`W = fpW + koffW + klenW + vlenW`; 9–10 in current builds):

| field    | offset | width | meaning                                             |
|----------|-------:|------:|-----------------------------------------------------|
| `fp`     | 0                    | 1–4   | `int(sha256(key) hex [0:6], 16) + 1` (24-bit); 0 = unused |
| `keyOff` | `fpW`                | 1–4   | absolute file offset of the key                     |
| `keyLen` | `fpW`+`koffW`        | 1–3   | key length                                          |
| `valLen` | `fpW`+`koffW`+`klenW`| 1–3   | value length (the value is at `keyOff + keyLen`)    |

An unused slot is `W` bytes of `0x01` (all fields zero); the probe decodes the full `fp` field (`fpW` b254 bytes) at each step, so a miss is a decoded-fingerprint compare against 0 (used slots hold `fp = int + 1`, never 0). The data region interleaves key and value bytes in JSON insertion order, so only the key offset is stored per entry.

**Decode table — static, not in the file.** The b254 alphabet (255 bytes `0x01`–`0xFF` → digits 0–254) is a format constant shared by every NFK v3 file, so it is stored in exactly one place: `nfd3-table.nix`, a 255-entry attrset (`byte 1-char string → digit`) generated by

```sh
python3 build_db3.py --write-table nfd3-table.nix
```

`kv3.nix` imports it once per eval (the Nix import cache makes repeat imports free), so the table costs nothing per lookup and no per-file 255 bytes. The file is generated, not hand-edited; it intentionally contains raw non-UTF-8 bytes (Nix accepts them in string literals) with only the four literal-breaking bytes escaped (`0x0A → \n`, `0x0D → \r`, `0x22 → \"`, `0x5C → \\`).

Invariants:

- `s0 = int(h[56:64], 16) AND (M − 1)`; linear probing, bounded by `M` steps. A fingerprint hit is confirmed by a byte-for-byte key compare, so a 24-bit collision (≈ 0.012 expected false fingerprint matches per lookup at 200k keys; ≈ 1,190 colliding key pairs total) costs one extra key read — never a wrong value.
- `M = next_pow2(max(16, ⌈1.25·N⌉))` → load ≤ 0.8 (fixed; no factor flag).
- b254 width limits (builder-enforced): `N` / `keyTotal` / `valTotal` / key length / value length < 254³ (~16.4 MB); `M` and offsets < 254⁴ (~4.16 GB); no NUL.
- Sizes: 1,005 keys → 78,990 B; 50,000 → 3,653,126 B; 200,000 → 14,700,716 B = 1.05× the 13.9 MB JSON (the index region is ~16% of the large file).
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
    e    = 64 + W * (s + i) AND (M - 1)  # W from the header; (bitAnd wrap, M a power of two)
    efp  = b254-decode(entry[e .. e+fpW]) # fpW static-table lookups
    if efp = 0: return null              # unused slot: key absent
    if efp ≠ fp: continue                # fingerprint miss: probe on
    k    = substring(raw, keyOff, keyLen)
    if k = key: return substring(raw, keyOff + keyLen, valLen)
  unreachable (load < 1 guarantees an unused slot)
```

- Per probe step: `fpW` (4) table lookups for the fingerprint via the static decode attrset, then `koffW + klenW + vlenW` (5–6) more for the offsets/lengths on a fingerprint hit, one `substring` key read, one string compare.
- One `sha256` per lookup; a successful walk averages ½(1 + 1/(1−α)) probe steps — ≈ 1.5 at load 0.49, ≈ 2.6 at load 0.76, 3 at the 0.8 cap.
- The fingerprint is compared as an int and every hit is confirmed byte-for-byte, so the 24-bit fingerprint can only add a key read, never a wrong value.

## Nix-side workarounds

- **No `builtins.parseInt`** — every b254 field decodes one byte at a time through the static `nfd3-table.nix` attrset via one width-specialized thunk per width (1–4 lookups per field, width from the header); the 24-bit fingerprint and the probe seed fold the 6/8 hex chars of the digest through a 16-entry inline table (no `%`: just `* 16 +`; the fingerprint adds 1, the seed is `bitAnd`-masked to `M − 1`).
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
- `--check` re-parses the file(s) independently — validates magic, revision byte, field widths, reserved header spaces, absence of NUL, and the exact file size — and re-probes every key through an independent Python probe plus a known miss. In sharded mode, shard membership is re-derived and every key is re-probed through the shard files.
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

One cold `nix eval` per point (median of 7; the n = 200 row is min-of-7), all methods in the same
session; harness in `bench.py`, raw results in `bench_results.json`. The
harness first measures the **Nix load floor** — a cold empty eval with the
same flags (`nix eval --impure --raw --expr '""'`, no expression work:
process spawn + runtime/evaluator init + output), 33.6 ms median this run (31.9–34.8) —
and then reports each method's **total** and **work = total − floor**
(paired per run). Cells show `total (work) ms`; the × multiplier is on work (fromJSON work ÷ method work), so the ~34 ms startup floor drops out of both. The n=0 point for `fromJSON`
on the 200k table additionally forces all key names (a count), so it
overstates plain parse cost; n=0 for the multiverse tables reads one field.

**Parent, large (200,000 keys; 13.9 MB JSON / 14.7 MB `.nfd3`; 256 shards of 50–72 KB):**

| lookups/eval | fromJSON | nfk3 | nfk3s |
|---:|---:|---:|---:|
| 0 | 266.2 (233.5) | 59.8 (25.9) | — |
| 1 | 212.7 (179.4) | 59.8 (25.9) (6.9×) | **33.6 (0.6)** (299×) |
| 5 | 212.5 (179.4) | 58.9 (24.8) (7.2×) | 34.2 (0.9) (199×) |
| 10 | 212.8 (179.2) | 59.0 (25.4) (7.1×) | 34.8 (2.5) (72×) |
| 30 | 213.7 (178.9) | 60.6 (27.4) (6.5×) | 37.1 (4.1) (44×) |
| 100 | 212.3 (178.2) | 61.6 (29.7) (6.0×) | 43.4 (10.6) (17×) |
| 200 | 208.7 (174.3) | 59.0 (27.1) (6.4×) | 54.2 (22.3) (7.8×) |

Single cold lookup by dataset size, median of 7 runs, `total (work)` ms
(fromJSON / nfk3; multipliers on work): 1k keys 34.9 (0.7) / 34.7 (1.1) —
parity, both startup-bound; 50k 80.1 (46.3) / 41.1 (7.8), 5.9×; 200k
212.7 (179.4) / 59.8 (25.9), 6.9×, vs 33.6 (0.6) ms sharded, 299×
(1k/50k: `bench_marginal.json`; 200k: `bench_results.json`).

**Multiverse (31,904 attrs each; multipliers on work):** versions 4.8 MB
JSON / 5.4 MB `.nfd3` — fromJSON 157–161 ms total (work 123–127) vs
nfk3 43–47 ms (10–13×) and nfk3s 33.5–52 ms (6.8–176×); history 6.9 MB
JSON / 7.4 MB `.nfd3` — fromJSON 253–266 ms total (work 217–230) vs
nfk3 46–56 ms (11–18×) and nfk3s 34–45 ms (23–251×). Full per-point tables
in [REPORT.md](REPORT.md) and
[`multiverse-faster/README.md`](multiverse-faster/README.md).

**Reading the numbers:**

- **The intercept is the game, and the split shows what it is.** `fromJSON`
  is flat (work ~174–180 ms on 200k, ~123–127 ms versions / ~217–230 ms
  history) because it must parse the whole file regardless of how many keys
  are asked for; its per-lookup cost after the parse is negligible.
- **Sharding wins the low-query regime.** nfk3s pays one small-shard
  readFile per *new* shard (~0.1–0.2 ms, import-cached for the eval)
  instead of a 14.7 MB readFile: at 1 lookup its **work is ~0.6 ms**
  (multiverse: ≈0–1 ms) — the ~34 ms total sits at or below the measured
  33.6 ms Nix load floor (31.9–34.8) — ~300× the data work of `fromJSON`
  on 200k keys (0.6 vs 179.4 ms).
- **Crossover with single-file nfk3:** on the 5.4–7.4 MB multiverse tables
  nfk3s is ahead through n = 100 (versions: 44.6 vs 47.2 ms); at
  n = 200 single-file takes over on versions (46.6 vs 51.7 on the min row,
  49.6 vs 53.9 median) and the crossover sits at the top of the range on
  history (min row 44.7 vs 51.3 favors sharded; 55.0 vs 52.7 median favors
  single-file); on the 14.7 MB 200k-key table nfk3s is ahead across the
  whole measured range (work 22.3 vs 27.1 ms at n = 200).
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
| `bench.py`, `bench_results.json` | 3-method cold-eval benchmark (fromJSON / nfk3 / nfk3s on the 200k table, 7 runs per point) + raw results |
| `REPORT.md` | full design + benchmark + trade-off write-up |
| `multiverse-faster/` | real-world workload: fkzakaria's nixpkgs-multiverse index (31,904 attrs) converted to NFK v3, 3-method cold-eval benchmark (single file and 256 shards), oracle, harness, and its own README |
| `suggestions.md` | NFK v3 improvement ideas and what was rejected, 2026-08-20 |

## Known limitations

- **`builtins.fromJSON` still wins for bulk scans** — if an evaluation touches most of the table, parsing once and indexing the attrset beats per-lookup file slicing. NFK v3 targets the common case: one or a few lookups per eval.
- **Nix's string model caps the alphabet at 254** — Nix strings cannot contain NUL, so the numeric fields stop at 254-valued digits (b254: digits 0–253 in bytes `0x01`–`0xFF`). The index region is not human-diffable (the data region is raw UTF-8); a future Nix with `builtins.parseInt` could shrink the index further, and raw-bytes support would lift the NUL limit.
- **The decode table must exist where `kv3.nix` sits** — it is imported by path relative to `kv3.nix`; if you copy the module elsewhere, regenerate/copy `nfd3-table.nix` alongside it (`build_db3.py --write-table`). The table is a deterministic function of the format, so there is exactly one correct content.
- **sha256 is the only stable hash available** — `hashString`'s other modes are not stable across Nix versions/platforms in the same documented way; sha256 is ~3× slower than the alternatives but the cost is one hash per lookup, not per entry.
- **A probe walk of up to M empty slots in the worst case** — bounded but not O(1); the expected successful-search chain is ½(1 + 1/(1−α)) slots: 3 at the 0.8 load cap, 2.6 at the 200k table (load 0.76), 1.5 at the multiverse tables (load 0.49).
- **Fixed table size** — the table is sized for the input at build time; growing it requires a rebuild (cheap: < 2 s for 200k keys; sharded rebuilds are parallelizable per shard).
- **File size ≈ 1.05× the JSON** (200k keys) — the 9-byte hash index is ~16% of the file; in exchange a single-lookup cold eval reads and decodes only a few hundred bytes of the 14.7 MB file.