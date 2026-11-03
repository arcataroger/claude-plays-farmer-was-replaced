#!/usr/bin/env bash
# Builds TfwrBridge.dll and, only if it changed, swaps it into the game and restarts it.
#
# Usage: bridge/install/redeploy_macos.sh [--check] [--force] [GAME_DIR]
#   --check  build and verify only; never touch the running game
#   --force  restart even when the built DLL matches the deployed one
#
# Steps:
#   1. Build. On a failure, print the errors and stop; the game keeps running.
#   2. Compare the new DLL with the deployed one. If they match, stop (nothing to do).
#   3. Save the game, keep the old DLL as TfwrBridge.dll.prev, copy the new one in.
#   4. Quit the game and wait until its process is gone.
#   5. Relaunch through run_bepinex.sh and wait for the bridge to answer.
#      If it never answers, restore the old DLL and print the launch log.
#   6. Press Start and wait until the farm is loaded.
#   7. Run tidy: the restart resets the camera and window layout.
set -euo pipefail
check=0 force=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --check) check=1 ;;
        --force) force=1 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done
here="$(cd "$(dirname "$0")/.." && pwd)"
game_dir="${1:-$HOME/Library/Application Support/Steam/steamapps/common/The Farmer Was Replaced}"
client=(python3 "$here/client/tfwr_bridge.py")
logs="$HOME/.cache/tfwr-bridge"
built="$here/plugin/bin/Release/TfwrBridge.dll"
deployed="$game_dir/BepInEx/plugins/TfwrBridge/TfwrBridge.dll"
t0=$SECONDS
say() { echo "[redeploy +$((SECONDS - t0))s] $*"; }
mkdir -p "$logs"

# 1. Build. The shared compiler server would outlive the build and hold our output
#    open, so we turn it off and write to a log file instead of a pipe.
#    Use the dotnet on PATH, else a user-local SDK in ~/.dotnet.
if ! command -v dotnet > /dev/null && [[ -x "$HOME/.dotnet/dotnet" ]]; then
    export DOTNET_ROOT="$HOME/.dotnet" PATH="$HOME/.dotnet:$PATH"
fi
# A fresh clone has no vendor folder; the game's own BepInEx/core fills it.
vendor="$here/vendor/BepInEx/core"
if [[ ! -f "$vendor/BepInEx.dll" && -d "$game_dir/BepInEx/core" ]]; then
    mkdir -p "$vendor" && cp "$game_dir/BepInEx/core/"*.dll "$vendor/"
fi
if ! dotnet build "$here/plugin/TfwrBridge.csproj" -c Release -nologo -v q \
        -p:UseSharedCompilation=false -p:GameDir="$game_dir" > "$logs/build.log" 2>&1 < /dev/null; then
    grep -E "error|Error" "$logs/build.log" | head -20
    say "build FAILED; the game was not touched"
    exit 1
fi
for src in "$here"/plugin/*.cs; do
    if [[ "$src" -nt "$built" ]]; then
        say "build output is older than $(basename "$src"); the build did not run"
        exit 1
    fi
done
say "built $(shasum "$built" | cut -c1-12)"

# 2. Skip the restart when nothing changed.
if [[ -f "$deployed" ]] && cmp -s "$built" "$deployed" && (( ! force )); then
    say "deployed DLL is already this build; nothing to restart"
    exit 0
fi
if (( check )); then
    say "check only: the build differs from the deployed DLL and is ready to deploy"
    exit 0
fi

# 3. Save and swap, keeping the old DLL for a rollback.
"${client[@]}" save > /dev/null 2>&1 || say "save failed or the game is not running; continuing"
[[ -f "$deployed" ]] && cp "$deployed" "$deployed.prev"
cp "$built" "$deployed"
say "swapped the DLL"

# 4. Quit and wait for the process to go.
#    If it ignores the polite quit for 10 s, force it, so the ping below can
#    only reach the new instance.
game_proc="TheFarmerWasReplaced.app/Contents/MacOS"
pkill -f "$game_proc" || true
for i in $(seq 1 60); do
    pgrep -f "$game_proc" > /dev/null || break
    (( i == 40 )) && { say "the game ignored the quit; forcing it"; pkill -9 -f "$game_proc" || true; }
    sleep 0.25
done
if pgrep -f "$game_proc" > /dev/null; then
    say "the old game is still running; giving up before launching a second copy"
    exit 1
fi

# 4b. Keep the app's code signature intact. Any extra file inside the bundle (early
#     BepInEx runs wrote preloader_*.log into Contents/MacOS) makes macOS call the app
#     "damaged" and refuse to open it (2026-11-03). Move strays to our log folder.
for f in "$game_dir"/*.app/Contents/MacOS/preloader_*.log; do
    [[ -e "$f" ]] && mv "$f" "$logs/" && say "moved a stray $(basename "$f") out of the app bundle"
done

# 5. Relaunch, detached from this script's output so the caller never waits on it.
#    Redirect the whole subshell, not just the command inside it. On macOS the
#    subshell lingers as the game's parent; if it keeps our stdout, a caller
#    reading the output through a pipe (`| tail`) never sees EOF and hangs.
#    `exec` also replaces the subshell with the launcher, so nothing lingers.
(cd "$game_dir" && exec nohup ./run_bepinex.sh) > "$logs/game-launch.log" 2>&1 < /dev/null &
disown
up=0
for _ in $(seq 1 120); do
    if "${client[@]}" ping > /dev/null 2>&1; then up=1; break; fi
    sleep 0.5
done
if (( ! up )); then
    say "the bridge never answered; restoring the old DLL"
    [[ -f "$deployed.prev" ]] && cp "$deployed.prev" "$deployed"
    tail -20 "$logs/game-launch.log"
    exit 1
fi
say "game is up"

# 6. Enter the farm.
"${client[@]}" play > /dev/null
for _ in $(seq 1 40); do
    "${client[@]}" status 2> /dev/null | grep -q '"in_game": true' && break
    sleep 0.25
done
say "in the farm"

# 7. A fresh start resets the camera and window sizes; restore our layout.
"${client[@]}" tidy > /dev/null 2>&1 || say "tidy failed; run 'tfwr_bridge.py tidy' by hand"
say "layout restored"
