# kv3s.nix — NFK v3 with optional file sharding (build_db3.py --shards).
#
# A sharded table is a directory of NFK v3 files, one per shard slice of the
# key hash: <dir>/<h[24:24+d]>.nfd3 where h = sha256(key) in lowercase hex
# and d = digits. The slice is disjoint from the probe-seed slice
# h[56:64] that kv3.nix uses, so sharding does not perturb the probing
# distribution. The builder writes every shard (empty ones are valid NFK v3
# files with N = 0, M = 16), so a key always resolves to an existing file:
#
#   python3 build_db3.py input.json --shards 256 --prefix dir/ --check
#   nix eval --impure --raw --expr
#     '(import ./kv3s.nix { digits = 2; dir = ./dir; }).getJson "some-key"'
#
# digits: 1 | 2 | 3 -> 16 | 256 | 4096 shards. Must match --shards.
#
# Only the shard a key hashes to is read per lookup (the Nix import cache
# keeps it for the rest of the eval), so the intercept is one small file
# instead of the whole table. `count` reads every shard — offline use.

{ digits ? 2, dir }:
assert digits == 1 || digits == 2 || digits == 3;
let
  KV3 = ./kv3.nix;
  # key -> shard slice, e.g. "3f". Same computation as the builder's
  # shard_slice (sha256 hex chars 24..24+digits).
  shard = key:
    builtins.substring 24 digits (builtins.hashString "sha256" key);
  # key -> the NFK v3 module for that key's shard file.
  db = key: import KV3 (dir + "/${shard key}.nfd3");
in
{
  # key -> shard slice (debug / shard-size checks).
  shard = shard;
  # key -> shard file path (debug).
  file = key: dir + "/${shard key}.nfd3";

  get = key: (db key).get key;
  getOr = key: default: (db key).getOr key default;
  getJson = key: (db key).getJson key;
  getOrJson = key: default: (db key).getOrJson key default;
  has = key: (db key).has key;

  # Expensive: imports (reads) every shard file. Offline use only —
  # e.g. verifying the total key count in a correctness run.
  count =
    let names = builtins.attrNames (builtins.readDir dir);
    in builtins.foldl'
      (a: n: a + (import KV3 (dir + "/${n}")).count)
      0
      (builtins.filter (n: builtins.substring (builtins.stringLength n - 5) 5 n == ".nfd3") names);
}