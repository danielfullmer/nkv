# test_correctness.nix — verify kv.nix lookups match fromJSON for every key.
# fromJSON is used HERE (test harness only), never in the implementation.
# Invoke:  nix eval --impure --expr '(import ./test_correctness.nix) "small"'
size:
let
  db    = (import ./kv.nix) (./data/${size}.nfd);
  j     = builtins.fromJSON (builtins.readFile (./data/${size}.json));
  names = builtins.attrNames j;
  n     = builtins.length names;

  mismatches = builtins.filter (k: (db.get k) != j."${k}") names;
  missNullOk = db.get "___definitely_not_present___" == null;
  presentKey = builtins.head names;
  hasPresent = db.has presentKey;          # true  == correct
  hasMiss    = db.has "___definitely_not_present___";  # false == correct
in
  {
    size = size;
    total = n;
    countOk = db.count == n;
    tableSize = db.tableSize;
    mismatchCount = builtins.length mismatches;
    firstMismatch = if (builtins.length mismatches) > 0 then (builtins.head mismatches) else null;
    missNullOk = missNullOk;
    hasPresent = hasPresent;
    hasMiss = hasMiss;
    ok = (builtins.length mismatches) == 0 && missNullOk && hasPresent && !hasMiss && db.count == n;
  }