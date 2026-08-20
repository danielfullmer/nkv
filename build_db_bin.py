#!/usr/bin/env python3
"""Build NKB v1 databases from JSON key/value files.

Usage: python3 build_db_bin.py input.json output.nkb [--check]

NKB v1 layout (all offsets absolute, chars == bytes):
  0..3    magic "NKB1"
  4..11   N        (4 base-255 digits, 2 chars each, little-endian digits)
  12..19  keyTotal
  20..27  valTotal
  28..63  spaces (header is 64 chars)
  64..    N entries of 24 chars: off_k 8 | len_k 4 | off_v 8 | len_v 4
  then    all keys concatenated (sorted bytewise by UTF-8 bytes),
          then all values concatenated (same order).

Field encoding: base-255, little-endian digits; each digit is 2 chars over
"abcdefghijklmnopqrstuvwxyz234567" (digit = hi*32 + lo). Lengths use 2
digits (< 255^2), offsets/counts use 4 (< 255^4).

JSON input: an object; duplicate keys keep the last (dict semantics,
matching fromJSON).
"""
import argparse
import json
import sys

MAGIC = b"NKB1"
H = 64
EW = 24
ALPH = "abcdefghijklmnopqrstuvwxyz234567"
B255 = 255
MAX_LEN = B255 ** 2   # per-field length limit (2 digits)
MAX_OFF = B255 ** 4   # per-field offset/count limit (4 digits)


def enc(v, digits):
    """Encode v as `digits` base-255 digits (little-endian), 2 chars each."""
    if v < 0 or v >= B255 ** digits:
        raise ValueError(f"value {v} out of range for {digits} base-255 digits")
    out = []
    for _ in range(digits):
        d = v % B255
        v //= B255
        out.append(ALPH[d // 32] + ALPH[d % 32])
    return "".join(out)


def dec(s, digits):
    """Inverse of enc."""
    v = 0
    for i in range(0, 2 * digits, 2):
        d = ALPH.index(s[i]) * 32 + ALPH.index(s[i + 1])
        v += d * (B255 ** (i // 2))
    return v


def build(pairs):
    """pairs: iterable of (key, value) str pairs. Returns file bytes."""
    items = sorted(pairs, key=lambda kv: kv[0].encode("utf-8"))
    n = len(items)
    keys = b"".join(k.encode("utf-8") for k, _ in items)
    vals = b"".join(v.encode("utf-8") for _, v in items)
    if n >= MAX_OFF:
        raise ValueError("too many entries")
    if len(keys) >= MAX_OFF or len(vals) >= MAX_OFF:
        raise ValueError("key/value data region too large")
    header = (
        MAGIC
        + enc(n, 4).encode()
        + enc(len(keys), 4).encode()
        + enc(len(vals), 4).encode()
    )
    header += b" " * (H - len(header))
    assert len(header) == H
    idx = bytearray()
    ko = H + EW * n
    vo = ko + len(keys)
    for k, v in items:
        kb, vb = k.encode("utf-8"), v.encode("utf-8")
        if len(kb) >= MAX_LEN or len(vb) >= MAX_LEN:
            raise ValueError(f"key or value too long: {len(kb)}, {len(vb)}")
        idx += enc(ko, 4).encode()
        idx += enc(len(kb), 2).encode()
        idx += enc(vo, 4).encode()
        idx += enc(len(vb), 2).encode()
        ko += len(kb)
        vo += len(vb)
    return header + bytes(idx) + keys + vals


def parse(path):
    """Independent parser: (n, [(key, value)]) decoded from the file."""
    data = open(path, "rb").read()
    if data[:4] != MAGIC:
        raise ValueError(f"bad magic: {data[:4]!r}")
    n = dec(data[4:12].decode("ascii"), 4)
    key_total = dec(data[12:20].decode("ascii"), 4)
    val_total = dec(data[20:28].decode("ascii"), 4)
    k0 = H + EW * n
    if len(data) != k0 + key_total + val_total:
        raise ValueError(
            f"size mismatch: file {len(data)}, "
            f"expected {k0 + key_total + val_total}"
        )
    out = []
    for i in range(n):
        e = H + EW * i
        off_k = dec(data[e:e + 8].decode("ascii"), 4)
        len_k = dec(data[e + 8:e + 12].decode("ascii"), 2)
        off_v = dec(data[e + 12:e + 20].decode("ascii"), 4)
        len_v = dec(data[e + 20:e + 24].decode("ascii"), 2)
        out.append(
            (
                data[off_k:off_k + len_k].decode("utf-8"),
                data[off_v:off_v + len_v].decode("utf-8"),
            )
        )
    return n, out


def python_get(path, key):
    """Binary search over raw bytes — independent of the Nix implementation."""
    data = open(path, "rb").read()
    n = dec(data[4:12].decode("ascii"), 4)
    kb = key.encode("utf-8")

    def kbytes(i):
        e = H + EW * i
        off_k = dec(data[e:e + 8].decode("ascii"), 4)
        len_k = dec(data[e + 8:e + 12].decode("ascii"), 2)
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
            e = H + EW * m
            off_v = dec(data[e + 12:e + 20].decode("ascii"), 4)
            len_v = dec(data[e + 20:e + 24].decode("ascii"), 2)
            return data[off_v:off_v + len_v].decode("utf-8")
    return None


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
        for k, v in pairs.items():
            got = python_get(args.output, k)
            if got != v:
                bad += 1
                if bad <= 5:
                    print(
                        f"MISMATCH {k!r}: want {v!r} got {got!r}",
                        file=sys.stderr,
                    )
        miss = "\x01\x02__definitely_missing__"
        if python_get(args.output, miss) is not None:
            print("check: unexpected hit for absent key", file=sys.stderr)
            bad += 1
        if bad:
            print(f"check: {len(pairs) - bad}/{len(pairs)} ok  FAIL")
            sys.exit(1)
        print(f"check: {len(pairs)}/{len(pairs)} ok (miss -> None)")


if __name__ == "__main__":
    main()