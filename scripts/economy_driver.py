#!/usr/bin/env python3
"""Economy driver: rotate the agent's farm scripts in time slices and buy unlocks between them.

usage: economy_driver.py --registry economy.json [--workdir DIR] [--slices N] [--remeasure N]
                         [--ff F] [--tree PATH] [--dry-run]

You supply every farm and every number in a JSON registry. The driver knows no crop,
cost or strategy. It measures each farm's rates itself and spends greedily.

Registry schema (paths resolve against the registry's folder):

  {
    "farms": [
      {"name": "farm_a", "file": "farm_a.py", "seconds": 40, "surface": true,
       "produces": ["Item_A"], "consumes": ["Item_B"]},
      {"name": "farm_b", "file": "farm_b.py", "seconds": 60, "surface": false,
       "produces": ["Item_B"], "consumes": []}
    ],
    "surface_code": "to_surface.py",
    "reserves": {"Item_B": 500},
    "targets": {"Item_A": 100000},
    "consume_floor": 100
  }

  farms          name (one word), file, seconds (real seconds per slice), surface,
                 produces and consumes (item names as in Items.X, without "Items.").
  surface_code   code you write that brings the drone to the state your surface farms
                 expect. We prepend it to every farm with "surface": true.
  reserves       the driver never spends an item below its reserve, and farms it back
                 up when it drops below. Default 0.
  targets        extra amounts to reach, e.g. for an achievement or a volume goal.
  consume_floor  automatic reserve for every item a farm consumes (default 100). Once we
                 measure a consumer, the floor rises to twice its use per slice, if larger.
                 An explicit reserve overrides the floor.

Each round:
  1. Probe the stock (num_items over Items) and every unlock's level.
  2. Keep only unlocks the player can see: owned, or every prerequisite met
     (references/tech_tree.json; run scripts/extract_tech_tree.py first). Probe their next costs.
  3. Need = the sum of visible next costs + targets + reserves. Short = need - stock.
  4. Pick a slice. An unmeasured farm runs first. Otherwise, for each short item, take
     the farm with its best measured rate; run the farm whose item finishes last.
  5. Run the slice in main for its seconds, at a fixed fast-forward, so rates compare.
  6. Measure each item's change per real second; then buy greedily, respecting reserves.

We stop on problems and never pass allow_warnings:
  - An error or warning in a slice or probe: we save its output, write <workdir>/STOP
    with the reason, and exit 7.
  - A `work 0` PHASE line (scripts/tfwr_log.md): we log a CHECK line and keep going.

Files in <workdir> (default: <registry folder>/economy):
  economy.log     human log, one line per slice.
  economy.jsonl   one JSON object per slice: time, farm, why, state, seconds, gains, purchases.
  slices/         every slice's full output.
  driver.json     heartbeat for scripts/campaign.py: pid, state, updated, expect_by, exit.
  STOP            create it to stop the driver after the current slice; delete it to resume.

Exit codes: 0 nothing short (driver.json says whether the tree is done), 2 setup problem,
3 a short item that no farm makes, 4 costs left that unlock() refused, or unlocks
missing from tech_tree.json, 5 STOP file,
6 slice limit reached, 7 error or warning (STOP written).

The buyer spends the player's items. Run it only with the player's standing permission to buy.
Run it under your OS's low-priority wrapper if you have one; the game does the heavy work.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "bridge", "client"))
from precheck import NUM, parse_log  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
DEFAULT_TREE = os.path.join(HERE, "..", "references", "tech_tree.json")
ENUMS = os.path.join(HERE, "..", "references", "enums.json")

# Probes print one line each, because the game's output buffer keeps only recent lines.
STOCK = """s = ""
for it in Items:
	s = s + str(it) + "=" + str(num_items(it)) + " "
quick_print("STOCK", s)
"""

# Fair play: we read levels for every unlock, but costs only for nodes the player can see.
LEVELS = """s = ""
for u in Unlocks:
	s = s + str(u) + "@" + str(num_unlocked(u)) + ";"
quick_print("LEVELS", s)
"""

COSTS = """WANT = %s
s = ""
for u in WANT:
	s = s + str(u) + "@" + str(num_unlocked(u))
	c = get_cost(u)
	if c != None:
		for it in c:
			s = s + "," + str(it) + "=" + str(c[it])
	s = s + ";"
quick_print("NEED", s)
"""

BUYER = """RESERVE = %s
WANT = %s
def ok(c):
	for it in c:
		r = 0
		if it in RESERVE:
			r = RESERVE[it]
		if num_items(it) - c[it] < r:
			return False
	return True
changed = True
while changed:
	changed = False
	for u in WANT:
		c = get_cost(u)
		if c != None and len(c) > 0 and ok(c) and unlock(u):
			quick_print("BUY", u, "level", num_unlocked(u))
			changed = True
quick_print("BUYER done")
"""


class Problem(Exception):
    """A slice or probe went wrong; the message names the saved output."""


def lit(n):
    """A number as in-game source: integers without a decimal point or exponent."""
    n = float(n)
    return "%d" % n if n.is_integer() else repr(n)


def wrapped(items, opening, closing, per_line=4):
    """Formats a list literal over short lines.

    A code window is as wide as its longest line, and that width limits the whole
    view's zoom (2026-11-04: one long WANT line held a 4K screen at zoom 0.2).
    """
    rows = [", ".join(items[i:i + per_line]) for i in range(0, len(items), per_line)]
    return opening + ",\n\t".join(rows) + closing


def unlock_list(names):
    return wrapped(["Unlocks.%s" % u for u in names], "[", "]")


def bare(name, prefix):
    name = str(name).strip()
    return name[len(prefix):] if name.startswith(prefix) else name


# ---------- registry ----------

def load_registry(path):
    with open(path, encoding="utf-8") as f:
        reg = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    farms = reg.get("farms") or []
    if not farms:
        raise SystemExit("registry: no farms")
    surface = None
    if reg.get("surface_code"):
        with open(os.path.join(base, reg["surface_code"]), encoding="utf-8") as f:
            surface = f.read()
    seen = set()
    for fm in farms:
        for key in ("name", "file", "seconds"):
            if key not in fm:
                raise SystemExit("registry: a farm lacks %r: %s" % (key, fm))
        if not NAME_RE.match(fm["name"]) or fm["name"] in seen:
            raise SystemExit("registry: farm names must be unique identifiers: %r" % fm["name"])
        seen.add(fm["name"])
        fm["produces"] = [bare(x, "Items.") for x in fm.get("produces", [])]
        fm["consumes"] = [bare(x, "Items.") for x in fm.get("consumes", [])]
        fm["seconds"] = float(fm["seconds"])
        fm["surface"] = bool(fm.get("surface", False))
        if fm["surface"] and surface is None:
            raise SystemExit("registry: farm %s needs the surface, but no surface_code is set" % fm["name"])
        with open(os.path.join(base, fm["file"]), encoding="utf-8") as f:
            code = f.read()
        fm["code"] = (surface.rstrip("\n") + "\n" + code) if fm["surface"] else code
    reg["reserves"] = {bare(k, "Items."): float(v) for k, v in (reg.get("reserves") or {}).items()}
    reg["targets"] = {bare(k, "Items."): float(v) for k, v in (reg.get("targets") or {}).items()}
    reg["consume_floor"] = float(reg.get("consume_floor", 100))
    names = set(reg["reserves"]) | set(reg["targets"])
    for fm in farms:
        names |= set(fm["produces"]) | set(fm["consumes"])
    bad = [n for n in names if not NAME_RE.match(n)]
    if bad:
        raise SystemExit("registry: bad item names %s" % bad)
    reg["items"] = names
    return reg


def enum_warnings(names):
    """Item names missing from references/enums.json. The in-game stock probe decides for real."""
    try:
        with open(ENUMS) as f:
            known = set(json.load(f).get("Items", []))
    except (OSError, ValueError):
        return []
    return sorted(n for n in names if known and n not in known)


def lint_farms(reg, workdir):
    """Lints each farm as the driver will send it. Returns a list of failing farm names."""
    d = os.path.join(workdir, "lint")
    os.makedirs(d, exist_ok=True)
    bad = []
    for fm in reg["farms"]:
        p = os.path.join(d, fm["name"] + ".py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(fm["code"])
        r = subprocess.run([sys.executable, os.path.join(HERE, "lint_tfwr.py"), "--quiet", p],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode != 0:
            bad.append(fm["name"])
            sys.stdout.write(r.stdout)
    return bad


def missing_unlocks(bridge, workdir):
    """Lints the driver's own in-game code against the current save.

    The driver needs Debug (quick_print), Costs (get_cost), Auto_Unlock (unlock)
    and the core language unlocks. On a fresh save they come later, so the early
    game is played by hand. Returns the linter's missing-unlock lines, or [].
    """
    try:
        bridge.save()
        info = bridge.ping()
        slot_dir = os.path.join(info["data_dir"], "Saves", info["active_save"])
    except Exception:
        return []  # no live save to check (tests, or an old plugin): let the probes decide
    d = os.path.join(workdir, "lint")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "driver_code.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(STOCK + LEVELS + COSTS % "[]" + BUYER % ("{}", "[]"))
    r = subprocess.run([sys.executable, os.path.join(HERE, "lint_tfwr.py"), "--save-dir", slot_dir, p],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return [ln.strip() for ln in r.stdout.splitlines() if "unlock" in ln.lower() and "ERROR" in ln.upper()]


# ---------- visibility (fair play: never act on hidden ???? nodes) ----------

def load_tree(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        d = json.load(f)
    u = d.get("unlocks", d)
    rows = u.values() if isinstance(u, dict) else u
    return {(r.get("enum_name_guess") or r["id"]): r for r in rows}


def visible(tree, levels, name):
    """Owned, or every prerequisite met. Same rule as scripts/unlocks.py."""
    e = tree.get(name)
    if not e:
        return False
    if levels.get(name, 0) > 0:
        return True
    byid = {r["id"]: n for n, r in tree.items()}
    return all(levels.get(byid.get(r["id"], r["id"]), 0) >= r["level"] for r in (e.get("requires") or []))


# ---------- parsing ----------

def parse_stock(out):
    m = re.search(r"STOCK (.*)", out)
    if not m:
        return None
    return {bare(k, "Items."): float(v) for k, v in re.findall(r"(\S+?)=(%s)" % NUM, m.group(1))}


def parse_need(out, tag="NEED"):
    """Returns {unlock: (level, {item: n})}, or None if the tag's line is missing."""
    m = re.search(tag + r" (.*)", out)
    if not m:
        return None
    res = {}
    for entry in m.group(1).split(";"):
        entry = entry.strip()
        if not entry or "@" not in entry:
            continue
        head, _, rest = entry.partition(",")
        u, _, lv = head.partition("@")
        cost = {bare(k, "Items."): float(v) for k, v in re.findall(r"([^,=]+)=(%s)" % NUM, rest)}
        res[bare(u, "Unlocks.")] = (float(lv or 0), cost)
    return res


def visible_costs(run, tree):
    """Probes levels, then costs of visible unlocks only. run(name, code) returns output text.

    Returns (costs {unlock: {item: n}}, unknown [unlocks missing from the tree, not owned]),
    or None if a probe printed no line.
    """
    rows = parse_need(run("probe_levels", LEVELS), "LEVELS")
    if rows is None:
        return None
    levels = {u: lv for u, (lv, _) in rows.items()}
    unknown = sorted(u for u in levels if u not in tree and levels[u] <= 0)
    seen = [u for u in levels if visible(tree, levels, u) or (u not in tree and levels[u] > 0)]
    if not seen:
        return {}, unknown
    rows = parse_need(run("probe_costs", COSTS % unlock_list(seen)))
    if rows is None:
        return None
    return {u: c for u, (_, c) in rows.items() if c}, unknown


def parse_buys(out):
    return [(u, float(lv)) for u, lv in re.findall(r"BUY\s+(?:Unlocks\.)?(\w+)\s+level\s+(%s)" % NUM, out)]


# ---------- the driver ----------

class Driver:
    def __init__(self, reg, workdir, bridge, ff, remeasure, tree):
        self.reg, self.wd, self.b, self.ff = reg, workdir, bridge, ff
        self.remeasure, self.tree = remeasure, tree
        self.rates = {}  # farm -> {item: change per real second}
        self.used = {}  # farm -> {item: amount consumed in its last slice}
        self.last_run = {}  # farm -> slice index
        self.k = 0
        self.unknown = set()  # unowned unlocks missing from tech_tree.json
        os.makedirs(os.path.join(workdir, "slices"), exist_ok=True)

    # files
    def path(self, *p):
        return os.path.join(self.wd, *p)

    def log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        print(line, flush=True)
        with open(self.path("economy.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def jsonl(self, row):
        with open(self.path("economy.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def beat(self, state, **extra):
        row = {"pid": os.getpid(), "state": state, "updated": time.time(), "slice": self.k}
        row.update(extra)
        tmp = self.path("driver.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(row, f, sort_keys=True)
        os.replace(tmp, self.path("driver.json"))

    def archive(self, name, out):
        fn = "%s_%04d_%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), self.k, name)
        with open(self.path("slices", fn), "w", encoding="utf-8") as f:
            f.write(out)
        return os.path.join("slices", fn)

    def stop_file(self, reason):
        with open(self.path("STOP"), "w", encoding="utf-8") as f:
            f.write(reason + "\n")

    # game calls
    def run(self, name, code, secs, wait_for):
        self.beat("running", farm=name, expect_by=time.time() + secs + 90)
        out, took, state = self.b.run_and_wait("main", code, timeout=secs, wait_for=wait_for,
                                               ff=self.ff, ff_after=0, ff_step=0)
        if state in ("error", "warning", "watchdog"):
            rel = self.archive(name, out)
            raise Problem("%s: %s; output saved in %s" % (name, state, rel))
        return out, took, state

    def stock(self):
        out, _, _ = self.run("probe_stock", STOCK, 20, "stock probe")
        st = parse_stock(out)
        if st is None:
            raise Problem("probe_stock: no STOCK line; output saved in %s" % self.archive("probe_stock", out))
        return st

    def need(self):
        outs = {}

        def run(name, code):
            outs[name] = self.run(name, code, 20, "unlock probe")[0]
            return outs[name]
        got = visible_costs(run, self.tree)
        if got is None:
            name = sorted(outs)[-1]
            raise Problem("%s: no probe line; output saved in %s" % (name, self.archive(name, outs[name])))
        costs, unknown = got
        new = [u for u in unknown if u not in self.unknown]
        if new:
            self.log("CHECK not in tech_tree.json and not owned, so we skip them: %s. "
                     "Re-run extract_tech_tree.py." % ", ".join(new))
        self.unknown = set(unknown)
        return costs

    def reserves(self):
        res = dict()
        for fm in self.reg["farms"]:
            for it in fm["consumes"]:
                floor = self.reg["consume_floor"]
                for other in self.reg["farms"]:
                    if it in other["consumes"]:
                        floor = max(floor, 2 * self.used.get(other["name"], {}).get(it, 0))
                res[it] = max(res.get(it, 0), floor)
        res.update(self.reg["reserves"])
        return res

    def buy(self, costs, stock):
        res = self.reserves()
        # Cheapest first, relative to what we hold, so small buys don't wait behind big ones.
        order = sorted(costs, key=lambda u: max(v / max(stock.get(i, 0), 1) for i, v in costs[u].items()))
        reserve = wrapped(["Items.%s: %s" % (i, lit(n)) for i, n in sorted(res.items()) if n > 0], "{", "}")
        want = unlock_list(order)
        if not order:
            return []
        out, _, state = self.run("buyer", BUYER % (reserve, want), 30, "buying unlocks")
        if "BUYER done" not in out:
            raise Problem("buyer: %s before it finished; output saved in %s" % (state, self.archive("buyer", out)))
        return parse_buys(out)

    def requirement(self, costs):
        req = {}
        for c in costs.values():
            for it, n in c.items():
                req[it] = req.get(it, 0) + n
        for extra in (self.reg["targets"], self.reserves()):
            for it, n in extra.items():
                req[it] = req.get(it, 0) + n
        return req

    def pick(self, short):
        """Returns (farm, why, stuck items)."""
        farms = self.reg["farms"]
        for fm in farms:
            if fm["name"] not in self.rates:
                return fm, "unmeasured", []
        if self.remeasure:
            for fm in farms:
                # Refresh only farms that declare a short item; an idle farm's rate doesn't matter now.
                if any(it in short for it in fm["produces"]) and self.k - self.last_run[fm["name"]] > self.remeasure:
                    return fm, "remeasure (last run %d slices ago)" % (self.k - self.last_run[fm["name"]]), []
        best, worst, why, stuck = None, -1.0, "", []
        for it, gap in sorted(short.items()):
            cands = [(self.rates[fm["name"]].get(it, 0), fm) for fm in farms]
            cands = [c for c in cands if c[0] > 0]
            if not cands:
                stuck.append(it)
                continue
            rate, fm = max(cands, key=lambda c: c[0])
            eta = gap / rate
            if eta > worst:
                best, worst = fm, eta
                why = "%s short %.4g, ~%.0f min at its best rate" % (it, gap, eta / 60)
        return best, why, stuck

    def slice(self, fm, before):
        name = fm["name"]
        self.say("Economy: %s for %.0f s" % (name, fm["seconds"]))
        out, took, state = self.run(name, fm["code"], fm["seconds"], "economy slice " + name)
        rel = self.archive(name, out)
        after = self.stock()
        delta = {it: after.get(it, 0) - before.get(it, 0) for it in set(after) | set(before)}
        self.rates[name] = {it: d / max(took, 1e-6) for it, d in delta.items()}
        self.used[name] = {it: -d for it, d in delta.items() if d < 0}
        self.last_run[name] = self.k
        checks = []
        lg = parse_log(out)
        for p in lg["zero"]:
            checks.append("zero work: " + p["line"])
        if lg["malformed"]:
            checks.append("%d malformed PHASE line(s)" % len(lg["malformed"]))
        if not lg["phases"] and not lg["summaries"]:
            checks.append("no PHASE or SUMMARY line")
        for c in checks[:5]:
            self.log("CHECK %s: %s; see %s" % (name, c, rel))
        if len(checks) > 5:
            self.log("CHECK %s: %d more; see %s" % (name, len(checks) - 5, rel))
        gains = {it: d for it, d in delta.items() if d != 0}
        return state, took, gains, after, rel, checks

    def say(self, text):
        try:
            self.b.say(text)
        except Exception:
            pass

    def loop(self, max_slices):
        while True:
            if os.path.exists(self.path("STOP")):
                self.log("STOP file found; exiting. Delete it to resume.")
                return 5, {}
            if max_slices and self.k >= max_slices:
                self.log("slice limit %d reached" % max_slices)
                return 6, {}
            st = self.stock()
            missing = sorted(i for i in self.reg["items"] if i not in st)
            if missing:
                self.log("registry items the game doesn't list: %s. Fix the registry." % missing)
                return 2, {"missing": missing}
            costs = self.need()
            req = self.requirement(costs)
            short = {it: n - st.get(it, 0) for it, n in req.items() if n - st.get(it, 0) > 0}
            if not short:
                bought = self.buy(costs, st)
                left = self.need()
                summary = {"tree_done": not left and not self.unknown, "unlocks_left": sorted(left),
                           "unknown": sorted(self.unknown), "bought": bought}
                if bought and left:
                    self.log("nothing short; bought %s; probing again" % bought)
                    continue
                if left:
                    self.log("nothing short, but unlock() refused: %s" % ", ".join(sorted(left)))
                    return 4, summary
                if self.unknown:
                    self.log("nothing short, but tech_tree.json lacks %s; re-run extract_tech_tree.py"
                             % ", ".join(sorted(self.unknown)))
                    return 4, summary
                self.log("nothing short; we maxed every visible unlock. Slices run: %d" % self.k)
                return 0, summary
            fm, why, stuck = self.pick(short)
            if stuck:
                makers = {it: [f["name"] for f in self.reg["farms"] if it in f["produces"]] for it in stuck}
                self.log("CHECK short, but no farm made it in its last slice: %s" % makers)
            if fm is None:
                self.log("only items no farm makes are short: %s" % {i: short[i] for i in stuck})
                return 3, {"stuck": {i: short[i] for i in stuck}}
            self.k += 1
            state, took, gains, after, rel, checks = self.slice(fm, st)
            costs = self.need()
            bought = self.buy(costs, after)
            self.log("%s (%s): %s in %.0f s; gains %s; bought %s" % (
                fm["name"], why, state, took,
                " ".join("%s%+.4g" % kv for kv in sorted(gains.items())) or "none",
                bought or "nothing"))
            self.jsonl({"time": time.time(), "slice": self.k, "farm": fm["name"], "why": why, "state": state,
                        "seconds": round(took, 2), "gains": gains, "purchases": bought,
                        "checks": checks, "output": rel})


def main(argv=None, bridge_factory=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--registry", required=True)
    p.add_argument("--workdir")
    p.add_argument("--slices", type=int, default=0, help="stop after this many slices (0 = no limit)")
    p.add_argument("--remeasure", type=int, default=20,
                   help="rerun any farm not run in this many slices, so stale rates refresh (0 = never)")
    p.add_argument("--ff", type=float, default=None, help="fixed fast-forward factor for every run")
    p.add_argument("--tree", default=DEFAULT_TREE, help="tech_tree.json for the visibility rule")
    p.add_argument("--dry-run", action="store_true", help="check the registry, lint the farms, print the probes")
    a = p.parse_args(argv)

    reg = load_registry(a.registry)
    wd = a.workdir or os.path.join(os.path.dirname(os.path.abspath(a.registry)), "economy")
    os.makedirs(wd, exist_ok=True)
    for n in enum_warnings(reg["items"]):
        print("warning: Items.%s is not in references/enums.json; the stock probe will decide" % n)
    bad = lint_farms(reg, wd)
    if bad:
        print("lint errors in farm(s): %s. Fix them, then run precheck.py on each." % ", ".join(bad))
        return 2
    tree = load_tree(a.tree)
    if tree is None:
        print("%s is missing. Run scripts/extract_tech_tree.py first: the driver must skip hidden nodes." % a.tree)
        return 2
    if a.dry_run:
        print("# registry ok: %d farm(s), items %s" % (len(reg["farms"]), sorted(reg["items"])))
        sys.stdout.write("# STOCK probe\n" + STOCK + "# LEVELS probe\n" + LEVELS)
        sys.stdout.write("# COSTS probe (example)\n" + COSTS % "[]")
        sys.stdout.write("# buyer (example)\n" + BUYER % ("{}", "[]"))
        return 0
    if os.path.exists(os.path.join(wd, "STOP")):
        print("%s exists. Read it, fix the cause, delete it, then start again." % os.path.join(wd, "STOP"))
        return 5

    if bridge_factory is None:
        from tfwr_bridge import Bridge, AUTO_FF

        def bridge_factory():
            return Bridge(timeout=60)
        ff = a.ff or AUTO_FF
    else:
        ff = a.ff or 128
    try:
        bridge = bridge_factory()
    except OSError as e:
        print("cannot reach the bridge: %s" % e)
        return 2
    missing = missing_unlocks(bridge, wd)
    if missing:
        print("The driver can't run on this save yet. Its own probes need unlocks you don't own:")
        for ln in missing[:12]:
            print("  " + ln)
        print("Play the early game by hand until you own them (SKILL.md, section 4), then start the driver again.")
        return 2
    d = Driver(reg, wd, bridge, ff, a.remeasure, tree)
    d.beat("starting")
    code, summary = 7, {}
    try:
        code, summary = d.loop(a.slices)
    except Problem as e:
        d.log("STOP: %s" % e)
        d.stop_file(str(e))
        code = 7
    except KeyboardInterrupt:
        d.log("interrupted")
        code = 5
    except Exception as e:  # a lost bridge or a bug: stop loudly, never idle silently
        d.log("STOP: %s: %s" % (type(e).__name__, e))
        d.stop_file("%s: %s" % (type(e).__name__, e))
        code = 7
    finally:
        state = {0: "done", 2: "setup", 3: "stuck", 4: "refused", 5: "stopped", 6: "limit"}.get(code, "failed")
        d.beat(state, exit=code, **summary)
        try:
            bridge.close()
        except Exception:
            pass
    print("SUMMARY exit %d, %d slice(s), %s" % (code, d.k, json.dumps(summary, sort_keys=True)))
    return code


if __name__ == "__main__":
    sys.exit(main())
