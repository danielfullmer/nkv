# nkv (dense hash, binary index) — fast static key/value lookup over a
# precomputed file, using only builtins.readFile, builtins.substring,
# builtins.hashString and integer math.
#
# Two access modes, one module:
#
#   import ./nkv.nix ./data/large.nkv              # a single nkv file
#   import ./nkv.nix { digits = 2; dir = ...; }    # a --shards directory
#
# Open addressing: one sha256-derived probe seed + linear probing over
# a power-of-two table at load <= 0.8. No fingerprints: every
# occupied slot is read and confirmed by a byte-for-byte
# key comparison. Each numeric field is 1-4 base-255 bytes (one byte
# per digit, byte = digit + 1, big-endian) so the file never contains
# NUL (the one byte Nix's readFile rejects). Field bytes are decoded
# via the static decode table `nkv-table.nix` (255-entry attrset, byte
# 1-char string -> digit). The table is a format constant shared by
# every nkv file — it is NOT carried in the database, and there is no
# generator — and is imported once per eval (Nix import cache), so it
# costs nothing per lookup.
#
# Layout (chars == bytes):
#   0..3    magic "NKV4"
#   4..6    N         3 base-255 bytes
#   7..10   M         4 base-255 bytes
#   11..13  entry field widths in bytes (base-255 digits):
#           keyOffW 1-4 | keyLenW 1-3 | valLenW 1-3;
#           entry width EW = keyOffW + keyLenW + valLenW (3-10)
#   14..    index region: M entries of EW bytes:
#           keyOff | keyLen | valLen at entry
#           offsets 0, keyOffW, keyOffW+keyLenW
#           (unused slot: EW bytes of 0x01; keyOff = 0 marks an unused
#           slot — a real keyOff is always >= 14 + EW*M)
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
# Sharded mode (build_nkv.py --shards): a directory of nkv files, one
# per shard slice of the key hash: <dir>/<h[24:24+d]>.nkv where
# h = sha256(key) in lowercase hex and d = digits (1 | 2 | 3 -> 16 |
# 256 | 4096 shards; must match --shards). The slice is disjoint from
# the probe-seed slice h[56:64], so sharding does not perturb the
# probing distribution. A lookup hashes the key once: the same digest
# supplies both the shard slice and the probe seed.
# valid nkv files with N = 0, M = 16), so a key always resolves to an
# existing file. Each shard file is read at most once per eval: the
# reader holds one shared `open` thunk per shard name, and Nix's
# call-by-need sharing forces a shard's readFile + header decode only
# on the first lookup that touches it (shards no key lands in are
# never read). `readFile` is not memoized by path — a fresh `open` per
# lookup would re-read the shard file every time — so the shared thunk
# is what makes repeated lookups on the same shard free. `count` reads
# every shard — offline use.
#
# See README.md for the format and benchmarks.

let
  H = 14;    # header width
  T0 = H;    # index region start
  B = 255;   # base-255 positional base (digit 0..254 -> byte 1..255)

  # Hex digits as a string, and the 2-char hex -> 0..255 pair table.
  # PAIR is built once per eval, lazily, from the digit string (this
  # Nix has no baseConvert / genString / parseInt), so decoding the
  # 8-char probe seed costs 4 table lookups instead of 8.
  HEXDIGITS = "0123456789abcdef";
  PAIR = builtins.listToAttrs (builtins.genList
    (i: { name = "${builtins.substring (builtins.div i 16) 1 HEXDIGITS}${builtins.substring (builtins.bitAnd i 15) 1 HEXDIGITS}";
          value = i; })
    256);

  # Hex string of even length -> int: v = v * 256 + pair, one PAIR
  # lookup per 2 chars.
  hexInt = s:
    builtins.foldl' (v: i: v * 256 + PAIR."${builtins.substring (i * 2) 2 s}")
      0
      (builtins.genList (i: i) (builtins.div (builtins.stringLength s) 2));

  # One nkv file (path) -> db with get/getDigest/getOr/getJson/
  # getOrJson/has/count/tableSize.
  open = path:
  let
    raw = builtins.readFile path;

    # Static decode table (format constant): byte 1-char string -> digit.
    table = import ./nkv-table.nix;

    # 1 byte at absolute position p -> int value (0..254)
    byte = p: table."${builtins.substring p 1 raw}";

    # 3 bytes -> value < 255^3 (lengths, counts)
    dec3 = p: (byte p * B + byte (p + 1)) * B + byte (p + 2);

    # 4 bytes -> value < 255^4 (offsets, table size)
    dec4 = p: ((byte p * B + byte (p + 1)) * B + byte (p + 2)) * B + byte (p + 3);

    # Header fields.
    n = dec3 4;
    m = dec4 7;

    # Entry field widths, stored once per file in the header at
    # offsets 11..13; each is a base-255 digit == width in bytes
    # (byte = digit + 1), so `byte` returns the width directly.
    koffW = byte 11;
    klenW = byte 12;
    vlenW = byte 13;

    # Width-selecting decoder: one specialized thunk per width (1-4
    # base-255 bytes), chosen once at import so the probe hot path stays
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
      probe key
        (hexInt (builtins.substring 56 8 (builtins.hashString "sha256" key))) 0;

    # get with a caller-supplied sha256 hex digest: the sharded reader
    # hashes once per lookup and passes the same digest as both the
    # shard slice and the probe seed (the probe masks the seed to the
    # table width at step 0).
    getDigest = key: h:
      probe key (hexInt (builtins.substring 56 8 h)) 0;
  in
  assert (builtins.substring 0 4 raw) == "NKV4";
  assert koffW >= 1 && koffW <= 4
    && klenW >= 1 && klenW <= 3 && vlenW >= 1 && vlenW <= 3;
  let
  in
  {
    get = get;
    getDigest = getDigest;

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
  };

  # A --shards directory -> db. args: { digits ? 2, dir } — digits
  # 1 | 2 | 3 -> 16 | 256 | 4096 shards, must match the builder's
  # --shards.
  #
  # A shard file is read at most once per eval: `dbs` holds one
  # unforced `open` thunk per shard name, and Nix's call-by-need
  # sharing forces a shard's readFile + header decode on the first
  # lookup that touches it — every later lookup reuses the same value,
  # and shards no key lands in are never read. Nix has no readFile
  # content cache (a fresh `open` per lookup would re-read the shard
  # file every time), so the shared thunk is the caching mechanism. A
  # lookup hashes the key once: the same digest supplies both the
  # shard slice and the probe seed.
  sharded = { digits ? 2, dir }:
  let
    # key -> shard slice, e.g. "3f". Same computation as the builder's
    # shard_slice (sha256 hex chars 24..24+digits).
    shard = key:
      builtins.substring 24 digits (builtins.hashString "sha256" key);

    # key -> shard file path.
    file = key: dir + "/${shard key}.nkv";

    # All 2^digits shard names: every lowercase-hex string of length
    # `digits` ("0".."f" | "00".."ff" | "000".."fff"), built by
    # prefixing each hex char to the shorter names (this Nix has no
    # baseConvert / genString).
    hexNames = d:
      if d == 0 then [ "" ]
      else builtins.concatMap
        (c: builtins.map (s: c + s) (hexNames (d - 1)))
        (builtins.genList (i: builtins.substring i 1 "0123456789abcdef") 16);

    # One unforced `open` thunk per shard name, shared for the whole
    # eval: building this attrset is O(2^digits) thunks (sub-ms for
    # 4096); nothing is forced until a shard is first touched.
    dbs = builtins.listToAttrs (builtins.map
      (name: { inherit name;
                value = open (dir + "/${name}.nkv"); })
      (hexNames digits));
    # One sha256 per lookup: the digest feeds both the shard slice
    # h[24:24+d] and the probe seed (via getDigest). The get family
    # lives in the let (not the result attrset) so getOr/getJson/
    # getOrJson/has can build on get.
    get = key:
      let h = builtins.hashString "sha256" key;
      in (dbs."${builtins.substring 24 digits h}").getDigest key h;
    getOr = key: default:
      let v = get key;
      in if v == null then default else v;
    getJson = key:
      let v = get key;
      in if v == null then null else builtins.fromJSON v;
    getOrJson = key: default:
      let v = get key;
      in if v == null then default else builtins.fromJSON v;
    has = key: get key != null;

  in
  assert digits == 1 || digits == 2 || digits == 3;
  {
    # key -> shard slice (debug / shard-size checks).
    shard = shard;
    # key -> shard file path (debug).
    file = file;

    # key -> value (or null): one sha256 per lookup, shard slice and
    # probe seed from the same digest (defined in the let above).
    inherit get getOr getJson getOrJson has;

    # Expensive: forces the open of every shard (reads every file).
    # Offline use only — e.g. verifying the total key count in a
    # correctness run.
    count = builtins.foldl' (a: d: a + d.count) 0 (builtins.attrValues dbs);
  };
in
# A path is a single nkv file; an attrset { digits, dir } is a
# --shards directory.
arg: if builtins.isAttrs arg then sharded arg else open arg
