# fast-nix-lookup

Fast key/value lookup for static string→value tables in **pure Nix** (no `builtins.exec`, no foreign interpreters, no per-eval parse of the data). Instead of `builtins.fromJSON` + attrset access — which parses the whole file on every `nix eval` — the table is precomputed into an **nkv** file that Nix probes with byte reads (`substring`).

    let db = (import ./nkv.nix) ./data/large.nkv;
    in db.get "pkgs484.env795.nix877.pkgs793"
    # -> chde4cf665ukuewyy-tx                 (~57 ms cold total / ~23 ms work)

For very large tables, or evals that do only a few lookups, the same format can be sharded so a lookup reads one small shard file instead of the whole table (`nkv.nix`):

    let db = import ./nkv.nix { digits = 2; dir = ./data/large_shards; };
    in db.get "pkgs484.env795.nix877.pkgs793"   # only the key's 1-of-256 shard is read
    # -> chde4cf665ukuewyy-tx                 (~34 ms cold total / ~1.2 ms work)

A single cold lookup (N = 1) costs ~34 ms sharded / ~57 ms single-file nkv vs ~210 ms for `builtins.fromJSON`. On the data work — total minus the 33.0 ms `nix eval` startup floor both sides pay (measured 23.2–38.7 for an empty eval) — that is ≈146× sharded / 7.6× single-file: the ~34 ms sharded total sits at the startup floor, and its data work is ~1.2 ms (one ~46–62 KB shard read + one probe) against `fromJSON`'s ~175 ms `readFile`+parse of the 13.9 MB file. nkv beats `fromJSON` on the data work at every measured query count (1–200 lookups/eval) on every table measured; on the 31,904-attr nixpkgs-multiverse workload the sharded variant is fastest through ~100 lookups and single-file takes over at 200. Full per-point multiverse tables in [`multiverse-faster/README.md`](multiverse-faster/README.md).

## Why not `builtins.fromJSON`?

The naive implementation:

    ( builtins.fromJSON (builtins.readFile "./large.json") )."some.key"

parses the whole 13.9 MB file into a Nix attrset **on every lookup**: ~207–214 ms wall (173–181 ms data work) at 200k entries, and the cost is *flat* in the number of lookups in one eval — the parse happens once and attrset access is O(1), so 1 and 200 lookups cost the same (173.3–174.7 ms work). That is what this project replaces.

Measured at a single lookup (N = 1, data work in parentheses): `fromJSON` 33.5–57.3 ms wall (work 0.0–22.9), single-file nkv 34.3–59.7 ms (work 12.4–22.9), sharded nkv 34.4–58.1 ms (work 0.0–3.1) — at small tables the methods are within a few ms because all of it is Nix's own eval startup; the separation appears in the data work, and grows with table size (7.6× on the 200k table, 11–18× on the multiverse tables).

At high lookup counts the advantage is inverted: `fromJSON`'s parse-once/index-many model beats nkv's per-lookup readFile + decode for bulk scans (measured: 0.38 s vs 0.89 / 2.49 s at 31,904 lookups/eval on the nixpkgs-multiverse data — see Performance). nkv is for the common case of one or a few lookups per eval.

## Design constraints (this Nix)

- **No `builtins.parseInt`** in this Nix — integer math is done with `/`, `div`, `bitAnd`, and width-specialized decode thunks; probing uses power-of-two table sizes so the wrap is a `bitAnd` mask.
- `hashString` gives sha1/sha256; sha256 is the only one with stable hex across Nix versions/platforms, so probe slots and shard names match between build time (Python) and eval time (Nix).
- Nix strings are binary-safe *I/O* strings (arbitrary bytes minus NUL); only source literals are UTF-8-decoded, so arbitrary bytes can be stored and sliced.

## File format: nkv (binary hash index)

nkv stores N (key, value) pairs in an open-addressing, linear-probing table (load ≤ 0.8) over the `sha256(key)` slot. Probing reads the key length at every slot, so **every occupied slot on the walk is byte-compared against the target key** — the probe can only ever add key reads, never return a wrong value. Numeric fields use **base-255, 1–4-byte widths**, with per-field widths stored in the header (big-endian; `byte = digit + 1`, so digits 0–253 are stored in bytes `0x01`–`0xFE` and no `0x00` byte ever appears in a file).

    offset  0  | header (14 bytes; table params)
    offset 14  | index (M × EW bytes)
    offset 14 + M·EW | data region (keys and values, interleaved)

Each key/value pair is written as `key || value` (no separator — the lengths are in the index). The index stores per-slot field-width offsets and lengths in base-255 (1–4 bytes per field, `byte = digit + 1`); the value offset is implicit: value bytes follow the key bytes at `keyOff + keyLen`. `EW = koffW + klenW + vlenW` is the per-slot entry width (3–10 bytes; 5 for small/medium/large with `koffW = 3`, 4 for the large 256-shard build, 6 for the multiverse single-file tables with `vlenW = 2`).

| field | offset | width |
|---|---:|---:|
| magic `"NKV4"` | 0 | 4 |
| N (key count) | 4 | 3 |
| M (slot count) | 7 | 4 |
| koffW | 11 | 1 |
| klenW | 12 | 1 |
| vlenW | 13 | 1 |

### Index region (offset `14`)

| field | offset (within slot) | width |
|---|---:|---:|
| keyOff | 0 | koffW |
| keyLen | koffW | klenW |
| valLen | koffW + klenW | vlenW |

A `keyOff` of 0 marks an unused slot. The value bytes are at `keyOff + keyLen`. The slot is EW bytes wide, so `EW = koffW + klenW + vlenW`.

An unused slot is EW bytes of `0x01` (the base-255 encoding of 0); `keyOff = 0` is how the probe walk terminates, and any real keyOff is always ≥ 14 + EW·M.

The base-255 decode table is a **static file** — `nkv-table.nix`, a 255-entry attrset checked in next to `nkv.nix` — imported once per eval and shared by both single-file and sharded lookups. It is a pure function of the format (digit `d` → byte `d + 1`, with non-UTF-8 bytes and Nix string-literal breakers escaped), so the checked-in content is always the correct one.

Invariants:

- `s0 = int(h[56:64], 16) AND (M − 1)`; linear probing, bounded by `M` steps. Every occupied slot in the walk is read and key-compared, so the probe can only add key reads — it never returns a wrong value.
- `M = next_pow2(max(16, ⌈1.25·N⌉))` → load ≤ 0.8 (fixed; no factor flag).
- base-255 width limits (builder-enforced): `N` / key length / value length < 255³ (~16.6 MB); `M` and offsets < 255⁴ (~4.23 GB); no NUL.
- Values are opaque: any UTF-8 minus NUL may be stored. String values are returned as-is by `get`; when a value holds a JSON document, `getJson`/`getOrJson` decode it with `builtins.fromJSON` at lookup time (a miss is still `null`).

Measured sizes (flat JSON → nkv, with the static table):

| dataset | keys | flat JSON | nkv | index | ratio |
|---|---:|---:|---:|---:|---:|
| small | 1,005 | 68,534 | 70,748 | 10,240 | 1.03× |
| medium | 50,000 | 3,463,238 | 3,390,932 | 327,680 | 0.98× |
| large | 200,000 | 13,941,356 | 13,652,090 | 1,310,720 | 0.98× |
| multiverse versions | 31,904 | 4,833,362 | 5,098,975 | 393,216 | 1.06× |
| multiverse history | 31,904 | 6,876,612 | 7,142,225 | 393,216 | 1.04× |

Multiverse rows are the flat JSON the builder consumes; `fromJSON` parses the nested `index/*.json` instead (5.48 / 7.84 MB). The index is ~9.6% of the medium/large files (14.5% of small, where the M = 2,048 table is proportionally large; 7.7% / 5.5% of the multiverse singles).

### Optional file sharding

Sharding splits the key space by a sha256 hex slice and writes one nkv file per shard: `python3 build_nkv.py INPUT.json --shards 256 --prefix sharded/ --check`. A key lands in `sharded/<h[24:24+d]>.nkv` where `d = 1/2/3` for 16/256/4096 shards.

- **Every shard file is always written** — including empty ones (valid table, N = 0, M = 16), so `readFile` and the index walk behave identically for empty and non-empty shards.
- The slice `[24:24+d)` is disjoint from the probe-seed slice `[56:64)` the probing algorithm uses, so sharding does not perturb probe distribution; each shard is a standalone nkv table with its own `M = next_pow2(max(16, ⌈1.25·N_shard⌉))`.
- The reader (`nkv.nix`) takes `digits` + `dir`, computes the shard name, and `import`s it — lazily, and Nix's import cache makes repeated imports of the same shard within one eval free — so a single lookup touches exactly one shard file.
- `--check` re-derives shard membership and re-probes every key through the shard files.

Measured shard sizes (256-shard build, EW 4): 46,025–62,052 B on the 200k table (vs 13.65 MB single file); 10,932–34,558 B and 15,294–50,096 B on the multiverse versions/history shards (EW 5).

## Lookup algorithm

    lookup(key):
      h  = sha256(key) in lowercase hex
      s  = int(h[56:64], 16) AND (M - 1)    # initial slot
      for i in 0..M:                        # bounded walk
        e    = 14 + EW * ((s + i) AND (M - 1))  # EW from the header (bitAnd wrap, M a power of two)
        koff = base-255-decode(entry[e .. e+koffW])           # koffW static-table lookups
        if koff = 0: return null             # unused slot: key absent
        klen = base-255-decode(entry[e+koffW .. e+koffW+klenW])
        k    = substring(raw, koff, klen)
        if k = key:
          vlen = base-255-decode(entry[e+koffW+klenW .. e+EW])
          return substring(raw, koff + klen, vlen)
      unreachable (load < 1 guarantees an unused slot)

- No `parseInt`: base-255 decoding is per-byte static-table lookups (digit = byte − 1) and the walk uses `bitAnd` masks with power-of-two `M`.
- In the sharded layout, `sha256(key)[24:26]` selects the shard file first (one lazy `import`, cached within the eval); the same probe runs inside it.

## Nix-side workarounds

- **Hex fold** — a 16-entry inline table folds 8 hex chars into a decimal integer (1–4 table lookups per field, width from the header) to get the probe seed, since `parseInt` is unavailable.
- **Integer math without `%`** — the hash has no `%`; everything integer is `/`, `div`, `bitAnd` with power-of-two `M`.
- **Binary-safe decode** — field digits are bytes `0x01`–`0xFE`; value bytes pass through as raw (Nix strings hold arbitrary bytes; UTF-8 decoding is applied to source literals, not to `readFile` results).
- **No mutation** — Nix eval is pure; the index is read-only at eval time.
- **Nix 2.34.7 source-literal pitfalls** — bytes that break Nix string literals (0x22, 0x27, 0x5C, 0x0D) are escaped in the static table; data values with 0x0D are stored as 0x0A.
- **List-literal parse quirk** — `[db.get "key"]` is parsed as a list literal containing a call; wrap in parens when the return type matters.
- The header is read once at import time; the table file itself is never parsed (only the static `nkv-table.nix` is imported, and that once per eval).

## Builder

    python3 build_nkv.py INPUT.json OUTPUT.nkv [--check]
    python3 build_nkv.py INPUT.json --shards {16,256,4096} --prefix DIR/ [--check]

- Takes **arbitrary JSON values** (not just strings); non-string values are stored as compact JSON and `get`/`getJson`/`getOrJson` handle retrieval (`getJson` returns the parsed value; a miss is `null`).
- `--shards` writes one nkv file per shard; **every shard file is always written**, including empty shards (valid table, N = 0, M = 16), so `readFile` and the index walk behave identically for empty and non-empty shards.
- `--check` re-derives shard membership and re-probes every key through the shard files.
- Width guards (reject inputs that would overflow the field widths).
- Single-file output is byte-identical across runs (deterministic key ordering).
- **Cross-language hash identity** — the builder's Python `hashlib.sha256(k).hexdigest()` is byte-identical to Nix's `hashString "sha256"`: same probe seed `int(h[56:64], 16) & (M − 1)`, same shard name, which the `--check` round-trip (build in Python, probe in Nix) proves.
- `gen_data.py` generates the small/medium/large test tables (1k / 50k / 200k, seeded RNG).

## Usage

Requires Nix 2.34.7+ (no flakes; plain `import` of a `.nix` file with path arguments).

    db = (import ./nkv.nix) ./data/large.nkv;
    db.get "some.key"          # -> value string or null
    db.getOr "some.key" "dflt" # -> value or "dflt"
    db.has "some.key"          # -> true / false
    db.count                   # -> N (header field)
    db.tableSize               # -> M (header field)

- A missing key returns `null` (not an error).
- `getJson` / `getOrJson` — same, but the value is run through `builtins.fromJSON` on hit (for values that store a JSON document); a miss is still `null`.
- `import` asserts the `NKV4` magic and the field widths (koffW/klenW/vlenW ∈ 1..4), so a wrong/corrupt file fails loudly at import time, not mid-lookup.
- Sharded: `import ./nkv.nix { digits = 2; dir = ./data/large_shards; }` — `digits` selects which `sha256` hex slice names the shard (1 → 16, 2 → 256, 3 → 4096 shards; must match what the builder was given) and `dir` is the shard directory. Lookup is lazy — no shard file is read until a lookup forces one, and Nix's import cache makes repeated imports of the same shard within one eval free. `db.count` imports **all** shards (it is the sum of the per-shard header `N` fields) — use it offline, not in a hot path.

## Correctness

    for s in small medium large; do
      python3 build_nkv.py data/$s.json data/$s.nkv --check
      nix eval --impure --json --expr "(import ./test_correctness.nix) \"$s\""
    done

evaluates `ok = true`, `mismatchCount = 0` — the builder's `--check` (an independent re-parser + probe of every key) and the Nix-side oracle (`get` vs `builtins.fromJSON` on the source JSON, 251,005 lookups total) agree on every key, and a known-missing key resolves to `null` on both sides.

Sharded correctness: `python3 build_nkv.py data/large.json --shards 256 --prefix data/large_shards/ --check` re-derives shard membership and re-probes all 200,000 keys through the shard files (0 mismatches, miss → `None`). The multiverse single-file and sharded indexes (256 shards each) are checked against `fromJSON` in `multiverse-faster/test_correctness.nix` and `multiverse-faster/test_correctness_shards.nix`: `mismatches = 0`, misses → `null`, `count = 31904` for both datasets — 127,616 multiverse lookups (31,904 × 2 datasets × {single, sharded}).

Total: 251,005 parent single-file + 200,000 parent sharded + 127,616 multiverse lookups, 0 mismatches, all misses → `null`.

## Performance summary

One cold `nix eval` per (method, table, n) point; n ∈ {0, 1, 5, 10, 30, 100, 200} lookups per eval, median of 7 runs (n = 200 row is the min of 7 — the most variable point; its work figure is paired with the min-total run). Harness: `benchmarks/bench.py`; results: `benchmarks/bench_results.json`.

The floor is a cold empty `nix eval --impure --raw --expr '""'` (33.0 ms median this run; 23.2–38.7 range); total and work figures are that subtraction, paired per run. `×` on work is fromJSON-work ÷ method-work. The n = 0 `fromJSON` point on the 200k table additionally forces all key names (a count), so it overstates plain parse cost; n = 0 for the multiverse tables reads one field. For `nkv`, n = 0 is `db.count` — a whole-table `readFile` plus a header read (the count is a header field, no slot walk); `nkv-sharded` has no n = 0 point, so its n = 1 row is the intercept. Environment: Nix 2.34.7+1 (non-FLAKE native eval), Linux x86-64 (Ryzen Threadripper 3970X), files hot in page cache; wall clock is `time.perf_counter` around subprocess `nix eval` invocations.

Cost model per `nix eval` doing n lookups (`floor` = the measured Nix load floor):

    fromJSON(n)   ≈ floor + readFile(JSON) + parse(JSON) + sub-ms·n
    nkv(n)        ≈ floor + readFile(.nkv) + probe-cost·n
    nkv-sharded(n) ≈ floor + Σ readFile(shard) for distinct shards hit + probe-cost·n

`fromJSON` reads and parses the whole file on the first lookup, then attrset access is O(1) — its per-lookup cost after the parse is negligible. nkv pays readFile + one probe per lookup.

Large table (200,000 keys, 13.9 MB JSON / 13.7 MB nkv) — total ms (work ms in parentheses); bold = best work per row:

| lookups/eval | fromJSON | nkv | nkv-sharded |
|---:|---:|---:|---:|
| 0 | 259.8 (225.0) | 58.6 (25.7) | — (no load point) |
| 1 | 210.0 (174.7) | 56.7 (22.9) (7.6×) | **34.3 (1.2)** (≈146×) |
| 5 | 208.1 (175.2) | 58.0 (25.0) (7.0×) | **34.9 (1.3)** (≈135×) |
| 10 | 213.5 (181.4) | 59.4 (26.0) (7.0×) | **35.1 (1.4)** (≈130×) |
| 30 | 212.4 (178.0) | 57.7 (24.8) (7.2×) | **34.5 (2.8)** (≈64×) |
| 100 | 210.4 (177.7) | 59.1 (25.0) (7.1×) | **43.1 (9.8)** (≈18×) |
| 200 | 207.0 (173.3) | 59.3 (26.0) (6.7×) | **46.2 (7.5)** (≈23×) |

(The n=200 row is the min-of-7 with work paired at the min-total run — see methodology above.)

Output size of the evaluated value (JSON string, n = 200): 6 / 66 / 195 / 502 / 1,359 / 4,285 / 8,656 B for n = 0/1/5/10/30/100/200.

Single cold lookup (N = 1) by table size — total ms (work ms):

| table | fromJSON | nkv | nkv-sharded |
|---|---:|---:|---:|
| 1,005 keys | 34.9 (0.1) | 34.3 (1.1) — parity | 34.3 (1.1) |
| 50,000 keys | 80.2 (45.9) | 39.7 (4.9) — 9.4× | 34.3 (1.1) |
| 200,000 keys | 210.0 (174.7) | 56.7 (22.9) — 7.6× | 34.3 (1.2) — ≈146× |

1k / 50k rows from `benchmarks/bench_marginal.json`; 200k rows from `benchmarks/bench_results.json`.

Multiverse (nixpkgs-multiverse, 31,904 attrs each) — versions 5.5 MB JSON / 5.1 MB `.nkv`: fromJSON 155–158 total (121–127 work) vs nkv 43–46 (8–12×) vs nkv-sharded 35–51 (7–42×); history 7.8 / 7.1 MB: fromJSON 253–259 (221–228) vs nkv 45–51 (11–18×) vs nkv-sharded 33–54 (21–149× at N ≤ 100; 7.3–11.5× at N = 200; at N = 1 the sharded work rounds to 0.0 ms).

Full per-point tables in [`multiverse-faster/README.md`](multiverse-faster/README.md).

### Reading the numbers

- **The intercept, not the slope, is where nkv wins.** `fromJSON` pays its readFile + parse once (~173–181 ms on 200k; 121–127 versions; 221–228 history) and then lookup is nearly free within the eval. nkv pays ~0.1–0.5 ms per lookup in-process, plus the per-import file cost.
- **Sharding wins the low-query regime.** nkv-sharded pays one small-shard readFile per *new* shard (~0.1–0.2 ms, import-cached for the eval) instead of a 13.7 MB readFile: at 1 lookup its **work is ~1.2 ms** (multiverse: ≈0–3 ms) — the ~34 ms total sits at the measured 33.0 ms Nix load floor (23.2–38.7) — ≈146× the data work of `fromJSON` on 200k keys (1.2 vs 174.7 ms); at n = 200 the work has risen to 7.5–19.5 ms (200k), 16.5–18.7 ms (versions), 19.2–23.4 ms (history) as queries spread across the 256 shards, ~0.03–0.12 ms/lookup.
- **Crossover.** On the multiverse tables nkv-sharded is ahead through n = 100 (versions 42.5 vs 46.1 total, work 10.1 vs 15.2; history 44.0 vs 49.3, work 10.6 vs 19.5) and single-file takes over at n = 200 (versions 44.2 vs 50.1, work 11.1 vs 16.5; history 49.7 vs 51.7, work 16.2 vs 19.2) — the crossover is between ~100 and ~200 lookups/eval. On the 13.7 MB 200k table nkv-sharded is ahead across the whole measured range (at n = 200: 46.2 vs 59.3 ms min row, work 7.5 vs 26.0; 52.9 vs 63.8 median, work 19.5 vs 32.2).
- **Bulk scans tip back to `fromJSON`.** When one evaluation touches most of the table, parse-once/index-many wins: a 31,904-lookup scan of all values (serialized) runs in 0.38 s with `fromJSON` vs 0.89 / 2.49 s with nkv / nkv-sharded.

**Verdict from the numbers:**

- **nkv (single file)** beats `fromJSON` at every measured point on 50k+ entry tables, on the data work: 9.4× at 50k, 7.6× (n = 1) / 6.7× (n = 200) on 200k, 8–12× versions, 11–18× history; at 1k the two are at parity (work 1.1 vs 0.1 ms — startup-bound).
- **Sharded nkv** adds the low-query regime: ~34 ms total per cold lookup on 200k keys (work 1.2 ms, ≈146× the data work of `fromJSON`), ahead of single-file up to ~100–200 lookups on the multiverse tables and across the whole measured range on the 13.7 MB table.
- **`fromJSON`** remains the right tool when one evaluation touches a large fraction of the table.

## Trade-offs and known limitations

- **`builtins.fromJSON` still wins for bulk scans** — if an evaluation touches most of the table, parsing once and indexing the attrset beats per-lookup file slicing (measured: 31,904-lookup scan 0.38 s vs 0.89 / 2.49 s). The crossover is beyond 200 lookups/eval on the parent tables and at ~100–200 for sharded vs single-file on the multiverse tables; at ~1k entries all approaches tie at ~34 ms total, of which ~0–1 ms is data work — the rest is Nix itself loading. nkv targets the common case: one or a few lookups per eval.
- **Per-lookup cost in-process is small** — a few `substring`s + static-table lookups per probe step; the key is read and byte-compared at every occupied walk step, the cost of having no fingerprint to pre-filter slots (and why a wrong value is impossible by construction).
- **File size ≈ 0.98× the JSON** (200k keys) — the EW-byte index is ~9.6% of the medium/large files (14.5% of small; 1.03× ratio at 1k, where the 14-byte header + 10,240-byte index dominate); in exchange a single-lookup cold eval reads and decodes only a few hundred bytes of the 13.7 MB file. Per-table minimum widths still pay off on small shards (shard-local `koffW` stays 2 digits; empty shards, M = 16, get the minimum widths).
- **Static only** — the table is precomputed and immutable at build time: adding keys requires re-running the builder (< 2 s for 200k keys; sharded rebuilds parallelize per shard), and there is no in-eval insertion.
- **Memory** — the index is never parsed into a Nix data structure; each lookup allocates a constant number of small heap strings (probe-seed, key/value fragments). The file string itself is materialised per import: the sharded variant reads one ~11–50 KB shard per lookup (all 256, ≈ the whole file, for `db.count`), the single-file variant reads the whole table.
- **The decode table must exist where `nkv.nix` sits** — it is imported by path relative to `nkv.nix`; if you copy the module elsewhere, copy `nkv-table.nix` alongside it. The table is a deterministic function of the format, so there is exactly one correct content.
- **Nix's string model caps the alphabet at 255** — Nix strings cannot contain NUL, so the numeric fields stop at 255-valued digits (base-255: digits 0–254 in bytes `0x01`–`0xFF`). The index region is not human-diffable (the data region is raw UTF-8); a future Nix with `builtins.parseInt` could shrink the index further, and raw-bytes support would lift the NUL limit.
- **Probe walk** — up to M empty slots in the worst case (bounded, not O(1)); the expected successful-search chain is ½(1 + 1/(1−α)) slots: 1.5 at the multiverse load 0.49, 2.6 at the 200k table's 0.76, 3 at the 0.8 cap.

## Repo layout

| file | role |
|---|---|
| `nkv.nix` | the Nix-side reader — single-file *and* sharded (two call shapes) |
| `nkv-table.nix` | checked-in static decode table (255-entry attrset) |
| `build_nkv.py` | builder + `--check` (single file and shards) |
| `gen_data.py` | test-data generator (1k / 50k / 200k) |
| `test_correctness.nix` | Nix-side oracle: `get` vs `builtins.fromJSON` |
| `data/` | small / medium / large `.json` + `.nkv` + `large_shards/` (256) |
| `benchmarks/` | `bench.py`, `bench_marginal.py`, `bench_results.json`, `bench_marginal.json` |
| `multiverse-faster/` | the nixpkgs-multiverse workload: its own README, bench + correctness files |
| `suggestions.md` | open improvement ideas, tiered (see the header inside for status) |

## Reproducing

The Correctness section's loop rebuilds everything from the generated JSON (`gen_data.py` → per-dataset `build_nkv.py` + `--check` + oracle). Benchmarks:

    python3 benchmarks/bench.py 7        # 3-method cold bench, 7 runs -> benchmarks/bench_results.json

The multiverse workload (convert → build → oracle → bench) rebuilds per `multiverse-faster/README.md`.
