#!/usr/bin/env python3
"""Gate a drone script before it joins any loop: lint it, then run it in simulate() on fixed seeds.

usage: precheck.py FILE.py [--seeds 3,8] [--speedup 64] [--timeout 120]
                   [--unlocks current|all] [--items current|none|JSON] [--save-dir DIR] [--dry-run]

Steps:
  1. Run lint_tfwr.py on FILE. Lint errors fail the check; lint warnings print only.
  2. Stage FILE through the bridge into a minimized window named claude_<stem>.
  3. For each seed, run a small runner in main that calls simulate() on that window.
  4. Parse the new output with the PHASE convention (scripts/tfwr_log.md).

We fail (exit 1) on:
  - a lint error,
  - an error or a warning in any seed's run,
  - any PHASE line that reports `work 0`,
  - a seed whose run printed no PHASE or SUMMARY line at all.

An endless loop never returns from simulate(), so its run ends on our timeout.
That is no failure: we judge the lines it printed and report the last PHASE time.

Options:
  --seeds 3,8        simulate() seeds, one run each. Fixed seeds serve fair A/B timing only;
                     never learn a layout from one.
  --speedup N        simulate()'s speedup argument (default 64).
  --timeout S        real seconds per seed before we stop the run (default 120).
  --unlocks MODE     current: this save's levels (default). all: pass the Unlocks enum.
  --items MODE       current: this save's stock (default). none: {}. Or a JSON object of
                     item names to counts, e.g. '{"Item_A": 500}'.
  --save-dir DIR     passed to the linter, to resolve imports and unlock checks.
  --dry-run          print the runner code and stop. Touches no game.

Exit codes: 0 pass, 1 fail, 2 setup problem (no bridge, bad arguments).
Run it under a low-priority wrapper if your OS has one; it does nothing heavy itself.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge", "client"))

# The one parser for the PHASE convention. economy_driver.py imports these too.
# The token anchors the match, not the line start, and numbers can print as 3 or 3.0.
NUM = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
PHASE_RE = re.compile(r"(?:^|\s)PHASE\s+(\S+)\s+t\s+(%s)\s+work\s+(%s)\s+left\s+(%s)" % (NUM, NUM, NUM))
SUMMARY_RE = re.compile(r"(?:^|\s)SUMMARY\b(.*)")
# A malformed PHASE line still counts as a line, but we report it.
LOOSE_PHASE_RE = re.compile(r"(?:^|\s)PHASE\b")


def parse_log(text):
    """Reads PHASE and SUMMARY lines from output text.

    Returns a dict: phases (list of {name, t, work, left, line}), summaries (lines),
    zero (phases with work 0), malformed (PHASE lines that miss the shape), last_t.
    """
    phases, summaries, malformed = [], [], []
    for line in text.splitlines():
        m = PHASE_RE.search(line)
        if m:
            phases.append({"name": m.group(1), "t": float(m.group(2)), "work": float(m.group(3)),
                           "left": float(m.group(4)), "line": line.strip()})
        elif LOOSE_PHASE_RE.search(line):
            malformed.append(line.strip())
        s = SUMMARY_RE.search(line)
        if s:
            summaries.append(line.strip())
    return {
        "phases": phases,
        "summaries": summaries,
        "zero": [p for p in phases if p["work"] == 0],
        "malformed": malformed,
        "last_t": max((p["t"] for p in phases), default=None),
    }


def opt(argv, name, default):
    if name in argv:
        i = argv.index(name)
        if i + 1 >= len(argv):
            sys.exit("%s needs a value" % name)
        val = argv[i + 1]
        del argv[i:i + 2]
        return val
    return default


def window_for(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem if stem.startswith("claude_") else "claude_" + stem


def items_code(mode):
    if mode == "current":
        return "items = {}\nfor it in Items:\n\titems[it] = num_items(it)\n"
    if mode == "none":
        return "items = {}\n"
    try:
        data = json.loads(mode)
    except ValueError:
        sys.exit("--items takes current, none or a JSON object")
    parts = []
    for k, v in data.items():
        name = k[len("Items."):] if k.startswith("Items.") else k
        if not re.match(r"^[A-Za-z_]\w*$", name):
            sys.exit("bad item name in --items: %r" % k)
        v = float(v)
        parts.append("Items.%s: %s" % (name, "%d" % v if v.is_integer() else repr(v)))
    return "items = {%s}\n" % ", ".join(parts)


def runner_code(window, seed, speedup, unlocks, items):
    """The code main runs for one seed. Tabs only, as the game expects."""
    if unlocks == "all":
        ul = "ul = Unlocks\n"
    else:
        ul = "ul = {}\nfor u in Unlocks:\n\tul[u] = num_unlocked(u)\n"
    return (ul + items_code(items)
            + 't = simulate("%s", ul, items, {}, %d, %s)\n' % (window, seed, speedup)
            + 'quick_print("PRECHECK_END seed", %d, "t", t)\n' % seed)


def lint(path, save_dir):
    cmd = [sys.executable, os.path.join(HERE, "lint_tfwr.py"), path]
    if save_dir:
        cmd += ["--save-dir", save_dir]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return p.returncode, p.stdout


def judge(out, state):
    """Returns (problems, summary dict) for one seed's output."""
    log = parse_log(out)
    problems = []
    if state in ("error", "warning", "watchdog"):
        tail = out[max(out.rfind("Error"), out.rfind("Warning"), 0):][:300]
        problems.append("%s: %s" % (state, tail.strip() or "(no message)"))
    elif re.search(r"\bError\b|\bWarning:", out):
        problems.append("the output mentions an error or warning")
    for p in log["zero"]:
        problems.append("zero work: " + p["line"])
    if not log["phases"] and not log["summaries"]:
        problems.append("no PHASE or SUMMARY line at all (see scripts/tfwr_log.md)")
    m = re.search(r"PRECHECK_END seed\s+\S+\s+t\s+(%s)" % NUM, out)
    return problems, {
        "game_seconds": float(m.group(1)) if m else None,
        "last_phase_t": log["last_t"],
        "phase_lines": len(log["phases"]),
        "summary_lines": len(log["summaries"]),
        "malformed": len(log["malformed"]),
    }


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    seeds = [int(s) for s in opt(argv, "--seeds", "3,8").split(",") if s.strip()]
    speedup = float(opt(argv, "--speedup", "64"))
    timeout = float(opt(argv, "--timeout", "120"))
    unlocks = opt(argv, "--unlocks", "current")
    items = opt(argv, "--items", "current")
    save_dir = opt(argv, "--save-dir", None)
    dry = "--dry-run" in argv
    files = [a for a in argv if not a.startswith("--")]
    if len(files) != 1 or unlocks not in ("current", "all"):
        print(__doc__)
        return 2
    path = files[0]
    with open(path, encoding="utf-8") as f:
        code = f.read()
    window = window_for(path)

    rc, text = lint(path, save_dir)
    sys.stdout.write(text)
    if rc != 0:
        print("PRECHECK FAIL: lint errors")
        return 1
    if dry:
        for s in seeds:
            print("# runner for seed %d, staged window %s" % (s, window))
            sys.stdout.write(runner_code(window, s, speedup, unlocks, items))
        return 0

    from tfwr_bridge import Bridge, BridgeError
    try:
        b = Bridge(timeout=60)
    except OSError as e:
        print("cannot reach the bridge: %s" % e)
        return 2
    failed = False
    try:
        for s in seeds:
            # The plugin closes claude_ windows once nothing runs, so stage right before each run.
            b.set_code(window, code)
            out, secs, state = b.run_and_wait("main", runner_code(window, s, speedup, unlocks, items),
                                              timeout=timeout, wait_for="precheck %s seed %d" % (window, s))
            problems, info = judge(out, state)
            gs = info["game_seconds"]
            if gs is not None:
                when = "%.1f game s" % gs
            elif state == "timeout":
                when = "no return (we stopped it at %.0f real s), last PHASE t %s" % (secs, info["last_phase_t"])
            else:
                when = "no PRECHECK_END line, last PHASE t %s" % info["last_phase_t"]
            print("seed %d: %s, %s; %d PHASE, %d SUMMARY, %d malformed line(s)" % (
                s, state, when, info["phase_lines"], info["summary_lines"], info["malformed"]))
            for p in problems:
                print("  FAIL " + p)
            failed = failed or bool(problems)
    except BridgeError as e:
        print("bridge error: %s" % e)
        return 2
    finally:
        b.close()
    print("PRECHECK FAIL" if failed else "PRECHECK PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
