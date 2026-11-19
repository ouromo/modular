# Study: incremental rebuilds through partial builds

November 2026. Status: proof of concept. Measured on Mojo 1.2.0 with a
modified compiler. This is not an official Modular document, and the code is
experimental.

- Compiler: this branch, `incremental` of https://github.com/ouromo/modular,
  on top of the `split-cache` branch.
- Driver, rules and results: `incremental-rebuild/` next to this file. The
  scripts that apply the test edits hold source of the test program and
  are not included; the result files carry no names from it.

## Summary

With a per-function object cache (the split cache, see
`SplitCacheStudy.md`) a rebuild after a one-line edit still takes about
half of a clean build: the compiler parses, type checks and elaborates the
whole program before any cache is consulted.

This study adds a second step. After an edit, a driver works out which
functions of the previous build the edit reaches, writes a small program
that calls just those, and compiles it as a *partial build*. The compiler
replaces the objects of the changed functions in the previous build's
object list and links. Whatever the driver cannot prove safe goes to the
full build, which is always correct.

Results on a real program (88k lines, 6,055 functions; full rebuild 110 s
with a warm split cache):

- **Twelve typical edits** (bodies, new functions, methods, files and
  imports, a changed signature, a changed constant, a reordered struct):
  eleven rebuild in 0.5 to 17 s against 110 s, one goes to the full build
  by rule.
- **The 194 changes of the program's history**, each built on its parent:
  71 of the 185 that can be built take the incremental path, with a median
  of 24% of the full build's time. 114 go to the full build, 59 of them
  because the affected functions reach most of the program.
- **Every incremental result has the same object list as a full build of
  the same source**, in all of the above and in 299 single functions rebuilt
  with the source unchanged.

## Background: where a rebuild spends its time

Program: a scheduler binary, 88k lines of its own source, 6,055 functions
after elaboration. Machine: 16 cores, x86-64 Linux. Times are wall clock.

Clean build with the official compiler, 203 s:

| Phase | Time |
|---|---|
| Parse and type check, one thread | 37.5 s |
| Compiler passes before LLVM | about 46 s |
| LLVM optimization, code generation, link | about 120 s |

Nothing changed, the compiler's own cache hits, 47.3 s:

| Phase | Time |
|---|---|
| Parse and type check | 37.5 s |
| Serialize the program, look up the first cache entry | 1.6 s |
| Load the cached module, compute the second key, look up | 7.5 s |
| Write the binary | 0.7 s |

Split cache warm, after a one-line edit, 106 s:

| Phase | Time |
|---|---|
| Parse and type check | 37.4 s |
| First cache lookup (miss) | 1.7 s |
| Elaboration and compiler optimization passes | 31.7 s |
| Serialize, second cache lookup (miss) | 5.4 s |
| Lower to LLVM, hash, fetch objects, link | 29.4 s |

The split cache removes LLVM optimization and code generation for unchanged
functions. Everything before that still runs on the whole program, and none
of it depends on the size of the edit.

For comparison, Zig 0.17.1 with its own backend rebuilds a 618k-line
generated program in 0.4 s after a local edit. It gets there with a
per-declaration dependency graph, a long-lived compiler process and a linker
that patches the binary in place. This study asks how far one can get
without any of those.

## The idea

The Mojo front end resolves an imported declaration only when something
refers to it. A `main` that calls three functions makes the compiler parse,
check, elaborate and lower those three functions and what they call, not
the whole program. For the test program that front-end work takes 1.2 s
where the full program takes 37.4 s.

The split cache already stores one object per function, named by a hash of
its LLVM bitcode, and links them with `ld -r`. If a function compiled in the
small program gets the same bitcode as in the full program, its object can
take the place of the old one in the previous build's object list.

Most of the work in this study went into that "if": making the result of a
partial build the same objects a full build produces, and knowing which
functions an edit reaches.

## How it works

### 1. The previous build writes a manifest

`mojo build --split-cache-dir DIR --split-cache-manifest FILE` writes:

| File | Content |
|---|---|
| `FILE` | The objects of the program in link order, each with the symbols it defines |
| `FILE.abi` | Per object, each symbol it defines or uses with a hash of its type, calling convention and parameter attributes |
| `FILE.src` | Per function, the source files and lines its code comes from just before lowering to LLVM. Inlined code keeps the lines of the function it came from |
| `FILE.loc` | Every source location the program reads as a value (the line number in an error message, for example) |
| `FILE.dup` | The functions the compiler merged because their code was identical, each with the one kept and the declarations of both |
| `FILE.use` | Per declaration, the declarations its types name while the program still has source types |
| `FILE.eq` | Per function, a hash of its code with source locations and without its own name, taken after identical functions are merged |

### 2. The driver finds what an edit reaches

`incr.py` gets the edited files with their previous content. `changes.py`
classifies each file's edit from the two texts. The result is a set of
source lines that count as edited, and with the source map a set of
functions of the previous build.

| Edit | What counts as edited |
|---|---|
| Lines in a function body | That function |
| New function, type, constant or file | Nothing that exists. A new name that already occurs in the program means a full build (overloads, shadowing) |
| Changed or removed `comptime` constant | Every function whose text holds the name, followed through other constants that hold it |
| Changed signature, removed function | The function and the functions that refer to it in the previous build |
| Any change in a type's declaration (fields, header, method signatures, methods added or removed, decorators) | Every function whose types named that type, or a type that contains or conforms to it, when the program was type checked (`FILE.use`); and every function instance with the type in its name |
| Added, removed or changed import | The functions of that file whose text holds the imported name |
| Lines moved up or down | The functions that read one of the moved lines as a value (`FILE.loc`) |
| Comments, blank lines, docstrings of modules and types | Nothing |

Full build, by rule: a removed or renamed type, a removed file, a changed
decorator or an overload outside a type, a changed statement at module
level, a moved line whose location is read at compile time, and a change
that reaches `main` or most of the program.

Two facts the rules rest on:

- **A struct loses its name early.** After elaboration a function sees a
  struct only as a layout, so nothing in the later program says which
  functions a changed field reaches. The compiler therefore records the
  uses before the pass that lowers source types, when every value still
  carries its declared type, including inferred ones.
- **A folded constant carries no line.** A changed `comptime` value leaves
  no trace in the source map. Its users are found by name in the source
  text, which can only add functions.

### 3. The driver writes a sub-project

A generated `main` calls each affected function under a condition that is
false at run time, with arguments taken from uninitialized memory:

```mojo
def _call0() raises:
    var a0 = alloc[_n1](1)
    var a1 = alloc[String](1)
    _ = _n0(a0[], a1.take_pointee())

def main() raises:
    if len(argv()) > 1000000:
        _call0()
```

A direct call matters. Taking a reference to the function instead makes
the compiler emit it with another calling convention under the same symbol
name, and the merged binary would link and be wrong.

A function the generator cannot call from source (an instance of a generic
function, a closure) is reached through the functions that call it in the
previous build. The functions of the program's main file are reached by
importing that file as a module.

### 4. The compiler builds and merges

    mojo build --split-cache-dir DIR \
        --split-cache-previous-manifest PREVIOUS.mf \
        --split-cache-changed-functions CHANGED.txt \
        --split-cache-manifest MERGED.mf subproject.mojo -o program

In a partial build the link step

1. takes the object list of the previous build;
2. replaces the object of each changed function with the one this build
   made;
3. adds the objects of this build that define a symbol the replacements
   use and the previous program lacks;
4. walks the result from `main` and keeps what the walk reaches, which is
   what a full build holds (true of all 144 full builds of the history);
5. fails if a changed function is not defined and still reached;
6. checks that every symbol of an object taken from this build has the
   interface the kept objects saw.

Nothing else of the sub-project is used, in particular not its `main`.

Three things had to change in the compiler for the objects to come out the
same in a small and in a full program:

- **Order.** Each split's globals and functions are sorted by name, and
  anonymous constants are named by a hash of their content. A numbered
  constant name could mean different data in two programs, and a merge
  would have linked the wrong table without an error.
- **Merged functions.** The compiler merges functions with identical code
  and keeps the one whose name sorts first, which depends on the functions
  a program holds. A partial build reads the previous build's choices and
  keeps them for the functions that did not change. A new function is
  compared with the previous build's functions by a hash of its code
  (`FILE.eq`) and merged as a full build would merge it.
- **Paths.** Objects must not hold the build directory; the driver passes
  the main file's directory the way the full build names it.

### 5. The driver handles what the compiler reports

Some effects are only visible after compiling:

| Report | Reaction |
|---|---|
| A changed function is not defined, and it was a root | Replace the root by its callers and compile again |
| A changed function is not defined though its callers were compiled | It is gone under that name (a new signature; an argument that is no longer unused). Its callers count as changed |
| The interface of a symbol differs | Its users count as changed |
| A function of the previous build is merged into a new one that sorts first | A full build points its callers to the new one. They count as changed |
| The sub-project does not compile at a generated call | Replace that root by its callers |

Each reaction costs another compile. Compile time follows the share of the
previous program's objects that the roots reach, which the driver computes
from the manifest: above 70% it goes to the full build without compiling,
and it starts a further compile only while the predicted total stays under
a full build's time.

### 6. The result is the next previous build

`MERGED.mf` and `MERGED.mf.abi` describe the merged program. The driver
completes the other files: lines of the previous build are kept, with the
line numbers the edit moved them to, and the changed functions come from
the sub-project. The next edit builds on that, with no full build in
between.

## Results

Machine and program as above. The check is exact everywhere: the manifest
of the incremental result must list the same objects as a full build of the
same source, and the binaries must print the same help text.

### Twelve edits

`incremental-rebuild/results/edits.tsv`. Full build of the same source: 109
to 111 s.

| Edit | Affected functions | Incremental | Full build | Same objects |
|---|---|---|---|---|
| New function, called from an existing one | 4 | 6.7 s | 111 s | yes |
| New import, used in a function | 3 | 5.5 s | 111 s | yes |
| New file, imported and called | 6 | 6.0 s | 111 s | yes |
| New method on a struct, called | 2 | 5.8 s | 109 s | yes |
| Bodies in two files | 9 | 6.7 s | 110 s | yes |
| New overload of an existing function | | full build by rule | | |
| New function that nothing calls | 0 | 0.5 s | 111 s | yes |
| Docstring added (lines below move) | 2 | 5.5 s | 111 s | yes |
| Comment at the top of a file (all lines move) | 5 | 6.7 s | 110 s | yes |
| Two fields of a struct swapped | 43 | 17.4 s | 110 s | yes |
| Parameter added to a function, callers updated | 4 | 6.6 s | 110 s | yes |
| Value of a `comptime` constant | 1 | 5.4 s | 110 s | yes |

Eleven of twelve take the incremental path, in 0.5 to 17 s against 110 s.
The struct edit needs two compiles: two of its 43 functions cannot be
called from generated source and are reached through their callers.

### Edits one after the other

`incremental-rebuild/results/session.tsv`: six edits, each building on
the result of the one before, no full build in between.

| Step | Affected functions | Incremental | Full build | Same objects |
|---|---|---|---|---|
| 1. New function | 4 | 6.6 s | 112 s | yes |
| 2. New method | 2 | 5.8 s | 112 s | yes |
| 3. Docstring added | 2 | 5.4 s | 110 s | yes |
| 4. Comment at the top of a file | 5 | 6.4 s | 111 s | yes |
| 5. New file | 6 | 5.8 s | 111 s | yes |
| 6. Body edit in the function of step 1 | 5 | 5.4 s | 111 s | yes |

After each step the source map and the recorded locations of the chained
manifest equal those of a full build line for line. The record of merged
functions grows by entries for functions that exist only in the generated
sub-projects (88 after the first step, 174 after the sixth); they name
functions that are identical and have no effect on a later build.

### One function at a time

`incremental-rebuild/results/null.tsv`: one function is declared
changed with the source unchanged, for 300 functions drawn at random from
the 2,737 with project code. The result must be the previous program.

| Result | Functions |
|---|---|
| The previous object list | 299 |
| Full build (no sub-project emits the function) | 1 |
| A different object list | 0 |

Median 6.2 s per function, at most 60 s.

### The history of the program

`historyrun.py`, `incremental-rebuild/results/history.tsv`: every change of the
program's repository that touches Mojo sources (194 changes over 17 days,
median 5 files per change), with its parent commit's build as the previous
build. Each change the rules allow is built incrementally and compared
with a full build of the same commit.

| Outcome | Changes | Share of 185 |
|---|---|---|
| Incremental, same objects as the full build | 71 | 38% |
| Incremental, different objects | 0 | |
| Full build: the affected functions reach more than 70% of the program | 59 | 32% |
| Full build: another compile would take longer than the full build | 4 | 2% |
| Full build: other driver reasons (a new name already in use, a changed name in a signature) | 10 | 5% |
| Full build by rule | 41 | 22% |

Nine more changes are not counted: six from before the program had a
binary, three whose parent commit does not build in the test environment.

Time of the 71 incremental builds against the full build of the same
commit (the program grows from 8 s to 110 s per full build over the
history):

| Incremental time as a share of the full build | Changes |
|---|---|
| under 10% | 16 |
| 10 to 25% | 20 |
| 25 to 50% | 14 |
| 50 to 82% | 21 |

The median is 24% (18 s against 88 s); the 71 builds take 1,692 s
together where the full builds take 5,634 s. None is slower than the full
build. Nine of them compile nothing: no function of the previous build is
affected.

The time follows the share of the program that the affected functions
reach. A sub-project that calls a function makes the compiler elaborate
everything that function calls, so an edit to a function near the top of
the call graph costs a full build. The driver computes that share from the
previous build's references before it compiles and goes to the full build
above 70%. In 52 of the 59 such changes the share is 100%: one of the
affected functions is at the top of the call graph, such as the command
dispatcher. A compiler that could emit one function and take its unchanged
callees as declarations would not pay for what the function calls; the
script cannot do that.

A first pass without that prediction built 119 changes incrementally. 46
of them took as long as the full build or longer, up to three times, which
is why the prediction was added. That pass also found the eight changes
whose objects differed from the full build; each led to a fix in the
driver or the compiler (see "What the history found"), and the final pass,
run from scratch with the fixed code, has none.

Full builds by rule (41):

| Reason | Changes |
|---|---|
| A declaration outside a function body changed or was removed in a way no rule covers | 15 |
| A dependency or the include path changed | 8 |
| A file was removed | 7 |
| A type was removed, renamed or is declared more than once | 6 |
| A constant is declared more than once | 2 |
| The signature of a special or overloaded function changed | 2 |
| A decorator was added to an existing declaration | 1 |

### What the history found

The edits written for the tests all passed before the history was run.
The history then produced these differences from the full build, all in
the first pass, all fixed:

| Difference | Fix |
|---|---|
| A moved line that a function reads as a value, where the reading operation was folded away before the source map is written | The whole enclosing function counts as edited |
| The main file named by a relative path in one build and an absolute one in the other | The driver passes the path the full build uses |
| A new function with the same code as existing ones whose name sorts first: the full build keeps the new one | The callers of the function kept before are compiled again |
| A function variant that vanishes because an argument is used now | Its callers count as changed |
| A changed function reported as missing though its last user is dropped | The compiler checks after dropping unused objects |
| A replaced function that nothing calls any more stayed in the program | The compiler keeps what `main` reaches, nothing else |
| A new generic instance with the same code as an existing one that the sub-project does not hold: the full build shares one copy, the partial build kept two (same behaviour, different objects) | The compiler records a hash of each function's code; a partial build merges with the previous build's functions through it |

A merged change is a whole feature. The edits between two local builds of
one developer are smaller and are not in the history; their mix is not
measured.

## Build system integration

The flow has to work where a build is an action with declared inputs and
outputs (Bazel, with or without remote execution) and in CI. In
`rules_mojo` a binary is one `mojo build --emit object` action followed by a
separate link.

- **The previous build is an input, not state.** The cache and manifest of
  the baseline are a published artifact, mounted read only
  (`--split-cache-base-dir`); new objects go to a separate directory.
- **The partial build ends at one relocatable object** with
  `--emit object`, so the build system keeps its own link step.
- **No absolute paths.** With `-strip-file-prefix $PWD/` the keys, the
  manifest and the executable of a test program are identical in two
  directories.
- **Precompiled packages do not change the keys.** The program built from
  `.mojoc` packages produced the same 6,055 object hashes as from source.
- **The library action is the new floor.** An edited library has to be
  precompiled again before the binary action runs: 46 s for the test
  program's package as one library of 232 modules.

## What is not done

1. **The driver is a script.** The rules work from the text of the old and
   the new file, by indentation. Inside the compiler they would compare
   declarations after parsing. The generator writes source text; inside the
   compiler it could request the instances directly, which covers the
   functions the script reaches only through callers.
2. **A function is compiled with everything it calls.** This is the main
   limit: a third of the history's changes touch a function near the top of
   the call graph and go to the full build for that reason alone. It needs
   the compiler to emit a function against declarations of its unchanged
   callees.
3. **Several compiles per build.** A reaction to a compiler report is a new
   compile of the sub-project. One process that keeps the parsed modules
   would make each round cheap.
4. **Periodic verification.** A full build compared with the incremental
   result, every Nth build or in CI, is the backstop for a rule that is
   wrong. `mfdiff.py` explains a difference.
5. **A program a full build rejects.** A partial build type checks only
   what the sub-project reaches. An edit that breaks an unrelated function
   (a new ambiguity, for example) is not reported until the next full
   build.
6. **Text search.** The users of a constant and of an imported name are
   found by word. That only adds functions, but reflection over
   declarations at compile time is not covered by it.
7. **Symbols referred to from outside the objects** (by name, from a
   runtime) would be dropped as unreached. None occurred.
8. **macOS.** The partial build is tested on Linux only. **One compiler
   version**: Mojo 1.2.0.
9. **Cost of the cache mode itself.** The split cache gives up inlining
   across functions in LLVM; see `SplitCacheStudy.md`. This is a
   development mode.
