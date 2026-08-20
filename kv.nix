# kv.nix — extremely fast hash-based key/value lookup for native Nix.
#
# Reads a precomputed binary database (built by build_db.py) using only
# builtins.readFile, builtins.substring, builtins.hashString and basic
# arithmetic / string builtins.  No builtins.fromJSON, no builtins.exec.
#
# File format "NFK v1" — fixed-width ASCII fields, three regions:
#
#   offset 0                 header (64 bytes)
#       [0..3]    magic   "NFK1"
#       [4..5]    version "01"
#       [6..7]    algo    "sh"  (sha256)
#       [8..17]   M       table size, 10-digit zero-padded decimal (power of two)
#       [18..27]  N       entry count, 10-digit zero-padded decimal
#       [28..63]  reserved (spaces)
#
#   offset 64                index region (M * 40 bytes)
#     one 40-byte entry per table slot s (offset 64 + s*40):
#       [0..15]   fp      first 16 hex chars of sha256(key); "g"*16 if unused
#       [16..25]  keyOff  10-digit decimal, byte offset of key in data region
#       [26..31]  keyLen   6-digit decimal, byte length of key
#       [32..39]  valLen   8-digit decimal, byte length of value
#       (value offset is keyOff + keyLen — not stored)
#
#   offset 64 + M*40         data region (variable)
#     concatenated (key bytes ++ value bytes) per entry, insertion order.
#
# Lookup algorithm:
#   h   = sha256(key) as 64 lowercase hex chars            (one hashString call)
#   fp  = h[0..16)                                          (fingerprint)
#   s0  = int(h[56..64), 16) AND (M - 1)                    (initial slot)
#   then linearly probe slots s0, s0+1, ... (mod M) comparing fp; on an fp
#   match the stored key is read and compared byte-for-byte (exactness),
#   and the value is returned.  "g"*16 marks an empty slot -> miss.
#
# Usage:
#   let db = (import ./kv.nix) ./data/test.nfd;
#   in db.get "someKey"       # -> value string, or null if absent
path:

let

  # ---- layout constants (must match build_db.py) ----
  H  = 64;   # header length
  W  = 40;   # index entry width
  FP = 16;   # fingerprint width in hex chars

  # empty-slot fingerprint: 'g' is not a hex digit, so it can never collide
  # with a real (0-9a-f) fingerprint.
  EMPTY = builtins.concatStringsSep "" (builtins.genList (_: "g") FP);

  # ---- decimal string -> integer, two digits at a time ----
  # (this Nix has no builtins.parseInt; we decode via a table + foldl')
  d2 = { "00" = 0 ; "01" = 1 ; "02" = 2 ; "03" = 3 ; "04" = 4 ; "05" = 5 ; "06" = 6 ; "07" = 7 ; "08" = 8 ; "09" = 9 ; "10" = 10 ; "11" = 11 ; "12" = 12 ; "13" = 13 ; "14" = 14 ; "15" = 15 ; "16" = 16 ; "17" = 17 ; "18" = 18 ; "19" = 19 ; "20" = 20 ; "21" = 21 ; "22" = 22 ; "23" = 23 ; "24" = 24 ; "25" = 25 ; "26" = 26 ; "27" = 27 ; "28" = 28 ; "29" = 29 ; "30" = 30 ; "31" = 31 ; "32" = 32 ; "33" = 33 ; "34" = 34 ; "35" = 35 ; "36" = 36 ; "37" = 37 ; "38" = 38 ; "39" = 39 ; "40" = 40 ; "41" = 41 ; "42" = 42 ; "43" = 43 ; "44" = 44 ; "45" = 45 ; "46" = 46 ; "47" = 47 ; "48" = 48 ; "49" = 49 ; "50" = 50 ; "51" = 51 ; "52" = 52 ; "53" = 53 ; "54" = 54 ; "55" = 55 ; "56" = 56 ; "57" = 57 ; "58" = 58 ; "59" = 59 ; "60" = 60 ; "61" = 61 ; "62" = 62 ; "63" = 63 ; "64" = 64 ; "65" = 65 ; "66" = 66 ; "67" = 67 ; "68" = 68 ; "69" = 69 ; "70" = 70 ; "71" = 71 ; "72" = 72 ; "73" = 73 ; "74" = 74 ; "75" = 75 ; "76" = 76 ; "77" = 77 ; "78" = 78 ; "79" = 79 ; "80" = 80 ; "81" = 81 ; "82" = 82 ; "83" = 83 ; "84" = 84 ; "85" = 85 ; "86" = 86 ; "87" = 87 ; "88" = 88 ; "89" = 89 ; "90" = 90 ; "91" = 91 ; "92" = 92 ; "93" = 93 ; "94" = 94 ; "95" = 95 ; "96" = 96 ; "97" = 97 ; "98" = 98 ; "99" = 99 ; };
  toDec = s:
    let
      n = builtins.stringLength s;
      m = builtins.div n 2;
    in builtins.foldl' (acc: p: acc * 100 + d2."${builtins.substring p 2 s}")
         0 (builtins.genList (i: i * 2) m);

  # ---- hex nibble -> value (for slot derivation) ----
  hexv = { "0" = 0 ; "1" = 1 ; "2" = 2 ; "3" = 3 ; "4" = 4 ; "5" = 5 ;
          "6" = 6 ; "7" = 7 ; "8" = 8 ; "9" = 9 ; "a" = 10 ; "b" = 11 ;
          "c" = 12 ; "d" = 13 ; "e" = 14 ; "f" = 15 ; };

  raw = builtins.readFile path;

  # ---- header fields ----
  magic = builtins.substring 0 4 raw;
  M     = toDec (builtins.substring 8 10 raw);   # table size (power of two)
  N     = toDec (builtins.substring 18 10 raw);  # entry count
  mask  = M - 1;
  D     = H + M * W;                             # data region start

  entryAt = s: H + s * W;
  digest  = key: builtins.hashString "sha256" key;

  # Linear-probing lookup.  fp/key are fixed for the whole probe; `left`
  # bounds the walk to M slots so a corrupt file can never loop forever.
  probe = fp: key: s0:
    let
      go = s: left:
        let
          e   = entryAt s;
          efp = builtins.substring e FP raw;
        in
          if left == 0 || efp == EMPTY then null
          else if efp != fp then go (builtins.bitAnd (s + 1) mask) (left - 1)
          else
            let
              ko   = toDec (builtins.substring (e + 16) 10 raw);
              kl   = toDec (builtins.substring (e + 26)  6 raw);
              key2 = builtins.substring (D + ko) kl raw;
            in
              if key2 != key
              then go (builtins.bitAnd (s + 1) mask) (left - 1)
              else
                let vl = toDec (builtins.substring (e + 32) 8 raw);
                in builtins.substring (D + ko + kl) vl raw;
    in go s0 M;

  lookup = key:
    let
      hd  = digest key;
      fp  = builtins.substring 0 FP hd;                       # fingerprint
      t   = builtins.substring 56 8 hd;                       # low 8 hex chars
      v32 = builtins.foldl' (a: i: a * 16 + hexv."${builtins.substring i 1 t}")
            0 (builtins.genList (i: i) 8);
      s0  = builtins.bitAnd v32 mask;                          # initial slot
    in probe fp key s0;

in
  assert magic == "NFK1";
  {
    # value string, or null if the key is absent
    get = key: lookup key;
    # value string, or `default` if the key is absent
    getOr = key: default:
      let v = lookup key;
      in if v == null then default else v;
    # true if the key is present
    has = key: lookup key != null;
    # number of stored entries
    count = N;
    # number of table slots (>= 2 * count)
    tableSize = M;
  }
