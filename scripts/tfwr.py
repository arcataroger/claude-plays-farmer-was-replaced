#!/usr/bin/env python3
"""Helper for The Farmer Was Replaced (and its mining DLC, The Miner Was Replaced).

Subcommands:
  paths                 Show the install docs folder, save root, save slots and freshest stub.
  status [--slot N]     Show unlocks, items, code files and the tail of output.txt.
                        Reads ONLY the 'unlocks' and 'items' keys of the save JSON.
  doc [NAME]            Print an in-game doc page with example display settings stripped.
                        With no NAME, list all pages. NAME matches a file stem, e.g. 'dynamite'.
  fn NAME               Print the in-game tooltip and stub docstring for a builtin function.
  errors [TEXT]         Print in-game error and warning strings, optionally filtered by TEXT.
  output [--slot N]     Print output.txt (the game writes it when a run stops).

Fair play: this tool never reads the save's 'grounds' or 'entities' keys. Those hold the
world map, including blocks the drone has not sensed. Scripts must discover the world
through the drone's own sensors.

Override detection with the env vars TFWR_LANGUAGES (the StreamingAssets/Languages folder)
and TFWR_SAVES (the folder that holds Save0, Save0.json, ...).
"""
import glob
import json
import os
import re
import sys

HOME = os.path.expanduser("~")

GAME = "The Farmer Was Replaced"
LANG_SUBDIRS = [
    "TheFarmerWasReplaced.app/Contents/Resources/Data/StreamingAssets/Languages",  # macOS
    "TheFarmerWasReplaced_Data/StreamingAssets/Languages",  # Windows and Linux
]


def steam_roots():
    """Steam's own folders for this OS, most likely first."""
    if sys.platform == "darwin":
        return [os.path.join(HOME, "Library", "Application Support", "Steam")]
    if sys.platform.startswith("win"):
        roots = [os.path.join(os.environ[v], "Steam") for v in ("ProgramFiles(x86)", "ProgramFiles")
                 if os.environ.get(v)]
        return roots + ["C:/Program Files (x86)/Steam", "C:/Program Files/Steam"]
    return [os.path.join(HOME, ".steam", "steam"), os.path.join(HOME, ".local", "share", "Steam")]


def steam_libraries():
    """Every Steam library: each root plus the "path" entries in its libraryfolders.vdf.

    Games often live in a second library (e.g. D:/SteamLibrary), so the default root is not enough.
    """
    libs = []
    for root in steam_roots():
        libs.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    # vdf escapes each backslash ("D:\\SteamLibrary"); collapse any run of them.
                    libs.append(os.path.normpath(re.sub(r"\\+", "/", m.group(1))))
        except OSError:
            pass
    out = []
    for lib in libs:
        if lib not in out:
            out.append(lib)
    return out


def install_candidates():
    return [os.path.join(lib, "steamapps", "common", GAME, sub) for lib in steam_libraries() for sub in LANG_SUBDIRS]


def game_build():
    """Steam's build id for the installed game (appmanifest_2060160.acf), or None.

    Steam can update the game mid-project; runs must record which build they played.
    """
    for lib in steam_libraries():
        acf = os.path.join(lib, "steamapps", "appmanifest_2060160.acf")
        try:
            with open(acf, encoding="utf-8", errors="replace") as f:
                m = re.search(r'"buildid"\s+"(\d+)"', f.read())
            if m:
                return m.group(1)
        except OSError:
            pass
    return None


SAVE_CANDIDATES = [
    HOME + "/Library/Application Support/com.TheFarmerWasReplaced.TheFarmerWasReplaced/Saves",
    os.path.expandvars("%USERPROFILE%/AppData/LocalLow/TheFarmerWasReplaced/TheFarmerWasReplaced/Saves"),
    HOME + "/.config/unity3d/TheFarmerWasReplaced/TheFarmerWasReplaced/Saves",
    HOME + "/.steam/steam/steamapps/compatdata/2060160/pfx/drive_c/users/steamuser/AppData/"
    "LocalLow/TheFarmerWasReplaced/TheFarmerWasReplaced/Saves",
]


def languages_dir():
    env = os.environ.get("TFWR_LANGUAGES")
    for p in ([env] if env else []) + install_candidates():
        if p and os.path.isdir(p):
            return p
    return None


def bridge_data_dir():
    """The game's own data folder, from the bridge's ping; None when the bridge is down."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge", "client"))
        from tfwr_bridge import Bridge
        return Bridge(timeout=1).call("ping").get("data_dir")
    except Exception:
        return None


def saves_dir():
    """TFWR_SAVES, then the running game's own data folder, then the usual locations per OS."""
    env = os.environ.get("TFWR_SAVES")
    live = bridge_data_dir()
    for p in ([env] if env else []) + ([os.path.join(live, "Saves")] if live else []) + SAVE_CANDIDATES:
        if p and os.path.isdir(p):
            return p
    return None


def slots(saves):
    return sorted(
        d for d in os.listdir(saves) if os.path.isdir(os.path.join(saves, d)) and re.match(r"Save\d+$", d)
    )


def freshest_stub(lang, saves):
    """Return the newest builtins stub. The game writes one per save; it can be newer than the install's."""
    cands = []
    if lang and os.path.exists(os.path.join(lang, "builtins.py")):
        cands.append(os.path.join(lang, "builtins.py"))
    if saves:
        cands += glob.glob(os.path.join(saves, "Save*", "__builtins__.py"))
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def pick_slot(saves, argv):
    if "--slot" in argv:
        return "Save" + argv[argv.index("--slot") + 1].replace("Save", "")
    s = slots(saves)
    if not s:
        return None
    # The running game knows its slot; ask the bridge when it's up.
    active = _bridge_active_save()
    if active in s:
        return active
    # Otherwise, the slot whose save.json the game wrote last.
    def written(d):
        j = os.path.join(saves, d, "save.json")
        return os.path.getmtime(j) if os.path.exists(j) else os.path.getmtime(os.path.join(saves, d))
    # A folder with no save.json holds nothing to read, so prefer slots that have one.
    real = [d for d in s if os.path.exists(os.path.join(saves, d, "save.json"))]
    return max(real or s, key=written)


def _bridge_active_save():
    """The game's active save slot from the TFWR Bridge, or None when the bridge is down."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(os.environ.get("TFWR_BRIDGE_PORT", "7777"))), timeout=0.3) as sock:
            sock.sendall(b"ping\n")
            line = sock.makefile("rb").readline()
        return json.loads(line).get("active_save")
    except (OSError, ValueError):
        return None


def strip_examples(text):
    # Replace the JSON display settings of {{codeexample {...} ...}} with a marker; keep the code.
    text = re.sub(r"\{\{codeexample\s*\{.*?\n\}\n", "{{example\n", text, flags=re.S)
    return text


def cmd_paths(argv):
    lang, saves = languages_dir(), saves_dir()
    print("languages:", lang or "NOT FOUND (set TFWR_LANGUAGES)")
    print("docs:     ", os.path.join(lang, "EN", "docs") if lang else "-")
    print("saves:    ", saves or "NOT FOUND (set TFWR_SAVES)")
    if saves:
        for s in slots(saves):
            p = os.path.join(saves, s)
            files = sorted(f for f in os.listdir(p) if f.endswith(".py") and f != "__builtins__.py")
            print("  %s: %d code files" % (s, len(files)))
    stub = freshest_stub(lang, saves)
    print("stub:     ", stub or "-")


def cmd_status(argv):
    saves = saves_dir()
    if not saves:
        sys.exit("Save folder not found. Set TFWR_SAVES.")
    slot = pick_slot(saves, argv)
    if not slot:
        sys.exit("No save slots in " + saves)
    print("slot:", slot, "(" + os.path.join(saves, slot) + ")")
    # Current format: Saves/SaveN/save.json. Pre-2026 format: Saves/SaveN.json (also held the world map).
    jpath = os.path.join(saves, slot, "save.json")
    if not os.path.exists(jpath):
        jpath = os.path.join(saves, slot + ".json")
    if os.path.exists(jpath) and os.path.getsize(jpath) == 0:
        sys.exit("save.json is empty: a brand new slot saves only later. Run `tfwr_bridge.py save` first.")
    if os.path.exists(jpath):
        with open(jpath) as f:
            data = json.load(f)
        # Read ONLY these two keys. Never touch 'grounds' or 'entities' (hidden world state).
        unlocks = data.get("unlocks", [])
        items = data.get("items", {}).get("serializeList", [])
        print("\nunlocks (%d, internal lowercase ids; includes language features):" % len(unlocks))
        print("  " + ", ".join(unlocks))
        print("\nitems:")
        for it in items:
            print("  %-18s %s" % (it.get("name"), it.get("nr")))
        wins = [w.get("key") for w in data.get("openFilePositions", [])]
        print("\nopen code windows:", ", ".join(str(w) for w in wins))
        print("\nsave json:", jpath, "modified", _mtime(jpath))
    else:
        print("no", slot + ".json yet")
    p = os.path.join(saves, slot)
    files = sorted(f for f in os.listdir(p) if f.endswith(".py") and f != "__builtins__.py")
    print("\ncode files:", ", ".join(files) or "(none)")
    out = os.path.join(os.path.dirname(saves), "output.txt")
    if os.path.exists(out):
        print("\noutput.txt (%s), last 15 lines:" % _mtime(out))
        with open(out, errors="replace") as f:
            for line in f.readlines()[-15:]:
                print("  " + line.rstrip())


def _mtime(p):
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))


def cmd_doc(argv):
    lang = languages_dir()
    if not lang:
        sys.exit("Install not found. Set TFWR_LANGUAGES.")
    docs = os.path.join(lang, "EN", "docs")
    pages = sorted(glob.glob(os.path.join(docs, "**", "*.md"), recursive=True))
    if not argv:
        for p in pages:
            print(os.path.relpath(p, docs)[:-3])
        return
    name = argv[0].lower().removesuffix(".md")
    hits = [p for p in pages if os.path.relpath(p, docs)[:-3].lower() == name]
    hits = hits or [p for p in pages if os.path.basename(p)[:-3].lower() == name.split("/")[-1]]
    hits = hits or [p for p in pages if name in os.path.relpath(p, docs).lower()]
    if not hits and "/" in name:
        # Doc links like objects/carrot point at tooltip strings, not pages.
        kind, key = name.split("/", 1)
        if kind == "functions":
            return cmd_fn([key])
        strings = os.path.join(lang, "EN", "Strings")
        found = []
        for fn in sorted(glob.glob(os.path.join(strings, "*.txt"))):
            with open(fn, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"^@(\w+?)_%s = (.*)" % re.escape(key), line.strip(), re.I)
                    if m:
                        found.append("%s: %s" % (m.group(1), m.group(2)))
        if found:
            print("=== %s (tooltip strings)" % name)
            print("\n".join(found))
            if kind == "objects":
                print("Costs: the game fills them in live. Read them from the link in-game, "
                      "or with get_cost(Entities.X) once you own Costs.")
            return
    if not hits:
        sys.exit("No doc page matches %r. Run 'doc' with no name to list pages." % name)
    for p in hits[:3]:
        print("=== " + os.path.relpath(p, docs))
        with open(p) as f:
            print(strip_examples(f.read()))


def cmd_fn(argv):
    if not argv:
        sys.exit("usage: fn NAME")
    name = argv[0].strip("()")
    lang = languages_dir()
    found = False
    if lang:
        tips = os.path.join(lang, "EN", "Strings", "code_tooltips.txt")
        if os.path.exists(tips):
            with open(tips) as f:
                text = f.read()
            m = re.search(r"^@code_tooltip_%s = (.*?)(?=^@)" % re.escape(name), text, re.S | re.M)
            if m:
                found = True
                print("--- in-game tooltip (authoritative wording, may lag on details)")
                print(m.group(1).strip())
    stub = freshest_stub(lang, saves_dir())
    if stub:
        with open(stub) as f:
            text = f.read()
        for m in re.finditer(r"^def %s\b.*?(?=^# -{10,}|^def |\Z)" % re.escape(name), text, re.S | re.M):
            found = True
            print("--- stub (%s)" % stub)
            print(m.group(0).strip())
    if not found:
        print("No tooltip or stub entry for %r." % name)


def cmd_errors(argv):
    lang = languages_dir()
    if not lang:
        sys.exit("Install not found.")
    pat = argv[0].lower() if argv else ""
    for fn in ("execute_errors.txt", "parse_errors.txt", "warnings.txt"):
        p = os.path.join(lang, "EN", "Strings", fn)
        if not os.path.exists(p):
            continue
        with open(p) as f:
            for block in re.split(r"\n(?=@)", f.read()):
                if pat in block.lower():
                    print(block.strip())


def cmd_output(argv):
    saves = saves_dir()
    if not saves:
        sys.exit("Save folder not found.")
    out = os.path.join(os.path.dirname(saves), "output.txt")
    if not os.path.exists(out):
        sys.exit("No output.txt yet. The game writes it when a program stops.")
    print("# output.txt modified", _mtime(out))
    with open(out, errors="replace") as f:
        sys.stdout.write(f.read())


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, argv = sys.argv[1], sys.argv[2:]
    {
        "paths": cmd_paths,
        "status": cmd_status,
        "doc": cmd_doc,
        "fn": cmd_fn,
        "errors": cmd_errors,
        "output": cmd_output,
    }.get(cmd, lambda a: sys.exit("unknown command " + cmd))(argv)


if __name__ == "__main__":
    main()
