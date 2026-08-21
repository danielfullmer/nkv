# [nkv](https://github.com/danielfullmer/nkv) - Key/value hash table implementation in native Nix

See [slop.md](./slop.md) for usage details.

## The idea
I recently read an interesting [blog post](https://fzakaria.com/2026/08/19/three-ways-to-smuggle-sqlite-into-nix) from Farid Zakaria in which he compares ways to use SQLite in Nix.
The core of the problem he is addressing is how to quickly look up a handful of values by key in a very large JSON object in a file.
For large JSON files, the cost of this is dominated by the time required to perform `builtins.readFile` and `builtins.fromJSON` to convert the data in the file into a Nix dictionary.
Once you have the object in memory as a dictionary, individual queries are very quick.
But when we only care about performing a small handful of queries, it's very wasteful to read and parse such a large file in order to query it.
Farid's post looks into using SQLite in Nix in various ways to get a similar effect, but the options he considers all have significant downsides.

What if, instead of parsing a large JSON file and querying the resulting attribute set, we could directly look up a value by key in a hash table on disk using only *native Nix language*?
We could use `builtins.substring` on a large binary string to look at only the specific locations necessary in order to retrieve the information required, using a relatively simple hash table lookup procedure.
This would save on the large upfront cost of JSON parsing, at the tradeoff of slower individual queries (custom hash table lookup logic vs attrset lookup).

However, this only solves half the problem.
Performing `builtins.readFile` alone can take a long time, which we'd prefer to avoid doing for a handful of queries.
Nix doesn't have any way to lazily read file contents like Haskell, or operate on an mmaped string.
So, one easy thing to do is to shard the hash table into multiple components, so we only have to read one of them for a given query.
Since we're already using hashes, we can just shard on one or more of the bytes from the hash, splitting the file into 16, 256, or 4096 individual files as desired.

The combination of these two tricks can produce dramatically faster lookup times.
I've made a quick and dirty (vibed) implementation of this in [nkv](https://github.com/danielfullmer/nkv).
This uses a simple custom file format combining a hash table index with blobs of key and value data.
The hash table uses open addressing with linear probing.
On the `history.json` file from Farid's repository (7.8 MB, 31,904 attributes), one cold `nix eval` performing a single attribute lookup:

| method | total | work |
|---|---:|---:|
| `readFile` + `fromJSON` | 256 ms | 228 ms |
| nkv, single file | 45 ms | 12 ms |
| nkv, 256 shards | 33 ms | ~0 ms |

The ~33 ms total is the measured floor of a cold `nix eval` on my machine, so a single sharded query is nearly free in this context.


## Nixisms
There were a few interesting technical challenges I had to overcome to use Nix in this manner.

One was due to the use of Nix strings to read and operate on a binary file.
Nix treats strings as arbitrary binary data, but the main implementation has a catch: NULL bytes are not allowed, since the underlying implementation assumes zero-terminated sequences.
Since we can't operate on 0x00 bytes, I instead use a "base 255" encoding of the hash table index data.
Integers from 0 to 254 are represented as 0x01 to 0xff bytes.
Multi-byte integers with digits $b_1, \ldots, b_k$ have the value $\sum_{i=1}^{k} b_i \cdot 255^{k-i}$

Another problem is that Nix does not have any native function to convert a string containing a byte into an integer.
Instead, we can use a predefined decoding table, which is just a Nix attribute set that contains string keys from 0x01-0xff mapping to the integers 0-254, and use that to lookup the corresponding integer for a given string of a single byte.

## Disclaimer
This is my first nontrivial try at "vibe coding" using agents.
I used Qwen3.8-27B on my 5090 to write the large majority of the code/docs here in a single evening.
I was very impressed with the ability to try out different variations of my ideas quickly to see if they would work.
This blog post was hand-written and reviewed by an LLM, but the rest of the docs were LLM written and not yet strictly reviewed.
I would want to take another deep manual pass on the implementation and documentation before I would be comfortable using it myself for anything real, but I wanted to share it now to get feedback on this idea.
