#!/usr/bin/env python3
"""history.py REPO [COUNT] : how often would a change take the incremental path?

Walks the last COUNT first-parent commits of REPO that change Mojo files under
src/ or bin/, and classifies each against (a) its parent and (b) a baseline
5, 10 and 20 such commits back, with the rules of incr.py (changes.py).
Reads the repository with git commands only."""
import os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from changes import classify, Fallback

repo, count = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 200
def git(*a):
    return subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True).stdout

def in_use(name, rev, changed):
    """True when `name` occurs as a word in the Mojo sources of `rev` (the
    standard library is not searched here, unlike in incr.py)."""
    hits = git("grep", "-lw", name, rev, "--", "*.mojo").splitlines()
    return bool(hits)

def classify_change(old_rev, new_rev):
    names = git("diff", "--name-status", "--no-renames", old_rev, new_rev, "--", "src", "bin").splitlines()
    files = [l.split("\t") for l in names if l.endswith(".mojo")]
    if not files:
        return "no change", 0
    if any(s == "D" for s, _ in files):
        return "full build: file removed", 0
    total, wider = 0, any(s == "A" for s, _ in files)
    for status, f in files:
        if status != "M":
            continue
        old = git("show", "%s:%s" % (old_rev, f))
        try:
            c = classify(old, git("show", "%s:%s" % (new_rev, f)))
        except Fallback as e:
            return "full build: " + str(e), 0
        for name in c.new_names:
            if in_use(name, old_rev, files):
                return "full build: a new name is already used in the program", 0
        for name in c.import_names:
            if re.search(r"\b%s\b" % re.escape(name), old):
                return "full build: an imported name is already used in the file", 0
        total += c.edited_functions
        wider = wider or bool(c.new_names or c.import_names)
    if wider:
        return "incremental: bodies plus new functions, imports or files", total
    return ("incremental: bodies only", total) if total else ("no change (comments only)", 0)

revs = git("log", "--first-parent", "--format=%H", "-n", str(count * 3), "--", "src", "bin").split()
revs = [r for r in revs if any(l.endswith(".mojo") for l in git("diff", "--name-only", r + "~1", r, "--", "src", "bin").splitlines())][:count]
print("commits that change Mojo sources: %d" % len(revs))
for back in (1, 5, 10, 20):
    tally = {}; funcs = {}
    for i, r in enumerate(revs):
        if i + back - 1 >= len(revs):
            break
        kind, n = classify_change(revs[i + back - 1] + "~1", r)
        tally[kind] = tally.get(kind, 0) + 1
        funcs.setdefault(kind, []).append(n)
    total = sum(tally.values())
    print("baseline %d changes back (%d cases):" % (back, total))
    for k, v in sorted(tally.items()):
        f = sorted(funcs[k])
        print("  %-66s %4d  %3.0f%%  edited functions median %d, max %d" % (k, v, 100.0 * v / total, f[len(f) // 2], f[-1]))
