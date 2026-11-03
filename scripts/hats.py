#!/usr/bin/env python3
"""Writes the player's cosmetic hats, newest first, into a script's `HATS = [...]` line.

Most hats only change the drone's look; one changes gameplay (read its doc page),
so we leave it out. The save's `unlocks` list keeps purchase
order, so its last hat is the newest. Scripts then dress drone k in HATS[k % len(HATS)],
which shows off the newest hats first.

Usage:
  hats.py                print the HATS line for the active save
  hats.py FILE.py ...    replace the `HATS = ...` line in each file
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tfwr  # noqa: E402

FUNCTIONAL = {"dinosaur_hat"}  # changes gameplay, not just looks


def hats_newest_first(slot_dir):
    with open(os.path.join(slot_dir, "save.json"), encoding="utf-8") as f:
        unlocks = json.load(f).get("unlocks", [])  # metadata only, never the map
    owned = [u for u in unlocks if (u.endswith("_hat") or u == "traffic_cone") and u not in FUNCTIONAL]
    # Enum names are the unlock ids in Title_Case: gold_hat -> Gold_Hat.
    return ["Hats." + "_".join(w.capitalize() for w in u.split("_")) for u in reversed(owned)]


def main():
    saves = tfwr.saves_dir()
    slot = saves and tfwr.pick_slot(saves, sys.argv)
    if not slot:
        sys.exit("Save folder not found. Set TFWR_SAVES.")
    line = "HATS = [" + ", ".join(hats_newest_first(os.path.join(saves, slot))) + "]"
    files = [a for i, a in enumerate(sys.argv[1:], 1) if a != "--slot" and sys.argv[i - 1] != "--slot"]
    if not files:
        print(line)
        return
    for path in files:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new, n = re.subn(r"^HATS = .*$", line, text, count=1, flags=re.M)
        if n == 0:
            sys.exit(f"{path} has no `HATS = ...` line")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"{path}: {line}")


if __name__ == "__main__":
    main()
