#!/usr/bin/env python3
"""survey.py BASE.mf PROJECT_PREFIX... -- ROOT... : how many functions the
script generator can make the compiler emit, without compiling anything.

For each function of the previous build whose code comes from a file under
one of the prefixes, as if an edit affected that function alone:
  direct   the generator can write a call for it;
  callers  it cannot, and the callers it climbs to can all be called
           (the rule of incr.py: every caller, none of them main, 8 levels);
  one      the rule fails, but some chain of callers would do;
  none     no chain of callers reaches a function that can be called.
Naming a call is taken as success; a call can still fail to compile.
"""
import sys
from incr import Generator, Imports, Unnameable, read_manifest, read_abi, is_data

args = sys.argv[1:]; k = args.index("--")
base, prefixes, roots = args[0], args[1:k], args[k + 1:]
gen = Generator(roots)
defined = {n for obj, names in read_manifest(base) for n in names if not is_data(n)}
callers = {}
for obj, items in read_abi(base + ".abi").items():
    defs = [n for kind, h, n in items if kind == "D" and not is_data(n)]
    for kind, h, n in items:
        if kind == "U":
            callers.setdefault(n, set()).update(defs)
named = {}
def nameable(s):
    if s not in named:
        try:
            gen.call(s, Imports()); named[s] = True
        except Unnameable:
            named[s] = False
    return named[s]
def is_main(s):
    return s == "main" or s.endswith("::main()")
def strict(s):
    roots, dead = {s}, set()
    for level in range(8):
        failed = [r for r in roots if not nameable(r)]
        if not failed:
            return True
        for f in failed:
            dead.add(f); roots.discard(f)
            up = {c for c in callers.get(f, ()) if c not in dead}
            if not up or any(is_main(c) for c in up):
                return False
            roots |= up
    return False
def loose(s, seen=None, depth=0):
    seen = seen or set()
    if nameable(s):
        return True
    if depth >= 8 or s in seen:
        return False
    seen.add(s)
    return any(not is_main(c) and loose(c, seen, depth + 1) for c in callers.get(s, ()))
count = {"direct": 0, "callers": 0, "one": 0, "none": 0}; examples = {"one": [], "none": []}
for line in open(base + ".src"):
    f = line.rstrip("\n").split("\t")
    if f[0] not in defined or is_main(f[0]) or not any(p.startswith(x) for p in f[1::2] for x in prefixes):
        continue
    kind = "direct" if nameable(f[0]) else "callers" if strict(f[0]) else "one" if loose(f[0]) else "none"
    count[kind] += 1
    if kind in examples and len(examples[kind]) < 6:
        examples[kind].append(f[0][:150])
total = sum(count.values())
print("functions with code from the project: %d" % total)
for kind, n in count.items():
    print("  %-8s %5d  %3.0f%%" % (kind, n, 100.0 * n / max(total, 1)))
for kind, ex in examples.items():
    for e in ex:
        print("  %s: %s" % (kind, e))
