Possible improvements:

In Nix:
Lack of a native conversion from a string to an integer prevents me from doing the kind of byte math I want to
Does builtins.readFile load the entire string into memory? mmap or lazy reading instead?
