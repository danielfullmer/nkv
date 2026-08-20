# test_correctness_bin2.nix — verify kv_bin2.nix (NKB v2, binary index)
# lookups match fromJSON.
# fromJSON is used HERE (test harness only), never in the implementation.
# Invoke:  nix eval --impure --expr '(import ./test_correctness_bin2.nix) "small"'
size:
let
  db    = (import ./kv_bin2.nix) (./data/${size}.nkb2);
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
    mismatchCount = builtins.length mismatches;
    firstMismatch = if (builtins.length mismatches) > 0 then (builtins.head mismatches) else null;
    missNullOk = missNullOk;
    hasPresent = hasPresent;
    hasMiss = hasMiss;
    ok = (builtins.length mismatches) == 0 && missNullOk && hasPresent && !hasMiss && db.count == n;
  }