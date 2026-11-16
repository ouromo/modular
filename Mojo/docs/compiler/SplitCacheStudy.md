# Study: a per-function object cache for `mojo build`

November 2026. Measured on Mojo 1.2.0. This is not an official Modular
document, and the code on this branch is experimental.

## Summary

`mojo build` takes about the same time after a one-line edit as it does from
scratch. This study measures how much of that time a cache of per-function
object code can save, and what it costs. For the study we added an opt-in
mode to the 1.2.0 compiler that splits the LLVM
module per function before optimization and keeps each split's object file in
a directory, under a hash of the split's bitcode. A later build optimizes and
compiles only the splits whose bitcode changed.

Rebuilds after an edit take 40 to 47% less time. The programs run 3 to 5%
slower overall (up to 13% in one hot path) and are 8 to 34% larger. We think
that cost is too high for the mode to be used as it is, so this is a study
and not a proposal.

The patch adds 503 lines over 7 files on `mojo/v1.2.0`, and it is the rest of
this branch: https://github.com/ouromo/modular/tree/split-cache-study

    mojo build --split-cache-dir DIR [--split-cache-max-size MB] app.mojo

A build without the option produces a binary that is byte-identical to the
official 1.2.0 compiler's.

## Measurements

Release-configuration compiler, official Mojo 1.2.0 as the baseline.

| Case | Official 1.2.0 | Cache, cold | Cache, after an edit |
|---|---|---|---|
| Program A (2,812 functions), Linux x86-64, 16 cores | 74.7 s | 46.4 s | 42 s (44% less) |
| Program A, macOS, Apple silicon, 10 cores | 30.6 s | 21.6 s | 16.3 s (47% less) |
| Program B (5,786 functions), Linux | 200 s | 122 s | 109 s (45% less) |
| 12 binaries of one project, Linux, cache from 32 commits earlier | 880 s | | 526 s (40% less) |

- After a one-line edit, 1 to 3 of 2,812 splits are compiled.
- In the 12-binary case, 88% of the objects came from the older commit's
  cache, although 32 source files and the dependencies had changed.
- The cold build is also faster, because per-function splits use all cores
  in the LLVM optimization. The existing `enableLLVMPerFunctionSplitting`
  path gives part of this by itself.

## Cost

LLVM no longer inlines across functions. That is where the slowdown comes
from, and it is the reason we do not propose the mode as it is.

| | Linux | macOS |
|---|---|---|
| A JSON-heavy benchmark, total | 5% slower | 3% slower |
| The same, parse part only | 13% slower | 5% slower |
| Binary size | 8 to 23% larger | 34% larger |

## Correctness checks

- The 292 standard library test programs that build give the same exit
  status and the same output with and without the cache: 2,594 passing
  tests and no failing test in both modes.
- A reverted edit reproduces the earlier binary byte for byte on Linux.
- Rebuilding from a warm cache, four builds at a time, gives byte-identical
  binaries.

## How it works

1. `emitArchive` lowers all functions to LLVM IR as today.
2. The per-function splitter plans the splits. A new chunked entry point
   opens the module bitcode once per chunk (64 chunks) and not once per
   split; opening it per split cost 131 CPU-seconds on program A.
3. Each chunk task carves its splits with `CloneModule`, hashes the bitcode
   of every split before it compiles any (optimization adds metadata kinds
   to the shared context, which would change later hashes), and looks the
   hashes up in the cache directory.
4. Misses run the usual optimization and code generation and are written to
   the cache. `ld -r` combines all objects, then the normal link runs.

The symbols that several splits share are weak and hidden. The mode is off
for the JIT, for `--emit asm` and with `--save-temps`.

## What is left after the cache

With every split coming from the cache, program A still takes 42 s on Linux.
In a phase trace from an assertion-enabled build (50 s in total), parsing and
the MLIR pipeline took 32 s and lowering all functions to LLVM IR took 14 s.
The cache key is computed after that lowering. A key computed on the
elaborated MLIR function would skip the lowering too.

## Limits we know of

- It needs a system `ld` that supports `-r`.
- Two builds that share a cache while one removes old objects can lose an
  object between the lookup and the link.
- Measured with `-g1` and without debug info, not with full debug info.
- The compiler's lit tests were not run with the mode on.

## Why we are sharing this

We do not propose this mode for the compiler: the slower and larger programs
make it unacceptable as it is. We provide the measurements and the patch in
the hope that they are useful to anyone who works on build times, in
particular these points:

- Most functions are unchanged after an edit, and their LLVM bitcode is
  stable enough to serve as a cache key: 99.9% of the splits hit.
- Splitting per function before the LLVM optimization shortens even a cold
  build by 29 to 39%, because the optimization then uses all cores.
- Opening the module bitcode once per split is a large hidden cost of the
  per-function splitter, and reading it in chunks removes it.
- After the cache, the time is in the frontend and in the lowering to LLVM
  IR, so that is where a further gain would have to come from.
