# NFK v3 (dense hash, binary index) — fast static key/value lookup over a
# precomputed file, using only builtins.readFile, builtins.substring,
# builtins.hashString and integer math.
#
# Open addressing: one sha256-derived probe seed + linear probing over
# a power-of-two table at load <= 0.8. No fingerprints (removed in
# rev 6): every occupied slot is read and confirmed by a byte-for-byte
# key comparison. Each numeric field is 1-4 "b254" bytes (one byte
# per digit, byte = digit + 1, big-endian) so the file never contains
# NUL (the one byte Nix's readFile rejects). Field bytes are decoded
# via the static decode table `nfd3-table.nix` (255-entry attrset, byte
# 1-char string -> digit). The table is a format constant shared by
# every NFK v3 file — it is NOT carried in the database — and is
# imported once per eval (Nix import cache), so it costs nothing per
# lookup. Generate it with
# `python3 build_db3.py --write-table nfd3-table.nix`.
#
# `db` is the path (or string path) of one NFK v3 file.
#
# Layout (chars == bytes):
#   0..3    magic "NFK3"
#   4..6    N         3 b254 bytes
#   7..10   M         4 b254 bytes
#   11      format revision byte: '6'
#   12      reserved: always 0x01 (b254 digit 0) — the former fpW;
#           asserted 0 at import
#   13..15  entry field widths in bytes (b254 digits):
#           keyOffW 1-4 | keyLenW 1-3 | valLenW 1-3;
#           entry width EW = keyOffW + keyLenW + valLenW (3-10)
#   16..    M entries of EW bytes: keyOff | keyLen | valLen at entry
#           offsets 0, keyOffW, keyOffW+keyLenW
#           (unused slot: EW bytes of 0x01; keyOff = 0 marks an unused
#           slot — a real keyOff is always >= 16 + EW*M)
#   then    data region: for each key in insertion order, the key's
#           bytes followed immediately by the value's bytes; keyOff
#           is absolute from the file start, the value offset is
#           keyOff + keyLen.
#
# Slot s0 = int(h[56:64], 16) AND (M-1) for h = sha256(key) hex;
# linear probe, wrap with bitAnd, bounded by M steps (load < 1
# guarantees an unused slot is reached). A walk ends at a key match
# or an unused slot — every occupied slot is read and compared
# key-by-key.
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

    # 4 bytes -> value < 254^4 (offsets, table size)
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

    # Entry field widths, stored once per file in the header at
    # offsets 13..15; each is a b254 digit == width in bytes
    # (byte = digit + 1), so `byte` returns the width directly.
    # Offset 12 is reserved (always b254 digit 0 — the former fpW;
    # fingerprints removed in rev 6) and asserted 0 below.
    reserved = byte 12;
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

    dKO = decW koffW;
    dKL = decW klenW;
    dVL = decW vlenW;

    # Entry geometry, recomputed from the header (not a format constant).
    EW = koffW + klenW + vlenW;
    OKO = 0;
    OKL = koffW;
    OVL = koffW + klenW;

    # Probe: at most m steps (bounded; load < 1 guarantees an unused
    # slot). Unused slot (keyOff 0) -> null; every occupied slot is read
    # and compared by key.
    probe = key: s0: i:
      let
        s = builtins.bitAnd (s0 + i) (m - 1);
        e = T0 + EW * s;
        koff = dKO e;   # OKO = 0
      in
        if koff == 0 then null
        else
          let
            klen = dKL (e + OKL);
            k = builtins.substring koff klen raw;
          in
            if k == key
            then builtins.substring (koff + klen) (dVL (e + OVL)) raw
            else probe key s0 (i + 1);

    get = key:
      let
        h = builtins.hashString "sha256" key;
        s0 = builtins.bitAnd (hexInt (builtins.substring 56 8 h)) (m - 1);
      in
        probe key s0 0;
  in
  assert (builtins.substring 0 4 raw) == "NFK3";
  assert (builtins.substring 11 1 raw) == "6";
  assert reserved == 0 && koffW >= 1 && koffW <= 4
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