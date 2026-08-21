Possible improvements:
If there happen to be no collisions in a file could we omit the fingerprints? If so, lets implement the ability for the builder to set fpW=0 .
Could have a traditional JSON file committed to make it easier to work with, and have a detached index that refers to key/value locations in the traditional JSON file.
We could handle json "objects of objects" keys by serializing the attrPath into a string (using a special separator?).
Remove padding in the header
Fix potential issue with b254 bytes? (calculation wrong? how to encode 255?)

In Nix:
Lack of a native conversion from a string to an integer prevents me from doing the kind of byte math I want to
Does builtins.readFile load the entire string into memory? mmap or lazy reading instead?
