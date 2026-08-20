# NKB v2 (binary index) — fast static key/value lookup over a precomputed
# file, using only builtins.readFile and builtins.substring (plus integer
# math and the bytewise string comparison operator <).
#
# Same algorithm as NKB v1 (keys sorted bytewise, binary search, no
# hashing), but the index is raw binary: each numeric field is 3-4 "b254"
# bytes (one byte per digit, byte = digit + 1, big-endian). Because every
# byte in the file is 0x01..0xFF, the file never contains NUL — the only
# byte Nix's readFile rejects.
#
# Nix has no byte->int builtin, and .nix source cannot express raw high
# bytes (invalid UTF-8 literals mangle to U+FFFD). NKB v1 worked around
# that with base-255 ASCII digits and a 255-entry pair table inlined in
# source. NKB v2 instead carries the byte table in the file itself (255
# bytes 0x01..0xFF at offset 64) and builds the byte->int attrset at
# import time from it.
#
# Layout (chars == bytes):
#   0..3    magic "NKB2"
#   4..6    N         3 b254 bytes
#   7..9    keyTotal  3 b254 bytes
#   10..12  valTotal  3 b254 bytes
#   13..63  reserved (spaces)
#   64..318 byte table T: 0x01 .. 0xFF
#   319..   N entries of 14 bytes: off_k 4 | len_k 3 | off_v 4 | len_v 3
#   then    all keys (sorted), then all values (same order); offsets are
#           absolute from the start of the file.
#
# See README.md and REPORT.md for the format and benchmarks.

let
  H = 64;    # header width
  T0 = 319;  # index region start (H + 255-byte table)
  W = 14;    # index entry width
  B = 254;   # b254 base (digit 0..253 -> byte 1..254)
in
db:
  let
    raw = builtins.readFile db;

    # Byte table carried in the file: T[i] is the byte whose value is i+1.
    # Built into an attrset once at import; no high byte ever appears in
    # .nix source.
    T = builtins.substring H 255 raw;
    table =
      builtins.foldl' (t: i: t // { "${builtins.substring i 1 T}" = i; })
      {}
      (builtins.genList (i: i) 255);

    # 1 byte at absolute position p -> int value (0..253)
    byte = p: table."${builtins.substring p 1 raw}";

    # 3 bytes -> value < 254^3 (lengths, counts)
    dec3 = p: (byte p * B + byte (p + 1)) * B + byte (p + 2);

    # 4 bytes -> value < 254^4 (offsets)
    dec4 = p: ((byte p * B + byte (p + 1)) * B + byte (p + 2)) * B + byte (p + 3);

    # Entry layout (14 bytes): off_k 4 | len_k 3 | off_v 4 | len_v 3.
    n = dec3 4;
    keyTotal = dec3 7;
    valTotal = dec3 10;

    keyAt = i:
      let
        e = T0 + W * i;
        off = dec4 e;
      in builtins.substring off (dec3 (e + 4)) raw;

    valueAt = i:
      let
        e = T0 + W * i;
        off = dec4 (e + 7);
      in builtins.substring off (dec3 (e + 11)) raw;

    # Binary search: keys are strictly ascending in byte order.
    search = key: lo: hi:
      if lo >= hi then null
      else
        let
          m = (lo + hi) / 2;   # integer division truncates
          km = keyAt m;
        in
          if km < key then search key (m + 1) hi
          else if km == key then valueAt m
          else search key lo m;
  in
  assert (builtins.substring 0 4 raw) == "NKB2";
  assert (builtins.stringLength T) == 255;
  assert (builtins.stringLength raw) == T0 + W * n + keyTotal + valTotal;
  let
    get = key: search key 0 n;   # value or null
  in
  {
    get = get;
    getOr = key: default:
      let v = get key;
      in if v == null then default else v;
    has = key: get key != null;
    count = n;
  }