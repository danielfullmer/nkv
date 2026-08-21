# NFK v3 (dense hash, binary index) — fast static key/value lookup over a
# precomputed file, using only builtins.readFile and builtins.substring
# (plus builtins.hashString and integer math).
#
# NFK v1/v2 algorithm (sha256 fingerprint + linear probing over a
# power-of-two table at load <= 0.8), but with NKB v2's binary index
# machinery: each numeric field is 3-4 "b254" bytes (one byte per digit,
# byte = digit + 1, big-endian) and the byte->int decode table is carried
# in the file itself (255 bytes 0x01..0xFF at offset 64), folded into an
# attrset at import time. No high byte ever appears in .nix source.
#
# Layout (chars == bytes):
#   0..3    magic "NFK3"
#   4..6    N         3 b254 bytes
#   7..10   M         4 b254 bytes
#   11..13  keyTotal  3 b254 bytes
#   14..16  valTotal  3 b254 bytes
#   17..63  reserved (spaces)
#   64..318 byte table T: 0x01 .. 0xFF
#   319..   M entries of 15 bytes: fp 4 | keyOff 4 | keyLen 3 | valLen 3
#   then    data region: for each key in insertion order, the key's bytes
#           followed immediately by the value's bytes; keyOff is absolute
#           from the file start, the value offset is keyOff + keyLen.
#
# fp = int(sha256(key) hex [0:6]) + 1 (24-bit; 0 marks an unused slot).
# Slot s0 = int(h[56:64], 16) AND (M-1); linear probe, wrap with bitAnd.
# A fingerprint hit is confirmed by byte-for-byte key comparison, so a
# fingerprint collision can only add a key read, never a wrong value.
# Values are opaque bytes; when a value holds a JSON document,
# getJson/getOrJson return builtins.fromJSON of it (a miss is null).
#
# See README.md and REPORT.md for the format and benchmarks.

let
  H = 64;    # header width
  T0 = 319;  # index region start (H + 255-byte table)
  W = 15;    # index entry width
  B = 254;   # b254 base (digit 0..253 -> byte 1..254)

  # hex char -> value; sha256 digests are lowercase hex, ASCII only
  HEX = {
    "0" = 0; "1" = 1; "2" = 2; "3" = 3; "4" = 4; "5" = 5;
    "6" = 6; "7" = 7; "8" = 8; "9" = 9;
    "a" = 10; "b" = 11; "c" = 12; "d" = 13; "e" = 14; "f" = 15;
  };
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

    # 4 bytes -> value < 254^4 (offsets, fingerprint, table size)
    dec4 = p: ((byte p * B + byte (p + 1)) * B + byte (p + 2)) * B + byte (p + 3);

    # hex string -> int (no builtins.parseInt; one HEX lookup per char)
    hexInt = s: let
      L = builtins.stringLength s;
    in builtins.foldl' (v: i: v * 16 + HEX."${builtins.substring i 1 s}")
      0
      (builtins.genList (i: i) L);

    # Header fields.
    n = dec3 4;
    m = dec4 7;
    keyTotal = dec3 11;
    valTotal = dec3 14;

    # Probe: at most m steps (bounded; an unused slot must exist).
    # Unused slot (fp 0) -> null. Fingerprint hit -> confirm key bytes.
    probe = key: fp: s0: i:
      let
        s = builtins.bitAnd (s0 + i) (m - 1);
        e = T0 + W * s;
        efp = dec4 e;
      in
        if efp == 0 then null
        else if efp != fp then probe key fp s0 (i + 1)
        else
          let
            koff = dec4 (e + 4);
            klen = dec3 (e + 8);
            k = builtins.substring koff klen raw;
          in
            if k == key
            then builtins.substring (koff + klen) (dec3 (e + 11)) raw
            else probe key fp s0 (i + 1);

    get = key:
      let
        h = builtins.hashString "sha256" key;
      in
        probe key
          (hexInt (builtins.substring 0 6 h) + 1)
          (builtins.bitAnd (hexInt (builtins.substring 56 8 h)) (m - 1))
          0;
  in
  assert (builtins.substring 0 4 raw) == "NFK3";
  assert (builtins.stringLength T) == 255;
  assert (builtins.stringLength raw) == T0 + W * m + keyTotal + valTotal;
  let
  in
  {
    get = get;

    # JSON mode: values are opaque; when a value holds a JSON document,
    # getJson/getOrJson return builtins.fromJSON of the stored string
    # (a miss is still null).
    getJson = key:
      let v = get key;
      in if v == null then null else builtins.fromJSON v;

    getOrJson = key: default:
      let v = get key;
      in if v == null then default else builtins.fromJSON v;
    getOr = key: default:
      let v = get key;
      in if v == null then default else v;
    has = key: get key != null;
    count = n;
    tableSize = m;
  }