#!/usr/bin/env python3
"""Records a playthrough as a git repo, one commit per milestone, so a later run can replay it.

Layout of one run (~/tfwr-journal/<save>-<date>/):
  run.json                     save slot, start time, game and plugin versions
  steps/000-fresh-save/        step.json only (the starting inventory and unlocks)
  steps/NNN-<slug>/
    script.py                  the code we ran in the agent window (optional: a step can be only purchases)
    check.json                 what must hold afterwards, e.g. {"items": {"hay": 5}, "unlocks": ["loops"]}
    step.json                  goal, purchases, unlocks/items after, unlocks gained, timing, output tail
    attempts/K/                failed tries since the last success (script.py, attempt.json)
  pending/                     failed tries waiting for the next success (moved into its step)

Fair play: we read only the `unlocks` and `items` keys of save.json, never the map.

Usage:
  journal.py init [--root DIR]                     start a run for the active save slot
  journal.py fail --goal G --script F [--output F] [--notes N]
  journal.py step --goal G --check JSON [--script F] [--buy Unlock ...] [--output F] [--seconds S] [--notes N]

A replay of a step: buy each --buy unlock in order, run script.py until check.json holds.
check.json uses save-file names (lowercase): items are minimum counts, unlocks must be present.
  journal.py log                                   one line per commit of the current run
  journal.py path                                  print the current run's folder
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge", "client"))
import tfwr_bridge  # noqa: E402
import tfwr  # noqa: E402

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), "tfwr-journal")


def git(run, *args):
    return subprocess.run(["git", "-C", run, *args], check=True, capture_output=True, text=True).stdout


def snapshot(bridge):
    """Saves the game, then returns (slot, unlocks, items) from the slot's save.json."""
    bridge.save()
    info = bridge.ping()
    slot = info["active_save"]
    path = os.path.join(info["data_dir"], "Saves", slot, "save.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Only these two keys: the rest of the file holds the world map.
    unlocks = sorted(data.get("unlocks", []))
    items = {}
    for entry in data.get("items", {}).get("serializeList", []):
        if isinstance(entry, dict):
            name = entry.get("key") or entry.get("item") or entry.get("name")
            count = entry.get("nr", entry.get("value"))
            if name is not None:
                items[str(name)] = count
    return slot, unlocks, items


def current_run(root):
    """The most recently started run under root."""
    if not os.path.isdir(root):
        sys.exit(f"no runs under {root}; run `journal.py init` first")
    runs = [os.path.join(root, d) for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "run.json"))]
    if not runs:
        sys.exit(f"no runs under {root}; run `journal.py init` first")
    return max(runs, key=lambda r: json.load(open(os.path.join(r, "run.json")))["started"])


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "step"


def next_step_number(run):
    steps = os.path.join(run, "steps")
    nums = [int(d[:3]) for d in os.listdir(steps) if d[:3].isdigit()] if os.path.isdir(steps) else []
    return max(nums) + 1 if nums else 0


def last_step(run):
    steps = os.path.join(run, "steps")
    latest = sorted(d for d in os.listdir(steps) if d[:3].isdigit())[-1]
    with open(os.path.join(steps, latest, "step.json"), encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def tail(path, lines=40):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return "".join(f.readlines()[-lines:])


def check_holds(check, unlocks, items):
    """Returns a list of the check's unmet conditions (empty when it holds)."""
    unmet = []
    for name, need in check.get("items", {}).items():
        if (items.get(name) or 0) < need:
            unmet.append(f"{name} {items.get(name) or 0} < {need}")
    for u in check.get("unlocks", []):
        if u not in unlocks:
            unmet.append(f"unlock {u} missing")
    return unmet


def cmd_init(args):
    bridge = tfwr_bridge.Bridge()
    info = bridge.ping()
    slot, unlocks, items = snapshot(bridge)
    now = datetime.datetime.now()
    run = os.path.join(args.root, f"{slot}-{now:%Y%m%d-%H%M}")
    if os.path.exists(run):
        sys.exit(f"{run} already exists")
    os.makedirs(os.path.join(run, "steps", "000-fresh-save"))
    write_json(os.path.join(run, "run.json"), {
        "save": slot,
        "started": now.isoformat(timespec="seconds"),
        "unity": info["unity"],
        "plugin": info["plugin"],
        "platform": info["platform"],
        "steam_buildid": tfwr.game_build(),
    })
    write_json(os.path.join(run, "steps", "000-fresh-save", "step.json"), {
        "goal": "starting state", "result": "success", "unlocks": unlocks, "items": items,
    })
    subprocess.run(["git", "init", "-q", run], check=True)
    git(run, "add", "-A")
    git(run, "commit", "-q", "-m", "step 000: starting state")
    print(run)


def cmd_fail(args):
    run = current_run(args.root)
    pending = os.path.join(run, "pending")
    os.makedirs(pending, exist_ok=True)
    k = len(os.listdir(pending))
    d = os.path.join(pending, str(k))
    os.makedirs(d)
    shutil.copy(args.script, os.path.join(d, "script.py"))
    write_json(os.path.join(d, "attempt.json"), {
        "goal": args.goal, "result": "failure", "notes": args.notes,
        "output_tail": tail(args.output), "at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    print(f"recorded failed attempt {k} (commits with the next success)")


def cmd_step(args):
    run = current_run(args.root)
    bridge = tfwr_bridge.Bridge()
    slot, unlocks, items = snapshot(bridge)
    meta = json.load(open(os.path.join(run, "run.json")))
    if slot != meta["save"]:
        sys.exit(f"the game has save {slot} open, but this run records {meta['save']}")
    check = json.loads(args.check)
    unmet = check_holds(check, unlocks, items)
    if unmet:
        sys.exit("the check doesn't hold yet: " + "; ".join(unmet))
    before = last_step(run)
    n = next_step_number(run)
    d = os.path.join(run, "steps", f"{n:03d}-{slugify(args.goal)}")
    os.makedirs(d)
    if args.script:
        shutil.copy(args.script, os.path.join(d, "script.py"))
    write_json(os.path.join(d, "check.json"), check)
    pending = os.path.join(run, "pending")
    attempts = 0
    if os.path.isdir(pending) and os.listdir(pending):
        shutil.move(pending, os.path.join(d, "attempts"))
        attempts = len(os.listdir(os.path.join(d, "attempts")))
    write_json(os.path.join(d, "step.json"), {
        "goal": args.goal,
        "result": "success",
        "buys": args.buy or [],
        "notes": args.notes,
        "seconds": args.seconds,
        "unlocks": unlocks,
        "unlocks_gained": sorted(set(unlocks) - set(before["unlocks"])),
        "items": items,
        "failed_attempts": attempts,
        "output_tail": tail(args.output),
    })
    git(run, "add", "-A")
    git(run, "commit", "-q", "-m", f"step {n:03d}: {args.goal}")
    print(f"step {n:03d} committed: {d}")
    push_if_remote(run)


def push_if_remote(run):
    """Pushes the run when it has a remote; a failed push never loses the local commit."""
    if not git(run, "remote").strip():
        return
    r = subprocess.run(["git", "-C", run, "push", "-q"], capture_output=True, text=True)
    print("pushed" if r.returncode == 0 else f"push failed (kept locally): {r.stderr.strip()[:200]}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=DEFAULT_ROOT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    f = sub.add_parser("fail")
    f.add_argument("--goal", required=True)
    f.add_argument("--script", required=True)
    f.add_argument("--output")
    f.add_argument("--notes")
    s = sub.add_parser("step")
    s.add_argument("--goal", required=True)
    s.add_argument("--script")
    s.add_argument("--check", required=True, help='JSON, e.g. \'{"items": {"hay": 5}}\'')
    s.add_argument("--buy", action="append", help="an unlock bought in this step (repeat in order)")
    s.add_argument("--output")
    s.add_argument("--seconds", type=float)
    s.add_argument("--notes")
    sub.add_parser("log")
    sub.add_parser("path")
    args = p.parse_args()
    if args.cmd == "init":
        cmd_init(args)
    elif args.cmd == "fail":
        cmd_fail(args)
    elif args.cmd == "step":
        cmd_step(args)
    elif args.cmd == "log":
        print(git(current_run(args.root), "log", "--reverse", "--format=%h %ad %s", "--date=format:%H:%M"), end="")
    elif args.cmd == "path":
        print(current_run(args.root))


if __name__ == "__main__":
    main()
