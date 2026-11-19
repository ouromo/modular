"""changes.py : classify the edit of one Mojo file for the incremental driver.

classify(old_text, new_text) returns a Change or raises Fallback.

Allowed:
  - changes inside the body of a function that exists on both sides;
  - a changed value or the removal of a `comptime` constant
    (Change.changed_values);
  - a changed signature or the removal of a function whose name is declared
    once in the file and is not a special method (Change.changed_functions,
    Change.removed_functions);
  - any change in the declaration of a type that exists on both sides: its
    fields, constants, decorators, header, and the signatures, addition and
    removal of its methods (Change.changed_types). The caller treats every
    function that uses the type as edited;
  - new functions, new types, new constants, new files;
  - added, removed and changed imports (Change.import_names);
  - comments and blank lines anywhere.
Not allowed (Fallback): a removed or renamed type, a changed decorator of a
function outside a type, the changed signature of an overloaded function
outside a type, a changed statement at module level.

When the edit changes the number of lines, Change.shift_from is the first
old line that moved; the functions below it are affected as well.

The caller still has to find the functions that use a changed constant,
the functions of this file that use a name of Change.import_names, and the
callers of a changed or removed function, and to check that every name in
Change.new_names is fresh in the whole program: a new name that already means something can change code the edit does
not touch (overloads, trait defaults, shadowing).

This is a text classifier working from indentation, a stand-in for the
declaration hashes the compiler would use.
"""
import difflib, re

class Fallback(Exception):
    def __init__(self, reason, line=""):
        Exception.__init__(self, reason)
        self.line = line

class Change:
    def __init__(self):
        self.edited_lines = set()   # 1-based old lines of the edited existing functions
        self.edited_functions = 0
        self.new_names = []         # functions, types, constants that are new
        self.import_names = []      # names whose import into this file was added, removed or changed
        self.shift_from = None      # first old line (1-based) that moved to another line number
        self.changed_values = []    # comptime constants whose value changed
        self.changed_functions = [] # functions whose signature changed
        self.removed_functions = [] # functions that are gone
        self.changed_types = {}     # name -> old line (1-based) of the header of a type whose declaration changed
        self.new_methods = {}       # name -> (type name, old line of its header): methods added to an existing type

HEADER = re.compile(r"\s*(def|struct|trait)\s+(\w+)")
DECL = re.compile(r"\s*(def |struct |trait |comptime |@|from |import )")
VALUE = re.compile(r"\s*comptime\s+(\w+)\s*(:[^=\[]*)?=(?!=)")
IMPORT = re.compile(r"\s*(from\s+[\w.]+\s+import\s+|import\s+)(.*)$")

def indent(line):
    return len(line) - len(line.lstrip())

def noise(line):
    s = line.strip()
    return not s or s.startswith("#")

class File:
    def __init__(self, text):
        self.lines = text.splitlines()
        self._defs = None
        n = len(self.lines)
        self.in_signature = [False] * n   # continuation lines of a def header
        self.in_import = [False] * n      # lines of a parenthesized import
        # For a line inside a triple-quoted string that began on an earlier
        # line: that line. Otherwise the line itself.
        self.anchor = list(range(n))
        opened = -1
        for k, line in enumerate(self.lines):
            if opened >= 0:
                self.anchor[k] = opened
            if line.count('"""') % 2 == 1:
                opened = k if opened < 0 else -1
        i = 0
        while i < n:
            line = self.lines[i]
            opens = None
            if self.anchor[i] != i:
                pass  # text of a string, whatever it looks like
            elif re.match(r"\s*def\s", line):
                opens = self.in_signature
            elif re.match(r"\s*(from|import)\s", line):
                opens = self.in_import
            elif VALUE.match(line):
                opens = "value"  # the lines of a continued value stand for its first line
            if opens is not None:
                depth = line.count("(") + line.count("[") - line.count(")") - line.count("]")
                j = i
                while depth > 0 and j + 1 < n:
                    j += 1
                    if opens == "value":
                        self.anchor[j] = i
                    else:
                        opens[j] = True
                    l = self.lines[j]
                    depth += l.count("(") + l.count("[") - l.count(")") - l.count("]")
                i = j
            i += 1

    def defs(self, key):
        """The headers of the functions with this def_key."""
        if self._defs is None:
            self._defs = {}
            for j, line in enumerate(self.lines):
                if self.anchor[j] == j and re.match(r"\s*def\s", line):
                    self._defs.setdefault(self.def_key(j), []).append(j)
        return self._defs.get(key, [])

    def def_key(self, k):
        """(name of the enclosing type or "", name) of the function header at line k."""
        c = self.enclosing(k, ("struct", "trait"))
        return (HEADER.match(self.lines[c]).group(2) if c >= 0 else "", HEADER.match(self.lines[k]).group(2))

    def type_level(self, k):
        """The header of the type whose declaration line k belongs to: a
        field, a constant, a decorator, the header or signature of a method,
        or the header of the type itself. -1 for a line of a method body and
        for a line outside any type."""
        line = self.lines[k]
        c = self.enclosing(k, ("struct", "trait"))
        if c < 0:
            m = HEADER.match(line)
            if m and m.group(1) != "def":
                return k
            if line.lstrip().startswith("@"):
                nxt = k + 1
                while nxt < len(self.lines) and (noise(self.lines[nxt]) or self.lines[nxt].lstrip().startswith("@")):
                    nxt += 1
                m = HEADER.match(self.lines[nxt]) if nxt < len(self.lines) else None
                if m and m.group(1) != "def" and indent(self.lines[nxt]) == indent(line):
                    return nxt
            return -1
        if self.in_signature[k]:
            return c
        return c if self.enclosing(k, ("def",)) < c else -1

    def count(self, pattern):
        return sum(1 for k, line in enumerate(self.lines) if self.anchor[k] == k and re.match(pattern, line))

    def docstring(self, k):
        """True when line k belongs to a docstring of the module or of a type."""
        k = self.anchor[k]
        return self.lines[k].lstrip().startswith('"""') and self.enclosing(k, ("def",)) < 0

    def enclosing(self, k, kinds):
        """Index of the nearest header of one of `kinds` above line k that is
        less indented than it, or -1."""
        ref = self.anchor[k]
        while ref > 0 and noise(self.lines[ref]):
            ref -= 1
        ref = self.anchor[ref]
        want = indent(self.lines[ref])
        for j in range(ref - 1, -1, -1):
            if self.anchor[j] != j or noise(self.lines[j]) or self.in_signature[j] or indent(self.lines[j]) >= want:
                continue
            m = HEADER.match(self.lines[j])
            if m and m.group(1) in kinds:
                return j
            want = indent(self.lines[j])  # an enclosing block of another kind
        return -1

    def block_end(self, start):
        end = start + 1
        while end < len(self.lines) and (noise(self.lines[end]) or self.in_signature[end] or indent(self.lines[end]) > indent(self.lines[start])
                                         or self.lines[end].lstrip().startswith(")")):
            end += 1
        return end

def bindings(f):
    """{name: where it is imported from} for the import statements of a file."""
    out, head = {}, None
    for k, line in enumerate(f.lines):
        if f.anchor[k] != k:
            continue
        m = IMPORT.match(line)
        if m:
            head = m.group(1).split()[1] if m.group(1).lstrip().startswith("from") else ""
            body = m.group(2)
        elif f.in_import[k]:
            body = line
        else:
            continue
        for part in body.replace("(", " ").replace(")", " ").split(","):
            words = part.split("#")[0].replace("\\", " ").split()
            if not words:
                continue
            if head == "":
                name, source = (words[-1] if "as" in words else words[0].split(".")[0]), words[0]
            else:
                name, source = words[-1], head + ":" + words[0]
            out.setdefault(name, set()).add(source)
    return out

def classify(old_text, new_text, collect=None):
    """With a list as `collect`, every reason for a full build is appended to
    it as (reason, line) and nothing is raised; the Change returned is then
    not to be used."""
    def fail(reason, line=""):
        if collect is None:
            raise Fallback(reason, line)
        collect.append((reason, line))
    old, new = File(old_text), File(new_text)
    a, b = old.lines, new.lines
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    to_old = {}
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for d in range(i2 - i1):
                to_old[j1 + d] = i1 + d
    change = Change()
    edited = set()  # old indexes of def headers

    def is_import(f, k):
        return f.anchor[k] == k and (f.in_import[k] or bool(re.match(r"\s*(from|import)\s", f.lines[k])))

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        if i2 - i1 != j2 - j1 and change.shift_from is None:
            # Everything below moves to other line numbers. Functions embed
            # their lines (error locations), so they compile differently.
            change.shift_from = i2 + 1
        # Blank lines, comments and the docstrings of modules and types do
        # not reach the code. Text inside a string stands for the line the
        # string began on.
        olds = sorted({old.anchor[k] for k in range(i1, i2) if (old.anchor[k] != k or not noise(a[k])) and not old.docstring(k)})
        news = sorted({new.anchor[k] for k in range(j1, j2) if (new.anchor[k] != k or not noise(b[k])) and not new.docstring(k)})
        def in_changed_type(f, k):
            """True when line k is part of the declaration of a type that is
            on both sides: the type as a whole counts as changed."""
            c = f.type_level(k)
            if c < 0:
                return False
            name = HEADER.match(f.lines[c]).group(2)
            if f is new and c != k:
                # the header, signature or decorators of a method the type did not have
                d = k
                while d < len(b) - 1 and (new.lines[d].lstrip().startswith("@") or noise(new.lines[d])):
                    d += 1
                if new.in_signature[k]:
                    d = max(j for j in range(k) if re.match(r"\s*def\s", b[j]))
                if re.match(r"\s*def\s", b[d]) and not old.defs(new.def_key(d)) and old.count(r"\s*(struct|trait)\s+%s\b" % name) == 1:
                    header = next(j for j in range(len(a)) if old.anchor[j] == j and re.match(r"\s*(struct|trait)\s+%s\b" % name, a[j])) + 1
                    change.new_methods[HEADER.match(b[d]).group(2)] = (name, header)
                    return True
            pattern = r"\s*(struct|trait)\s+%s\b" % name
            if old.count(pattern) == 1 and new.count(pattern) == 1:
                change.changed_types[name] = next(j for j in range(len(a)) if old.anchor[j] == j and re.match(pattern, a[j])) + 1
                return True
            if f is old or old.count(pattern):
                fail("a type was removed, renamed or is declared more than once", f.lines[c])
                return True
            return False  # a new type

        for k in olds:
            if is_import(old, k):
                continue  # compared for the whole file below
            if in_changed_type(old, k):
                continue
            v = VALUE.match(a[k])
            if v and old.enclosing(k, ("def",)) < 0:
                # a constant: its value may change, the constant has to stay
                # a constant: its value may change, and it may go; its users are found by its name
                if old.count(r"\s*comptime\s+%s\b" % v.group(1)) == 1 and new.count(r"\s*comptime\s+%s\b" % v.group(1)) <= 1:
                    if v.group(1) not in change.changed_values:
                        change.changed_values.append(v.group(1))
                else:
                    fail("a constant is declared more than once", a[k])
                continue
            if re.match(r"\s*def\s", a[k]) or old.in_signature[k]:
                # a signature changed or the function is gone
                d = k if not old.in_signature[k] else max(j for j in range(k) if re.match(r"\s*def\s", a[j]))
                name = HEADER.match(a[d]).group(2)
                key = old.def_key(d)
                twins = new.defs(key)
                if name.startswith("__") or len(old.defs(key)) != 1 or len(twins) > 1:
                    fail("the signature of a special or overloaded function changed", a[d])
                    continue
                target = change.changed_functions if twins else change.removed_functions
                if name not in target:
                    target.append(name)
                edited.add(d)
                continue
            if DECL.match(a[k]):
                fail("an existing declaration changed or was removed", a[k])
                continue
            d = old.enclosing(k, ("def",))
            if d < 0:
                fail("a change outside a function body", a[k])
                continue
            edited.add(d)
        for k in news:
            line = b[k]
            if is_import(new, k):
                continue
            if in_changed_type(new, k):
                continue
            if not (j1 <= k < j2) and VALUE.match(line) and k in to_old and new.enclosing(k, ("def",)) < 0:
                # the text of a constant changed below its first line
                name = VALUE.match(line).group(1)
                if name not in change.changed_values:
                    change.changed_values.append(name)
                continue
            if not (j1 <= k < j2) and DECL.match(line) and k in to_old:
                # the text of a string that an existing declaration begins
                fail("an existing declaration changed or was removed", line)
                continue
            m = HEADER.match(line)
            d = new.enclosing(k, ("def",))
            v = VALUE.match(line)
            if v and new.enclosing(k, ("def",)) < 0 and old.count(r"\s*comptime\s+%s\b" % v.group(1)):
                # the new value of a constant; the old side decides
                if old.count(r"\s*comptime\s+%s\b" % v.group(1)) == 1 and new.count(r"\s*comptime\s+%s\b" % v.group(1)) == 1:
                    if v.group(1) not in change.changed_values:
                        change.changed_values.append(v.group(1))
                else:
                    fail("a constant is declared more than once", line)
                continue
            if new.in_signature[k] or (m and m.group(1) == "def" and new.anchor[k] == k):
                # a line of a signature: of a new function, or of an existing one
                d = k if not new.in_signature[k] else max(j for j in range(k) if re.match(r"\s*def\s", b[j]))
                name = HEADER.match(b[d]).group(2)
                twins = old.defs(new.def_key(d))
                if twins:
                    if name.startswith("__") or len(twins) != 1 or len(new.defs(new.def_key(d))) != 1:
                        fail("the signature of a special or overloaded function changed", line)
                        continue
                    if name not in change.changed_functions:
                        change.changed_functions.append(name)
                    edited.add(twins[0])
                    continue
                if new.in_signature[k]:
                    continue  # the signature of a new function
            if d >= 0:
                if d in to_old:
                    if m or line.lstrip().startswith("@"):
                        fail("a declaration nested in an existing function changed", line)
                    edited.add(to_old[d])
                # else: a line of a new function
                continue
            # not inside any function
            container = new.enclosing(k, ("struct", "trait"))
            in_new_type = container >= 0 and container not in to_old
            if m:
                kind, name = m.group(1), m.group(2)
                if in_new_type:
                    continue
                if container >= 0:
                    if kind != "def":
                        fail("a type nested in an existing type was added", line)
                    if name.startswith("__"):
                        fail("a special method was added to an existing type", line)
                change.new_names.append(name)
                continue
            if in_new_type:
                continue
            if line.lstrip().startswith("@"):
                nxt = k + 1
                while nxt < len(b) and (noise(b[nxt]) or b[nxt].lstrip().startswith("@")):
                    nxt += 1
                if nxt < len(b) and HEADER.match(b[nxt]) and nxt not in to_old:
                    continue
                fail("a decorator was added to an existing declaration", line)
                continue
            c = re.match(r"comptime\s+(\w+)\s*(:[^=]*)?=", line)
            if c and container < 0:
                change.new_names.append(c.group(1))
                continue
            fail("a change outside a function body", line)
    before, after = bindings(old), bindings(new)
    change.import_names = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    for d in edited:
        change.edited_lines.update(range(d + 1, old.block_end(d) + 1))
    change.edited_functions = len(edited)
    return change
