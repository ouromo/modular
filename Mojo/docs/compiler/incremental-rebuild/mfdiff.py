#!/usr/bin/env python3
"""mfdiff.py DIR : explain how DIR/merged.mf differs from DIR/truth.mf.

For each function whose object differs between the incremental result and
the full build: whether the driver had it as changed
(DIR/changed-functions.txt), where the merged build took its object from,
and the symbols only one of the two objects refers to."""
import os, sys
d = sys.argv[1]
def manifest(p): return {l.split("\t")[0]: l.rstrip("\n").split("\t")[1:] for l in open(p)}
def interfaces(p): return {l.split("\t")[0]: set(l.rstrip("\n").split("\t")[1:]) for l in open(p)}
m, t, b = (manifest(os.path.join(d, n + ".mf")) for n in ("merged", "truth", "base"))
ma, ta = interfaces(os.path.join(d, "merged.mf.abi")), interfaces(os.path.join(d, "truth.mf.abi"))
changed = set(open(os.path.join(d, "changed-functions.txt")).read().split("\n")) if os.path.exists(os.path.join(d, "changed-functions.txt")) else set()
code = lambda names: [n for n in names if not n.startswith(("static_string_", "global_constant_"))]
where = {n: o for o, names in m.items() for n in names}
in_truth = {n for names in t.values() for n in names}
for o, names in t.items():
    if o in m:
        continue
    name = (code(names) or names)[0]
    mo = where.get(name)
    print("full build differs: %s\n    changed: %s; merged object: %s" % (name[:150], name in changed, "none" if mo is None else "previous build" if mo in b else "sub-project"))
    if mo:
        for x in sorted(ta[o] - ma[mo])[:4]: print("    full build only:", x[:150])
        for x in sorted(ma[mo] - ta[o])[:4]: print("    merged only:    ", x[:150])
for o, names in m.items():
    if o not in t and not any(n in in_truth for n in code(names) or names):
        print("only in merged: %s (%s)" % ((code(names) or names)[0][:150], "previous build" if o in b else "sub-project"))
