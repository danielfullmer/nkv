Possible improvements:
Could have a traditional JSON file committed to make it easier to work with, and have a detached index that refers to key/value locations in the traditional JSON file.
We could handle json "objects of objects" keys by serializing the attrPath into a string (using a special separator?).
Fix potential issue with b254 bytes? (calculation wrong? how to encode 255?)
Index could be even smaller by bit packing max number of bits for each field
Adjustable load factor.
Minimal Perfect Hash Tables, Robin Hood Hashing, or Cuckoo Hashing

In Nix:
Lack of a native conversion from a string to an integer prevents me from doing the kind of byte math I want to
Does builtins.readFile load the entire string into memory? mmap or lazy reading instead?
