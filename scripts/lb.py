#!/usr/bin/env python3
"""Runs one leaderboard: lb.py BOARD PROGRAM_FILE [SPEEDUP] [MAX_SECONDS]

Stages PROGRAM_FILE in a claude_ window, starts leaderboard_run() from main, polls
the result screen, prints the time, presses OK and closes the staging window.
It runs on any OS; scripts/lb.sh calls it.

Steps:
  1. Stage the program in a window named claude_<file stem>.
  2. Set the plugin watchdog: MAX_SECONDS overall, 10 minutes without progress.
  3. Start the board without waiting (a `run` timeout would cancel a long board).
  4. Poll every 5 s and print a check-in line every minute.
  5. On the result screen, print it and any watchdog stop, press OK, close the window.

Exit codes: 0 result screen reached, 1 no result (timeout or the run ended without
one), 2 bridge unreachable.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge", "client"))
from tfwr_bridge import Bridge, BridgeError, clean  # noqa: E402


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    board, prog = argv[0], argv[1]
    speed = argv[2] if len(argv) > 2 else "128"
    limit = float(argv[3]) if len(argv) > 3 else 900.0
    # A path as given, else relative to this folder (the old lb.sh behavior).
    path = prog if os.path.isfile(prog) else os.path.join(HERE, prog)
    stem = os.path.splitext(os.path.basename(path))[0]
    # The plugin opens only claude_ staging windows besides main.
    win = stem if stem.startswith("claude_") else "claude_" + stem
    try:
        b = Bridge()
    except OSError as e:
        print(f"cannot reach the bridge: {e}", file=sys.stderr)
        return 2

    def close_win():
        # Closing deletes the save's copy; the program's source stays in PROGRAM_FILE.
        try:
            b.call("close", win)
        except BridgeError:
            pass

    with open(path, encoding="utf-8") as f:
        b.set_code(win, f.read())
    # The plugin watchdog enforces the limit even if this script dies. Boards get a
    # 10-minute stall window, since some pay out in long lumps.
    b.watchdog(limit=limit, stall=600)
    try:
        b.run("main", f'leaderboard_run(Leaderboards.{board}, "{win}", {speed})\n')
    except BridgeError as e:
        print(f"{board}: the run did not start: {clean(str(e))}")
        close_win()
        return 1
    start = time.time()
    next_check = start + 60
    st = {}
    while time.time() - start < limit:
        time.sleep(5)
        st = b.call("lb")
        if st.get("screen"):
            print(f"{board}: {json.dumps(st)}")
            stopped = b.status().get("watchdog_stopped")
            if stopped:
                print(f"{board}: watchdog stopped the run: {stopped}")
            b.call("lb_ok")
            close_win()
            return 0
        # A run that ended with no result screen failed: say why instead of waiting.
        if not st.get("running") and not b.status().get("executing"):
            open_windows = [w["name"] for w in b.windows()]
            err = b.error("main") or (b.error(win) if win in open_windows else None)
            print(f"{board}: the run ended without a result: {clean(err or '') or 'no error shown'}")
            stopped = b.status().get("watchdog_stopped")
            if stopped:
                print(f"{board}: watchdog stopped the run: {stopped}")
            close_win()
            return 1
        # A check-in every minute, so a log reader sees a run that drags on.
        if time.time() >= next_check:
            print(f"{board} check-in {time.time() - start:.0f}s: {json.dumps(st)}", flush=True)
            next_check += 60
    print(f"{board}: no result after {limit:.0f}s: {json.dumps(st)}")
    b.stop()
    close_win()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
