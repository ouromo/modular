#!/usr/bin/env python3
"""historyrun.py : run the incremental driver on every change of a repository's history.

  historyrun.py --repo CLONE --work DIR --env PIXI_ENV --stdlib DIR --out RESULTS.tsv
                [--dep NAME=CLONE...] [--only REV...] [--limit N] -- COMPILER

For each first-parent commit that changes Mojo files under src/ or bin/, with
its parent as the previous build:

  1. the rules of changes.py decide from the two source states whether the
     change may take the incremental path; if not, the row says why;
  2. otherwise the parent is built in full (manifest and split cache), the
     commit is checked out, incr.py builds it incrementally, and a full
     build of the commit gives the reference. The two manifests must hold
     the same objects, and the two binaries must print the same help text.

CLONE is a clone this script may check out freely. --dep names a dependency
compiled from a sibling checkout: NAME is its key in pixi.toml, CLONE a clone
that is checked out at the revision each commit pins. The packages of
PIXI_ENV stay the same for the whole run; a commit that does not build with
them is reported as such.

Columns: commit, date, Mojo files changed, result, affected functions,
incremental seconds, full build seconds, same objects, detail.
"""
import argparse, os, re, shutil, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from changes import classify, Fallback

ap = argparse.ArgumentParser()
for a in ("repo", "work", "env", "stdlib", "out"):
    ap.add_argument("--" + a, required=True)
ap.add_argument("--dep", action="append", default=[], metavar="NAME=CLONE")
ap.add_argument("--only", action="append", default=[])
ap.add_argument("--max-reach", default="0.7", help="passed to incr.py")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--main", default="bin/scheduler.mojo")
ap.add_argument("--max-functions", default="400")
ap.add_argument("--budget", type=float, default=1.0, help="share of the last full build's time after which the driver stops trying")
ap.add_argument("--tip", default="origin/HEAD", help="the commit whose history is walked")
ap.add_argument("compiler", nargs="+")
o = ap.parse_args()
repo, work = os.path.abspath(o.repo), os.path.abspath(o.work)
deps = dict((d.split("=", 1)[0], os.path.abspath(d.split("=", 1)[1])) for d in o.dep)
here = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ, MODULAR_HOME=o.env + "/share/max", PATH=o.env + "/bin:" + os.environ["PATH"],
           MODULAR_TELEMETRY_ENABLED="false", MODULAR_CRASH_REPORTING_ENABLED="false")
cache = os.path.join(work, "sc"); os.makedirs(cache, exist_ok=True)

def git(*a, cwd=repo):
    return subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, text=True).stdout

def pins(rev):
    """({dependency: pinned revision}, include options) of the stable environment at rev."""
    toml = git("show", rev + ":pixi.toml")
    found = {}
    for name in deps:
        m = re.search(r'^%s\s*=\s*\{[^}]*rev\s*=\s*"([0-9a-f]+)"' % re.escape(name), toml, re.M)
        found[name] = m.group(1) if m else None
    m = re.search(r'^DEP_INC\s*=\s*"([^"]*)"', toml, re.M)
    return found, ["-I", "src"] + (m.group(1).split() if m else [])

def checkout(rev):
    subprocess.run(["git", "-C", repo, "checkout", "-q", "-f", "--detach", rev], check=True)
    subprocess.run(["git", "-C", repo, "clean", "-fdq", "src", "bin"], check=True)
    for name, pinned in pins(rev)[0].items():
        if pinned:
            subprocess.run(["git", "-C", deps[name], "checkout", "-q", "-f", "--detach", pinned], check=True)

def full_build(tag, d, include):
    """Full build of the checked out source. Returns (ok, seconds)."""
    own = os.path.join(d, "k-" + tag)
    cmd = o.compiler + ["build", "--split-cache-dir", cache, "--split-cache-manifest", os.path.join(d, tag + ".mf")] + include + \
          [o.main, "--emit", "object", "-o", os.path.join(d, tag + ".o")]
    start = time.time()
    run = subprocess.run(cmd, cwd=repo, env=dict(env, MODULAR_CACHE_DIR=own), capture_output=True, text=True)
    shutil.rmtree(own, ignore_errors=True)
    open(os.path.join(d, tag + ".log"), "w").write(run.stdout + run.stderr)
    return run.returncode == 0, time.time() - start

def link(obj, exe):
    return subprocess.run(["cc", obj, o.env + "/lib/libKGENCompilerRTShared.so", "-Xlinker", "-rpath", "-Xlinker", o.env + "/lib",
                           "-o", exe, "-Wl,--gc-sections", "-lm"], capture_output=True, text=True).returncode == 0

def help_text(exe):
    run = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=60)
    return run.returncode, run.stdout + run.stderr

def prefilter(parent, rev):
    """(reason for a full build or None, [(status, file)])."""
    names = git("diff", "--name-status", "--no-renames", parent, rev, "--", "src", "bin").splitlines()
    files = [tuple(l.split("\t")) for l in names if l.endswith(".mojo")]
    if pins(parent) != pins(rev):
        return "a dependency or the include path changed", files
    if any(s == "D" for s, _ in files):
        return "a file was removed", files
    for status, f in files:
        if status == "M":
            try:
                classify(git("show", "%s:%s" % (parent, f)), git("show", "%s:%s" % (rev, f)))
            except Fallback as e:
                return "%s: %s" % (f, e), files
    return None, files

revs = git("log", "--first-parent", "--reverse", "--format=%H", o.tip, "--", "src", "bin").split()
revs = [r for r in revs if any(l.endswith(".mojo") for l in git("diff", "--name-only", r + "~1", r, "--", "src", "bin").splitlines())]
if o.only:
    wanted = [git("rev-parse", r).strip() for r in o.only]
    revs = [r for r in revs if r in wanted]
if o.limit:
    revs = revs[-o.limit:]
out = open(o.out, "a")
def row(rev, files, result, affected="", incr="", full="", same="", detail=""):
    date = git("log", "-1", "--format=%ad", "--date=short", rev).strip()
    out.write("\t".join([rev[:9], date, str(len(files)), result, str(affected), incr, full, same, detail.replace("\t", " ").replace("\n", " ")[:300]]) + "\n")
    out.flush()

previous = None  # (rev, directory) of the last full build that succeeded
last_full = 0    # seconds the last full build took
for rev in revs:
    parent = git("rev-parse", rev + "~1").strip()
    reason, files = prefilter(parent, rev)
    if reason:
        row(rev, files, "full build: rule", detail=reason); continue
    if not git("ls-tree", "--name-only", parent, o.main).strip():
        row(rev, files, "not run: no program to build at the parent"); continue
    d = os.path.join(work, rev[:9]); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    include = pins(parent)[1]
    checkout(parent)
    if previous and previous[0] == parent:
        for suffix in ("", ".src", ".abi", ".loc", ".dup", ".use", ".eq"):
            shutil.copy(os.path.join(previous[1], "truth.mf" + suffix), os.path.join(d, "base.mf" + suffix))
        shutil.copy(os.path.join(previous[1], "truth-exe"), os.path.join(d, "base-exe"))
    else:
        ok, last_full = full_build("base", d, include)
        ok = ok and link(os.path.join(d, "base.o"), os.path.join(d, "base-exe"))
        if not ok:
            row(rev, files, "not run: the parent does not build in the test environment", detail=open(os.path.join(d, "base.log")).read()[-300:]); continue
    edits = []
    for status, f in files:
        if status == "M":
            old = os.path.join(d, f.replace("/", "_")); open(old, "w").write(git("show", "%s:%s" % (parent, f)))
            edits += ["--edit", "%s=%s" % (f, old)]
        else:
            edits += ["--edit", f + "=-"]
    checkout(rev)
    roots = [p for p in include if p != "-I"] + [o.stdlib]
    cmd = [sys.executable, os.path.join(here, "incr.py"), "--base", os.path.join(d, "base.mf"), "--cache", cache, "--work", d,
           "--out", os.path.join(d, "merged.o"), "--max-functions", o.max_functions, "--main-dir", os.path.dirname(o.main), "--budget", str(o.budget * last_full), "--full-time", str(last_full), "--max-reach", o.max_reach] + edits + sum((["--root", r] for r in roots), []) + ["--"] + o.compiler + ["build", "--emit", "object"] + include
    start = time.time()
    run = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True)
    open(os.path.join(d, "incr.log"), "w").write(run.stdout + run.stderr)
    m = re.search(r"affected functions (\d+)", run.stdout); affected = m.group(1) if m else ""
    incremental = run.returncode in (0, 3)
    if run.returncode == 3:
        # no function of the previous build is affected: its output stands
        seconds = time.time() - start
        shutil.copy(os.path.join(d, "base.mf"), os.path.join(d, "merged.mf"))
        os.rename(os.path.join(d, "base-exe"), os.path.join(d, "merged-exe"))
    elif incremental:
        ok = link(os.path.join(d, "merged.o"), os.path.join(d, "merged-exe"))
        seconds = time.time() - start
        if not ok:
            row(rev, files, "WRONG: the merged object does not link", affected, "%.1f" % seconds); continue
    elif run.returncode != 2:
        row(rev, files, "driver error", affected, detail=(run.stdout + run.stderr)[-300:]); continue
    else:
        m = re.search(r"FALLBACK: (.*)", run.stdout); why = m.group(1) if m else ""
    # the reference: a full build of the same source
    ok, full = full_build("truth", d, include)
    if not ok:
        row(rev, files, "not run: the commit does not build in the test environment", affected, detail=open(os.path.join(d, "truth.log")).read()[-300:])
        previous = None; continue
    link(os.path.join(d, "truth.o"), os.path.join(d, "truth-exe"))
    previous = (rev, d); last_full = full
    if not incremental:
        row(rev, files, "full build: driver", affected, full="%.1f" % full, detail=why); continue
    merged = {l.split("\t")[0] for l in open(os.path.join(d, "merged.mf"))}
    truth = {l.split("\t")[0] for l in open(os.path.join(d, "truth.mf"))}
    same = merged == truth
    try:
        behaves = help_text(os.path.join(d, "merged-exe")) == help_text(os.path.join(d, "truth-exe"))
    except Exception as e:
        behaves = False
    detail = "only in merged %d, only in full build %d; help text %s" % (len(merged - truth), len(truth - merged), "same" if behaves else "DIFFERS")
    row(rev, files, "incremental", affected, "%.1f" % seconds, "%.1f" % full, "yes" if same else "NO", detail)
    for f in ("merged.o", "truth.o", "base.o", "merged-exe", "base-exe"):
        try: os.remove(os.path.join(d, f))
        except OSError: pass
print("HISTORYDONE")
