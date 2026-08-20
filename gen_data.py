#!/usr/bin/env python3
"""Generate deterministic test datasets as JSON objects.

Emits:
  data/small.json   1_000 keys + explicit edge cases
  data/medium.json  50_000 keys
  data/large.json   200_000 keys

Keys mimic realistic Nix attribute names (dotted, with some noise); values
are strings of varying length.  Seeded RNG => reproducible.
"""
import json
import random
import sys

WORDS = ["nix", "flake", "stdenv", "pkgs", "build", "native", "python", "node",
         "go", "rust", "cmake", "cargo", "gcc", "clang", "openssl", "zlib",
         "curl", "git", "system", "config", "env", "user", "home", "bin",
         "lib", "dev", "share", "man", "info", "doc", "src", "pkg", "mod"]


def gen(n, seed, value_len_range=(4, 80)):
    rng = random.Random(seed)
    out = {}
    for i in range(n):
        depth = rng.randint(1, 4)
        key = ".".join(rng.choice(WORDS) + str(rng.randint(0, 999)) for _ in range(depth))
        # de-duplicate key
        while key in out:
            key = key + "_" + str(rng.randint(0, 9999))
        vlen = rng.randint(*value_len_range)
        val = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789.-_ ") for _ in range(vlen))
        out[key] = val
    return out


def small():
    base = gen(1000, seed=1)
    # explicit edge cases
    base[""] = ""                                   # empty key -> empty value
    base["a"] = "short"
    base["ünïcode-ключ-ключи"] = "unicode key value"
    base["key with spaces"] = "value with spaces"
    base["dotted.nested.attr.name"] = "nested"
    return base


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    sets = {
        "small": (small(), 10),
        "medium": (gen(50000, seed=2), 20),
        "large": (gen(200000, seed=3), 30),
    }
    if which != "all":
        sets = {which: sets[which]}
    for name, (obj, indent) in sets.items():
        p = f"data/{name}.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=None)
        print(f"wrote {p}: {len(obj)} keys")


if __name__ == "__main__":
    main()