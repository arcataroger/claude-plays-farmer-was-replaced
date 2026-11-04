#!/usr/bin/env python3
"""Unlock bookkeeping for The Farmer Was Replaced: levels, in-game cost probe, and budget plans.

  levels                 Unlock levels from the save file, mapped to Unlocks.X names.
  probe [--costs] [-o F] Write a game script that prints num_unlocked() (and get_cost() with --costs,
                         which needs the Costs unlock) for every known unlock. It uses only straight-line
                         calls, so it runs at any progress level. Costs cover visible nodes only.
                         Run it with `tfwr_bridge.py run`, save its output, then `parse`.
  parse [FILE]           Parse probe output (default: output.txt) into the cache and print a table.
  plan Unlocks.X [...]   Compare next-level costs with the inventory. Show the shortfall per item.
                         Uses the in-game probe cache, else references/tech_tree.json.
  info Unlocks.X [...]   Rebuild the tech-tree hover from files: description, level/max, effect
                         current => next, next cost, prerequisites, what it grants.
  next                   List every unlock you can buy now (prerequisites met), with cost and shortfall.

Cache: ~/.cache/tfwr/unlocks.json (levels, costs, probe time). Costs change as levels rise,
so re-probe after every purchase. Buying unlocks spends the player's items and cannot be undone:
propose, then ask, unless the user granted standing permission.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tfwr  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENUMS = os.path.join(HERE, "..", "references", "enums.json")
# Probed costs and levels belong to one save slot, so each slot gets its own cache.
CACHE = os.path.expanduser("~/.cache/tfwr/unlocks-%s.json" % (tfwr.pick_slot(tfwr.saves_dir(), sys.argv) or "none"))


def enum_unlocks():
    d = json.load(open(ENUMS))
    bad = set(d.get("_uncertain", {}))
    return [u for u in d["Unlocks"] if "Unlocks." + u not in bad]


_saved = []


def fresh_save():
    """Saves the game once per process, so levels can't lag a purchase. No-op without the bridge."""
    if _saved:
        return
    _saved.append(True)
    try:
        sys.path.insert(0, os.path.join(HERE, "..", "bridge", "client"))
        from tfwr_bridge import Bridge
        Bridge(timeout=2).save()
    except Exception:
        pass


def save_data(argv):
    fresh_save()
    saves = tfwr.saves_dir()
    slot = tfwr.pick_slot(saves, argv)
    for p in (os.path.join(saves, slot, "save.json"), os.path.join(saves, slot + ".json")):
        if os.path.exists(p):
            if os.path.getsize(p) == 0:
                sys.exit("save.json is empty: a brand new slot saves only later. Run `tfwr_bridge.py save` first.")
            d = json.load(open(p))
            # Only unlocks and items. Never the world map.
            return d.get("unlocks", []), d.get("items", {}).get("serializeList", [])
    sys.exit("no save json found")


def live_inventory():
    """Item counts straight from the running game, or None when the bridge is down."""
    try:
        sys.path.insert(0, os.path.join(HERE, "..", "bridge", "client"))
        from tfwr_bridge import Bridge
        r = Bridge(timeout=2).call("inventory")
        items = r.get("items", r)
        return {"_".join(p.capitalize() for p in str(k).split("_")): v
                for k, v in items.items() if k != "ok"}
    except Exception:
        return None


def inventory(argv):
    live = live_inventory()
    if live is not None:
        return live
    sys.stderr.write("note: bridge not reachable; item counts come from the save file, "
                     "which lags until the game saves (tfwr_bridge.py save).\n")
    _, items = save_data(argv)
    inv = {}
    for it in items:
        name = "_".join(p.capitalize() for p in str(it.get("name", "")).split("_"))
        inv[name] = it.get("nr", 0)
    return inv


def cmd_levels(argv):
    raw, _ = save_data(argv)
    low = {u.lower(): u for u in json.load(open(ENUMS))["Unlocks"]}
    rows, other = [], []
    for r in raw:
        # An id can itself end in _N (debug_2), so try the whole id first.
        m = None if r in low else re.match(r"(.*?)_(\d+)$", r)
        base, n = (m.group(1), m.group(2)) if m else (r, None)
        name = low.get(r) or low.get(base)
        if name:
            rows.append((name, n))
        else:
            other.append(r)
    print("Tech-tree unlocks in the save (suffix = save's level marker; `probe` gives exact num_unlocked):")
    for name, n in sorted(rows):
        print("  Unlocks.%-22s %s" % (name, ("suffix " + n) if n else "unlocked"))
    print("\nLanguage features / items / other ids:", ", ".join(other))
    missing = sorted(set(low.values()) - {r[0] for r in rows})
    print("\nNot unlocked yet (per save):", ", ".join(missing))


def cmd_probe(argv):
    costs = "--costs" in argv
    out = argv[argv.index("-o") + 1] if "-o" in argv else None
    lines = ["# Generated by unlocks.py probe. Prints one @@u line per unlock.",
             "# Straight-line code: needs only quick_print and num_unlocked."]
    # Fair play: ask the game for costs only of nodes the player can see. Hidden
    # (????) nodes get their level only, which shows 0 and reveals nothing.
    t, lv = (tree(), current_levels(argv)) if costs else ({}, {})
    if costs and not t:
        sys.exit("--costs needs references/tech_tree.json to tell visible nodes from hidden ones. "
                 "Run scripts/extract_tech_tree.py first (see SKILL.md).")
    for u in enum_unlocks():
        if costs and visible(t, lv, u):
            lines.append('quick_print("@@u", "%s", num_unlocked(Unlocks.%s), get_cost(Unlocks.%s))' % (u, u, u))
        else:
            lines.append('quick_print("@@u", "%s", num_unlocked(Unlocks.%s))' % (u, u))
    lines.append('quick_print("@@probe_done")')
    text = "\n".join(lines) + "\n"
    if out:
        open(out, "w").write(text)
        print("wrote", out, "(%d unlocks%s)" % (len(lines) - 3, ", with costs" if costs else ""))
    else:
        sys.stdout.write(text)


def parse_cost(s):
    # Accepts dict text like {Items.Hay: 10, Items.Wood : 5} or {hay: 10}.
    cost = {}
    for name, n in re.findall(r"(?:Items\.)?([A-Za-z_]+)\s*:\s*([\d.]+)", s):
        cost["_".join(p.capitalize() for p in name.split("_"))] = float(n)
    return cost


def cmd_parse(argv):
    src = argv[0] if argv else os.path.join(os.path.dirname(tfwr.saves_dir()), "output.txt")
    text = open(src, errors="replace").read()
    text = re.sub(r"</?noparse>", "", text)
    data = {"probed": time.strftime("%Y-%m-%d %H:%M:%S"), "unlocks": {}}
    for line in text.splitlines():
        m = re.match(r"@@u\s+(\w+)\s+([\d.]+)\s*(.*)$", line.strip())
        if m:
            entry = {"level": float(m.group(2))}
            if m.group(3).strip():
                entry["cost"] = parse_cost(m.group(3))
            data["unlocks"][m.group(1)] = entry
    if "@@probe_done" not in text:
        print("WARNING: probe did not finish (missing @@probe_done). Check output for an Error: block.")
        err = re.search(r"Error:.*", text)
        if err:
            print(err.group(0))
    if not data["unlocks"]:
        sys.exit("no @@u lines found in " + src)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(data, open(CACHE, "w"), indent=1)
    print("cached %d unlocks -> %s" % (len(data["unlocks"]), CACHE))
    for name, e in sorted(data["unlocks"].items()):
        c = e.get("cost")
        cs = "maxed" if c == {} else (", ".join("%s %g" % kv for kv in c.items()) if c else "-")
        print("  %-24s level %-4g next: %s" % (name, e["level"], cs))


TREE = os.path.join(HERE, "..", "references", "tech_tree.json")


def item_name(n):
    return "_".join(p.capitalize() for p in str(n).split("_"))


def tree():
    if not os.path.exists(TREE):
        return {}
    d = json.load(open(TREE))
    u = d.get("unlocks", d)
    rows = u.values() if isinstance(u, dict) else u
    return {(r.get("enum_name_guess") or r["id"]): r for r in rows}


def current_levels(argv):
    if os.path.exists(CACHE):
        return {k: v["level"] for k, v in json.load(open(CACHE))["unlocks"].items()}
    raw, _ = save_data(argv)
    lv = {}
    t = tree()
    ids = {r["id"]: name for name, r in t.items()}
    for r in raw:
        # An id can itself end in _N (debug_2), so try the whole id first.
        m = None if r in ids else re.match(r"(.*?)_(\d+)$", r)
        base, n = (m.group(1), int(m.group(2))) if m else (r, 1)
        if base in ids:
            lv[ids[base]] = n
    return lv


def next_cost(entry, level):
    cbl = entry.get("costs_by_level", [])
    lvl = int(level)
    if lvl >= entry.get("max_level", len(cbl)) or lvl >= len(cbl):
        return {}
    return {item_name(k): v for k, v in cbl[lvl].items()}


def strings():
    lang = tfwr.languages_dir()
    out = {}
    if lang:
        for fn in ("unlock_tooltips.txt",):
            p = os.path.join(lang, "EN", "Strings", fn)
            if os.path.exists(p):
                for m in re.finditer(r"^@(\w+) = (.*?)(?=^@|\Z)", open(p).read(), re.S | re.M):
                    out[m.group(1)] = m.group(2).strip()
    return out


def visible(t, lv, name):
    """True if the player can see this node: owned, or every prerequisite met.

    The in-game tree shows other nodes as ????; fair play forbids reading them.
    """
    e = t.get(name)
    if not e:
        return False
    if int(lv.get(name, 0)) > 0:
        return True
    byid = {r["id"]: n for n, r in t.items()}
    return all(lv.get(byid.get(r["id"], r["id"]), 0) >= r["level"] for r in (e.get("requires") or []))


def cmd_info(argv):
    t, lv, st = tree(), current_levels(argv), strings()
    if not t:
        sys.exit("references/tech_tree.json missing. Run scripts/extract_tech_tree.py (see SKILL.md).")
    inv = inventory([a for a in argv if not a.startswith("Unlocks.")])
    for a in [x for x in argv if x.startswith("Unlocks.")]:
        name = a.replace("Unlocks.", "")
        e = t.get(name)
        if not e:
            print("%s: not in tech_tree.json" % a)
            continue
        if not visible(t, lv, name):
            print("%s: hidden (????) until its prerequisites are met" % a)
            continue
        L = int(lv.get(name, 0))
        mx = e.get("max_level", 1)
        key = e.get("multi_descr_key") if L >= 1 and e.get("multi_descr_key") else e.get("descr_key")
        print("== %s   level %d / %d%s" % (a, L, mx, "  (DLC)" if e.get("dlc") else ""))
        print("  " + st.get(key or "", "(no description)").replace("\n", "\n  "))
        eff = {x["level"]: x for x in (e.get("effects_by_level") or [])}
        if L + 1 in eff:
            cur = eff.get(L, {}).get("value", "-")
            print("  effect: %s%s => %s%s" % (cur, eff[L + 1]["unit"] if L in eff else "", eff[L + 1]["value"], eff[L + 1]["unit"]))
        elif e.get("effect_params") and not e["effect_params"].get("verified"):
            print("  effect: formula unverified; params", e["effect_params"])
        c = next_cost(e, L)
        print("  next cost:", ", ".join("%s %g (have %g)" % (k, v, inv.get(k, 0)) for k, v in c.items()) or "maxed")
        req = ", ".join("%s>=%d" % (r["id"], r["level"]) for r in (e.get("requires") or []))
        print("  requires:", req or "-")
        gr = [g["name"] for g in (e.get("grants") or []) if g.get("level", 1) == L + 1]
        if gr:
            print("  next level grants:", ", ".join(gr))


def cmd_next(argv):
    t, lv = tree(), current_levels(argv)
    inv = inventory(argv)
    byid = {r["id"]: n for n, r in t.items()}
    rows = []
    for name, e in t.items():
        L = int(lv.get(name, 0))
        c = next_cost(e, L)
        if not c:
            continue
        ok = all(lv.get(byid.get(r["id"], r["id"]), 0) >= r["level"] for r in (e.get("requires") or []))
        if not ok:
            continue
        short = {k: v - inv.get(k, 0) for k, v in c.items() if v > inv.get(k, 0)}
        rows.append((bool(short), name, L, c, short))
    for _, name, L, c, short in sorted(rows):
        print("%-22s L%d->%d  cost %-28s %s" % (name, L, L + 1, ", ".join("%s %g" % kv for kv in c.items()),
                                              "AFFORDABLE" if not short else "short " + ", ".join("%s %g" % kv for kv in short.items())))


def cmd_plan(argv):
    if not os.path.exists(CACHE) and not os.path.exists(TREE):
        sys.exit("No costs: run a probe with --costs, or generate references/tech_tree.json.")
    if not os.path.exists(CACHE) or not any("cost" in v for v in json.load(open(CACHE))["unlocks"].values()):
        t, lv = tree(), current_levels(argv)
        data = {"probed": "tech_tree.json (levels from save)", "unlocks": {
            n: {"level": lv.get(n, 0), "cost": next_cost(e, lv.get(n, 0))} for n, e in t.items()}}
    else:
        data = json.load(open(CACHE))
    inv = inventory([a for a in argv if not a.startswith("Unlocks.")])
    targets = [a.replace("Unlocks.", "") for a in argv if a.startswith("Unlocks.")]
    t_vis, lv_vis = tree(), current_levels(argv)
    if t_vis:
        hidden = [x for x in targets if not visible(t_vis, lv_vis, x)]
        for x in hidden:
            print("Unlocks.%s: hidden (????) until its prerequisites are met; skipped" % x)
        targets = [x for x in targets if x not in hidden]
    print("costs probed:", data["probed"], "(re-probe after any purchase)")
    total = {}
    for t in targets:
        e = data["unlocks"].get(t)
        if not e or "cost" not in e:
            print("  %s: no cost cached" % t)
            continue
        print("  Unlocks.%s (level %g): %s" % (t, e["level"], e["cost"] or "maxed"))
        for k, v in e["cost"].items():
            total[k] = total.get(k, 0) + v
    print("\nitem         need        have        short")
    for k, v in sorted(total.items()):
        have = inv.get(k, 0)
        print("%-12s %-11g %-11g %s" % (k, v, have, max(0, v - have) or "ok"))


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    {"levels": cmd_levels, "probe": cmd_probe, "parse": cmd_parse, "plan": cmd_plan,
     "info": cmd_info, "next": cmd_next}.get(
        sys.argv[1], lambda a: sys.exit("unknown command"))(sys.argv[2:])


if __name__ == "__main__":
    main()
