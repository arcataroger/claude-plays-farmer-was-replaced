#!/usr/bin/env python3
"""Extract tech-tree metadata from The Farmer Was Replaced's Unity assets into JSON.

What it reads: only the 53-ish `UnlockSO` ScriptableObjects in `resources.assets`, plus the
class name of each MonoBehaviour's MonoScript (to find the UnlockSO ones). It reads NO world
generation data: no *Feature / stratum objects, no seeds, no spawn rates, no game code.

Fields per unlock: id, enum_name_guess, requires, max_level, costs_by_level, descr_key,
multi_descr_key, grants (functions/items it unlocks, with the level that grants them),
effect_params (raw fields the game's tooltip code turns into the "current => next" line),
effects_by_level (rendered only where the formula is verified), dlc, gain_resource,
unlocked_hat, docs, order.

Dependency: UnityPy (pip). Run it with a Python that has UnityPy, e.g.
    python3 -m venv ~/.cache/tfwr/venv && ~/.cache/tfwr/venv/bin/pip install UnityPy
    ~/.cache/tfwr/venv/bin/python -I scripts/extract_tech_tree.py -o references/tech_tree.json --check

Usage:
    extract_tech_tree.py [-o OUT.json] [--data DIR] [--check]
      -o FILE    write JSON to FILE (default: print to stdout)
      --data DIR the game's Data folder (default: derived from tfwr.languages_dir(), or TFWR_DATA)
      --check    run self-checks (known Carrots cost/effect, `for` at Expand 2, prerequisites
                 versus the levels cached by `unlocks.py parse` in ~/.cache/tfwr/unlocks.json)

The script opens the game files read-only and never writes into the game folder.
If parsing fails with "layout changed", a game update changed the UnlockSO fields: regenerate
the layout with UnityPy's TypeTreeGenerator (Core.dll, class UnlockSO) and update read_unlock().
"""
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ENUMS = os.path.join(HERE, "..", "references", "enums.json")
LEVEL_CACHE = os.path.expanduser("~/.cache/tfwr/unlocks.json")

# multiUnlockDescrMode -> what the tooltip's effect line shows (inferred from which unlocks use it).
MODE_LABELS = {
    0: "none (single-level unlock, no effect line)",
    1: "percent multiplier (yield / speed / power)",
    2: "farm size (Expand)",
    3: "drone count (Megafarm)",
    4: "items received per 10 s (Watering, Fertilizer)",
}


def data_dir(argv):
    if "--data" in argv:
        return argv[argv.index("--data") + 1]
    if os.environ.get("TFWR_DATA"):
        return os.environ["TFWR_DATA"]
    try:
        import tfwr
        lang = tfwr.languages_dir()
    except Exception:
        lang = None
    if not lang:
        sys.exit("game install not found: pass --data DIR or set TFWR_DATA / TFWR_LANGUAGES")
    return os.path.dirname(os.path.dirname(lang))  # .../Data/StreamingAssets/Languages -> .../Data


class Reader:
    """Little-endian reader for Unity's serialized MonoBehaviour layout (4-byte aligned strings)."""

    def __init__(self, b):
        self.b, self.p = b, 0

    def _u(self, fmt, n):
        v = struct.unpack_from(fmt, self.b, self.p)[0]
        self.p += n
        return v

    def i32(self):
        return self._u("<i", 4)

    def f32(self):
        return self._u("<f", 4)

    def f64(self):
        return self._u("<d", 8)

    def align(self):
        self.p = (self.p + 3) & ~3

    def u8(self):
        v = self.b[self.p]
        self.p += 1
        self.align()
        return v

    def str(self):
        n = self.i32()
        if n < 0 or self.p + n > len(self.b):
            raise ValueError("bad string length %d at %d" % (n, self.p))
        v = self.b[self.p:self.p + n].decode("utf-8")
        self.p += n
        self.align()
        return v

    def pptr(self):
        fid = self.i32()
        pid = self._u("<q", 8)
        return fid, pid

    def items(self):  # ItemBlock { SerializableItem[] serializeList { string name; double nr } }
        return {self.str(): self.f64() for _ in range(self.i32())}


def read_unlock(b):
    """Parse one UnlockSO (field order from the Core.dll typetree, Unity 6000.0)."""
    r = Reader(b)
    r.pptr()  # m_GameObject
    r.u8()  # m_Enabled
    r.pptr()  # m_Script
    d = {"m_Name": r.str(), "unlockName": r.str(), "description": r.str(), "docs": r.str()}
    r.pptr()  # mesh
    d["gainResource"] = r.str()
    r.f32(), r.f32(), r.f32()  # iconRotation
    d["displayCode"] = r.str()
    d["unlocks"] = [r.str() for _ in range(r.i32())]
    d["parentUnlock"] = r.str()
    d["additionalUnlockRequirement"] = {"unlockName": r.str(), "level": r.i32()}
    d["unlockedHat"] = r.str()
    d["order"] = r.i32()
    d["unlockCost"] = r.items()
    d["multiUnlockCost"] = [r.items() for _ in range(r.i32())]
    d["multiUnlockFactor"] = r.f32()
    d["multiUnlockDescr"] = r.str()
    d["multiUnlockDescrMode"] = r.i32()
    d["additivePercentStart"] = r.f32()
    d["additivePercentFactor"] = r.f32()
    d["maxUnlockLevel"] = r.i32()
    d["dlc"] = r.i32()
    d["enabled"] = r.u8()
    if r.p != len(b):
        raise ValueError("read %d of %d bytes" % (r.p, len(b)))
    return d


def load_records(data):
    try:
        import UnityPy
    except ImportError:
        sys.exit("UnityPy is missing. Install it in a venv:\n"
                 "  python3 -m venv ~/.cache/tfwr/venv && ~/.cache/tfwr/venv/bin/pip install UnityPy\n"
                 "then run this script with ~/.cache/tfwr/venv/bin/python -I")
    path = os.path.join(data, "resources.assets")
    if not os.path.exists(path):
        sys.exit("not found: " + path)
    env = UnityPy.load(path)
    f = env.files[next(k for k in env.files if k.endswith("resources.assets"))]
    script_names = {}  # (fileID, pathID) -> MonoScript class name; only MonoScript name fields are read

    def class_of(ptr):
        if ptr not in script_names:
            name = None
            try:
                fid, pid = ptr
                target = f if fid == 0 else None
                if fid > 0:
                    ext = os.path.join(data, os.path.basename(f.externals[fid - 1].path))
                    if ext not in loaded:
                        loaded[ext] = UnityPy.load(ext)
                    e = loaded[ext]
                    target = e.files[next(iter(e.files))]
                obj = target.objects.get(pid)
                if obj is not None and obj.type.name == "MonoScript":
                    name = obj.read().m_ClassName
            except Exception:
                name = None
            script_names[ptr] = name
        return script_names[ptr]

    loaded = {}
    out = []
    for obj in f.objects.values():
        if obj.type.name != "MonoBehaviour":
            continue
        b = obj.get_raw_data()
        if len(b) < 28:
            continue
        ptr = struct.unpack_from("<iq", b, 16)  # m_Script follows m_GameObject (12) + m_Enabled (4)
        if class_of(ptr) != "UnlockSO":
            continue
        try:
            out.append(read_unlock(b))
        except Exception as e:
            sys.exit("UnlockSO layout changed (path_id %d: %s). Regenerate the field layout with "
                     "UnityPy's TypeTreeGenerator (Core.dll, UnlockSO) and update read_unlock()." % (obj.path_id, e))
    if not out:
        sys.exit("no UnlockSO objects found in " + path)
    return out


def enum_guess(uid, known):
    guess = "_".join(p.capitalize() for p in uid.split("_"))
    return guess, guess in known


def costs_by_level(d):
    """costs_by_level[k] = items to go from level k to level k+1 (k = 0 .. max_level-1)."""
    mx, multi, fac = d["maxUnlockLevel"], d["multiUnlockCost"], d["multiUnlockFactor"]
    costs = []
    for k in range(mx):
        if k == 0:
            c = dict(d["unlockCost"])
        elif k - 1 < len(multi):
            c = dict(multi[k - 1])
        else:
            base, extra = (multi[-1], k - len(multi)) if multi else (d["unlockCost"], k)
            c = {it: n * fac ** extra for it, n in base.items()}
        costs.append({it: (int(n) if float(n).is_integer() else n) for it, n in c.items()})
    return costs


def effects(d):
    mode, start, fac, mx = (d["multiUnlockDescrMode"], d["additivePercentStart"],
                            d["additivePercentFactor"], d["maxUnlockLevel"])
    params = {"mode": mode, "mode_label": MODE_LABELS.get(mode, "unknown"),
              "additive_percent_start": start, "additive_percent_factor": fac}
    rows = None
    if mode == 1 and start == 0 and mx > 1:
        # Verified: Carrots level 3 -> 4 shows "400% => 800%".
        params["formula"] = "percent(L) = 100 * additive_percent_factor ** (L - 1), L >= 1"
        params["verified"] = True
        rows = [{"level": L, "value": 100 * fac ** (L - 1), "unit": "%", "label": "multiplier"}
                for L in range(1, mx + 1)]
    elif mode == 4 and mx > 1:
        # Docs: watering and fertilizer give 1 per 10 s and double with each upgrade.
        params["formula"] = "per_10s(L) = additive_percent_factor ** (L - 1), L >= 1 (from docs; tooltip format unverified)"
        params["verified"] = False
        rows = [{"level": L, "value": fac ** (L - 1), "unit": "per 10 s", "label": "items received"}
                for L in range(1, mx + 1)]
    elif mode != 0:
        params["formula"] = None
        params["verified"] = False
        params["note"] = ("tooltip value computed in game code from these fields; formula not "
                          "confirmed (need one in-game hover to pin it down)")
    return params, rows


def grants(d):
    out = []
    for s in d["unlocks"]:
        m = re.match(r"^(\d+)(.*)$", s)
        out.append({"name": m.group(2), "level": int(m.group(1))} if m else {"name": s, "level": 1})
    return out


def build(records):
    known = set(json.load(open(ENUMS))["Unlocks"]) if os.path.exists(ENUMS) else set()
    out = []
    for d in sorted(records, key=lambda d: d["unlockName"]):
        guess, in_enum = enum_guess(d["unlockName"], known)
        req = []
        if d["parentUnlock"]:
            req.append({"id": d["parentUnlock"], "level": 1})
        a = d["additionalUnlockRequirement"]
        if a["unlockName"]:
            req.append({"id": a["unlockName"], "level": a["level"]})
        eff_params, eff_rows = effects(d)
        out.append({
            "id": d["unlockName"],
            "enum_name_guess": guess,
            "enum_name_in_enums_json": in_enum,
            "requires": req,
            "max_level": d["maxUnlockLevel"],
            "costs_by_level": costs_by_level(d),
            "cost_scaling": {"explicit_levels": min(d["maxUnlockLevel"], 1 + len(d["multiUnlockCost"])),
                             "factor_after": d["multiUnlockFactor"]},
            "descr_key": d["description"],
            "multi_descr_key": d["multiUnlockDescr"] or None,
            "grants": grants(d),
            "effect_params": eff_params,
            "effects_by_level": eff_rows,
            "dlc": bool(d["dlc"]),
            "enabled": bool(d["enabled"]),
            "gain_resource": d["gainResource"] or None,
            "unlocked_hat": d["unlockedHat"] or None,
            "docs": d["docs"] or None,
            "display_code": d["displayCode"] or None,
            "order": d["order"],
        })
    return out


def fmt_pct(v):
    return ("%g" % v) + "%"


def check(tree):
    by = {u["id"]: u for u in tree}
    ok = True

    def expect(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok = ok and cond

    c = by.get("carrots")
    expect(c and c["max_level"] == 10, "carrots max_level == 10")
    expect(c and c["costs_by_level"][3] == {"wood": 6250}, "carrots level 3 -> 4 costs 6250 wood (hover: 6.25k)")
    if c and c["effects_by_level"]:
        v = {r["level"]: r["value"] for r in c["effects_by_level"]}
        line = "%s => %s" % (fmt_pct(v[3]), fmt_pct(v[4]))
        expect(line == "400% => 800%", "carrots effect line at level 3: " + line)
    e = by.get("expand")
    expect(e and {"name": "for", "level": 2} in e["grants"], "`for` comes with Expand level 2")
    if os.path.exists(LEVEL_CACHE):
        lv = {k: v.get("level", 0) for k, v in json.load(open(LEVEL_CACHE))["unlocks"].items()}
        enum_to_id = {u["enum_name_guess"]: u["id"] for u in tree}
        levels = {enum_to_id.get(k, k.lower()): v for k, v in lv.items()}
        over, bad, n_lv, n_req = [], [], 0, 0
        for u in tree:
            L = levels.get(u["id"], 0)
            if L > u["max_level"]:
                over.append("%s %d > max %d" % (u["id"], L, u["max_level"]))
            if L >= 1:
                n_lv += 1
                for r in u["requires"]:
                    n_req += 1
                    have = levels.get(r["id"], 0)
                    if have < r["level"]:
                        bad.append("%s at %d requires %s>=%d (have %d)" % (u["id"], L, r["id"], r["level"], have))
        expect(not over, "all %d cached levels <= max_level%s" % (len(levels), (": " + "; ".join(over)) if over else ""))
        expect(not bad, "prerequisites consistent for %d unlocks at level >= 1 (%d requirements)%s"
               % (n_lv, n_req, (": " + "; ".join(bad)) if bad else ""))
        missing = sorted(set(lv) - set(enum_to_id))
        expect(not missing, "every cached Unlocks enum name maps to an extracted id" + (": " + str(missing) if missing else ""))
    else:
        print("SKIP prerequisite check: no " + LEVEL_CACHE + " (run unlocks.py probe + parse)")
    return ok


def main(argv):
    data = data_dir(argv)
    tree = build(load_records(data))
    text = json.dumps({"_source": "resources.assets UnlockSO objects (extract_tech_tree.py)",
                       "_cost_rule": "costs_by_level[k] buys level k+1. k=0: unlockCost; k=1..n: "
                                     "multiUnlockCost[k-1]; beyond: last explicit block * factor_after^(k-n)",
                       "unlocks": tree}, indent=1, ensure_ascii=False)
    if "-o" in argv:
        out = os.path.abspath(argv[argv.index("-o") + 1])
        if out.startswith(os.path.abspath(data)) or "steamapps" in out:
            sys.exit("refusing to write into the game folder")
        with open(out, "w") as fh:
            fh.write(text + "\n")
        print("wrote %s (%d unlocks)" % (out, len(tree)), file=sys.stderr)
    else:
        print(text)
    if "--check" in argv:
        sys.exit(0 if check(tree) else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
