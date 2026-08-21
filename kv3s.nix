# kv3s.nix — NFK v3 with optional file sharding (build_db3.py --shards).
#
# A sharded table is a directory of NFK v3 files, one per shard slice of the
# key hash: <dir>/<h[24:24+d]>.nfd3 where h = sha256(key) in lowercase hex
# and d = digits. The slice is disjoint from the fp slice 0..6 and the s0
# slice 56..64 that kv3.nix uses, so sharding does not perturb the probing
# distribution. The builder writes every shard (empty ones are valid NFK v3
# files with N = 0, M = 16), so a key always resolves to an existing file:
#
#   python3 build_db3.py input.json --shards 256 --prefix dir/ --check
#   nix eval --impure --raw --expr
#     '(import ./kv3s.nix { dir = ./dir; }).getJson "some-key"'
#
# digits: 1 | 2 | 3 -> 16 | 256 | 4096 shards. Must match --shards.
#
# Only the shard a key hashes to is read per lookup (the Nix import cache
# keeps it for the rest of the eval), so the intercept is one small file
# instead of the whole table. Every NFK v3 file carries the identical
# 255-byte decode table at offset 64, so it is built once per eval (from
# the all-zero shard, which the builder always writes) and shared by all
# shard imports — without this each import would rebuild it, which was
# the dominant part of the per-shard fixed cost. `count` reads every
# shard — offline use.

{ digits ? 2, dir }:
assert digits == 1 || digits == 2 || digits == 3;
let
  KV3 = ./kv3.nix;
  # key -> shard slice, e.g. "3f". Same computation as the builder's
  # shard_slice (sha256 hex chars 24..24+digits).
  shard = key:
    builtins.substring 24 digits (builtins.hashString "sha256" key);
  # The all-zero shard name for this digits count.
  zeroName = builtins.getAttr (builtins.toString digits)
    { "1" = "0"; "2" = "00"; "3" = "000"; };
  # The 255 table bytes, sourced from the zero shard (always present,
  # even when empty).
  T0 = builtins.substring 64 255 (builtins.readFile (dir + "/${zeroName}.nfd3"));
  # char -> int decode table, built once per eval and shared by every
  # shard import via kv3.nix's { file, table } form.
  table0 =
    builtins.foldl' (a: i: a // { "${builtins.substring i 1 T0}" = i; })
    {}
    (builtins.genList (i: i) 255);
  # key -> the NFK v3 module for that key's shard file, using the shared
  # table (skips the per-shard table rebuild).
  table = key:
    import KV3 { file = dir + "/${shard key}.nfd3"; table = table0; };
in
{
  # key -> shard slice (debug / shard-size checks).
  shard = shard;
  # key -> shard file path (debug).
  file = key: dir + "/${shard key}.nfd3";

  get = key: (table key).get key;
  getOr = key: default: (table key).getOr key default;
  getJson = key: (table key).getJson key;
  getOrJson = key: default: (table key).getOrJson key default;
  has = key: (table key).has key;

  # Expensive: imports (reads) every shard file. Offline use only —
  # e.g. verifying the total key count in a correctness run.
  count =
    let names = builtins.attrNames (builtins.readDir dir);
    in builtins.foldl'
      (a: n: a + (import KV3 { file = dir + "/${n}"; table = table0; }).count)
      0
      (builtins.filter (n: builtins.substring (builtins.stringLength n - 5) 5 n == ".nfd3") names);
}