# NFK v3 improvement suggestions

2026-08-20. Grounded in the current code (`kv3.nix`, `kv3s.nix`,
`nfd3-table.nix`, `build_db3.py`) and the measured 3-method benchmarks
(fromJSON / nfk3 / nfk3s; parent 200k `large` dataset and the
`multiverse-faster/` 31,904-attr workload, Nix 2.34.7). Nothing here is a
correctness fix — NFK v3 is sound; these are performance and robustness.

**Status (updated 2026-08-20):**

- **Tier 1 (sharding)** — implemented: `build_db3.py --shards/--prefix` +
  `kv3s.nix`; measured results at the end of the Tier 1 section.
- **Static decode table** — implemented: the 255-byte b254 table moved out
  of every `.nfd3` into a single generated `nfd3-table.nix` (imported once
  per eval by `kv3.nix`; Nix's import cache makes repeat imports free). Data
  files shrank by 255 B each (matters for the 256-shard dirs), sharded shard
  imports are now plain readFile + header asserts, and the `0x0D`-escape
  hazard is confined to one generated file.
- Remaining: Tier 2 (v4 hex fingerprint), Tier 3 (cheaper hex decode), and
  the hardening items below.

## Where the cost is (measured, current format)

| component | large (16.3 MB, 200k keys) | multiverse (5.7–7.7 MB, 31,904 attrs) |
|---|---|---|
| `nix eval` load floor (cold empty eval, measured) | 33.7 ms median (min 29.4) | 32.9 ms median (min 31.6) |
| nfk3 n = 1 (readFile + import + 1 lookup) | 62.1 total (work 28.4) | 45.3 / 46.1 (work 13.1 / 12.5) |
| nfk3s n = 1 (one shard read + 1 lookup) | 34.6 (work 1.6) | 34.4 / 35.2 (work 2.7 / 2.1) |
| fromJSON n = 1 (readFile + parse + 1 lookup) | 211.8 (work 178.1) | 159.9 / 257.9 (work 126.2 / 226.3) |

A single nfk3 lookup is ~50 interpreter operations:

- 1 `builtins.hashString "sha256"`;
- **two hex folds over 14 hex chars** (24-bit fingerprint from `h[0:6]`,
  probe seed from `h[56:64]`) — ~18 substrings + attrset lookups, pure
  overhead from the missing `builtins.parseInt`;
- per probe: `dec4`/`dec3` through the 255-entry static byte-table attrset
  (3–4 lookups per field) — plus ~8 more substrings (slot, key, value).

Two levers: the **intercept** (dominated by `readFile` of the whole file —
sharding attacks this) and the **marginal** (interpreter op count per
lookup — Tier 2/3 attack this).

## Tier 1 — shard the file (biggest absolute win) — **implemented**

`builtins.readFile` is all-or-nothing, so every eval pays ~10–40 ms copying
5.7/16.3 MB even for one lookup. Split the table by a second digest slice
(`sha256[24:24+d]` → 16/256/4096 shards):

- multiverse: ~22 KB per shard → single-lookup eval 34–35 ms total (work ~2
  ms; fromJSON total ~160 / ~258);
- 200k: ~80 KB per shard → single-lookup eval 34.6 ms (work 1.6 ms;
  fromJSON 211.8 total, work 178.1);
- wins hardest at low N (the lock-file case: N = 30 touches ~27 shards ≈ 1–3
  ms of reads); as N → full-scan, most shards are imported and the advantage
  narrows — that regime belongs to single-file (or `fromJSON` at bulk scan),
  matching the measured crossover below.

Cost: 256 files, a `build_db3.py --shards 256 --prefix dir/` mode (route
each key by its own digest slice), and a thin `kv3s.nix` wrapper that picks
the shard and delegates to `kv3.nix`. You lose the single-file property —
that is the trade. (This is the same gap the fkzakaria article's wasm
section fights: Nix has no partial-read builtin.)

**Implemented** (2026-08-20): `build_db3.py --shards {16,256,4096} --prefix
DIR/` writes `DIR/<h[24:24+d]>.nfd3` for every shard (empty shard = valid
NFK v3 file, N = 0, M = 16); `kv3s.nix` selects the shard by
`sha256(key) hex [24:24+d]` and delegates to `kv3.nix` (same API; lazy — no
shard file is read until a lookup is forced). The static `nfd3-table.nix`
is imported once per eval no matter how many shards are touched, so a shard
import is just readFile + header asserts. Measured (multiverse, 256 shards,
median of 3 cold `nix eval`s, `total (work)` ms, `multiverse-faster/bench.py`):

| n lookups/eval | fromJSON | nfk3 (single file) | nfk3s (256 shards) |
|---:|---:|---:|---:|
| **versions** | | | |
| 1 | 159.9 (126.2) | 45.3 (13.1) | **34.4 (2.7)** |
| 5 | 157.4 (124.6) | 46.0 (12.8) | 34.6 (1.8) |
| 10 | 156.7 (124.2) | 43.4 (11.8) | 33.8 (2.2) |
| 30 | 168.4 (136.8) | 45.3 (12.5) | **39.5 (6.6)** |
| 100 | 158.3 (125.9) | 47.9 (15.0) | 49.1 (16.3) |
| 200 | 167.4 (134.5) | 49.2 (16.4) | 50.8 (17.2) |
| **history** | | | |
| 1 | 257.9 (226.3) | 46.1 (12.5) | **35.2 (2.1)** |
| 5 | 257.0 (225.4) | 45.6 (14.0) | 34.6 (0.9) |
| 10 | 259.9 (228.3) | 51.0 (18.2) | 36.0 (3.9) |
| 30 | 265.5 (231.8) | 47.4 (13.8) | **36.5 (3.8)** |
| 100 | 262.2 (228.5) | 52.3 (18.7) | **45.5 (11.8)** |
| 200 | 267.1 (233.4) | 55.0 (22.6) | 53.2 (20.3) |

The single-lookup intercept sits at 34.4/35.2 ms total (work 2.7/2.1 ms) —
within ~2 ms of the measured empty-eval floor (32.9 ms median) — i.e.
~47× (versions) / ~110× (history) the `fromJSON` data work. Crossover
nfk3s vs nfk3: on versions, sharded is ahead through n = 30 (39.5 vs 45.3
total; work 6.6 vs 12.5) and
single-file takes over by n = 100 (49.1 vs 47.9; work 16.3 vs 15.0) —
crossover ~30–100; on history sharded is still ahead at n = 200 (53.2 vs
55.0 total; work 20.3 vs 22.6) — crossover beyond 200. The sharded slope
rises (~0.1–0.2 ms per new distinct shard imported) as queries spread across
the 256 shards: work 2.7 → 17.2 ms (versions) and 2.1 → 20.3 ms (history)
by n = 200. On the 16.3 MB 200k-key table, single-file's 16 MB readFile
keeps nfk3s ahead across the whole measured range (55.4 vs 66.5 ms total at
n = 200; work 21.7 vs 34.6); the crossover is beyond 200 lookups there.

**History of the fix** (2026-08-20): the first `kv3s.nix` rebuilt the decode
table at every shard import (a 255×`//` fold, ~0.8 ms/shard), which made the
sharded-vs-single crossover sit at ~10–30 lookups. Sharing the table once
per eval (first via the always-present zero shard, then via the static
`nfd3-table.nix` after the table left the data files) moved the crossover
right to ~100–200 lookups and cut per-shard import cost 0.82 → 0.18 ms.

## Tier 2 — v4 slot: store the fingerprint as raw hex (W 15 → 16)

Today the probe pays a 6-char hex fold of the key's digest **and** `dec4`
of the slot's fingerprint (~18 ops) just to compare fingerprints. Instead:

- store the 24-bit fp as the **6 ASCII hex chars verbatim** (the key's
  `sha256` hex `h[0:6]`, unchanged):
  slot = fp 6 | keyOff dec4 4 | keyLen dec3 3 | valLen dec3 3 = 16 bytes
  (the current pad byte drops out);
- keep the unused marker 16×0x01, tested with a **1-byte compare**
  against a raw-0x01 literal (raw control bytes pass through Nix string
  literals unchanged on this Nix; only 0x0D needs escaping). Collision-proof:
  a used slot's first byte is an ASCII hex char (0x30–0x66), never 0x01.
  This also removes the need for the `fp = int + 1` / reserved-0 scheme;
- the probe becomes 2 substrings + 2 string-equality compares (~4 ops) per
  step: compare `substring e 6 raw` against the key's `h[0:6]` directly —
  no hex-fold of the fingerprint at all; `dec4`/`dec3` of keyOff/klen/vlen
  happen only on a fingerprint hit.

Net: **~−18 ops per lookup**; a fingerprint collision still only adds a key
read, never a wrong value (same correctness argument as today). File cost:
+6.7% of the index region = +66 KB / +1.2% (multiverse versions), +256 KB /
+1.6% (large). Requires a format version bump (revision byte 1 → 2) +
parser v4; the existing `--check` + fromJSON-oracle harness makes that a
mechanical verification pass.

## Tier 3 — cheaper hex decode + sliced s0 (import-time tables)

- Precompute a **256-entry two-char→int attrset** from the ASCII constant
  `"0123456789abcdef"` once at import (pure ASCII — no byte-table file
  needed for hex). Decoding a 2-char hex slice = 1 substring + 1 attrset
  lookup instead of a 2-iteration fold.
- `s0` only needs `k = ceil(log2 M / 4)` hex chars of `h[56:64]` (the mask
  `bitAnd (… ) (M - 1)` keeps only the low `log2 M` bits, `M` is a power of
  two): M = 65,536 → **4 chars** (today 8), M = 262,144 → 5. Compute k at
  import.

Net: ~−8–10 ops per lookup. Combined with Tier 2 the ~52-op lookup drops to
~28 ops — expect ~30 µs/lookup (large) and ~15–22 µs (multiverse);
re-bench to confirm.

## Cheap hardening (no format change)

- **`build_db3.py --check`**: also verify each stored fp against
  `hashlib.sha256(key)` (today the probe re-validates keys and values, not
  the fp field); `sys.exit` on duplicate keys in `build()` (a dict input
  dedups silently; an explicit pair list would insert twice and the lookup
  would return whichever entry the probe sequence reaches first —
  order-dependent and surprising).
- **Opt-in integrity checksum**: use 16 of the 46 reserved header bytes
  (offsets 18–63) for a sha256-of-data-region (ASCII hex) + an opt-in
  `assertIntegrity` function. Zero hot-path cost; catches partial writes and
  bit rot.
- **API**: `getMany` (map `get` over a key list), `keys` (O(M) slot walk —
  for debugging / nix-side checks), `load` (n/m float, diagnostics).

## Considered and rejected

| idea | why not |
|---|---|
| Non-pow2 M via emulated mod (`x − m*(x/m)`; `builtins.mod` missing) | Saves 182 KB–1.6 MB (−1–9% of file; worst case when n just misses a pow2) but adds integer divisions per probe and breaks the fixed-M policy (`M = next_pow2(max(16, ceil(1.25·n)))`). Not worth it. |
| Hex-encode all numeric fields (drop b254) | W 15 → 26, +17% of the file — destroys the binary-density win. |
| Drop the fingerprint, compare keys directly | At load 0.49–0.76 a lookup takes 1.3–1.6 probes; each would copy and compare the key instead of 16 slot bytes. Slower on the common case. |
| md5/sha1 instead of sha256 | The builtin call + hex formatting dominates; the digest itself is ns-level at these key lengths. |
| Value deduplication | No meaningful duplication observed (multiverse values are per-(attr,date) lists); would need a second probe at lookup. |
| Compress the file | No decompress builtin; `builtins.exec`-based decompression breaks the pure-eval, cold-process design. |
| Raw 0x0D bytes in `.nix` source | The lexer normalizes raw 0x0D to 0x0A (verified byte-for-byte) — a raw-bytes table literal would corrupt the 0x0D digit. Escaped literals (`\r`) and binary files via `readFile` are safe; `nfd3-table.nix` escapes the four literal-breaking bytes. |

## Upstream watch items

Any of these landing re-opens the corresponding suggestion above:

- `builtins.parseInt` → kills the hex folds entirely; all decoders shrink ~10×.
- `builtins.mod` (or a modulo operator) → clean non-pow2 M and probe wrap.
- Partial file reads → makes Tier 1 (sharding) unnecessary.
- NUL-capable strings → lifts the 254 base (b254 → b255/true binary).

## Workload trick (no code)

For a lock file that re-pins the same ~30 attrs, build a **micro-table of
exactly those 30 keys** (M = 64, ~10 KB): the whole lock answers in ~35 ms
vs ~157–160 ms for `fromJSON` on the 4.8 MB `versions_flat.json` (measured,
`multiverse-faster/`). `build_db3.py` already accepts arbitrary JSON input —

## Priority (updated 2026-08-20)

1. **Tier 2 (v4 hex fp)** — self-contained format change with a clean
   correctness story; pays on every lookup of every table.
2. **Tier 3** — rides along with Tier 2 (same hex-decoding site).
3. Hardening items — do whenever the builder is touched.
4. ~~Tier 1 (sharding)~~ — **implemented**: `build_db3.py --shards` +
   `kv3s.nix`. Use for lock-file-style workloads (crossover ~30–100
   lookups/eval on versions, ~100–200 on history); on the 16 MB table
   sharded wins across the measured range.
5. ~~Static decode table~~ — **implemented** (2026-08-20): `nfd3-table.nix`,
   one import per eval.