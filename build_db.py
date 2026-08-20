#!/usr/bin/env python3
"""build_db.py — build an NFK v1 key/value database from a JSON object.

Input  : a JSON object {"key": "value", ...} (string keys, string values).
Output : a binary .nfd file consumable by kv.nix.

Only the standard library is used.  The hash/slot/fingerprint scheme here
MUST match kv.nix exactly:

    h  = sha256(key-utf8).hexdigest()        # 64 lowercase hex chars
    fp = h[:16]                              # 16-char fingerprint
    s0 = int(h[-8:], 16) & (M - 1)           # initial slot (low 32 bits)
    linear probing with +1 (mod M) on collision

File layout (NFK v1):
    header   64 bytes
    index    M * 40 bytes   (one entry per slot)
    data     variable       (concatenated key bytes ++ value bytes)

Usage:
    build_db.py INPUT.json OUTPUT.nfd [--check]
"""
import argparse
import hashlib
import json
import sys

MAGIC = b"NFK1"
VERSION = b"01"
ALGO = b"sh"          # sha256
H = 64                # header length
W = 40                # index entry width
FP = 16               # fingerprint width
EMPTY = b"g" * FP     # unused-slot fingerprint ('g' is not a hex digit)

# fixed field widths (must match kv.nix substring offsets)
W_MOFF = 8            # M starts at offset 8
W_NOFF = 18           # N starts at offset 18
W_MLEN = 10
W_NLEN = 10
E_FP = 0              # within an entry
E_KO = 16
E_KL = 26
E_VL = 32
W_KO = 10
W_KL = 6
W_VL = 8


def next_pow2(n):
    p = 1
    while p < n:
        p <<= 1
    return p


def build(pairs, m_factor=2):
    """pairs: list of (key, value). Returns (blob, M, N, table, data)."""
    n = len(pairs)
    m = next_pow2(max(16, m_factor * n))
    mask = m - 1
    table = [None] * m
    data_parts = []
    off = 0
    for k, v in pairs:
        kb = k.encode("utf-8")
        vb = v.encode("utf-8")
        if len(kb) > 10 ** 6 - 1 or len(vb) > 10 ** 8 - 1 or off > 10 ** 10 - 1:
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

    # index (M * 40 bytes)
    idx = bytearray()
    for s in range(m):
        e = table[s]
        if e is None:
            idx += EMPTY + b"0" * W_KO + b"0" * W_KL + b"0" * W_VL
        else:
            fp, ko, kl, vl = e
            idx += fp.encode()
            idx += f"{ko:0{W_KO}d}".encode()
            idx += f"{kl:0{W_KL}d}".encode()
            idx += f"{vl:0{W_VL}d}".encode()
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
        e = d0 = H + s * W
        fp = b[e + E_FP:e + E_FP + FP].decode()
        if fp == EMPTY.decode():
            continue
        ko = int(b[e + E_KO:e + E_KO + W_KO])
        kl = int(b[e + E_KL:e + E_KL + W_KL])
        vl = int(b[e + E_VL:e + E_VL + W_VL])
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
    ap.add_argument("--m-factor", type=int, default=2,
                    help="table size ~= m_factor * n, rounded up to a power of two (default 2)")
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