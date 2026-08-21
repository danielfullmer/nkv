# Sharded nkv getJson vs builtins.fromJSON oracle over every attribute.
#
#   nix eval --impure --json --expr "(import ./test_correctness_shards.nix)
#     { dir = /path/to/versions_shards; jsonPath = /path/to/index/versions.json; }"
#
# `table` is a directory built with `build_nkv.py --shards 256 --prefix`.
# Prints { total, mismatches, firstBad, missNull, count }. `count` is the
# expensive sharded path: it imports (reads) all 256 shard files and must
# equal the oracle key total.

{ dir, jsonPath }:
let
  db = import ../nkv.nix { digits = 2; dir = dir; };
  o = builtins.fromJSON (builtins.readFile jsonPath);
  ks = builtins.attrNames o.attrs;
  bad = builtins.filter (k: db.getJson k != o.attrs.${k}) ks;
in
builtins.toJSON {
  total = builtins.length ks;
  mismatches = builtins.length bad;
  firstBad = if builtins.length bad > 0 then builtins.elemAt bad 0 else null;
  missNull = db.getJson "no-such-attr-xyz" == null;
  count = db.count;
}