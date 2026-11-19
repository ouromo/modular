#!/usr/bin/env python3
"""incr.py : prototype of the incremental driver.

  incr.py --base BASE.mf --cache DIR --work DIR --edit REL.mojo=OLD_COPY...
          --root DIR... --out OUTPUT -- COMPILER ARGS...

BASE.mf and BASE.mf.src, .abi, .loc and .dup come from the previous build
of the whole program (--split-cache-manifest). REL.mojo is an edited file as
the compiler names it, OLD_COPY its content at the previous build, or - for
a new file. --root are the directories modules are looked up in. COMPILER
ARGS is the build command without the source file, the output and the split
cache options.

Steps: find the edited declarations, select the functions whose code comes
from them, write a sub-project that calls them, and compile it as a partial
build (--split-cache-previous-manifest). The compiler replaces the objects
of the changed functions in the previous build's list, checks the
interfaces and writes OUTPUT. WORK/merged.mf and its companion files
describe the result and can be the BASE.mf of the next edit.

Exit status: 0 OUTPUT is written; 2 use the full build; 3 no function of
the previous build is affected, its output is still valid.
"""
import argparse, difflib, os, re, shutil, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from changes import classify, Fallback, File, VALUE, noise

def fallback(why):
    print("FALLBACK: " + why)
    sys.exit(2)

def is_data(name):
    return name.startswith(("static_string_", "global_constant_"))

# ---------------------------------------------------------------- manifests
def read_manifest(path):
    return [(p[0], p[1:]) for p in (l.rstrip("\n").split("\t") for l in open(path))]

def read_abi(path):
    rows = {}
    for line in open(path):
        obj, *items = line.rstrip("\n").split("\t")
        rows[obj] = [tuple(i.split(":", 2)) for i in items]  # (kind, hash, name)
    return rows

def parse_ranges(text):
    out = set()
    for part in text.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out

def affected_symbols(srcmap, file, lines):
    out = []
    for line in open(srcmap):
        f = line.rstrip("\n").split("\t")
        for i in range(1, len(f) - 1, 2):
            if f[i].endswith(file) and parse_ranges(f[i + 1]) & lines:
                out.append(f[0])
                break
    return out

# -------------------------------------------------------------- the edit
def name_in_use(name, roots, skip, old_texts):
    """True when `name` already occurs as a word in the program's sources."""
    word = re.compile(r"\b%s\b" % re.escape(name))
    if any(word.search(t) for t in old_texts):
        return True
    found = subprocess.run(["grep", "-rlw", "--include=*.mojo", name] + roots, capture_output=True, text=True).stdout.split("\n")
    return any(f and os.path.realpath(f) not in skip for f in found)

def captured_lines(base, file):
    """Lines of `file` whose location the previous build read as a value, from
    BASE.mf.loc: (resolved in code, resolved at compile time). None when the
    previous build wrote no such list."""
    if not os.path.exists(base + ".loc"):
        return None
    code, comptime = set(), set()
    for line in open(base + ".loc"):
        kind, path, number = line.rstrip("\n").split("\t")
        if path.endswith(file):
            (comptime if kind == "C" else code).add(int(number))
    return code, comptime

def users_of_constants(names, olds, roots, lines, types, only=None):
    """A changed constant leaves no line in the code that uses it, so its
    users are found in the source text: every function whose text holds the
    name counts as edited (added to `lines`), a constant whose value holds
    the name is followed in turn, and a use anywhere else (a signature, a
    field, a decorator) means a full build, except in the declaration of a
    type, which then counts as changed (added to `types`). The search takes
    a word for the constant wherever it stands, which only adds functions.
    With `only`, a file, the names are imported names whose meaning changed
    in that file, and only that file is searched for them."""
    queue, seen = [(n, only) for n in names], set(names)
    project = [r for r in roots if not os.path.isdir(os.path.join(r, "std"))]  # the standard library does not use the program
    while queue:
        name, where = queue.pop()
        word = re.compile(r"(?<!\.)\b%s\b" % re.escape(name)) if where else re.compile(r"\b%s\b" % re.escape(name))
        if where:
            texts = {where: olds[where]}
        else:
            hits = subprocess.run(["grep", "-rlw", "--include=*.mojo", name] + project, capture_output=True, text=True).stdout.split("\n")
            texts = dict(olds)
            skip = {os.path.realpath(f) for f in olds}
            for f in hits:
                if f and os.path.realpath(f) not in skip:
                    texts[f] = open(f).read()
        for f, text in texts.items():
            if not word.search(text):
                continue
            src = File(text)
            for k, line in enumerate(src.lines):
                if not word.search(line) or (src.anchor[k] == k and noise(line)):
                    continue
                k0 = src.anchor[k]; first = src.lines[k0]
                if re.match(r"\s*(from|import)\s", first) or src.in_import[k0]:
                    alias = re.search(r"\b%s\s+as\s+(\w+)" % re.escape(name), line)
                    if alias and not where and alias.group(1) not in seen:
                        seen.add(alias.group(1)); queue.append((alias.group(1), None))
                    continue
                c = src.type_level(k0)
                if c >= 0:
                    types.append((f, re.match(r"\s*\w+\s+(\w+)", src.lines[c]).group(1), c + 1))
                    continue
                d = src.enclosing(k0, ("def",))
                if src.in_signature[k0] or re.match(r"\s*def\s", first):
                    fallback("the changed name %s is used in a signature (%s)" % (name, f))
                if d >= 0:
                    lines.setdefault(f, set()).update(range(d + 1, src.block_end(d) + 1))
                elif VALUE.match(first):
                    other = VALUE.match(first).group(1)
                    if other != name and other not in seen:
                        seen.add(other); queue.append((other, None))
                elif not src.docstring(k0):
                    fallback("the changed name %s is used outside a function body (%s)" % (name, f))
    return len(seen)

def users_of_types(base, types, olds, lines):
    """A changed type (a field, a method signature, its header) changes
    every function that handles a value of it, and later stages of the
    compiler know a struct only by its layout. BASE.mf.use lists, for each
    declaration, the declarations its types named when the program was type
    checked. Every function that names a changed type, or a type that
    contains or conforms to one, counts as edited (added to `lines`).
    types: [(file, name, old header line)]. Returns the qualified names of
    the types reached; the caller adds the functions that carry one of them
    in their own name (instances of generic functions of other modules)."""
    if not os.path.exists(base + ".use"):
        fallback("a type changed and the previous build recorded no uses of declarations")
    rows = [l.rstrip("\n").split("\t") for l in open(base + ".use")]
    decls = [r for r in rows if r[0] in ("lit.struct.decl", "lit.trait.decl")]
    reached = set()
    for f, name, line in types:
        hits = [r for r in decls if r[1].endswith("::" + name) and r[2].rpartition(":")[0].endswith(f)]
        if not hits:
            continue  # the program does not use the type: the compiler never loaded it
        reached.add(min(hits, key=lambda r: abs(int(r[2].rpartition(":")[2] or 0) - line))[1])
    def names_one(refs):
        return any(r in reached or any(r.startswith(q + "::") for q in reached) for r in refs)
    grew = True
    while grew:
        grew = False
        for r in decls:
            if r[1] not in reached and names_one(r[3:]):
                reached.add(r[1]); grew = True
    files = {}
    for r in rows:
        if r[0] in ("lit.struct.decl", "lit.trait.decl", "lit.file_module") or not (names_one(r[3:]) or any(r[1].startswith(q + "::") for q in reached)):
            continue
        path, _, number = r[2].rpartition(":")
        if not number.isdigit():
            continue
        f = next((e for e in olds if path.endswith(e)), path)
        if f not in files:
            if f not in olds and not os.path.exists(f):
                fallback("a user of a changed type is in %s, which is not a source file of this build" % f)
            files[f] = File(olds[f] if f in olds else open(f).read())
        src, k = files[f], int(number) - 1
        if k < len(src.lines):
            lines.setdefault(f, set()).update(range(k + 1, src.block_end(k) + 1))
    return reached

def read_edit(edits, roots, base):
    """edits: [(file, old copy or '-')]. Returns ({file: old lines of the edited
    functions}, names of the functions whose signature changed or that are gone,
    qualified names of the changed types)."""
    skip = {os.path.realpath(f) for f, _ in edits}
    olds = {f: open(old).read() for f, old in edits if old != "-"}
    lines, values, signatures, types, summary = {}, [], set(), [], {"functions": 0, "new names": 0, "imports": 0, "new files": 0, "files with shifted lines": 0, "moved lines that are read as values": 0}
    for f, old in edits:
        if old == "-":
            summary["new files"] += 1
            continue
        try:
            change = classify(olds[f], open(f).read())
        except Fallback as e:
            fallback("%s: %s" % (f, e))
        for name in change.new_names:
            if name_in_use(name, roots, skip, olds.values()):
                fallback("%s: the new name '%s' is already used in the program" % (f, name))
        for name, (type_name, line) in change.new_methods.items():
            # A new method with a fresh name changes nothing that exists. With a
            # name in use it can take the place of another one (a trait default, an
            # overload) for the users of its type, which then count as edited.
            if name.startswith("__") or name_in_use(name, roots, skip, olds.values()):
                change.changed_types[type_name] = line
        # A name whose import changed can mean something else now in the
        # functions of this file that use it.
        users_of_constants(change.import_names, olds, roots, lines, types, only=f)
        if change.shift_from is not None:
            # Code below the first moved line changes only where it reads
            # its own location as a value.
            moved = set(range(change.shift_from, olds[f].count("\n") + 2))
            captured = captured_lines(base, f)
            if captured is not None:
                if captured[1] & moved:
                    fallback("%s: a moved line has its location read at compile time" % f)
                moved &= captured[0]
                summary["moved lines that are read as values"] += len(moved)
                # The code that read the location can be folded away from the
                # source map: take the whole function the line stands in.
                src = File(olds[f])
                for number in sorted(moved):
                    d = src.enclosing(number - 1, ("def",)) if number <= len(src.lines) else -1
                    if d >= 0:
                        moved.update(range(d + 1, src.block_end(d) + 1))
            change.edited_lines.update(moved)
            summary["files with shifted lines"] += 1
        if change.edited_lines:
            lines[f] = change.edited_lines
        summary["functions"] += change.edited_functions
        summary["new names"] += len(change.new_names); summary["imports"] += len(change.import_names)
        values += change.changed_values
        signatures.update(change.changed_functions + change.removed_functions)
        types += [(f, name, line) for name, line in change.changed_types.items()]
    if values:
        summary["changed constants and the constants that use them"] = users_of_constants(values, olds, roots, lines, types)
    summary["changed or removed signatures"] = len(signatures)
    reached = users_of_types(base, types, olds, lines) if types else set()
    summary["changed types and the types that hold them"] = len(reached)
    print("edit: " + ", ".join("%d %s" % (v, k) for k, v in summary.items()))
    return lines, signatures, reached

def merged_with_unedited(base, lines):
    """The compiler keeps one of several identical functions and BASE.mf.dup
    lists the others with their declarations. When the edit changes one
    function of such a pair and not the other, the callers of the kept one
    have to be told apart again, which this driver does not do. Returns the
    name of such a function, or None."""
    if not os.path.exists(base + ".dup"):
        return None
    def edited(where):
        path, _, number = where.rpartition(":")
        return number.isdigit() and any(path.endswith(f) and int(number) in ls for f, ls in lines.items())
    for line in open(base + ".dup"):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 4 and edited(p[2]) != edited(p[3]):
            return p[0] if edited(p[2]) else p[1]
    return None

# ------------------------------------------- the manifest for the next build
def format_ranges(numbers):
    out, numbers = [], sorted(numbers)
    i = 0
    while i < len(numbers):
        j = i
        while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
            j += 1
        out.append(str(numbers[i]) if i == j else "%d-%d" % (numbers[i], numbers[j]))
        i = j + 1
    return ",".join(out)

def complete_manifest(base, merged, edits, changed, partial):
    """Writes MERGED.src, .loc, .dup, .use and .eq for the merged program, so that it can
    be the previous build of the next edit. The compiler wrote MERGED and
    MERGED.abi for the whole merged program and the other three for the
    sub-project only. Lines of the previous build are kept, with the line
    numbers the edit moved them to; the changed functions and the new ones
    come from the sub-project."""
    maps = {}
    for f, old in edits:
        if old != "-":
            a, b = open(old).read().splitlines(), open(f).read().splitlines()
            maps[f] = {i1 + d + 1: j1 + d + 1 for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                       if tag == "equal" for d in range(i2 - i1)}
    def moved(path):
        return next((m for f, m in maps.items() if path.endswith(f)), None)
    def read(suffix):
        return open(merged + suffix).read().splitlines() if partial else []
    sub_src = {l.split("\t", 1)[0]: l for l in read(".src")}
    sub_loc, sub_dup, sub_use = read(".loc"), read(".dup"), read(".use")
    defined = {n for l in open(merged) for n in l.rstrip("\n").split("\t")[1:]}
    # source map
    out, seen = [], set()
    for line in open(base + ".src"):
        f = line.rstrip("\n").split("\t"); seen.add(f[0])
        if f[0] in changed and f[0] in sub_src:
            out.append(sub_src[f[0]]); continue
        row = [f[0]]
        for i in range(1, len(f) - 1, 2):
            m = moved(f[i])
            ranges = f[i + 1] if m is None else format_ranges({m[n] for n in parse_ranges(f[i + 1]) if n in m})
            if ranges:
                row += [f[i], ranges]
        out.append("\t".join(row))
    out += [l for name, l in sub_src.items() if name not in seen and name in defined]
    open(merged + ".src", "w").write("".join(l + "\n" for l in out))
    # locations read as values
    out = set(sub_loc)
    for line in open(base + ".loc"):
        kind, path, number = line.rstrip("\n").split("\t")
        m = moved(path)
        if m is None:
            out.add(line.rstrip("\n"))
        elif int(number) in m:
            out.add("%s\t%s\t%d" % (kind, path, m[int(number)]))
    open(merged + ".loc", "w").write("".join(l + "\n" for l in sorted(out)))
    # merged functions
    def moved_declaration(where):
        path, _, number = where.rpartition(":")
        m = moved(path)
        return where if m is None or not number.isdigit() or int(number) not in m else "%s:%d" % (path, m[int(number)])
    out = {}
    for line in open(base + ".dup"):
        p = line.rstrip("\n").split("\t")
        if p[0] not in changed and p[1] not in changed:
            out[p[0]] = "\t".join(p[:2] + [moved_declaration(w) for w in p[2:]])
    for line in sub_dup:
        out.setdefault(line.split("\t", 1)[0], line)
    open(merged + ".dup", "w").write("".join(out[k] + "\n" for k in sorted(out)))
    # declarations and the declarations they name: the sub-project's lines
    # are newer, the previous build has the rest of the program
    if os.path.exists(base + ".use"):
        out = {}
        for line in open(base + ".use"):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3:
                p[2] = moved_declaration(p[2]); out[(p[0], p[1])] = "\t".join(p)
        for line in sub_use:
            p = line.split("\t")
            if len(p) >= 3 and not p[1].startswith("subproject"):
                out[(p[0], p[1])] = line
        open(merged + ".use", "w").write("".join(l + "\n" for l in out.values()))
    # code hashes: the previous build's, and the sub-project's for what it changed or added
    if os.path.exists(base + ".eq"):
        out = dict(l.rstrip("\n").split("\t") for l in open(base + ".eq"))
        for name in changed:
            out.pop(name, None)
        for line in read(".eq"):
            name, code = line.split("\t")
            if name in changed or (name not in out and name in defined):
                out[name] = code
        open(merged + ".eq", "w").write("".join("%s\t%s\n" % kv for kv in sorted(out.items())))

# --------------------------------------------------------- symbols to source
class Unnameable(Exception):
    pass

ORIGIN_PARAMS = re.compile(r"([\w:]+)\[(?:(?:LITImmOrigin|LITMutOrigin|::Origin\[(?:False|True), \$\d+\]),?)+\](?=\()")
# A string slice of any origin, and one of the static origin.
WRITE_TO = re.compile(r"[\w:]+::write_to\[::Writer & ::AnyType\]\(([\w:]+),\$0\),writer\.T")
STRING_SPAN = re.compile(r"::StringSpan\[(?:\$\d+, \$\d+|ImmStaticOrigin, ::Origin\[False, ImmStaticOrigin\]\(\))\]")

def split_symbol(sym):
    """'a::b::f(x,y)' -> (['a','b','f'], ['x','y']). Generic instances, closures
    and anything with a suffix other than _REMOVED_ARG cannot be named. A
    function whose only parameters are origins has one instance whatever the
    origins are, and is named like a plain function."""
    m = ORIGIN_PARAMS.match(sym)
    if m:
        sym = m.group(1) + sym[m.end():]
    depth = 0
    for i, ch in enumerate(sym):
        if ch == "[":
            raise Unnameable(sym)
        if ch == "(":
            break
    else:
        raise Unnameable(sym)
    head, rest = sym[:i], sym[i + 1:]
    if not re.fullmatch(r"[A-Za-z_][\w]*(::[A-Za-z_]\w*)+", head):
        raise Unnameable(sym)
    args, cur, depth = [], "", 0
    for j, ch in enumerate(rest):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur); cur = ""
        else:
            cur += ch
    else:
        raise Unnameable(sym)
    if cur:
        args.append(cur)
    if rest[j + 1:] not in ("", "_REMOVED_ARG"):
        raise Unnameable(sym)
    return head.split("::"), [a.strip() for a in args]

def drop_type_lists(t):
    """'::Tuple[A, B, ::TypeList[Trait, A, B]()]' -> '::Tuple[A, B]': the
    compiler prints the element list of a variadic type a second time."""
    while True:
        i = t.find(", ::TypeList[")
        if i < 0:
            return t
        depth, j = 0, i + len(", ::TypeList")
        while j < len(t):
            depth += t[j] == "["; depth -= t[j] == "]"
            j += 1
            if depth == 0:
                break
        if not t.startswith("()", j):
            raise Unnameable(t)
        t = t[:i] + t[j + 2:]

class Generator:
    def __init__(self, roots):
        self.roots = roots
        # Precompiled packages: modules without a source file to look for.
        dirs = list(roots)
        if os.environ.get("MODULAR_HOME"):
            dirs.append(os.path.join(os.environ["MODULAR_HOME"], "..", "..", "lib", "mojo"))
        self.packages = {f[:-6] for d in dirs if os.path.isdir(d) for f in os.listdir(d) if f.endswith(".mojoc")} - {"std"}

    def module_of(self, parts):
        """Longest prefix of parts that is a module; returns (module, rest)."""
        for n in range(len(parts) - 1, 0, -1):
            rel = os.path.join(*parts[:n])
            for root in self.roots:
                if os.path.exists(os.path.join(root, rel + ".mojo")) or os.path.exists(os.path.join(root, rel, "__init__.mojo")):
                    return ".".join(parts[:n]), parts[n:]
        if parts[0] in self.packages and len(parts) > 1:
            # a type of a package: everything before the last name is the module
            return ".".join(parts[:-1]), parts[-1:]
        raise Unnameable("::".join(parts))

    def source_type(self, t, imports):
        t = STRING_SPAN.sub("StringSlice[ImmStaticOrigin]", t)
        t = t.replace(", ::AHasher[::SIMD[DType.uint64, 4](0)]", "")  # the default hasher of a Dict
        t = drop_type_lists(t)
        if re.search(r"[$!*&]|LIT|kgen|typevalue|def\(|->", t):
            raise Unnameable(t)
        def path(m):
            module, rest = self.module_of(m.group(0).split("::"))
            if len(rest) != 1:
                raise Unnameable(t)
            return imports.name(module, rest[0])
        t = re.sub(r"(?<![\w:])[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+", path, t)
        return re.sub(r"(?<![\w:])::(?=[A-Za-z_])", "", t)

    def call(self, sym, imports):
        """(text of the callee or None, [(argument type, owned)], statements or None)."""
        m = WRITE_TO.match(sym)
        if m:
            # An instance of write_to for one of the writers of the standard
            # library: printing and formatting a value make them.
            return None, [(self.source_type(m.group(1), imports), False)], ["print(a0[])", 'var s = String(a0[], "")', "s.write(a0[])"]
        parts, args = split_symbol(sym)
        module, rest = self.module_of(parts)
        if len(rest) > 2 or rest[-1] == "__init__" or rest[-1] == "main":
            raise Unnameable(sym)
        target = imports.name(module, rest[0]) + ("." + rest[1] if len(rest) == 2 else "")
        # NAME$ is an argument the function takes ownership of
        types = [(self.source_type(a.rstrip("$"), imports), a.endswith("$")) for a in args]
        return target, types, None

class Imports:
    def __init__(self):
        self.names = {}
    def name(self, module, name):
        return self.names.setdefault((module, name), "_n%d" % len(self.names))

def write_subproject(path, gen, symbols):
    """Returns ({line: symbol}, [symbols that could not be named])."""
    imports, bodies, failed = Imports(), [], []
    for sym in symbols:
        trial = Imports(); trial.names = dict(imports.names)
        try:
            target, types, statements = gen.call(sym, trial)
        except Unnameable:
            failed.append(sym); continue
        imports = trial
        bodies.append((sym, target, types, statements))
    out = ["from std.sys import argv", "from std.memory import alloc"]
    line_of = {}
    import_users = {}
    for (module, name), alias in imports.names.items():
        out.append("from %s import %s as %s" % (module, name, alias))
        import_users[len(out)] = alias
    out += ["", ""]
    for k, (sym, target, types, statements) in enumerate(bodies):
        out.append("def _call%d() raises:" % k); line_of[len(out)] = sym
        for i, (t, owned) in enumerate(types):
            out.append("    var a%d = alloc[%s](1)" % (i, t)); line_of[len(out)] = sym
        for text in statements or ["_ = %s(%s)" % (target, ", ".join(("a%d.take_pointee()" if owned else "a%d[]") % i for i, (t, owned) in enumerate(types)))]:
            out.append("    " + text); line_of[len(out)] = sym
        out += ["", ""]
    out.append("def main() raises:")
    out.append("    # Never true at run time; the calls only make the compiler emit the functions.")
    out.append("    if len(argv()) > 1000000:")
    for k in range(len(bodies)):
        out.append("        _call%d()" % k)
    if not bodies:
        out.append("        pass")
    text = "\n".join(out) + "\n"
    open(path, "w").write(text)
    # an error on an import line is charged to every call that uses the alias
    for line, alias in import_users.items():
        users = [sym for sym, target, types, statements in bodies if re.search(r"\b%s\b" % alias, (target or "") + " " + " ".join(t for t, owned in types))]
        line_of[line] = users
    return line_of, failed

# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    for a in ("base", "cache", "work", "out"):
        ap.add_argument("--" + a, required=True)
    ap.add_argument("--edit", action="append", default=[], metavar="FILE=OLD_COPY")
    ap.add_argument("--affected", action="append", default=[], metavar="SYMBOL",
                    help="treat this function as affected, with the source unchanged (a test: the result must be the previous object list)")
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--main-dir", help="directory of the program's main file; the sub-project imports that file as a module to reach its functions")
    ap.add_argument("--max-functions", type=int, default=400)
    ap.add_argument("--max-reach", type=float, default=0.7,
                    help="go to the full build when the affected functions reach more than this share of the previous program's objects; compile time follows that share")
    ap.add_argument("--full-time", type=float, default=0,
                    help="seconds a full build takes; a further sub-project is only tried while the predicted total stays under it")
    ap.add_argument("--budget", type=float, default=0, help="seconds after which no further sub-project is tried (a share of a full build's time)")
    ap.add_argument("compiler", nargs="+")
    o = ap.parse_args()
    started = time.time()
    if o.main_dir:
        o.root.append(o.main_dir)
    clock = time.time(); stamp = {}
    def lap(name):
        nonlocal clock
        now = time.time(); stamp[name] = stamp.get(name, 0) + now - clock; clock = now

    # a. what changed
    edits = [tuple(e.split("=", 1)) for e in o.edit]
    lines, signatures, types = read_edit(edits, o.root, o.base) if edits else ({}, set(), set())
    shared = merged_with_unedited(o.base, lines)
    if shared:
        fallback("%s shares its code with a function the edit does not change" % shared[:120])
    merged_mf = os.path.join(o.work, "merged.mf"); os.makedirs(o.work, exist_ok=True)
    base = read_manifest(o.base); base_abi = read_abi(o.base + ".abi")
    base_def = {n: obj for obj, names in base for n in names if not is_data(n)}
    callers = {}
    for obj, items in base_abi.items():
        defs = [n for k, h, n in items if k == "D" and not is_data(n)]
        for k, h, n in items:
            if k == "U":
                callers.setdefault(n, set()).update(defs)
    uses = {obj: [n for k, h, n in items if k == "U"] for obj, items in base_abi.items()}
    object_of = {n: obj for obj, names in base for n in names}
    def reach(names):
        """The share of the previous program's objects that these functions reach."""
        seen, todo = set(), [object_of[n] for n in names if n in object_of]
        while todo:
            obj = todo.pop()
            if obj not in seen:
                seen.add(obj)
                todo += [object_of[n] for n in uses.get(obj, ()) if n in object_of]
        return len(seen) / max(len(base), 1)
    affected, touched = [], set()
    own = re.compile(r"::(%s)[(\[]" % "|".join(sorted(signatures))) if signatures else None
    for f, ls in lines.items():
        found = affected_symbols(o.base + ".src", f, ls)
        if own:
            # the callers of a function whose signature changed compile differently
            found += sorted({c for s in found if own.search(s) for c in callers.get(s, ())})
        touched.update(found)
        affected += [s for s in found if s in base_def and s not in affected]
    if types:
        # functions that carry a changed type in their own name
        named = re.compile(r"(?<![\w:])(%s)(?!\w)" % "|".join(re.escape(q) for q in sorted(types)))
        found = [l.split("\t", 1)[0] for path in (o.base + ".src", o.base + ".dup") if os.path.exists(path) for l in open(path)]
        found = [s for s in dict.fromkeys(found) if named.search(s)]
        touched.update(found)
        affected += [s for s in found if s in base_def and s not in affected]
    affected += [s for s in o.affected if s in base_def and s not in affected]
    print("affected functions %d" % len(affected))
    if not affected:
        # nothing the previous build contains comes from the edited lines: the binary does not change
        print("no function of the previous build is affected; the previous output is kept")
        for suffix in ("", ".abi"):
            shutil.copy(o.base + suffix, merged_mf + suffix)
        complete_manifest(o.base, merged_mf, edits, set(touched), False)
        sys.exit(3)
    if len(affected) > o.max_functions:
        fallback("%d affected functions, more than the limit" % len(affected))
    if any(s == "main" or s.endswith("::main()") for s in affected):
        fallback("the edit reaches the entry point")
    os.makedirs(o.work, exist_ok=True)
    changed = os.path.join(o.work, "changed-functions.txt")
    with open(changed, "w") as f:
        f.write("".join(n + "\n" for n in sorted(touched | set(affected))))
    lap("find changes")

    # b/c/d. generate and compile, replacing what cannot be named by its callers
    gen = Generator(o.root)
    sub_path = os.path.join(o.work, "subproject.mojo")
    # A function with a new signature is reached through its callers, which are affected too.
    roots = {s for s in affected if not (own and own.search(s))} or set(affected)
    unnameable = set()
    for attempt in range(1, 13):
        if o.budget and attempt > 1 and time.time() - started > o.budget:
            fallback("the time budget is used up after %d sub-projects" % (attempt - 1))
        line_of, failed = write_subproject(sub_path, gen, sorted(roots))
        if not failed:
            lap("generate")
            share = reach(roots)
            if share > o.max_reach:
                fallback("the affected functions reach %d%% of the program" % round(100 * share))
            if o.full_time and attempt > 1 and time.time() - started + share * o.full_time > o.full_time:
                fallback("another sub-project would take longer than the full build (after %d)" % (attempt - 1))
            cmd = o.compiler + ["--split-cache-dir", o.cache, "--split-cache-previous-manifest", o.base,
                                "--split-cache-changed-functions", changed, "--split-cache-manifest", merged_mf, sub_path, "-o", o.out] + (["-I", os.path.abspath(o.main_dir)] if o.main_dir else [])
            env = dict(os.environ, MODULAR_CACHE_DIR=os.path.join(o.work, "compiler-cache-%d" % attempt))
            run = subprocess.run(cmd, capture_output=True, text=True, env=env)
            lap("compile, merge and link")
            if run.returncode == 0:
                # A function of the previous build that is now merged into another one
                # (a new function with the same code sorts first): a full build points
                # its callers to that one, so they are compiled again.
                regrouped = set()
                if os.path.exists(merged_mf + ".dup"):
                    for line in open(merged_mf + ".dup"):
                        name = line.split("\t", 1)[0]
                        if name in base_def:
                            regrouped.update(c for c in callers.get(name, ()) if c in base_def and c not in affected)
                if not regrouped:
                    break
                affected += sorted(regrouped); roots.update(regrouped)
                with open(changed, "w") as f:
                    f.write("".join(n + "\n" for n in sorted(touched | set(affected))))
                print("attempt %d: %d callers of functions that are merged into others now" % (attempt, len(regrouped)))
                continue
            text = run.stdout + run.stderr
            m = re.search(r"partial build: the interface of '(.*)' differs", text)
            if m:
                # A function can keep its name and change its calling convention with
                # its body. Its other users then have to be compiled again too.
                extra = sorted(s for s in {m.group(1)} | set(callers.get(m.group(1), ())) if s in base_def and s not in affected)
                if not extra:
                    fallback("the interface of %s differs from the previous build" % m.group(1)[:120])
                affected += extra; roots.update(extra)
                with open(changed, "w") as f:
                    f.write("".join(n + "\n" for n in sorted(touched | set(affected))))
                print("attempt %d: the interface of %s changed, %d users added" % (attempt, m.group(1)[:80], len(extra)))
                continue
            m = re.search(r"partial build: (the object .*|failed to read .*)", text)
            if m:
                fallback(m.group(1)[:160])
            missing = re.findall(r"^  missing: (.*)$", text, re.M)
            if missing:
                # named and still not emitted, or reached through a caller and not emitted: go one caller up
                failed = [s for s in missing if s in roots]
                if not failed:
                    # Compiled through its callers and still absent: the function is
                    # gone under this name (a signature changed; an argument is no
                    # longer unused), and the callers refer to something else now.
                    extra = sorted({c for s in missing for c in callers.get(s, ()) if c in base_def and c not in affected})
                    if not extra:
                        fallback("no sub-project emits " + missing[0][:120])
                    affected += extra; roots.update(extra)
                    with open(changed, "w") as f:
                        f.write("".join(n + "\n" for n in sorted(touched | set(affected))))
                    print("attempt %d: %d callers of functions that are gone under their name" % (attempt, len(extra)))
                    continue
            else:
                for m in re.finditer(r"subproject\.mojo:(\d+):\d+: error", text):
                    hit = line_of.get(int(m.group(1)))
                    failed += hit if isinstance(hit, list) else [hit] if hit else []
                if not failed:
                    print(text[-2000:])
                    fallback("the sub-project does not compile")
        print("attempt %d: %d roots, %d not usable" % (attempt, len(roots), len(set(failed))))
        for s in set(failed):
            unnameable.add(s); roots.discard(s)
            up = callers.get(s, ())
            if not up or any(c == "main" or c.endswith("::main()") for c in up):
                fallback("no nameable caller reaches " + s[:120])
            # callers that were tried before are already replaced by their own callers
            roots.update(c for c in up if c not in unnameable)
        if len(roots) > o.max_functions:
            fallback("too many roots")
    else:
        fallback("no sub-project found that emits every affected function")
    before = [obj for obj, _ in base]; after = [obj for obj, _ in read_manifest(merged_mf)]
    print("sub-project: %d roots (%d of them affected functions), %d attempts" % (len(roots), len(roots & set(affected)), attempt))
    complete_manifest(o.base, merged_mf, edits, touched | set(affected), True)
    lap("manifest for the next build")
    print("replaced %d objects, added %d, total %d" % (len(set(before) - set(after)), len(after) - len(before), len(after)))
    print("times: " + ", ".join("%s %.2f s" % kv for kv in stamp.items()))

if __name__ == "__main__":
    main()
