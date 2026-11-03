#!/usr/bin/env python3
"""Lists this game's Steam achievements: which are unlocked, and what the rest require.

Usage: python3 scripts/achievements.py [--json] [--stats DIR]

It reads Steam's local cache (the same data the Steam overlay shows): the
achievement schema and the user's stats file, both in binary KeyValues. A hidden
achievement shows as "Hidden achievement", name and description both, until you
unlock it, as in the Steam overlay.
It looks for Steam's appcache/stats folder on macOS, Windows and Linux. Pass
--stats DIR (or set TFWR_STEAM_STATS) when Steam lives somewhere else.
"""
import glob
import json
import os
import struct
import sys

APP = "2060160"  # The Farmer Was Replaced
HIDDEN = "Hidden achievement"


def stats_dirs():
    """Candidate appcache/stats folders for this OS, most likely first."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        roots = [os.path.join(home, "Library", "Application Support", "Steam")]
    elif sys.platform.startswith("win"):
        roots = [os.path.join(os.environ[v], "Steam") for v in ("ProgramFiles(x86)", "ProgramFiles")
                 if os.environ.get(v)]
        roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    else:
        roots = [os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam"),
                 os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam")]
    return [os.path.join(r, "appcache", "stats") for r in roots]


def find_stats(override=None):
    override = override or os.environ.get("TFWR_STEAM_STATS")
    candidates = [override] if override else stats_dirs()
    for d in candidates:
        if os.path.isfile(os.path.join(d, f"UserGameStatsSchema_{APP}.bin")):
            return d
    raise SystemExit("no Steam achievement cache for this game in: " + ", ".join(candidates)
                     + "\nlaunch the game through Steam once, or pass --stats DIR")


def parse(b, i=0):
    """Binary KeyValues: type byte, NUL-terminated key, value; type 8 ends a block."""
    out = {}
    while i < len(b):
        t = b[i]
        i += 1
        if t == 8:
            return out, i
        j = b.index(0, i)
        key = b[i:j].decode("utf-8", "replace")
        i = j + 1
        if t == 0:
            val, i = parse(b, i)
        elif t == 1:
            j = b.index(0, i)
            val = b[i:j].decode("utf-8", "replace")
            i = j + 1
        elif t == 2:
            val = struct.unpack_from("<i", b, i)[0]
            i += 4
        elif t == 3:
            val = struct.unpack_from("<f", b, i)[0]
            i += 4
        elif t == 7:
            val = struct.unpack_from("<Q", b, i)[0]
            i += 8
        else:
            raise ValueError(f"unknown KeyValues type {t} at byte {i}")
        out[key] = val
    return out, i


def load(stats):
    with open(os.path.join(stats, f"UserGameStatsSchema_{APP}.bin"), "rb") as f:
        schema = parse(f.read())[0][APP]["stats"]
    users = glob.glob(os.path.join(stats, f"UserGameStats_*_{APP}.bin"))
    if not users:
        raise SystemExit("no user stats file; launch the game through Steam once")
    with open(max(users, key=os.path.getmtime), "rb") as f:
        cache = parse(f.read())[0]["cache"]
    rows = []
    for sid, st in schema.items():
        if "bits" not in st:
            continue
        # Each "bits" block holds up to 32 achievements; the user file keeps one
        # integer per block, with bit i set when achievement i is unlocked.
        mask = cache.get(sid, {}).get("data", 0)
        for bit, a in st["bits"].items():
            d = a.get("display", {})
            hidden = str(d.get("hidden", "0")) == "1"
            got = bool(mask >> int(bit) & 1)
            # The Steam overlay hides a locked hidden achievement's name and description.
            secret = hidden and not got
            rows.append({
                "name": HIDDEN if secret else d.get("name", {}).get("english", a.get("name")),
                "unlocked": got,
                "hidden": hidden,
                "desc": HIDDEN if secret else d.get("desc", {}).get("english", ""),
            })
    return rows


def main():
    stats = None
    if "--stats" in sys.argv:
        i = sys.argv.index("--stats")
        if i + 1 >= len(sys.argv):
            raise SystemExit("--stats needs a folder")
        stats = sys.argv[i + 1]
    rows = load(find_stats(stats))
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
        return
    got = [r for r in rows if r["unlocked"]]
    print(f"{len(got)} of {len(rows)} unlocked\n\nNot yet unlocked:")
    for r in rows:
        if not r["unlocked"]:
            print(f"  - {r['name']}" if r["name"] == r["desc"] == HIDDEN else f"  - {r['name']}: {r['desc']}")


if __name__ == "__main__":
    main()
