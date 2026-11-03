#!/usr/bin/env python3
"""Campaign wake-up: read the game and the campaign state, then print ONE next instruction.

usage: campaign.py [--state campaign_state.json] [--workdir DIR] [--leaderboards FILE]
                   [--idle 90] [--stale-min 60] [--no-probe] [--status-json FILE]

Call it at the start of every /loop turn, and do what the line says. It never starts
a run, buys or farms; at most it runs a short read-only cost probe in an idle game.

The campaign has three phases, in order:
  tree          max the tech tree. Done when no visible unlock has a next cost. We read
                the economy driver's final driver.json, or probe the game when it is idle.
  achievements  unlock every revealed achievement. Done when `achievements.py --json`
                lists no entry with unlocked=false and hidden=false.
  leaderboards  climb the boards. Done when every board in leaderboards.json has done=true.

campaign_state.json (created on first use, next to the default path):
  {"phase": "tree", "last_progress": 0, "remaining": null, "last_busy": 0,
   "runs_finished": 0, "economy_workdir": "economy", "leaderboards": "leaderboards.json"}
We add phase_seen and phase_since; a driver verdict older than phase_since doesn't count.
Edit "phase" by hand to jump back when a game update reveals new work.
Relative paths resolve against the state file's folder.

leaderboards.json, which you maintain:
  {"boards": [{"name": "board_a", "done": false, "best": 123.4, "note": "..."}]}
Mark a board done only when you measured it near its limit, as SKILL.md asks.

Checks, first match wins:
  1. The bridge is unreachable.
  2. The economy driver wrote STOP.
  3. The economy driver runs: read its log and newest slice.
  4. The plugin watchdog stopped the last run.
  5. The driver exited for another reason than "done".
  6. The phase is complete: we advance the state and say so.
  7. The phase made no progress for --stale-min minutes.
  8. A run is going.
  9. The game sits idle: start the next step.

We measure idle time between wake-ups, so its precision is the wake-up interval.
Exit codes: 0 an instruction printed, 2 the bridge is unreachable.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "bridge", "client"))

PHASES = ["tree", "achievements", "leaderboards"]
DEFAULT_STATE = {"phase": "tree", "last_progress": 0, "remaining": None, "last_busy": 0,
                 "runs_finished": 0, "economy_workdir": "economy", "leaderboards": "leaderboards.json"}


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def tail(path, n=20):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def newest(folder):
    try:
        names = [os.path.join(folder, x) for x in os.listdir(folder)]
    except OSError:
        return None
    return max(names, key=os.path.getmtime) if names else None


def clock(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


# ---------- phase checks: each returns (done, remaining count, detail) or None if unknown ----------

def tree_check(driver, bridge, tree_path, since):
    # Trust the driver's verdict only if it came after this phase began.
    if driver and driver.get("state") == "done" and driver.get("tree_done") and driver.get("updated", 0) > since:
        return True, 0, "the economy driver reports every visible unlock maxed"
    if bridge is None:
        return None
    from economy_driver import load_tree, visible_costs
    tree = load_tree(tree_path)
    if tree is None:
        return None

    def run(name, code):
        out, _, state = bridge.run_and_wait("main", code, timeout=20, wait_for="campaign " + name, ff=1)
        return out if state == "finished" else ""
    try:
        got = visible_costs(run, tree)
    except Exception:
        return None
    if got is None or got[1]:
        # Early saves lack quick_print or get_cost, and unknown nodes block a verdict.
        return None
    left = sorted(got[0])
    return not left, len(left), ("%d visible unlock(s) with a next cost" % len(left))


def achievements_check():
    p = subprocess.run([sys.executable, os.path.join(HERE, "achievements.py"), "--json"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        rows = json.loads(p.stdout)
    except ValueError:
        return None
    left = [r.get("name") for r in rows if not r.get("unlocked") and not r.get("hidden")]
    return not left, len(left), ("%d revealed achievement(s) locked" % len(left))


def leaderboards_check(path):
    data = load_json(path)
    if not data or not data.get("boards"):
        return None
    left = [b.get("name") for b in data["boards"] if not b.get("done")]
    return not left, len(left), ("%d board(s) not done" % len(left))


# ---------- the decision ----------

def decide(st, status, args, base, bridge):
    now = time.time()
    phase = st["phase"]
    # A new phase, or a hand edit that sent the campaign back, restarts the phase clock.
    if st.get("phase_seen") != phase:
        st["phase_seen"], st["phase_since"] = phase, now
        st["remaining"], st["last_progress"] = None, now
    wd = os.path.join(base, st.get("economy_workdir") or "economy")
    driver = load_json(os.path.join(wd, "driver.json"))
    executing = bool(status.get("executing"))
    runs = int(status.get("runs_finished") or 0)
    if executing or runs != st.get("runs_finished") or not st.get("last_busy"):
        st["last_busy"] = now
    st["runs_finished"] = runs
    idle = now - (st.get("last_busy") or 0)

    stop = os.path.join(wd, "STOP")
    if os.path.exists(stop):
        reason = " ".join(tail(stop, 3)) or "(no reason written)"
        return ("economy driver stopped: %s. Read %s and the slice it names, fix the farm, run "
                "precheck.py on it, delete STOP, then restart the driver." % (reason, stop))

    if driver and driver.get("state") in ("starting", "running"):
        expect = driver.get("expect_by") or (driver.get("updated", 0) + 300)
        log = os.path.join(wd, "economy.log")
        if now <= expect + 60:
            lines = tail(log, 20)
            checks = sum(1 for x in lines if " CHECK " in x)
            return ("economy driver running (slice %s, %s): read the last 20 lines of %s and the newest "
                    "slice file %s; %d CHECK line(s) among them." % (
                        driver.get("slice"), driver.get("farm", "probe"), log, newest(os.path.join(wd, "slices")),
                        checks))
        return ("economy driver silent since %s (stale heartbeat): check that its process runs, read the "
                "tail of %s, and restart it if it died." % (clock(driver.get("updated", 0)), log))

    if status.get("watchdog_stopped") and not executing:
        return ("the plugin watchdog stopped the last run: read claude_watchdog.log in the game's data folder "
                "and the run's output, and fix the stall before you rerun it.")

    if driver and driver.get("state") not in ("done", None) and not executing:
        return ("economy driver exited (%s, code %s, %s): read the tail of %s, fix the cause, then restart it "
                "or delete driver.json if you moved on." % (
                    driver.get("state"), driver.get("exit"),
                    json.dumps({k: driver[k] for k in ("stuck", "unlocks_left", "missing") if k in driver}),
                    os.path.join(wd, "economy.log")))

    check = None
    if phase == "tree":
        check = tree_check(driver, None if (executing or args.no_probe) else bridge, args.tree,
                           st.get("phase_since") or 0)
    elif phase == "achievements":
        check = achievements_check()
    elif phase == "leaderboards":
        lb = os.path.join(base, st.get("leaderboards") or "leaderboards.json")
        check = leaderboards_check(lb)
        if check is None:
            return ("phase leaderboards: write %s listing every board with done=false, rank them by how far we "
                    "trail, and start on the weakest." % lb)
    if check:
        done, left, detail = check
        if st.get("remaining") is None or left < st["remaining"]:
            st["last_progress"] = now
        st["remaining"] = left
        if done:
            i = PHASES.index(phase) if phase in PHASES else len(PHASES)
            nxt = PHASES[i + 1] if i + 1 < len(PHASES) else "complete"
            st["phase"], st["remaining"], st["last_progress"] = nxt, None, now
            st["phase_seen"], st["phase_since"] = nxt, now
            if nxt == "complete":
                return "phase leaderboards done (%s): campaign complete. Report each phase's numbers." % detail
            return "phase %s done (%s): move to %s and report the phase's numbers." % (phase, detail, nxt)
    if phase == "complete":
        return ("campaign complete. Set phase back in %s only when an update reveals new nodes, "
                "achievements or boards." % args.state)
    if not st.get("last_progress"):
        st["last_progress"] = now
    left = "" if st.get("remaining") is None else " (%s left)" % st["remaining"]
    stale = now - st["last_progress"]
    if stale > args.stale_min * 60:
        return ("no progress in phase %s for %.0f min%s: stop waiting; profile the slowest step and change "
                "the approach." % (phase, stale / 60, left))
    if executing:
        return "a run is going in window %s: let it finish, then read its output." % (status.get("window") or "main")
    if idle > args.idle:
        return "game idle %.0f s: start the next step of phase %s%s." % (idle, phase, left)
    return ("game idle %.0f s: read the last run's output, then start the next step of phase %s%s "
            "within a minute." % (idle, phase, left))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default="campaign_state.json")
    p.add_argument("--workdir", help="the economy driver's workdir (overrides the state file)")
    p.add_argument("--leaderboards", help="leaderboards.json path (overrides the state file)")
    p.add_argument("--idle", type=float, default=90, help="seconds of idle game before we say so")
    p.add_argument("--stale-min", type=float, default=60, help="minutes without phase progress before we say so")
    p.add_argument("--tree", default=os.path.join(HERE, "..", "references", "tech_tree.json"))
    p.add_argument("--no-probe", action="store_true", help="never run the read-only cost probe")
    p.add_argument("--status-json", help="read status from this file instead of the bridge (tests)")
    a = p.parse_args(argv)

    st = dict(DEFAULT_STATE)
    st.update(load_json(a.state, {}) or {})
    if a.workdir:
        st["economy_workdir"] = a.workdir
    if a.leaderboards:
        st["leaderboards"] = a.leaderboards
    base = os.path.dirname(os.path.abspath(a.state))

    bridge = None
    if a.status_json:
        status = load_json(a.status_json, {})
    else:
        try:
            from tfwr_bridge import Bridge
            bridge = Bridge(timeout=30)
            status = bridge.status()
        except Exception as e:
            print("bridge unreachable (%s): start the game with the plugin, then run tfwr_bridge.py ping." % e)
            return 2
    try:
        line = decide(st, status, a, base, bridge)
    finally:
        if bridge is not None:
            bridge.close()
    save_json(a.state, st)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
