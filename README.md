# fast-nix-lookup

Fast key/value lookup for native Nix eval. Instead of `builtins.fromJSON` + attrset access (which parses the whole file on every `nix eval`), the table is precomputed into an **nkv** file that Nix can probe with byte reads, string slicing, and `hashString` — no parse.

```nix
let db = (import ./nkv.nix) ./data/large.nkv;
in db.get "pkgs484.env795.nix877.pkgs793"
# -> chde4cf665ukuewyy-tx
```

For very large tables, or evals that do only a few lookups, the same format can be sharded so a lookup reads one small shard file instead of the whole table (`nkv.nix`):

```nix
let db = import ./nkv.nix { digits = 2; dir = ./data/large_shards; };
in db.get "pkgs484.env795.nix877.pkgs793"   # only the key's 1-of-256 shard is read
```

A single cold lookup (N = 1) costs ~34 ms sharded / ~57 ms single-file nkv
vs ~210 ms for `builtins.fromJSON`. On the data work — total minus the 33.0 ms
`nix eval` startup floor both sides pay (measured 23.2–38.7 for an empty eval) —
that is ≈146× sharded / 7.6× single-file: the ~34 ms sharded total sits at the
startup floor, and its data work is ~1.2 ms (one ~46–62 KB shard read + one
probe) against `fromJSON`'s ~175 ms `readFile`+parse of the 13.9 MB file. nkv
beats `fromJSON` on the data work at every measured query count (1–200 lookups/eval)
on every table measured; on the 31,904-attr nixpkgs-multiverse workload the
sharded variant is fastest through ~100 lookups and single-file takes over at
200. Details in REPORT.md and multiverse-faster/.

## Why not `builtins.fromJSON`?

A single lookup in the JSON version looks like:

```nix
( builtins.fromJSON (builtins.readFile "./large.json") )."some.key"
```

`builtins.fromJSON` costs ~207–214 ms wall per eval on the 200k-key table —
~173–181 ms of `readFile`+parse work on top of the ~33 ms `nix eval` floor —
and the cost is essentially flat from 1 to 200 lookups per eval: the JSON
parse is the whole bill and lookup adds nothing measurable. nkv is
7.6× faster single-file and ≈146× with 256 shards on the data work (N = 1), and
stays ahead of `fromJSON` at every measured N (up to 200 lookups/eval) on every
table. Single-lookup cost scales with
file size — measured 33–57 ms wall per eval (work 0.0–22.9 ms: 0.0–3.1 ms
sharded, 12.4–22.9 ms single-file) — and with shard count at higher N, since each distinct
shard file is read once per eval. The one regime where `fromJSON` wins is a
whole-table scan: 31,904 lookups with all values serialized take 0.38 s
`fromJSON` vs 0.89 s / 2.49 s nkv single / sharded — one parse amortized
over 31,904 attrset lookups beats 31,904 sha256+probe+decode cycles.

## Design constraints (this Nix)

The target is Nix 2.34.7 with a stripped-down builtin set. What is missing shapes the format:

- **No `builtins.parseInt` (and no `%`, no `builtins.mod`, no `builtins.hasSuffix`, no `or`)** — integer arithmetic is limited to `/` (truncating division), `builtins.div`, `builtins.bitAnd`; the design works around it with fixed-width fields decoded by per-byte table lookups (one width-specialized thunk per file) and power-of-two tables masked with `bitAnd`.
- **Hash via `builtins.hashString "sha256"`** — stable across processes and platforms, so the probe slot and shard a key hashes to at build time match at eval time.
- **Binary-safe strings** — Nix I/O strings are arbitrary bytes minus NUL; only string *literals in source* are UTF-8-decoded. Raw bytes from `readFile` pass through `substring`, `stringLength`, and `sha256` untouched (verified on 2.34.7). The builder therefore refuses NUL, and no file byte is `0x00` — the one byte Nix's `readFile` rejects (base-255 digits occupy `0x01`–`0xFE`; data bytes are raw UTF-8).
- **One source-literal gotcha** — the lexer normalizes a raw `0x0D` byte in `.nix` *source* to `0x0A` (verified byte-for-byte). Raw-bytes data files are unaffected (`readFile` output is raw), but any table material emitted into Nix source must escape `0x0D` as `\r` — which is exactly how the generated decode table (below) is written.
- **No in-eval mutation** — the lookup builds one string (the value) from `substring` slices of the file; the index is walked by recursion over integer positions, never by re-reading the file.

## File format: nkv (binary hash index)

Open addressing: one `sha256` per lookup and linear probing over a power-of-two table at load ≤ 0.8, where every occupied slot is confirmed by a byte-for-byte key compare (revision 6 dropped the rev-5 24-bit fingerprint — the key compare is the only confirmation). Numeric fields are **base-255** bytes, 1–4 wide — per-field widths are chosen at build time and stored in the header (one byte per digit, `byte = digit + 1`, big-endian digits) — so no file byte is `0x00`, the one byte Nix's `readFile` rejects.

```
offset 0              header, 16 bytes
offset 16             index region, M × EW bytes (EW = koffW + klenW + vlenW, 3–10;
                      5/6 in the shipped single tables, 4/5 in the 256-shard builds)
offset 16 + M·EW      data region (interleaved, variable)
```

**Header (16 bytes):**

| field      | offset | width | meaning |
|------------|-------:|------:|---------|
| magic      | 0      | 4     | `NKV3` |
| `N`        | 4      | 3     | entry count (base-255) |
| `M`        | 7      | 4     | table size, power of two (base-255) |
| revision   | 11     | 1     | `6` (0x36) — no data-region totals |
| reserved   | 12     | 1     | `0x01` (base-255 digit 0) — rev-5's `fpW` width byte, kept as a reserved slot |
| `koffW`    | 13     | 1     | width of `keyOff` (1–4) |
| `klenW`    | 14     | 1     | width of `keyLen` (1–3) |
| `vlenW`    | 15     | 1     | width of `valLen` (1–3) |

**Index region** — one EW-byte entry per table slot `s` at offset `16 + EW·s` (`EW = koffW + klenW + vlenW`, 3–10; 5 for the parent tables — `keyOff` 3 + `keyLen` 1 + `valLen` 1, 6 for the multiverse single tables whose largest value length needs 2 base-255 digits, 4/5 for the 256-shard builds whose shard-local offsets fit in 2 digits):

| field    | offset | width | meaning |
|----------|-------:|------:|---------|
| `keyOff` | 0                | 1–4   | absolute file offset of the key; `0` = unused slot |
| `keyLen` | `koffW`          | 1–3   | key length |
| `valLen` | `koffW`+`klenW`  | 1–3   | value length (the value is at `keyOff + keyLen`) |

An unused slot is EW bytes of `0x01` (all fields zero); the walk ends at a decoded `keyOff` of 0 (a real key offset is always ≥ `16 + EW·M`). Every occupied slot is read and byte-compared against the key, so a mismatch costs one extra key read and a wrong value is impossible by construction. The data region interleaves key and value bytes in JSON insertion order, so only the key offset is stored per entry — the value offset is implicit.

**Decode table — static, not in the file.** The base-255 alphabet (255 bytes `0x01`–`0xFF` → digits 0–254) is a format constant shared by every nkv file, so it is stored in exactly one place: `nkv-table.nix`, a 255-entry attrset (`byte 1-char string → digit`) generated by

```sh
python3 build_db3.py --write-table nkv-table.nix
```

`nkv.nix` imports it once per eval (the Nix import cache makes repeat imports free), so the table costs nothing per lookup and no per-file 255 bytes. The file is generated, not hand-edited; it intentionally contains raw non-UTF-8 bytes (Nix accepts them in string literals) with only the four literal-breaking bytes escaped (`0x0A → \n`, `0x0D → \r`, `0x22 → \"`, `0x5C → \\`).

Invariants:

- `s0 = int(h[56:64], 16) AND (M − 1)`; linear probing, bounded by `M` steps. Every occupied slot in the walk is read and key-compared, so the probe can only add key reads — it never returns a wrong value.
- `M = next_pow2(max(16, ⌈1.25·N⌉))` → load ≤ 0.8 (fixed; no factor flag).
- base-255 width limits (builder-enforced): `N` / key length / value length < 254³ (~16.4 MB); `M` and offsets < 254⁴ (~4.16 GB); no NUL.
- Sizes: 1,005 keys → 70,750 B; 50,000 → 3,390,934 B; 200,000 → 13,652,092 B = 0.98× the 13.9 MB JSON (the index region is ~10% of the medium/large files; 14% of small, where the 10,240-byte index at M = 2,048 dominates).
- Values are opaque: any UTF-8 minus NUL may be stored. String values are returned as-is by `get`; when a value holds a JSON document, `getJson`/`getOrJson` decode it with `builtins.fromJSON` at lookup time (a miss is still `null`).

### Optional file sharding

For very large tables, or evals that do only a few lookups, `build_db3.py` can split the table into a directory of independent nkv files, one per slice of the key hash:

```sh
python3 build_db3.py INPUT.json --shards 256 --prefix sharded/ --check
```

- Shard of key `k` = `sharded/<h[24:24+d]>.nkv`, where `h` is the lowercase hex of `sha256(k)` and `d` is the number of digits: `--shards 16/256/4096` → `d = 1/2/3`.
- **Every shard file is always written** — an empty shard is a valid nkv file with `N = 0`, `M = 16` — so a key always resolves to an existing file.
- The slice `[24:24+d)` is disjoint from the probe-seed slice `[56:64)` the probing algorithm uses, so sharding does not perturb probe distribution; each shard is a standalone nkv table with its own `M`.
- Reader: `nkv.nix` — per lookup only the shard the key hashes to is read, and Nix's import cache keeps it for the rest of the eval. The static decode table is shared automatically: `nkv.nix`'s single `import ./nkv-table.nix` is evaluated once per process no matter how many shard files are imported.
- `--check` in sharded mode re-derives shard membership from the input keys and re-probes every key through the shard files.
- Measured shard sizes (256 shards): 46,025–62,052 B each on the 200k-key table (EW 4 — shard-local `keyOff` fits in 2 base-255 digits); 10,932–34,558 B and 15,294–50,096 B on the multiverse versions/history tables (EW 5).

## Lookup algorithm

```
lookup(key):
  h  = sha256(key) in lowercase hex
  s  = int(h[56:64], 16) AND (M - 1)    # initial slot
  for i in 0..M:                        # bounded walk
    e    = 16 + EW * ((s + i) AND (M - 1))  # EW from the header (bitAnd wrap, M a power of two)
    koff = base-255-decode(entry[e .. e+koffW]) # koffW static-table lookups
    if koff = 0: return null             # unused slot: key absent
    k    = substring(raw, koff, klen)
    if k = key: return substring(raw, koff + klen, vlen)
  unreachable (load < 1 guarantees an unused slot)
```

- Per probe step: `koffW + klenW + vlenW` static-table lookups for the field offsets/lengths (widths from the header, one width-specialized thunk per width), one `substring` key read, and one string compare — at **every** occupied slot; there is no fingerprint to short-circuit a mismatch.
- One `sha256` per lookup; a successful walk averages ½(1 + 1/(1−α)) probe steps — ≈ 1.5 at load 0.49, ≈ 2.6 at load 0.76, 3 at the 0.8 cap.
- Sharded: `sha256(key)[24:26]` selects the shard file first (one lazy import, cached for the eval), then the same probe runs inside that shard.

## Nix-side workarounds

- **No `builtins.parseInt`** — every base-255 field decodes one byte at a time through the static `nkv-table.nix` attrset via one width-specialized thunk per width (1–4 lookups per field, width from the header); the probe seed folds the 8 hex chars of the digest through a 16-entry inline table (no `%`: just `* 16 +`, then `bitAnd`-masked to `M − 1`).
- **No `%`** — probe wrap is `builtins.bitAnd (s + i) (M - 1)` (M is a power of two); the midpoint-free design (hash probing, no binary search) means no other modulo is needed.
- **No `or`** — the modules use `if x == null then a else b` and plain booleans throughout.
- **No mutation** — everything is a pure `let`/recursion over the file string; the only "state" is integer positions.
- **Binary-safe base-255 decoding** — raw `readFile` bytes pass through `substring` untouched; the decode table (an attrset in source) is the only place where bytes meet the lexer, and it escapes the four literal-breaking bytes (see the format section).
- **List-literal parse quirk (Nix 2.34.7)** — `[db.get "key"]` inside a list literal parses as a two-element list `[<fun>, <str>]` (a dotted function call followed by its argument); the bench harness and tests build lookup lists via `builtins.map` / parenthesized application instead.

Header fields and the table import happen once at import time; each lookup then costs one `hashString`, one 8-char hex fold, and a few `substring`s per probe step.

## Builder

`build_db3.py` reads a JSON object and emits nkv files (single file or sharded), with an independent re-parser and `--check`:

```sh
python3 build_db3.py INPUT.json OUTPUT.nkv [--check]
python3 build_db3.py INPUT.json --shards {16,256,4096} --prefix DIR/ [--check]
python3 build_db3.py --write-table nkv-table.nix
```

- Input: a JSON object with string keys and **arbitrary JSON values** — string values are stored raw; non-string values are stored as compact JSON documents, which `getJson`/`getOrJson` decode back.
- Sharded mode writes `DIR/<h[24:24+d]>.nkv` for every shard, including empty ones (see the sharding subsection); single-file mode is unchanged.
- `--check` re-parses the file(s) independently — validates magic, revision byte, field widths, reserved header spaces, absence of NUL — and re-probes every key through an independent Python probe plus a known miss. In sharded mode, shard membership is re-derived and every key is re-probed through the shard files.
- Width guards: `N` / key length / value length < 254³ bytes, `M` / offsets < 254⁴, no NUL.
- Single-file output is byte-identical across rebuilds (no timestamps, insertion-order data region).

`gen_data.py` generates the deterministic test datasets (1k / 50k / 200k keys, seeded RNG).

## Usage

Nix 2.34.7+ (uses `hashString` and byte-safe `substring`; no `builtins.parseInt`).

```nix
let db = (import ./nkv.nix) ./data/large.nkv;
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.count; db.tableSize }
```

`get` returns `null` on a miss. `db.getJson "k"` / `db.getOrJson "k" default` return `builtins.fromJSON` of the stored string when the value holds a JSON document (a miss still returns `null`). The module asserts the `NKV3` magic, the revision byte, and the field widths at import.

Sharded nkv (`nkv.nix`) takes a directory built with `--shards` (`digits` must match the shard count):

```nix
let db = import ./nkv.nix { digits = 2; dir = ./data/large_shards; };
in { db.get "k"; db.getOr "k" "d"; db.has "k"; db.getJson "k"; db.count }
```

Importing the module is lazy — no shard file is read until a lookup is forced. Per lookup only the key's shard is read. `db.count` imports every shard file — offline / inspection use only.

## Correctness

Each dataset is cross-checked against the `fromJSON` oracle in `test_correctness3.nix` (every key, plus a known miss, plus `count`/`tableSize`):

```sh
python3 gen_data.py
python3 build_db3.py --write-table nkv-table.nix
for s in small medium large; do
  python3 build_db3.py data/$s.json data/$s.nkv --check
  nix eval --impure --json --expr '(import ./test_correctness3.nix) "$s"'
done
```

Expected: `ok = true`, `mismatchCount = 0` on all three datasets (251,005 oracle lookups total). The builder's own `--check` independently re-probes every key at build time.

Sharded correctness: `python3 build_db3.py data/large.json --shards 256 --prefix data/large_shards/ --check` re-derives shard membership and re-probes all 200,000 keys through the shard files (0 mismatches, miss → `None`). The multiverse sharded indexes (256 shards each) are checked against `fromJSON` in `multiverse-faster/test_correctness_shards.nix`: `mismatches = 0`, misses → `null`, `count = 31904` for both datasets.

## Performance summary

One cold `nix eval` per point (median of 7; the n = 200 row is min-of-7, `work`
paired at the min-total run), all methods in the same session; harness in
`bench.py`, raw results in `bench_results.json`. The harness first measures the
**Nix load floor** — a cold empty eval with the same flags
(`nix eval --impure --raw --expr '""'`, no expression work: process spawn +
runtime/evaluator init + output), 33.0 ms median this run (23.2–38.7) — and
then reports each method's **total** and **work = total − floor** (paired per
run). Cells show `total (work) ms`; the × multiplier is on work (fromJSON work
÷ method work), so the ~33 ms startup floor drops out of both. The n=0 point
for `fromJSON` on the 200k table additionally forces all key names (a count),
so it overstates plain parse cost; n=0 for the multiverse tables reads one
field.

**Parent, large (200,000 keys; 13.9 MB JSON / 13.7 MB `.nkv`; 256 shards of 46–62 KB):**

| lookups/eval | fromJSON | nkv | nkvs |
|---:|---:|---:|---:|
| 0 | 259.8 (225.0) | 58.6 (25.7) | — (no load point) |
| 1 | 210.0 (174.7) | 56.7 (22.9) (7.6×) | **34.3 (1.2)** (≈146×) |
| 5 | 208.1 (175.2) | 58.0 (25.0) (7.0×) | 34.9 (1.3) (≈135×) |
| 10 | 213.5 (181.4) | 59.4 (26.0) (7.0×) | 35.1 (1.4) (≈130×) |
| 30 | 212.4 (178.0) | 57.7 (24.8) (7.2×) | 34.5 (2.8) (≈64×) |
| 100 | 210.4 (177.7) | 59.1 (25.0) (7.1×) | 43.1 (9.8) (≈18×) |
| 200 | 207.0 (173.3) | 59.3 (26.0) (6.7×) | 46.2 (7.5) (≈23×) |

Output sizes per point: 6 / 66 / 195 / 502 / 1,359 / 4,285 / 8,656 B.

Single cold lookup by dataset size, median of 7 runs, `total (work)` ms
(fromJSON / nkv; multipliers on work): 1k keys 34.9 (0.1) / 34.3 (1.1) —
parity, both startup-bound; 50k 80.2 (45.9) / 39.7 (4.9), 9.4×; 200k
210.0 (174.7) / 56.7 (22.9), 7.6×, vs 34.3 (1.2) ms sharded, ≈146×
(1k/50k: `bench_marginal.json`; 200k: `bench_results.json`).

**Multiverse (31,904 attrs each; multipliers on work; `fromJSON` parses the
nested `index/*.json`):** versions 5.5 MB JSON / 5.1 MB `.nkv` — fromJSON
155–158 ms total (work 121–127) vs nkv 43–46 ms (8–12×) and nkvs 35–51 ms
(7–42×); history 7.8 MB JSON / 7.1 MB `.nkv` — fromJSON 253–259 ms total
(work 221–228) vs nkv 45–51 ms (11–18×) and nkvs 33–54 ms (21–149× at
N ≤ 100; 7.3–11.5× at N = 200; at N = 1 the sharded work rounds to 0.0 ms).
Full per-point tables in
[REPORT.md](REPORT.md) and
[`multiverse-faster/README.md`](multiverse-faster/README.md).

**Reading the numbers:**

- **The intercept is the game, and the split shows what it is.** `fromJSON`
  is flat (work ~173–181 ms on 200k, ~121–127 ms versions / ~221–228 ms
  history) because it must parse the whole file regardless of how many keys
  are asked for; its per-lookup cost after the parse is negligible.
- **Sharding wins the low-query regime.** nkvs pays one small-shard
  readFile per *new* shard (~0.1–0.2 ms, import-cached for the eval)
  instead of a 13.7 MB readFile: at 1 lookup its **work is ~1.2 ms**
  (multiverse: ≈0–3 ms) — the ~34 ms total sits at the measured 33.0 ms Nix
  load floor (23.2–38.7) — ≈146× the data work of `fromJSON` on 200k keys
  (1.2 vs 174.7 ms).
- **Crossover with single-file nkv:** on the 5.1–7.1 MB multiverse tables
  nkvs is ahead through n = 100 (versions: 42.5 vs 46.1 ms total; work 10.1
  vs 15.2; history: 44.0 vs 49.3, work 10.6 vs 19.5); at n = 200
  single-file takes over (versions min row 44.2 vs 50.1; work 11.1 vs 16.5;
  history 49.7 vs 51.7, work 16.2 vs 19.2) — crossover ~100–200 lookups/eval.
  On the 13.7 MB 200k-key table nkvs is ahead across the whole measured
  range (n = 200: 46.2 vs 59.3 min row, work 7.5 vs 26.0; 52.9 vs 63.8
  median, work 19.5 vs 32.2).
- **Bulk scans tip back to `fromJSON`** — if an eval touches most of the
  table, parsing once and indexing the attrset beats per-lookup file
  slicing: the measured 31,904-lookup scan (all values serialized) takes
  0.38 s fromJSON vs 0.89 / 2.49 s nkv / nkvs.

## Repo layout

| path | role |
|---|---|
| `nkv.nix` | nkv lookup module: `db` is a path (or string path) to one `.nkv` file; imports the static decode table once per eval |
| `nkv.nix` | sharded nkv reader: takes a `--shards` directory + `digits`, reads only the key's hash shard (lazy; `count` reads all shards — offline use) |
| `nkv-table.nix` | the 255-entry base-255 decode table (static format constant; generated, not hand-edited) |
| `build_db3.py` | JSON → nkv builder (single file or `--shards/--prefix`) with independent parser + `--check`; `--write-table` regenerates `nkv-table.nix` |
| `gen_data.py` | deterministic test-data generator (1k / 50k / 200k keys) |
| `test_correctness3.nix` | `fromJSON`-oracle correctness test (every key + miss + count) |
| `data/` | `small|medium|large.{json,nkv}` (1k / 50k / 200k keys) + `large_shards/` (256-shard nkv of `large.json`) |
| `bench.py`, `bench_results.json`, `bench_marginal.py`, `bench_marginal.json` | 3-method cold-eval benchmark (fromJSON / nkv / nkvs on the 200k table, 7 runs per point) + marginal 1-lookup table + raw results |
| `REPORT.md` | full design + benchmark + trade-off write-up |
| `multiverse-faster/` | real-world workload: fkzakaria's nixpkgs-multiverse index (31,904 attrs) converted to nkv, 3-method cold-eval benchmark (single file and 256 shards), oracle, harness, and its own README |
| `suggestions.md` | nkv improvement ideas and what was rejected, 2026-08-20 |

## Known limitations

- **`builtins.fromJSON` still wins for bulk scans** — if an evaluation touches most of the table, parsing once and indexing the attrset beats per-lookup file slicing (measured: 31,904-lookup scan 0.38 s vs 0.89 / 2.49 s). nkv targets the common case: one or a few lookups per eval.
- **Nix's string model caps the alphabet at 254** — Nix strings cannot contain NUL, so the numeric fields stop at 254-valued digits (base-255: digits 0–253 in bytes `0x01`–`0xFE`). The index region is not human-diffable (the data region is raw UTF-8); a future Nix with `builtins.parseInt` could shrink the index further, and raw-bytes support would lift the NUL limit.
- **The decode table must exist where `nkv.nix` sits** — it is imported by path relative to `nkv.nix`; if you copy the module elsewhere, regenerate/copy `nkv-table.nix` alongside it (`build_db3.py --write-table`). The table is a deterministic function of the format, so there is exactly one correct content.
- **sha256 is the only stable hash available** — `hashString`'s other modes are not stable across Nix versions/platforms in the same documented way; sha256 is ~3× slower than the alternatives but the cost is one hash per lookup, not per entry.
- **A probe walk of up to M empty slots in the worst case** — bounded but not O(1); the expected successful-search chain is ½(1 + 1/(1−α)) slots: 3 at the 0.8 load cap, 2.6 at the 200k table (load 0.76), 1.5 at the multiverse tables (load 0.49). Every occupied slot in the walk is key-compared, so the extra reads never produce a wrong value.
- **Fixed table size** — the table is sized for the input at build time; growing it requires a rebuild (cheap: < 2 s for 200k keys; sharded rebuilds are parallelizable per shard).
- **File size ≈ 0.98× the JSON** (200k keys) — the EW-byte index is ~10% of the file; in exchange a single-lookup cold eval reads and decodes only a few hundred bytes of the 13.7 MB file.
