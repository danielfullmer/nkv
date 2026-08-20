#!/usr/bin/env python3
"""Build NKB v2 (binary index) databases from JSON key/value files.

Usage: python3 build_db_bin2.py input.json output.nkb2 [--check]

NKB v2 layout (all offsets absolute, chars == bytes):
  0..3     magic "NKB2"
  4..6     N         (3 b254 bytes)
  7..9     keyTotal  (3 b254 bytes)
  10..12   valTotal  (3 b254 bytes)
  13..63   spaces (header is 64 bytes)
  64..318  byte table T: the 255 bytes 0x01 .. 0xFF, in order
  319..    N entries of 14 bytes: off_k 4 | len_k 3 | off_v 4 | len_v 3
  then     all keys concatenated (sorted bytewise by UTF-8 bytes),
           then all values concatenated (same order).

Field encoding ("b254"): each byte of a field is one digit plus one
(digit in 0..253 -> byte in 1..254, big-endian digits), so every byte in
the whole file is in 0x01..0xFF and the file never contains NUL (the one
byte Nix's readFile rejects). The Nix side has no byte->int builtin, so
kv_bin2.nix builds a lookup table at import time from the embedded T:
T[i] (the byte of value i+1) is keyed by that byte's own 1-char string,
giving int value = table."${s[p:p+1]}".

Limits (builder-enforced): N, keyTotal, valTotal, and per-key/value
lengths < 254^3; file offsets < 254^4.

JSON input: an object; duplicate keys keep the last (dict semantics,
matching fromJSON).
"""
import argparse
import json
import sys

MAGIC = b"NKB2"
H = 64
TBL = bytes(range(1, 256))          # 255-byte byte table, T[i] = i+1
T0 = H + len(TBL)                   # index region start (319)
EW = 14                             # entry width
FO = 4                              # offset field width
FL = 3                              # length/count field width
B = 254
MAX_FIELD3 = B ** FL - 1            # 16,387,063
MAX_FIELD4 = B ** FO - 1            # 4,162,314,255


def enc(v, digits):
    """Encode v as `digits` b254 bytes (big-endian digits, byte = d+1)."""
    if v < 0 or v >= B ** digits:
        raise ValueError(f"value {v} out of range for {digits} b254 bytes")
    out = bytearray()
    for i in range(digits - 1, -1, -1):
        d = (v // B ** i) % B
        out.append(d + 1)
    return bytes(out)


def dec(b, digits):
    """Inverse of enc."""
    v = 0
    for x in b[:digits]:
        v = v * B + (x - 1)
    return v


def build(pairs):
    """pairs: iterable of (key, value) str pairs. Returns file bytes."""
    items = sorted(pairs, key=lambda kv: kv[0].encode("utf-8"))
    n = len(items)
    keys = b"".join(k.encode("utf-8") for k, _ in items)
    vals = b"".join(v.encode("utf-8") for _, v in items)
    if n > MAX_FIELD3:
        raise ValueError("too many entries")
    if len(keys) > MAX_FIELD3 or len(vals) > MAX_FIELD3:
        raise ValueError("key/value data region too large")
    header = MAGIC + enc(n, FL) + enc(len(keys), FL) + enc(len(vals), FL)
    header += b" " * (H - len(header))
    assert len(header) == H
    idx = bytearray()
    ko = T0 + EW * n
    vo = ko + len(keys)
    for k, v in items:
        kb, vb = k.encode("utf-8"), v.encode("utf-8")
        if len(kb) > MAX_FIELD3 or len(vb) > MAX_FIELD3:
            raise ValueError(f"key or value too long: {len(kb)}, {len(vb)}")
        idx += enc(ko, FO)
        idx += enc(len(kb), FL)
        idx += enc(vo, FO)
        idx += enc(len(vb), FL)
        ko += len(kb)
        vo += len(vb)
    blob = header + TBL + bytes(idx) + keys + vals
    if len(blob) > MAX_FIELD4:
        raise ValueError("file too large for 4-byte b254 offsets")
    return blob


def parse(path):
    """Independent parser: (n, [(key, value)]) decoded from the file."""
    data = open(path, "rb").read()
    if data[:4] != MAGIC:
        raise ValueError(f"bad magic: {data[:4]!r}")
    n = dec(data[4:7], FL)
    key_total = dec(data[7:10], FL)
    val_total = dec(data[10:13], FL)
    if data[13:H] != b" " * (H - 13):
        raise ValueError("header reserved region not spaces")
    if data[H:H + len(TBL)] != TBL:
        raise ValueError("embedded byte table is not 0x01..0xFF")
    if 0 in data:
        raise ValueError("file contains NUL (invalid for Nix readFile)")
    k0 = T0 + EW * n
    if len(data) != k0 + key_total + val_total:
        raise ValueError(
            f"size mismatch: file {len(data)}, "
            f"expected {k0 + key_total + val_total}"
        )
    out = []
    for i in range(n):
        e = T0 + EW * i
        off_k = dec(data[e:e + FO], FO)
        len_k = dec(data[e + FO:e + FO + FL], FL)
        off_v = dec(data[e + FO + FL:e + 2 * FO + FL], FO)
        len_v = dec(data[e + 2 * FO + FL:e + EW], FL)
        out.append(
            (
                data[off_k:off_k + len_k].decode("utf-8"),
                data[off_v:off_v + len_v].decode("utf-8"),
            )
        )
    return n, out


def python_get_data(data, key):
    """Binary search over in-memory file bytes."""
    n = dec(data[4:7], FL)
    kb = key.encode("utf-8")

    def kbytes(i):
        e = T0 + EW * i
        off_k = dec(data[e:e + FO], FO)
        len_k = dec(data[e + FO:e + FO + FL], FL)
        return data[off_k:off_k + len_k]

    lo, hi = 0, n
    while lo < hi:
        m = (lo + hi) // 2
        k = kbytes(m)
        if k < kb:
            lo = m + 1
        elif k > kb:
            hi = m
        else:
            e = T0 + EW * m
            off_v = dec(data[e + FO + FL:e + 2 * FO + FL], FO)
            len_v = dec(data[e + 2 * FO + FL:e + EW], FL)
            return data[off_v:off_v + len_v].decode("utf-8")
    return None


def python_get(path, key):
    """Binary search over raw bytes — independent of the Nix implementation."""
    return python_get_data(open(path, "rb").read(), key)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify every key with an independent binary search",
    )
    args = ap.parse_args()

    with open(args.input) as f:
        pairs = json.load(f)
    if not isinstance(pairs, dict):
        sys.exit("input must be a JSON object")
    for k, v in pairs.items():
        if not isinstance(k, str) or not isinstance(v, str):
            sys.exit(f"non-string key/value: {k!r}: {v!r}")

    data = build(list(pairs.items()))
    with open(args.output, "wb") as f:
        f.write(data)
    print(f"wrote {args.output}: {len(data)} bytes, {len(pairs)} entries")

    if args.check:
        n, entries = parse(args.output)
        if n != len(pairs):
            sys.exit(f"check: entry count {n} != {len(pairs)}")
        ks = [k for k, _ in entries]
        if any(ks[i].encode() >= ks[i + 1].encode() for i in range(n - 1)):
            sys.exit("check: keys not strictly ascending (byte order)")
        bad = 0
        blob = open(args.output, "rb").read()
        for k, v in pairs.items():
            got = python_get_data(blob, k)
            if got != v:
                bad += 1
                if bad <= 5:
                    print(
                        f"MISMATCH {k!r}: want {v!r} got {got!r}",
                        file=sys.stderr,
                    )
        miss = "\x01\x02__definitely_missing__"
        if python_get_data(blob, miss) is not None:
            print("check: unexpected hit for absent key", file=sys.stderr)
            bad += 1
        if bad:
            print(f"check: {len(pairs) - bad}/{len(pairs)} ok  FAIL")
            sys.exit(1)
        print(f"check: {len(pairs)}/{len(pairs)} ok (miss -> None)")


if __name__ == "__main__":
    main()