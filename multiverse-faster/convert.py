#!/usr/bin/env python3
"""Convert nixpkgs-multiverse index JSON files to nkv flat form.

The index files (https://github.com/fzakaria/nixpkgs-multiverse,
commit 9cc02098e177f784f822c57973ebfc3c02c21bed) are:

    { "revisionCount": 1534,
      "attrs": { "<attr>": { "<key>": <value> | null }, ... } }

nkv keys are flat strings, and the inner keys contain dots
(version-like "1.3.3.9" / dates), so we flatten exactly one level:

    nkv key    = the attribute name (exact match, no delimiter)
    nkv value  = the inner map, stored as a compact JSON document,
                 decoded by nkv.nix's getJson / getOrJson

Usage:  convert.py in.json out.json
"""
import json
import sys


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        d = json.load(f)
    if "attrs" not in d or not isinstance(d["attrs"], dict):
        sys.exit(f"{src}: missing top-level object 'attrs'")
    flat = {k: v for k, v in d["attrs"].items()}
    with open(dst, "w") as f:
        json.dump(flat, f, separators=(",", ":"))
    print(f"{dst}: {len(flat)} attrs (from {src})")


if __name__ == "__main__":
    main()