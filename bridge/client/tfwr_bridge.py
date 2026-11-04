#!/usr/bin/env python3
"""Command-line client for the TFWR Bridge plugin (stdlib only, any OS).

Examples:
  python3 tfwr_bridge.py ping
  python3 tfwr_bridge.py run my_script.py            # window name = file stem
  python3 tfwr_bridge.py run my_script.py --timeout 300   # runs only in main
  python3 tfwr_bridge.py output
  python3 tfwr_bridge.py screenshot shot.png
  python3 tfwr_bridge.py center-farm 0.5 0.5
  python3 tfwr_bridge.py tidy                        # run this after loading a save
  python3 tfwr_bridge.py watchdog --items hay,wood --stall 60   # only these count as progress
  python3 tfwr_bridge.py intervene "user typed a hint"            # mark a human step in the audit log

Exit codes: 0 ok, 2 bridge unreachable, 4 the plugin watchdog stopped the run,
5 timeout, 6 warning, 7 script or bridge error, 8 farm stalled, 9 farm script ended.
"""

import argparse
import base64
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import time

# Takeover mode runs everything through this one window.
# The one window the agent drives. "main" is every save's default file, so a run
# keeps a single window. Takeover replaces main's code, so ask the player first.
AGENT_WINDOW = os.environ.get("TFWR_WINDOW", "main")
LEGACY_WINDOWS = ("claude",)  # agent windows from older versions
COLUMN = 0.45  # the code column's share of the screen width; the farm gets the rest
# Scratch windows the agent made earlier; takeover closes them (which deletes their files).
AGENT_PREFIXES = ("claude_", "bridge_")
# Auto fast-forward for long runs; the env vars override the defaults.
AUTO_FF = float(os.environ.get("TFWR_FF", "128"))  # the top speed
AUTO_FF_AFTER = float(os.environ.get("TFWR_FF_AFTER", "10"))  # real time before it starts
AUTO_FF_STEP = float(os.environ.get("TFWR_FF_STEP", "3"))  # seconds per doubling
BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".tfwr-bridge", "backups")

DEFAULT_PORT = int(os.environ.get("TFWR_BRIDGE_PORT", "7777"))


class BridgeError(RuntimeError):
    pass


class Bridge:
    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, timeout=40):
        self.port = port
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.file = self.sock.makefile("rwb")

    def close(self):
        self.file.close()
        self.sock.close()

    def call(self, *args):
        line = "\t".join(str(a) for a in args) + "\n"
        self.file.write(line.encode("utf-8"))
        self.file.flush()
        reply = self.file.readline()
        if not reply:
            raise BridgeError("the game closed the connection")
        data = json.loads(reply.decode("utf-8"))
        if not data.get("ok"):
            raise BridgeError(data.get("error", "unknown error"))
        return data

    # Convenience wrappers.
    def ping(self):
        return self.call("ping")

    def windows(self):
        return self.call("windows")["windows"]

    def status(self):
        return self.call("status")

    def set_code(self, window, code):
        return self.call("set", window, b64(code))

    def run(self, window, code=None):
        if code is None:
            return self.call("run", window)
        return self.call("run", window, b64(code))

    def stop(self):
        return self.call("stop")

    def output(self):
        return self.call("output")["text"]

    def screenshot(self, path):
        return self.call("screenshot", os.path.abspath(path))

    def center_farm(self, fx=0.5, fy=0.5):
        return self.call("center_farm", fx, fy)

    def zoom(self, value):
        return self.call("zoom", value)

    def move(self, window, x, y):
        return self.call("move", window, x, y)

    def minimize(self, window, on=True):
        return self.call("minimize", window, 1 if on else 0)

    def farm_rect(self):
        return self.call("farm_rect")

    def follow(self, on=True):
        return self.call("follow", 1 if on else 0)

    def arrange(self, column=COLUMN, keep=""):
        """Stacks windows in a left column; never minimizes the window named `keep`."""
        return self.call("arrange", column, keep)

    def layout(self, column=COLUMN):
        """Code column on the left, farm filling the rest.

        Zoom scales the windows and the farm together, and a code window is never
        narrower than its longest line. So we pick the zoom that fits the code
        into the column first, then fit the farm beside it at that zoom or less,
        then arrange again (windows scroll with the farm, so the pan moves them).
        """
        for _ in range(4):
            right = self.arrange(column, keep=AGENT_WINDOW).get("right", column)
            if right <= column + 0.02:
                break
            z = self.farm_rect()["zoom"]
            self.zoom(max(0.2, z * column / right))
            time.sleep(0.1)
        zoom_cap = min(0.6, self.farm_rect()["zoom"])
        self.fit_farm(column, max_zoom=zoom_cap, min_zoom=0.2)
        return self.arrange(column, keep=AGENT_WINDOW)

    def check(self):
        return self.call("check")

    def fit_farm(self, column=COLUMN, rounds=3, max_zoom=0.6, min_zoom=0.25):
        """Zooms and pans so the farm fills the screen to the right of the window column.

        Zoom also scales the code windows, so we keep it between min_zoom and max_zoom.
        A small farm (one tile at the start) then stays small instead of filling the screen.
        """
        fx = (1 + column) / 2
        for _ in range(rounds):
            r = self.farm_rect()
            room_w = (1 - column) * r["screen_w"] * 0.9
            room_h = r["screen_h"] * 0.8
            scale = min(room_w / max(r["w"], 1), room_h / max(r["h"], 1))
            target = min(max(r["zoom"] * scale, min_zoom), max_zoom)
            if abs(target / r["zoom"] - 1) < 0.05:
                break
            self.zoom(target)
            time.sleep(0.1)  # let the camera apply the zoom
        return self.center_farm(fx, 0.5)

    def tidy(self, column=COLUMN):
        """Puts a freshly loaded game into a known state.

        Steps: enter the farm, fit the farm into the right of the screen, stack the
        windows in a left column, and parse every code window. Windows scroll with
        the farm, so we arrange them last. Fix every window the result lists.
        """
        if not self.status()["in_game"]:
            self.call("play")
            time.sleep(2)
        arranged = self.layout(column)
        checked = self.check()
        checked["arranged"] = arranged["windows"]
        return checked

    def backup_saves(self):
        """Copies the game's whole Saves folder to BACKUP_DIR/<timestamp>; returns the path."""
        src = os.path.join(self.ping()["data_dir"], "Saves")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(BACKUP_DIR, stamp)
        n = 1
        while os.path.exists(dst):  # two takeovers in one second
            n += 1
            dst = os.path.join(BACKUP_DIR, f"{stamp}-{n}")
        shutil.copytree(src, dst)
        return dst

    def hover(self, x, y):
        """The tile tooltip at screen point (x, y), in screenshot pixels. Fair: only visible blocks answer."""
        reply = self.call("hover", x, y)
        if reply.get("text"):
            reply["text"] = clean(reply["text"])
        return reply

    def fast_forward(self, factor, until_idle=False):
        """Runs the game's clock `factor` times faster (1 = real time, max 128).

        With until_idle, the plugin drops back to 1 as soon as no code runs.
        """
        return self.call("fast_forward", factor, 1 if until_idle else 0)

    def buy(self, unlock):
        """Clicks a tech-tree node, as the player would. Spends items; ask the player first."""
        reply = self.call("buy", unlock)
        self.call("close_docs")  # a first unlock opens its doc page
        if reply.get("unlock") == "expand":
            self.tidy()  # a bigger farm needs a new frame, or windows cover it
        return reply

    def save(self):
        """Presses Save. The plugin turns the file watcher off on the first injection,
        so Save's own file writes can't come back and replace injected code."""
        return self.call("save")

    def say(self, text, seconds=0):
        """Shows a caption at the bottom of the game window (0 seconds = until replaced)."""
        return self.call("say", b64(text), seconds)

    def wait(self, text="", timeout=0, goal=None):
        """Shows a live "Waiting for TEXT · progress · speed" line under the caption.
        goal is "ITEM=TARGET" (e.g. "bone=2000000"): the line then shows the item's count
        against the target and an estimate of the time left. Without one, it shows
        elapsed against timeout. Empty text clears it. A run sets "WINDOW to finish"
        itself and clears it at the end."""
        if not text:
            return self.call("wait")
        if goal:
            item, target = goal.split("=")
            return self.call("wait", b64(text), timeout, item.strip(), float(target))
        return self.call("wait", b64(text), timeout)

    def watcher(self, on):
        """Turns the game's file watcher on or off. Off while the bridge drives; turn it
        back on before handing the game to a player who edits files outside the game."""
        return self.call("watcher", 1 if on else 0)

    def saves(self):
        return self.call("saves")

    def switch_save(self, name=None, create=False, wait=60):
        """Creates and/or loads a save slot, then waits for the game to come back.

        The game reloads its scene to switch slots, which drops our connection.
        Returns a fresh Bridge connected to the reloaded game.
        """
        reply = self.call("new_save", name or "") if create else self.call("load_save", name)
        self.close()
        deadline = time.time() + wait
        time.sleep(1)
        while time.time() < deadline:
            try:
                b = Bridge(port=self.port)
                if b.ping()["active_save"] == reply["save"]:
                    b.call("play")
                    time.sleep(2)
                    return b, reply["save"]
                b.close()
            except (OSError, BridgeError):
                pass
            time.sleep(0.5)
        raise BridgeError("the game did not come back after switching saves")

    def takeover(self, column=COLUMN):
        """Takes over the workspace. Ask the player first; this rearranges their screen.

        Steps: back up the saves, close doc pages and the agent's old scratch windows,
        minimize the player's own code windows (their files stay), open one agent
        window, fit the farm, and arrange. Returns (backup path, arranged windows).
        """
        if not self.status()["in_game"]:
            self.call("play")
            time.sleep(2)
        backup = self.backup_saves()
        self.call("close_docs")
        for w in self.windows():
            name = w["name"]
            if name == AGENT_WINDOW:
                continue
            if name.startswith(AGENT_PREFIXES) or name in LEGACY_WINDOWS:
                self.call("close", name)
            elif not w["minimized"]:
                self.minimize(name)
        if not any(w["name"] == AGENT_WINDOW for w in self.windows()):
            self.set_code(AGENT_WINDOW, "# The agent's window. Code arrives here through the bridge.\n")
        arranged = self.layout(column)
        return backup, arranged["windows"]

    def error(self, window):
        return self.call("error", window)["error"]

    def run_and_wait(self, window, code=None, timeout=120.0, poll=0.1, ff=AUTO_FF, ff_after=AUTO_FF_AFTER,
                     ff_step=AUTO_FF_STEP, wait_for=None, goal=None, stop_at_goal=False,
                     allow_warnings=False, run_id=None, keep_running=False, started=None):
        """Runs a window, waits for it to end, and returns (output, seconds, state).

        state is "finished", "error", "goal", "watchdog" or "timeout" (we stop the run on a
        timeout). "watchdog" means the plugin watchdog stopped the run; the output then
        ends with a "Watchdog: REASON" line, so a loop can't mistake it for a clean finish.
        A warning in the output stops the run as "warning" (a loop repeats it every
        lap), unless allow_warnings.
        With stop_at_goal, we stop the run once the goal item reaches its target:
        an endless farming loop then ends exactly when we have what we need.
        The plugin counts finished runs, so even a run that ends between two polls
        is caught, and the player's code needs no end marker.
        With run_id, we watch that run instead of starting one; started is its start
        time (for the ff ramp). With keep_running, a timeout leaves the run going.
        self.last_run_id holds the run we watched.
        Auto fast-forward: a run still going after ff_after real seconds speeds up
        2x, then doubles every ff_step seconds (4x, 8x, ...) up to ff. So the first
        stretch stays watchable and only the long, boring part skips. ff=1 turns it
        off. The plugin resets the clock when the run ends.
        """
        before = self.output()
        if run_id is None:
            # The status clock counts toward our own timeout, so viewers see how long is left.
            self.wait(wait_for or f"{window} to finish", timeout, goal)
            if not keep_running:
                # Backstop: the plugin stops the run shortly after our own timeout.
                self.watchdog(limit=timeout + 30)
            run_id = self.run(window, code)["run_id"]
        self.last_run_id = run_id
        start = time.time()
        ramp_start = started or start
        sped = started is not None
        factor = None if sped else 1.0
        polls = 0
        while time.time() - start < timeout:
            time.sleep(poll)
            elapsed = time.time() - ramp_start - ff_after
            if ff > 1 and elapsed >= 0:
                want = min(ff, 2.0 ** (1 + int(elapsed / ff_step))) if ff_step > 0 else ff
                if want != factor:
                    self.fast_forward(want, until_idle=True)
                    factor = want
                    sped = True
            st = self.status()
            self.last_goal_have = float(st.get("goal_have", 0) or 0)
            # The game can flag an error while the program still counts as
            # executing (e.g. a locked feature). Check the window every half
            # second, so we report it at once instead of waiting for the timeout.
            polls += 1
            if polls % max(1, int(0.5 / poll)) == 0:
                err = self.error(window)
                if not allow_warnings and st.get("warning"):
                    self.stop()
                    self.wait()
                    if sped:
                        self.fast_forward(1)
                    log = self.output()
                    new = _new_output(before, log)
                    if "Warning:" not in new:  # the game logs a repeat warning only once
                        new += ("\n" if new else "") + log[log.rfind("Warning:"):] if "Warning:" in log else ""
                    return clean(new), time.time() - start, "warning"
                if err:
                    self.stop()
                    self.wait()
                    if sped:
                        self.fast_forward(1)
                    out = clean(_new_output(before, self.output()))
                    return out + ("\n" if out else "") + "Error: " + clean(err), time.time() - start, "error"
            if stop_at_goal and st.get("goal_reached") and st["runs_finished"] < run_id:
                self.stop()
                self.wait()
                if sped:
                    self.fast_forward(1)
                return clean(_new_output(before, self.output())), time.time() - start, "goal"
            if st["runs_finished"] >= run_id:
                out = _new_output(before, self.output())
                err = self.error(window)
                if err and "Error:" not in out:
                    out += "\nError: " + err
                failed = bool(err) or "Error:" in out
                stopped = st.get("watchdog_stopped")
                if stopped and not failed:
                    out += ("\n" if out and not out.endswith("\n") else "") + "Watchdog: " + stopped
                    return clean(out), time.time() - start, "watchdog"
                return clean(out), time.time() - start, "error" if failed else "finished"
        if keep_running:
            return clean(_new_output(before, self.output())), time.time() - start, "timeout"
        self.stop()
        self.wait()
        if sped:
            self.fast_forward(1)
        return clean(_new_output(before, self.output())), time.time() - start, "timeout"

    def watchdog(self, limit=None, stall=None, items=None):
        """Sets the plugin watchdog for the next run (0 = off), or reads it.

        The plugin stops a bridge run after `limit` real seconds, or after `stall`
        seconds with no progress. items is a list of goal items (or "a,b"); with goal
        items set, only their rise counts. A `wait` goal (run --goal, farm) adds its item.
        With no goal item at all, a rise in any item except water and fertilizer counts.
        items=[] clears them. The run takes these settings when it starts, and the
        next run gets the defaults again (20 min, 5 min, no goal items).
        """
        args = []
        if limit is not None:
            args += ["limit", str(limit)]
        if stall is not None:
            args += ["stall", str(stall)]
        if items is not None:
            if not isinstance(items, str):
                items = ",".join(items)
            args += ["items", items or "none"]
        return self.call("watchdog", *args)

    def inventory(self):
        """Every item count, as the top bar shows them (during simulate(), the sandbox's)."""
        return self.call("inventory")["items"]

    def intervene(self, note):
        """Marks a human intervention in the plugin's audit log, for benchmark scoring."""
        return self.call("intervene", b64(note))

    def farm(self, window, code, goal, chunk=15.0, wait_for=None, stall_chunks=2, max_chunks=0,
             max_minutes=60, **kw):
        """Runs an endless farming script in short chunks until the goal item reaches its target.

        After each chunk it reads the item count. It stops and reports on the goal, an
        error, a warning, or a stall: silence longer than 3x the longest gap between
        gains seen so far (at least 10 s), or no gain in the first 60 s.
        The script starts once and keeps running; a chunk is only a check-in, so a
        long task never gets cut off. We stop it on the goal, an
        error, a warning, a stall or the chunk limit.
        Yields (chunk, have, target, state, output) after every chunk.
        """
        target = float(goal.split("=")[1])
        # The plugin enforces the cap even if this client dies mid-run.
        self.watchdog(limit=max_minutes * 60)
        last, i = None, 0
        run_id, started = None, None
        gain_at, first_at, max_gap = None, time.time(), 0.0
        while True:
            i += 1
            out, secs, state = self.run_and_wait(window, code if run_id is None else None, timeout=chunk,
                                                 wait_for=wait_for, goal=goal, stop_at_goal=True,
                                                 run_id=run_id, keep_running=True, started=started, **kw)
            if run_id is None:
                run_id, started = self.last_run_id, time.time() - secs
            # Read the count the watch saw last: a stop clears the status goal.
            have = getattr(self, "last_goal_have", 0.0)
            if state in ("error", "warning", "goal", "finished", "watchdog"):
                self.wait()
                yield i, have, target, state, out
                return
            # Adaptive stall check: learn this script's normal gap between gains.
            # A steady producer stalls after 3x its longest gap (at least 10 s);
            # a lumpy one (one big payout) keeps its long gaps. No gain at all
            # within the first 60 s also counts as a stall.
            now = time.time()
            if last is not None and have > last:
                if gain_at is not None:
                    max_gap = max(max_gap, now - gain_at)
                gain_at = now
            last = have
            # While the player holds manual speed (the in-game buttons), the gaps say
            # nothing about a stall: they watch the run themselves, so the clock pauses.
            try:
                manual = self.call("speed", "status").get("manual")
            except Exception:
                manual = False
            if manual:
                gain_at = now
            quiet = now - (gain_at or first_at)
            limit = max(10.0, 3 * max_gap) if max_gap else 60.0
            stalled = quiet > limit
            if stalled or (max_chunks and i >= max_chunks):
                self.stop()
                self.wait()
                self.fast_forward(1)
                yield i, have, target, "stalled" if stalled else "chunks", out
                return
            yield i, have, target, "running", out


def clean(text):
    """Strips the game's rich-text tags (<font=...>, <noparse>, <color=...>)."""
    return re.sub(r"</?(font|noparse|color|b|i)(=[^>]*)?>", "", text)


def _new_output(before, after):
    """The part of the game's output log written since `before` was read."""
    return after[len(before):] if after.startswith(before) else after


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def lint_passes(bridge, path):
    """Lints a script against the current save before it runs.

    A rule the agent must remember is optional in practice: an agent skipped lint for
    a whole session (2026-11-04). So run and farm lint for it and refuse on any ERROR.
    """
    linter = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts", "lint_tfwr.py")
    if not (os.path.isfile(linter) and os.path.isfile(path)):
        return True
    cmd = [sys.executable, linter, path]
    try:
        info = bridge.ping()
        cmd += ["--save-dir", os.path.join(info["data_dir"], "Saves", info["active_save"])]
    except (BridgeError, KeyError, OSError):
        pass
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if "0 error(s), 0 warning(s)" not in r.stdout:
        sys.stdout.write(r.stdout)
    if r.returncode != 0:
        print("[bridge] lint found errors, so we did not run it. Fix them, or pass --no-lint.")
    return r.returncode == 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    sub.add_parser("play", help="press Start on the title screen")
    sub.add_parser("save", help="press the menu's Save button")
    sub.add_parser("lb", help="leaderboard run state and the result screen's text")
    sub.add_parser("lb-ok", help="press OK on the leaderboard result screen")
    sub.add_parser("windows")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("output")
    er = sub.add_parser("error", help="a window's current error message, if any")
    er.add_argument("window")
    r = sub.add_parser("run", help="send a file's code to a window, run it, wait and print output")
    r.add_argument("file")
    r.add_argument("--window", default=AGENT_WINDOW, help="window name (default: %(default)s)")
    r.add_argument("--timeout", type=float, default=120.0)
    r.add_argument("--no-wait", action="store_true")
    r.add_argument("--say", help="caption to show while it runs (default: 'Running WINDOW...')")
    r.add_argument("--wait-for", help="status line text (default: 'WINDOW to finish')")
    r.add_argument("--goal", help="ITEM=TARGET the script works toward, e.g. bone=2000000; the status line shows progress on it")
    r.add_argument("--allow-warnings", action="store_true",
                   help="keep running when the game prints a warning (default: stop and report it)")
    r.add_argument("--stop-at-goal", action="store_true",
                   help="stop the run once the goal is reached (for endless farming loops; never for scripts that must clean up at the end)")
    r.add_argument("--ff", type=float, default=AUTO_FF,
                   help="top clock factor for long runs (default %(default)s, env TFWR_FF; 1 = never)")
    r.add_argument("--ff-step", type=float, default=AUTO_FF_STEP,
                   help="real seconds per doubling from 2x (default %(default)s; 0 = jump to the top)")
    r.add_argument("--ff-after", type=float, default=AUTO_FF_AFTER,
                   help="real seconds before the fast-forward starts (default %(default)s; 0 = at once)")
    r.add_argument("--no-lint", action="store_true", help="skip the automatic lint (not for scripts that join a loop)")
    fa = sub.add_parser("farm", help="run an endless farming script in short chunks until a goal; stop on error, warning or stall")
    fa.add_argument("--no-lint", action="store_true", help="skip the automatic lint (not for scripts that join a loop)")
    fa.add_argument("file")
    fa.add_argument("--goal", required=True, help="ITEM=TARGET, e.g. hay=1500")
    fa.add_argument("--window", default=AGENT_WINDOW)
    fa.add_argument("--chunk", type=float, default=15.0, help="real seconds per run (default %(default)s)")
    fa.add_argument("--wait-for", help="status line text")
    fa.add_argument("--max-chunks", type=int, default=0, help="stop after this many chunks (0 = until the goal)")
    fa.add_argument("--max-minutes", type=float, default=60, help="the plugin watchdog stops the run after this (default %(default)s)")
    fa.add_argument("--allow-warnings", action="store_true")
    fa.add_argument("--ff", type=float, default=AUTO_FF)
    fa.add_argument("--ff-after", type=float, default=2.0)
    fa.add_argument("--ff-step", type=float, default=1.0)
    s = sub.add_parser("set", help="put a file's code into a window without running it")
    s.add_argument("file")
    s.add_argument("--window")
    sh = sub.add_parser("screenshot")
    sh.add_argument("path")
    c = sub.add_parser("center-farm")
    c.add_argument("fx", type=float, nargs="?", default=0.5)
    c.add_argument("fy", type=float, nargs="?", default=0.5)
    z = sub.add_parser("zoom")
    z.add_argument("value", type=float)
    m = sub.add_parser("move")
    m.add_argument("window")
    m.add_argument("x", type=float)
    m.add_argument("y", type=float)
    sub.add_parser("farm-rect", help="the farm's on-screen rectangle, in pixels")
    sub.add_parser("check", help="parse every code window and list the ones with errors")
    a = sub.add_parser("arrange", help="stack windows in a left column")
    a.add_argument("column", type=float, nargs="?", default=COLUMN)
    t = sub.add_parser("tidy", help="after loading: arrange windows, center the farm, check code")
    t.add_argument("column", type=float, nargs="?", default=COLUMN)
    hv = sub.add_parser("hover", help="the tooltip a mouse would show at screen point X Y (screenshot pixels)")
    hv.add_argument("x", type=float)
    hv.add_argument("y", type=float)
    ff = sub.add_parser("fast-forward", help="run the game clock N times faster (1 = real time, max 128)")
    ff.add_argument("factor", type=float)
    bu = sub.add_parser("buy", help="click a tech-tree node (spends items)")
    bu.add_argument("unlock", help="e.g. Loops or Unlocks.Loops")
    sub.add_parser("saves", help="list save slots and the active one")
    ns = sub.add_parser("new-run", help="save, create a new slot, load it, and take it over")
    ns.add_argument("name", nargs="?", help="slot name (default: the next free SaveN)")
    ls_ = sub.add_parser("load-save", help="save, then load another slot")
    ls_.add_argument("name")
    tk = sub.add_parser("takeover", help="back up, then consolidate everything into one agent window")
    tk.add_argument("column", type=float, nargs="?", default=COLUMN)
    fo = sub.add_parser("follow", help="keep the farm in view when it drifts")
    fo.add_argument("--off", action="store_true")
    sy = sub.add_parser("say", help="show a caption at the bottom of the game window")
    sy.add_argument("text")
    sy.add_argument("--seconds", type=float, default=0, help="hide after this long (default: until replaced)")
    wt = sub.add_parser("wait", help="show what we wait for in a live line under the caption (no text clears it)")
    wt.add_argument("text", nargs="?", default="")
    wt.add_argument("--timeout", type=float, default=0, help="show elapsed against this many seconds")
    wt.add_argument("--goal", help="ITEM=TARGET: show progress on that item instead of the clock")
    wa = sub.add_parser("watcher", help="turn the game's file watcher on or off (the bridge turns it off)")
    wa.add_argument("state", choices=["on", "off"])
    ts = sub.add_parser("text-scale", help="code text size relative to the farm: 1 to 8, or auto (screen height / 540); it persists (no value reads it)")
    ts.add_argument("value", nargs="?")
    sp = sub.add_parser("speed", help="the player's speed buttons: play (1x), ff (cycle 2x..128x), auto, status")
    sp.add_argument("mode", choices=["play", "ff", "auto", "status"])
    wd = sub.add_parser("watchdog", help="read or set the plugin's run limits for the next run (seconds, 0 = off)")
    wd.add_argument("--limit", type=float)
    wd.add_argument("--stall", type=float, help="seconds without progress (0 = off, e.g. for simulate() benches)")
    wd.add_argument("--items", help="comma-separated goal items; only their rise counts as progress ('none' clears)")
    sub.add_parser("inventory", help="every item count, as the top bar shows them")
    iv = sub.add_parser("intervene", help="log a human-intervention marker in the plugin's audit log")
    iv.add_argument("note")
    mn = sub.add_parser("minimize")
    mn.add_argument("window")
    mn.add_argument("--off", action="store_true")
    args = p.parse_args(argv)

    try:
        bridge = Bridge(port=args.port)
    except OSError as e:
        print(f"cannot reach the bridge on port {args.port}: {e}", file=sys.stderr)
        print("Is the game running with BepInEx and the TfwrBridge plugin?", file=sys.stderr)
        return 2

    if args.cmd in ("run", "farm") and not args.no_lint and not lint_passes(bridge, args.file):
        return 7

    try:
        if args.cmd == "farm":
            with open(args.file, encoding="utf-8") as f:
                code = f.read()
            state = "running"
            for i, have, target, state, out in bridge.farm(
                    args.window, code, args.goal, chunk=args.chunk, wait_for=args.wait_for,
                    max_chunks=args.max_chunks, max_minutes=args.max_minutes, allow_warnings=args.allow_warnings,
                    ff=args.ff, ff_after=args.ff_after, ff_step=args.ff_step):
                print(f"[farm] chunk {i}: {have:,.0f} of {target:,.0f} ({state})", flush=True)
                if state not in ("running", "goal"):
                    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
            return {"goal": 0, "chunks": 0, "error": 7, "warning": 6, "stalled": 8, "finished": 9, "watchdog": 4}.get(state, 0)
        if args.cmd in ("run", "set"):
            with open(args.file, encoding="utf-8") as f:
                code = f.read()
            window = args.window if args.cmd == "run" else (args.window or os.path.splitext(os.path.basename(args.file))[0])
            if args.cmd == "set":
                print(json.dumps(bridge.set_code(window, code)))
                return 0
            if args.say:
                bridge.say(args.say)
            if args.no_wait:
                print(json.dumps(bridge.run(window, code)))
                return 0
            out, secs, state = bridge.run_and_wait(window, code, timeout=args.timeout,
                                                    ff=args.ff, ff_after=args.ff_after, ff_step=args.ff_step,
                                                    wait_for=args.wait_for, goal=args.goal,
                                                    stop_at_goal=args.stop_at_goal,
                                                    allow_warnings=args.allow_warnings)
            sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
            print(f"[bridge] {window} {state} in {secs:.1f}s", file=sys.stderr)
            return {"finished": 0, "goal": 0, "error": 7, "warning": 6, "timeout": 5, "watchdog": 4}[state]
        if args.cmd == "error":
            print(clean(bridge.error(args.window) or ""))
            return 0
        if args.cmd == "output":
            sys.stdout.write(bridge.output())
            return 0
        if args.cmd == "windows":
            for w in bridge.windows():
                print(json.dumps(w))
            return 0
        if args.cmd == "screenshot":
            print(json.dumps(bridge.screenshot(args.path)))
            return 0
        if args.cmd == "center-farm":
            print(json.dumps(bridge.center_farm(args.fx, args.fy)))
            return 0
        if args.cmd == "zoom":
            print(json.dumps(bridge.zoom(args.value)))
            return 0
        if args.cmd == "move":
            print(json.dumps(bridge.move(args.window, args.x, args.y)))
            return 0
        if args.cmd in ("check", "tidy"):
            result = bridge.check() if args.cmd == "check" else bridge.tidy(args.column)
            for w in result["windows"]:
                mark = "ok " if w["ok"] else "ERR"
                print(f"{mark} {w['name']}" + ("" if w["ok"] else f": {clean(w['error'])}"))
            print(f"[bridge] {result['errors']} window(s) with errors", file=sys.stderr)
            return 0 if result["errors"] == 0 else 7
        if args.cmd == "hover":
            r = bridge.hover(args.x, args.y)
            print(r["text"] if r.get("hit") else "(no tile there)")
            return 0
        if args.cmd == "fast-forward":
            print(json.dumps(bridge.fast_forward(args.factor)))
            return 0
        if args.cmd == "buy":
            print(json.dumps(bridge.buy(args.unlock)))
            return 0
        if args.cmd == "saves":
            print(json.dumps(bridge.saves()))
            return 0
        if args.cmd in ("new-run", "load-save"):
            bridge, slot = bridge.switch_save(args.name, create=args.cmd == "new-run")
            print(f"active save: {slot}")
            if args.cmd == "new-run":
                backup, arranged = bridge.takeover()
                print(f"backup: {backup}")
            return 0
        if args.cmd == "takeover":
            backup, arranged = bridge.takeover(args.column)
            print(f"backup: {backup}")
            for w in arranged:
                print(("min  " if w["minimized"] else "open ") + w["name"])
            return 0
        if args.cmd == "arrange":
            print(json.dumps(bridge.arrange(args.column)))
            return 0
        if args.cmd == "farm-rect":
            print(json.dumps(bridge.farm_rect()))
            return 0
        if args.cmd == "follow":
            print(json.dumps(bridge.follow(not args.off)))
            return 0
        if args.cmd == "text-scale":
            print(json.dumps(bridge.call("text_scale", *([args.value] if args.value is not None else []))))
            return 0
        if args.cmd == "speed":
            print(json.dumps(bridge.call("speed", args.mode)))
            return 0
        if args.cmd == "watchdog":
            items = None if args.items is None else ("" if args.items.lower() == "none" else args.items)
            print(json.dumps(bridge.watchdog(args.limit, args.stall, items)))
            return 0
        if args.cmd == "inventory":
            print(json.dumps(bridge.inventory()))
            return 0
        if args.cmd == "intervene":
            print(json.dumps(bridge.intervene(args.note)))
            return 0
        if args.cmd == "minimize":
            print(json.dumps(bridge.minimize(args.window, not args.off)))
            return 0
        if args.cmd == "say":
            print(json.dumps(bridge.say(args.text, args.seconds)))
            return 0
        if args.cmd == "wait":
            print(json.dumps(bridge.wait(args.text, args.timeout, args.goal)))
            return 0
        if args.cmd == "watcher":
            print(json.dumps(bridge.watcher(args.state == "on")))
            return 0
        if args.cmd == "lb":
            print(json.dumps(bridge.call("lb")))
            return 0
        if args.cmd == "lb-ok":
            print(json.dumps(bridge.call("lb_ok")))
            return 0
        if args.cmd == "save":
            print(json.dumps(bridge.save()))
            return 0
        print(json.dumps(bridge.call(args.cmd)))
        return 0
    except BridgeError as e:
        print(f"bridge error: {e}", file=sys.stderr)
        return 7
    finally:
        bridge.close()


if __name__ == "__main__":
    sys.exit(main())
