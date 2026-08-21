# test_correctness.nix — verify nkv.nix (nkv, dense hash + binary
# index) lookups match fromJSON.
# Invoke:  nix eval --impure --expr '(import ./test_correctness.nix) "small"'
size:
let
  db = (import ./nkv.nix) (./data/${size}.nkv);
  jsonData = builtins.fromJSON (builtins.readFile (./data/${size}.json));
  names = builtins.attrNames jsonData;
  n = builtins.length names;

  mismatches = builtins.filter (k: (db.get k) != jsonData."${k}") names;
  missNullOk = db.get "___definitely_not_present___" == null;
  presentKey = builtins.head names;
  hasPresent = db.has presentKey;          # true  == correct
  hasMiss    = db.has "___definitely_not_present___";  # false == correct
in
  {
    size = size;
    total = n;
    countOk = db.count == n;
    tableSizeOk = db.tableSize >= n;       # at least one empty slot guaranteed
    mismatchCount = builtins.length mismatches;
    firstMismatch = if (builtins.length mismatches) > 0 then (builtins.head mismatches) else null;
    missNullOk = missNullOk;
    hasPresent = hasPresent;
    hasMiss = hasMiss;
    ok = (builtins.length mismatches) == 0 && missNullOk && hasPresent && !hasMiss
      && db.count == n && db.tableSize >= n;
  }
