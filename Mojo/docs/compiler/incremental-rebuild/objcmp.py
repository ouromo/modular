#!/usr/bin/env python3
"""objcmp.py A.o B.o : compare two relocatable objects function by function."""
import re, subprocess, sys
def funcs(path):
    out = subprocess.run(["objdump", "-dr", "--no-show-raw-insn", path], capture_output=True, text=True).stdout
    res, name, body = {}, None, []
    for line in out.splitlines():
        m = re.match(r"^[0-9a-f]+ <(.*)>:$", line)
        if m:
            if name: res[name] = "\n".join(body)
            name, body = m.group(1), []
        elif name and line.strip():
            # drop the address column; keep instruction and relocation text
            body.append(re.sub(r"^\s*[0-9a-f]+:\s*", "", line))
    if name: res[name] = "\n".join(body)
    return res
norm = lambda s: re.sub(r"\.L[\w.]*|[0-9a-f]+ <[^>]*>", "X", s)
a, b = funcs(sys.argv[1]), funcs(sys.argv[2])
only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
diff = [n for n in a if n in b and norm(a[n]) != norm(b[n])]
print("functions: %d and %d; only in first %d; only in second %d; different code %d" % (len(a), len(b), len(only_a), len(only_b), len(diff)))
for label, names in (("only in first", only_a), ("only in second", only_b), ("different", diff)):
    for n in names[:8]: print("  %s: %s" % (label, n[:140]))
