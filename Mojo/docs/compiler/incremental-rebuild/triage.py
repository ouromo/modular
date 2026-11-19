#!/usr/bin/env python3
"""triage.py REPO : why the changes of a repository's history need a full build.

For each first-parent commit that changes Mojo files under src/ or bin/,
against its parent: every reason the rules of changes.py give, refined by
the kind of line that triggered it. Prints how many changes carry each
reason, how many carry only that reason, and the changes that would qualify
if a given set of reasons were handled. Reads the repository with git
commands only."""
import collections, itertools, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from changes import classify

repo = sys.argv[1]
def git(*a):
    return subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True).stdout

def names(text, kind):
    return set(re.findall(r"^\s*%s\s+(\w+)" % kind, text, re.M))

def refine(reason, line, old, new):
    s = line.strip()
    if reason == "an existing declaration changed or was removed":
        m = re.match(r"(def|struct|trait)\s+(\w+)", s)
        if m and m.group(1) == "def":
            if m.group(2) not in names(new, "def"):
                return "function removed or renamed"
            if m.group(2) not in names(old, "def"):
                return "function added in place of another"
            return "function signature changed"
        if m:
            return "type header changed or type removed"
        if s.startswith("comptime"):
            return "comptime value changed or removed"
        if s.startswith("@"):
            return "decorator changed or removed"
        if s.startswith(("from ", "import ")):
            return "import changed"
        return "function signature changed"  # a continuation line of a header
    if reason == "an existing signature changed":
        return "function signature changed"
    if reason == "a change outside a function body":
        if re.match(r"var\s+\w+\s*:", s):
            return "field of a type changed"
        if s.startswith("comptime"):
            return "comptime value changed or removed"
        if s.startswith(('"""', "'''")) or not re.match(r"[\w@]", s):
            return "docstring or text outside a function"
        return "other code outside a function: " + (re.match(r"\w+", s).group(0) if re.match(r"\w+", s) else "?")
    return reason

revs = git("log", "--first-parent", "--reverse", "--format=%H", "--", "src", "bin").split()
per_commit = {}
for r in revs:
    files = [tuple(l.split("\t")) for l in git("diff", "--name-status", "--no-renames", r + "~1", r, "--", "src", "bin").splitlines() if l.endswith(".mojo")]
    if not files:
        continue
    reasons = set()
    for status, f in files:
        if status == "D":
            reasons.add("file removed")
        elif status == "M":
            old, new = git("show", "%s~1:%s" % (r, f)), git("show", "%s:%s" % (r, f))
            found = []
            change = classify(old, new, found)
            reasons.update(refine(why, line, old, new) for why, line in found)
            if not found:
                # the driver's name checks, on the repository's own sources
                for name in change.new_names:
                    if git("grep", "-lw", name, r + "~1", "--", "*.mojo"):
                        reasons.add("a new name is already used in the program")
    per_commit[r] = reasons

total = len(per_commit)
ok = sum(1 for v in per_commit.values() if not v)
print("changes %d, incremental by the rules %d (%.0f%%)" % (total, ok, 100.0 * ok / total))
count = collections.Counter(x for v in per_commit.values() for x in v)
alone = collections.Counter(next(iter(v)) for v in per_commit.values() if len(v) == 1)
print("\n%-52s %8s %10s" % ("reason", "changes", "only this"))
for reason, n in count.most_common():
    print("%-52s %8d %10d" % (reason, n, alone[reason]))
print("\nnumber of reasons per full-build change: " + ", ".join("%d: %d" % kv for kv in sorted(collections.Counter(len(v) for v in per_commit.values() if v).items())))
print("\nchanges that qualify when these reasons are handled, in the order that adds most:")
handled, left = set(), [v for v in per_commit.values() if v]
while left:
    best = max(count, key=lambda c: (sum(1 for v in left if v <= handled | {c}), -len(c)) if c not in handled else (-1, 0))
    handled.add(best)
    now = ok + sum(1 for v in per_commit.values() if v and v <= handled)
    left = [v for v in left if not v <= handled]
    print("  + %-50s %4d (%.0f%%)" % (best, now, 100.0 * now / total))

scenarios = [
    ("comptime values", {"comptime value changed or removed", "docstring or text outside a function"}),
    ("+ signatures, removed functions", {"function signature changed", "function removed or renamed", "function added in place of another"}),
    ("+ removed imports", {"an import was removed or renamed", "import changed", "an import changed together with other lines", "file removed"}),
    ("+ fields and type headers", {"field of a type changed", "type header changed or type removed"}),
]
print("\nscenarios (each adds to the one before; lines of continued statements count as their statement):")
handled = {c for c in count if c.startswith("other code outside a function")}
for label, more in scenarios:
    handled |= more
    now = ok + sum(1 for v in per_commit.values() if v and v <= handled)
    print("  %-40s %4d (%.0f%%)" % (label, now, 100.0 * now / total))
