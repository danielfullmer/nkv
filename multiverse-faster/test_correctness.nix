# NFK3 getJson vs builtins.fromJSON oracle over every attribute.
#
#   nix eval --impure --json --expr "(import ./test_correctness.nix)
#     { table = /path/to/versions.nfd3; jsonPath = /path/to/index/versions.json; }"
#
# Prints { total, mismatches, firstBad, missNull }.

{ table, jsonPath }:
let
  db = import ../kv3.nix table;
  o = builtins.fromJSON (builtins.readFile jsonPath);
  ks = builtins.attrNames o.attrs;
  bad = builtins.filter (k: db.getJson k != o.attrs.${k}) ks;
in
builtins.toJSON {
  total = builtins.length ks;
  mismatches = builtins.length bad;
  firstBad = if builtins.length bad > 0 then builtins.elemAt bad 0 else null;
  missNull = db.getJson "no-such-attr-xyz" == null;
}