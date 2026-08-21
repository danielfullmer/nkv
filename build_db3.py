#!/usr/bin/env python3
"""Build NFK v3 (dense hash, binary index) databases from JSON key/value
files.

Usage: python3 build_db3.py input.json output.nfd3 [--check]

NFK v3 layout (all offsets absolute, chars == bytes):
  0..3     magic "NFK3"
  4..6     N         (3 b254 bytes)
  7..10    M         (4 b254 bytes)
  11..13   keyTotal  (3 b254 bytes)
  14..16   valTotal  (3 b254 bytes)
  17..63   spaces (header is 64 bytes)
  64..318  byte table T: the 255 bytes 0x01 .. 0xFF, in order
  319..    M entries of 15 bytes: fp 4 | keyOff 4 | keyLen 3 | valLen 3
           (unused slot: all fields zero, i.e. 15 bytes 0x01)
  then     data region: for each key in JSON insertion order, the key's
           UTF-8 bytes immediately followed by the value's UTF-8 bytes.
           keyOff is absolute from the file start; the value offset is
           keyOff + keyLen (implicit, not stored).

Hashing (identical to NFK v1/v2): h = sha256(key) in lowercase hex.
Fingerprint fp = int(h[0:6], 16) + 1 — a 24-bit value stored in 4 b254
bytes; 0 marks an unused slot. Initial slot s0 = int(h[56:64], 16) AND
(M-1); linear probing, first empty slot wins. M = next_pow2(max(16,
ceil(1.25*n))), so load <= 0.8. The fingerprint is compared as an int;
a hit is always confirmed by a byte-for-byte key comparison, so a
24-bit fingerprint (expected ~N^2/2^24 false pairs at 200k keys) can
only add a key read, never return a wrong value.

Field encoding ("b254"): each byte of a field is one digit plus one
(digit in 0..253 -> byte in 1..254, big-endian digits), so every byte in
the whole file is in 0x01..0xFF and the file never contains NUL (the one
byte Nix's readFile rejects). The Nix side has no byte->int builtin, so
kv3.nix builds a lookup table at import time from the embedded T:
T[i] (the byte of value i+1) is keyed by that byte's own 1-char string.

Limits (builder-enforced): N, keyTotal, valTotal, and per-key/value
lengths < 254^3; M and file offsets < 254^4. No key or value may
contain a NUL byte.

JSON input: an object; duplicate keys keep the last (dict semantics,
matching fromJSON).
"""
import argparse
import hashlib
import json
import sys

MAGIC = b"NFK3"
H = 64
TBL = bytes(range(1, 256))          # 255-byte byte table, T[i] = i+1
T0 = H + len(TBL)                   # index region start (319)
EW = 15                             # entry width
FPW = 4                             # fingerprint field width (b254)
FO = 4                              # offset field width
FL = 3                              # length/count field width
B = 254
MAX_FIELD3 = B ** FL - 1            # 16,387,063
MAX_FIELD4 = B ** FO - 1            # 4,162,314,255

# entry layout: fp @0 (4) | keyOff @4 (4) | keyLen @8 (3) | valLen @11 (3)


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


def next_pow2(v):
    p = 1
    while p < v:
        p *= 2
    return p


def fp_of(kb):
    h = hashlib.sha256(kb).hexdigest()
    return int(h[:6], 16) + 1, int(h[56:64], 16)


def build(pairs):
    """pairs: iterable of (key, value) str pairs. Returns file bytes."""
    items = list(pairs)
    n = len(items)
    m = next_pow2(max(16, -(-5 * n // 4)))   # ceil(1.25 n), load <= 0.8
    if n > MAX_FIELD3:
        raise ValueError("too many entries")
    if m > MAX_FIELD4:
        raise ValueError("table too large")
    kb_list = []
    vb_list = []
    for k, v in items:
        kb, vb = k.encode("utf-8"), v.encode("utf-8")
        if 0 in kb or 0 in vb:
            raise ValueError("key or value contains NUL")
        kb_list.append(kb)
        vb_list.append(vb)
    # data region: interleaved (key, value, key, value, ...); a value
    # immediately follows its key, so only keyOff is stored per entry
    data_region = b"".join(kb + vb for kb, vb in zip(kb_list, vb_list))
    key_total = sum(map(len, kb_list))
    val_total = sum(map(len, vb_list))
    if key_total > MAX_FIELD3 or val_total > MAX_FIELD3:
        raise ValueError("key/value data region too large")

    # open addressing, first empty slot (identical to the Nix side)
    slot_of = {}
    fp_of_i = {}
    used = set()
    for i, kb in enumerate(kb_list):
        fp, hlo = fp_of(kb)
        s0 = hlo & (m - 1)
        s = s0
        while s in used:
            s = (s + 1) & (m - 1)
        slot_of[i] = s
        fp_of_i[i] = fp
        used.add(s)

    header = (
        MAGIC
        + enc(n, FL)
        + enc(m, FO)
        + enc(key_total, FL)
        + enc(val_total, FL)
    )
    header += b" " * (H - len(header))
    assert len(header) == H

    idx = bytearray(b"\x01" * (EW * m))   # b254 zero = unused slot
    # NOTE: e below is buffer-relative (idx starts at file offset T0).
    ko = T0 + EW * m
    for i in range(n):
        kb, vb = kb_list[i], vb_list[i]
        if len(kb) > MAX_FIELD3 or len(vb) > MAX_FIELD3:
            raise ValueError(f"key or value too long: {len(kb)}, {len(vb)}")
        e = EW * slot_of[i]
        assert e + EW <= len(idx)
        idx[e:e + FPW] = enc(fp_of_i[i], FPW)
        idx[e + FPW:e + FPW + FO] = enc(ko, FO)
        idx[e + FPW + FO:e + FPW + FO + FL] = enc(len(kb), FL)
        idx[e + FPW + FO + FL:e + FPW + FO + 2 * FL] = enc(len(vb), FL)
        ko += len(kb) + len(vb)

    blob = header + TBL + bytes(idx) + data_region
    if len(blob) > MAX_FIELD4:
        raise ValueError("file too large for 4-byte b254 offsets")
    return blob


def parse(path):
    """Independent parser: (n, m, [(key, value)]) decoded from the file."""
    data = open(path, "rb").read()
    if data[:4] != MAGIC:
        raise ValueError(f"bad magic: {data[:4]!r}")
    n = dec(data[4:7], FL)
    m = dec(data[7:11], FO)
    key_total = dec(data[11:14], FL)
    val_total = dec(data[14:17], FL)
    if m < n:
        raise ValueError(f"table size {m} < entry count {n}")
    if data[17:H] != b" " * (H - 17):
        raise ValueError("header reserved region not spaces")
    if data[H:H + len(TBL)] != TBL:
        raise ValueError("embedded byte table is not 0x01..0xFF")
    if 0 in data:
        raise ValueError("file contains NUL (invalid for Nix readFile)")
    d0 = T0 + EW * m
    if len(data) != d0 + key_total + val_total:
        raise ValueError(
            f"size mismatch: file {len(data)}, "
            f"expected {d0 + key_total + val_total}"
        )
    out = []
    for s in range(m):
        e = T0 + EW * s
        fp = dec(data[e:e + FPW], FPW)
        if fp == 0:
            continue
        koff = dec(data[e + FPW:e + FPW + FO], FO)
        klen = dec(data[e + FPW + FO:e + 2 * FPW + FO + FL], FL)
        vlen = dec(data[e + FPW + FO + FL:e + FPW + FO + 2 * FL], FL)
        out.append(
            (
                data[koff:koff + klen].decode("utf-8"),
                data[koff + klen:koff + klen + vlen].decode("utf-8"),
            )
        )
    if len(out) != n:
        raise ValueError(f"decoded {len(out)} entries, header says {n}")
    return n, m, out


def python_get_data(data, key):
    """Open-addressing probe over in-memory file bytes — independent of
    the Nix implementation."""
    n = dec(data[4:7], FL)
    m = dec(data[7:11], FO)
    kb = key.encode("utf-8")
    fp, hlo = fp_of(kb)
    s0 = hlo & (m - 1)
    for i in range(m):
        s = (s0 + i) & (m - 1)
        e = T0 + EW * s
        efp = dec(data[e:e + FPW], FPW)
        if efp == 0:
            return None
        if efp == fp:
            koff = dec(data[e + FPW:e + FPW + FO], FO)
            klen = dec(data[e + FPW + FO:e + 2 * FPW + FO + FL], FL)
            if data[koff:koff + klen] == kb:
                vlen = dec(data[e + FPW + FO + FL:e + FPW + FO + 2 * FL], FL)
                return data[koff + klen:koff + klen + vlen].decode("utf-8")
    return None


def python_get(path, key):
    """Probe over raw bytes."""
    return python_get_data(open(path, "rb").read(), key)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify every key with an independent probe",
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
    n, m = dec(data[4:7], FL), dec(data[7:11], FO)
    print(
        f"wrote {args.output}: {len(data)} bytes, {n} entries, "
        f"M={m} load={n / m:.3f}"
    )

    if args.check:
        n2, m2, entries = parse(args.output)
        if (n2, m2) != (n, m):
            sys.exit(f"check: header mismatch {(n2, m2)} != {(n, m)}")
        if len({k for k, _ in entries}) != n:
            sys.exit("check: duplicate keys in decoded file")
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