# Incremental rebuild prototype

Scripts and results behind `../IncrementalRebuildStudy.md`. Experimental.

- `incr.py`: the driver. Edited files and the previous build's manifest in,
  rebuilt program out. Exit status 2 means "use the full build", 3 means
  "the previous output is still valid".
- `changes.py`: the rules that decide from the old and new text of a file
  whether an edit can take the incremental path.
- `historyrun.py`: every change of a repository's history, with its parent
  as the previous build. Builds the ones the rules allow incrementally and
  compares each with a full build.
- `history.py REPO`, `triage.py REPO`: the rules alone on a repository's
  history, and the reasons for the changes that go to the full build. No
  builds.
- `mfdiff.py DIR`: explains the difference between the manifest of an
  incremental result and of the full build in a work directory of
  `historyrun.py`.
- `survey.py BASE.mf PREFIX... -- ROOT...`: counts, without compiling, the
  functions the generator can call directly or reach through callers.
- `objcmp.py A.o B.o`: per-function disassembly comparison of two
  relocatable objects.

## Results

| File | Run |
|---|---|
| `results/history.tsv` | `historyrun.py` on 194 changes of the test program |
| `results/edits.tsv` | twelve edits, each compared with a full build |
| `results/session.tsv` | six edits one after the other, no full build in between |
| `results/null.tsv` | 300 functions, each declared changed with the source unchanged |

The test program is not public. The result files give counts and times and
leave out its file and function names.

## A build by hand

    mojo build --split-cache-dir CACHE --split-cache-manifest base.mf \
        -I src app.mojo -o app
    # edit src/x.mojo, keeping the old text as x.old
    python3 incr.py --base base.mf --cache CACHE --work WORK --out app.o \
        --edit src/x.mojo=x.old --root src -- mojo build --emit object -I src
