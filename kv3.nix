# NFK v3 (dense hash, binary index) — fast static key/value lookup over a
# precomputed file, using only builtins.readFile, builtins.substring,
# builtins.hashString and integer math.
#
# Open addressing: sha256 fingerprint + linear probing over a power-of-two
# table at load <= 0.8. Each numeric field is 1-4 "b254" bytes (one byte
# per digit, byte = digit + 1, big-endian) so the file never contains NUL
# (the one byte Nix's readFile rejects). Field bytes are decoded via the
# static decode table `nfd3-table.nix` (255-entry attrset, byte 1-char
# string -> digit). The table is a format constant shared by every NFK v3
# file — it is NOT carried in the database — and is imported once per eval
# (Nix import cache), so it costs nothing per lookup. Generate it with
# `python3 build_db3.py --write-table nfd3-table.nix`.
#
# `db` is the path (or string path) of one NFK v3 file.
#
# Layout (chars == bytes):
#   0..3    magic "NFK3"
#   4..6    N         3 b254 bytes
#   7..10   M         4 b254 bytes
#   11      format revision byte: '5'
#   12..15  entry field widths in bytes (b254 digits):
#           fpW 1-4 | keyOffW 1-4 | keyLenW 1-3 | valLenW 1-3;
#           entry width EW = fpW + keyOffW + keyLenW + valLenW (7-14)
#   16..    M entries of EW bytes: fp | keyOff | keyLen | valLen at
#           entry offsets 0, fpW, fpW+keyOffW, fpW+keyOffW+keyLenW
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
  H = 16;    # header width
  T0 = H;    # index region start
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

    # Static decode table (format constant): byte 1-char string -> digit.
    table = import ./nfd3-table.nix;

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

    # Entry field widths, stored once per file in the header
    # at offsets 12..15; each is a b254 digit == width in bytes
    # (byte = digit + 1), so `byte` returns the width directly.
    fpW = byte 12;
    koffW = byte 13;
    klenW = byte 14;
    vlenW = byte 15;

    # Width-selecting decoder: one specialized thunk per width (1-4
    # b254 bytes), chosen once at import so the probe hot path stays
    # plain integer math (no genList/fold per field).
    decW = w:
      if w == 1 then (p: byte p)
      else if w == 2 then (p: byte p * B + byte (p + 1))
      else if w == 3 then (p: (byte p * B + byte (p + 1)) * B + byte (p + 2))
      else (p: ((byte p * B + byte (p + 1)) * B + byte (p + 2)) * B + byte (p + 3));

    dFP = decW fpW;
    dKO = decW koffW;
    dKL = decW klenW;
    dVL = decW vlenW;

    # Entry geometry, recomputed from the header (not a format constant).
    EW = fpW + koffW + klenW + vlenW;
    OKO = fpW;
    OKL = fpW + koffW;
    OVL = fpW + koffW + klenW;

    # Probe: at most m steps (bounded; an unused slot must exist).
    # Unused slot (fp 0) -> null. Fingerprint hit -> confirm key bytes.
    probe = key: fp: s0: i:
      let
        s = builtins.bitAnd (s0 + i) (m - 1);
        e = T0 + EW * s;
        efp = dFP e;
      in
        if efp == 0 then null
        else if efp != fp then probe key fp s0 (i + 1)
        else
          let
            koff = dKO (e + OKO);
            klen = dKL (e + OKL);
            k = builtins.substring koff klen raw;
          in
            if k == key
            then builtins.substring (koff + klen) (dVL (e + OVL)) raw
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
  assert (builtins.substring 11 1 raw) == "5";
  assert fpW >= 1 && fpW <= 4 && koffW >= 1 && koffW <= 4
    && klenW >= 1 && klenW <= 3 && vlenW >= 1 && vlenW <= 3;
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