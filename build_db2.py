#!/usr/bin/env python3
"""build_db2.py — build an NFK v2 (dense) key/value database from a JSON object.

Same hash/linear-probe scheme as NFK v1 (sha256 fingerprint + probe), but a
much smaller index region:

    M = next_pow2(max(16, ceil(1.25 * n)))     # load factor <= 0.8
    entry = 22 chars:  fp 8 hex | keyOff 6 base-36 | keyLen 4 | valLen 4

Why 22 chars per entry (vs 40 in v1):
  - fp 8 hex (32 bits) instead of 16: a fp collision only forces one extra
    key read during a probe — the key compare is authoritative, so lookup
    stays correct.  Expected false fp matches in a 200k table ~ 5.
  - base-36 digits instead of decimal: 6 base-36 digits cover 2.18 GB of
    data region (10 decimal digits covered 10 GB); 4 base-36 digits cover
    1.68 MB of key/value length (vs 1M / 100M decimal).

The data region is byte-identical to NFK v1 for the same input (same
insertion order), so the whole savings is the index: 22*M instead of 40*M,
and M halved for large tables.

Only the standard library is used.  The hash/slot/fingerprint scheme here
MUST match kv2.nix exactly:

    h  = sha256(key-utf8).hexdigest()        # 64 lowercase hex chars
    fp = h[:8]                               # 8-char fingerprint
    s0 = int(h[-8:], 16) & (M - 1)           # initial slot (low 32 bits)
    linear probing with +1 (mod M) on collision

File layout (NFK v2):
    header   64 bytes
        [0..4)  magic "NFK2"   [4..6) version "02"   [6..8) algo "sh"
        [8..18)  M  10-digit decimal (power of two)
        [18..28) N  10-digit decimal
        [28..64) reserved (spaces)
    index    M * 22 bytes   (one entry per slot)
        [0..8)   fp      first 8 hex chars of sha256(key); "g"*8 if unused
        [8..14)  keyOff  6 base-36 digits, offset into the data region
        [14..18) keyLen  4 base-36 digits, byte length of key
        [18..22) valLen  4 base-36 digits, byte length of value
        (value offset is keyOff + keyLen — not stored)
    data     variable       (concatenated key bytes ++ value bytes)

Usage:
    build_db2.py INPUT.json OUTPUT.nfd2 [--check]
"""
import argparse
import hashlib
import json
import math
import sys

MAGIC = b"NFK2"
VERSION = b"02"
ALGO = b"sh"          # sha256
H = 64                # header length
W = 22                # index entry width
FP = 8                # fingerprint width (hex chars)
EMPTY = b"g" * FP     # unused-slot fingerprint ('g' is not a hex digit)

B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
MAX_DATA = 36 ** 6    # 2,176,782,336 (~2.18 GB)
MAX_LEN = 36 ** 4     # 1,679,616 (~1.68 MB)

# fixed field widths (must match kv2.nix substring offsets)
W_MOFF = 8
W_NOFF = 18
W_MLEN = 10
W_NLEN = 10
E_FP = 0              # within an entry
E_KO = 8
E_KL = 14
E_VL = 18
W_KO = 6
W_KL = 4
W_VL = 4


def next_pow2(n):
    p = 1
    while p < n:
        p <<= 1
    return p


def b36(x, width):
    """x as a big-endian base-36 string of exactly `width` digits (0..36**width-1)."""
    if x < 0 or x >= 36 ** width:
        raise ValueError(f"value {x} does not fit in {width} base-36 digits")
    s = ""
    for _ in range(width):
        x, r = divmod(x, 36)
        s = B36[r] + s
    return s


def build(pairs, m_factor=1.25):
    """pairs: list of (key, value). Returns (blob, M, N, table, data)."""
    n = len(pairs)
    m = next_pow2(max(16, math.ceil(m_factor * n)))
    mask = m - 1
    table = [None] * m
    data_parts = []
    off = 0
    for k, v in pairs:
        kb = k.encode("utf-8")
        vb = v.encode("utf-8")
        if len(kb) >= MAX_LEN or len(vb) >= MAX_LEN or off + len(kb) + len(vb) >= MAX_DATA:
            raise ValueError(f"field too wide for key {k!r}")
        ko = off
        off += len(kb) + len(vb)
        h = hashlib.sha256(kb).hexdigest()
        fp = h[:FP]
        s = int(h[-8:], 16) & mask
        while table[s] is not None:
            s = (s + 1) & mask
        table[s] = (fp, ko, len(kb), len(vb))
        data_parts.append(kb)
        data_parts.append(vb)
    data = b"".join(data_parts)

    # header (64 bytes)
    hdr = MAGIC + VERSION + ALGO
    hdr += f"{m:0{W_MLEN}d}".encode()
    hdr += f"{n:0{W_NLEN}d}".encode()
    hdr += b" " * (H - len(hdr))
    assert len(hdr) == H, len(hdr)

    # index (M * 22 bytes)
    idx = bytearray()
    for s in range(m):
        e = table[s]
        if e is None:
            idx += EMPTY + b"0" * W_KO + b"0" * W_KL + b"0" * W_VL
        else:
            fp, ko, kl, vl = e
            idx += fp.encode()
            idx += b36(ko, W_KO).encode()
            idx += b36(kl, W_KL).encode()
            idx += b36(vl, W_VL).encode()
    assert len(idx) == m * W

    blob = hdr + bytes(idx) + data
    return blob, m, n, table, data


def read_db(path):
    """Independent parser: returns (M, N, {slot: (fp, ko, kl, vl)}, data)."""
    with open(path, "rb") as f:
        b = f.read()
    assert b[:4] == MAGIC, "bad magic"
    m = int(b[W_MOFF:W_MOFF + W_MLEN])
    n = int(b[W_NOFF:W_NOFF + W_NLEN])
    d = H + m * W
    table = {}
    for s in range(m):
        e = H + s * W
        fp = b[e + E_FP:e + E_FP + FP].decode()
        if fp == EMPTY.decode():
            continue
        ko = int(b[e + E_KO:e + E_KO + W_KO], 36)
        kl = int(b[e + E_KL:e + E_KL + W_KL], 36)
        vl = int(b[e + E_VL:e + E_VL + W_VL], 36)
        table[s] = (fp, ko, kl, vl)
    return m, n, table, b[d:]


def lookup_parsed(m, table, data, key):
    """Lookup using an already-parsed db (see read_db)."""
    kb = key.encode("utf-8")
    h = hashlib.sha256(kb).hexdigest()
    fp = h[:FP]
    s = int(h[-8:], 16) & (m - 1)
    for _ in range(m):
        e = table.get(s)
        if e is None:
            return None
        efp, ko, kl, vl = e
        if efp != fp:
            s = (s + 1) & (m - 1)
            continue
        if data[ko:ko + kl] != kb:
            s = (s + 1) & (m - 1)
            continue
        return data[ko + kl:ko + kl + vl].decode("utf-8")
    return None


def python_get(path, key):
    """Convenience: parse + lookup for a single key."""
    m, n, table, data = read_db(path)
    return lookup_parsed(m, table, data, key)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--m-factor", type=float, default=1.25,
                    help="table size ~= m_factor * n, rounded up to a power of two (default 1.25)")
    ap.add_argument("--check", action="store_true",
                    help="re-read the produced file and verify every key round-trips")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        sys.exit("input must be a JSON object of key -> string value")
    for k, v in obj.items():
        if not isinstance(v, str):
            sys.exit(f"value for key {k!r} must be a string, got {type(v).__name__}")
    pairs = list(obj.items())

    blob, m, n, _, data = build(pairs, args.m_factor)
    with open(args.output, "wb") as f:
        f.write(blob)
    print(f"built {args.output}: n={n} M={m} data={len(data)}B total={len(blob)}B "
          f"load={n / m:.2f}")

    if args.check:
        m2, n2, table, data = read_db(args.output)
        bad = 0
        for k, v in obj.items():
            got = lookup_parsed(m2, table, data, k)
            if got != v:
                bad += 1
                if bad <= 5:
                    print(f"  MISMATCH {k!r}: want {v!r} got {got!r}")
        # a guaranteed-missing key
        miss = "___definitely_not_present___"
        if lookup_parsed(m2, table, data, miss) is not None:
            bad += 1
            print(f"  MISMATCH miss key {miss!r} should be None")
        if bad:
            sys.exit(f"CHECK FAILED: {bad} mismatches")
        print(f"check OK: {n}/{n} keys round-trip, miss->None")


if __name__ == "__main__":
    main()