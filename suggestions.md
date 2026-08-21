# NFK3 improvement suggestions

2026-08-20. Grounded in `kv3.nix` (tag #1649), `build_db3.py`, and the measured
benchmarks (parent six-method session, 200k `large` dataset; and the
`multiverse-faster/` 31,904-key workload, Nix 2.34.7). Nothing here is a
correctness fix — NFK v3 is sound; these are performance and robustness.

## Where the cost is (measured)

| component | large (16.3 MB, 200k keys) | multiverse (5.7 MB, 31,904 keys) |
|---|---|---|
| `nix eval` startup | ~36 ms | ~36 ms |
| intercept: `readFile` + import (N=0) | 60.4 ms | 46.2 ms |
| marginal per lookup (slope, 10–400 lookups) | ~58 µs | ~20–47 µs |

A single lookup is ~50 interpreter operations:

- 1 `builtins.hashString "sha256"`;
- **two `hexInt` folds over 14 hex chars** (kv3.nix:70–74, 107–108) — ~18
  substrings + attrset lookups, pure overhead from the missing
  `builtins.parseInt`;
- per probe: `dec4`/`dec3` through the 255-entry byte-table attrset
  (kv3.nix:61–67, 88–99) — ~22 substring+lookup ops — plus ~8 more substrings
  (slot, key, value).

Two levers: the **intercept** (dominated by `readFile` of the whole file) and
the **marginal** (interpreter op count per lookup).

## Tier 1 — shard the file (biggest absolute win)

`builtins.readFile` is all-or-nothing, so every eval pays ~10–24 ms copying
5.7/16.3 MB even for one lookup. Split the table by a second digest slice
(e.g. `sha256[24:26]` → 256 shards named `s-0f.nfd3`, `s-1a.nfd3`, …):

- multiverse: ~23 KB per shard (125 keys, M=256) → intercept 46 ms → ~37 ms
  (−10 ms, −22%);
- 200k: ~61 KB per shard (781 keys, M=1024) → 60.4 ms → ~37 ms
  (−23 ms, −38%);
- wins hardest at low N (the lock-file case: N=30 touches ~27 shards ≈ 1 ms
  of reads); at N → full-scan most of the file is re-read and the advantage
  vanishes — that regime belongs to `fromJSON` anyway (matches the existing
  crossover policy).

Cost: 256 files, a `build_db3.py --shards 256 --prefix dir/` mode (route each
key by its own digest slice), and a ~20-line `kv3s.nix` wrapper that picks the
shard and delegates to `kv3.nix`. You lose the single-file property — that is
the trade. (This is the same gap the fkzakaria article's wasm section fights:
Nix has no partial-read builtin.)

## Tier 2 — v4 slot: store the fingerprint as raw hex (W 15 → 16)

Today the probe pays `hexInt(h[0:6])` + `dec4(slot fp)` (~18 ops) just to
compare fingerprints. Instead:

- store the 24-bit fp as the **6 ASCII hex chars verbatim**:
  slot = fp 6 | keyOff dec4 4 | keyLen dec3 3 | valLen dec3 3 = 16 bytes
  (the current pad byte drops out);
- keep the unused marker `16×0x01`, tested with a **1-byte compare**
  (`substring e 1 raw == T[0]`, where `T[0]` is the first byte of the
  file-carried table = 0x01). Collision-proof: a used slot's first byte is an
  ASCII hex char (0x30–0x66), never 0x01. This also removes the need for the
  `fp = int + 1` / reserved-0 scheme;
- the probe becomes 2 substrings + 2 string-equality compares (~4 ops) per
  step; `dec4`/`dec3` of keyOff/klen/vlen happen only on a fingerprint hit.

Net: **~−18 ops per lookup**; a fingerprint collision still only adds a key
read, never a wrong value (same correctness argument as today,
kv3.nix:27–28). File cost: +6.7% of the index region = +66 KB / +1.2%
(multiverse), +256 KB / +1.6% (large). Requires a format version bump +
parser v4; the existing `--check` + fromJSON-oracle harness makes that a
mechanical verification pass.

## Tier 3 — cheaper hex decode + sliced s0 (import-time tables)

- Precompute a **256-entry two-char→int attrset** from the ASCII constant
  `"0123456789abcdef"` once at import (no file-carried table needed — hex is
  pure ASCII). Decoding a 2-char hex slice = 1 substring + 1 attrset lookup
  instead of a 2-iteration fold.
- `s0` only needs `k = ceil(log2 m / 4)` hex chars of `h[56:64]` (the mask
  `bitAnd (… ) (m-1)` keeps only the low `log2 m` bits, m is a power of two):
  m = 65,536 → **4 chars** (today 8), m = 262,144 → 5. Compute k at import.

Net: ~−8–10 ops per lookup. Combined with Tier 2 the ~52-op lookup drops to
~28 ops — expect ~30 µs/lookup (large) and ~15–22 µs (multiverse); re-bench to
confirm.

## Cheap hardening (no format change)

- **`build_db3.py --check`**: also verify each stored fp against
  `hashlib.sha256(key)` (today it only verifies value bytes); `sys.exit` on
  duplicate keys in `build()` pairs (a dict input dedups silently; an explicit
  pair list would insert twice and the lookup would return whichever entry the
  probe sequence reaches first — order-dependent and surprising); guard
  per-value length < 254³.
- **Opt-in integrity checksum**: use 16 of the 47 reserved header bytes
  (offsets 17–63) for a sha256-of-data-region (ASCII hex) + an opt-in
  `assertIntegrity` function. Zero hot-path cost; catches partial writes and
  bit rot.
- **API**: `getMany` (map `get` over a key list), `keys` (O(M) slot walk —
  for debugging / nix-side checks), `load` (n/m float, diagnostics).
- **Byte-table build** at import (kv3.nix:55–58) is a 255×`//` fold —
  O(n²) ≈ 33k attrset copies, <1 ms one-off. Replace with a
  split-and-merge (O(n log n)); free win.

## Considered and rejected

| idea | why not |
|---|---|
| Non-pow2 M via emulated mod (`x − m*(x/m)`; `builtins.mod` missing) | Saves 182 KB–1.6 MB (−1–9% of file; worst case when n just misses a pow2) but adds integer divisions per probe and breaks the fixed-M policy (`M = next_pow2(max(16, ceil(1.25·n)))`). Not worth it. |
| Hex-encode all numeric fields (drop b254) | W 15 → 26, +17% of the file — destroys the binary-density win that made v3 competitive with NKB v2. |
| Drop the fingerprint, compare keys directly | At load 0.49–0.76 a lookup takes 1.3–1.6 probes; each would copy and compare the key instead of 16 slot bytes. Slower on the common case. |
| md5/sha1 instead of sha256 | The builtin call + hex formatting dominates; the digest itself is ns-level at these key lengths. |
| Value deduplication | No meaningful duplication observed (multiverse values are per-(attr,date) lists); would need a second probe at lookup. |
| Compress the file | No decompress builtin; `builtins.exec`-based decompression breaks the pure-eval, cold-process design. |

## Upstream watch items

Any of these landing re-opens the corresponding suggestion above:

- `builtins.parseInt` → kills `HEX`/`hexInt` entirely; all decoders shrink ~10×.
- `builtins.mod` (or a modulo operator) → clean non-pow2 M and probe wrap.
- Partial file reads → makes Tier 1 (sharding) unnecessary.
- NUL-capable strings → lifts the 254 base (b254 → b255/true binary).

## Workload trick (no code)

For a lock file that re-pins the same ~30 attrs, build a **micro-table of
exactly those 30 keys** (M=64, ~10 KB): the whole lock answers in ~35 ms vs
~158 ms for `fromJSON` on the 5.3 MB `versions.json` (measured,
`multiverse-faster/`). `build_db3.py` already accepts arbitrary JSON input —
no new code.

## Priority

1. **Tier 2 (v4 hex fp)** — self-contained format change with a clean
   correctness story; pays on every lookup of every table.
2. **Tier 1 (sharding)** — if the multiverse-style workload (many small
   files' worth of lookups, low N per eval) matters more than the 200k
   single-file one.
3. **Tier 3** — rides along with Tier 2 (same hex-decoding site).
4. Hardening items — do whenever the builder is touched.