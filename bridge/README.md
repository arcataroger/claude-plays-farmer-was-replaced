# TFWR Bridge

TFWR Bridge lets a script or agent drive *The Farmer Was Replaced* without touching your mouse or keyboard.
A BepInEx plugin inside the game listens on `127.0.0.1:7777`.
A small Python client sends it commands: put code in a window, press Execute, read the output, take a screenshot.
The game can sit in the background while you work in other apps.

It runs on macOS and Windows. The plugin is one `net472` DLL; the client needs only the Python standard library.
Every bridge command and every run lands in an audit log, so you can score and review an agent's run afterwards.

## Fair play

The bridge does only what a player's hands and eyes could do.

- It calls the game's UI handlers: the Execute button, the code editor, the window manager, the camera.
- It never reads world-generation settings or RNG state, and it exposes no command that could.
- Your scripts still discover the world through the drone's own sensors.

### Fair-play audit

This table lists every place the plugin reads game state, and why each read is UI or metadata.

| Read | Used by | Why it is fair |
|---|---|---|
| Item counts (`GetNumItem`, `GetInventory`) | `inventory`, `status` goal fields, the status line, the watchdog | The top bar shows every count. The drone reads them too, with `num_items()`. |
| Grid size (`groundData.sizexy`) | `farm-rect`, camera framing | `get_world_size()` returns the same number. |
| Average farm depth (`averageDepth`) | camera framing only | The game's own camera frames the farm at this height. We return only the farm's pixel rectangle, which a screenshot shows. |
| Visible depth (`maxUncoveredDepth`, `ComputeAverageDepth`, `MaxVisibleDepth`) | `hover` | It caps the raycast at the depth the renderer draws, so hidden blocks never answer. |
| The hovered block's tooltip (`GetHoveredTooltip`) | `hover` | It is the tooltip a player gets by resting the mouse there. |
| Tech-tree state (`NumUnlocked`, `GetUnlockCost`, the node button's `interactable`) | `buy` | The tech-tree screen shows levels, costs and which nodes you can press. |
| Leaderboard screen texts (`timeText`, `finishTimeText`, `runCancelled`, title) | `lb`, watchdog check-ins | They are the labels on the leaderboard clock and result screen. |
| Output panel, window errors, the warning icon | `output`, `error`, `check`, `status` | The player reads them on screen. |
| Clock speed (`TimeFactor`), `IsExecuting` | speed buttons, fast-forward, run tracking | The player sets the speed and sees whether code runs. |
| Save slot names, the active slot | `saves`, `ping`, audit lines | The save chooser lists them. |
| Window and camera positions | `windows`, `arrange`, `tidy`, `center-farm`, `zoom` | They are UI layout, not world state. |

Two reads go further than a human's eyes. Treat them with care:

1. **`hover` returns the block's x, y and depth** next to the tooltip text, and it answers in milliseconds.
   A player sees the block's position on screen, but not as numbers.
   A loop that sweeps `hover` over every pixel maps every visible block far faster than a person could.
   The skill's rules tell the agent to ask the user to hover instead; a benchmark must treat `hover` calls in the audit log as a rules question.
2. **`farm-rect` moves with the farm's average depth.** A client could infer the average dig depth from the rectangle.
   A screenshot shows the same thing, so we judge it fair.

During `simulate()`, item counts come from the sandbox, not your farm (see "Watchdog").

## Prerequisites

1. *The Farmer Was Replaced* from Steam, installed in a Steam library. Add the mining DLC, *The Miner Was Replaced*, if you play it.
2. Steam itself, so the game can start.
3. The .NET SDK 8 or later (`dotnet --version`), from https://dotnet.microsoft.com/download. The build targets `net472` through NuGet reference assemblies, so it needs no Mono and no Visual Studio. The first build downloads them, so it needs internet access.
4. Python 3.9 or later, for the client.
5. `git`, to clone the skill.

## Install

### macOS

1. Clone the skill and enter it:
   ```bash
   git clone https://github.com/arcataroger/claude-plays-farmer-was-replaced farmer-was-replaced
   cd farmer-was-replaced
   ```
2. Run the installer. Pass the game folder if it isn't in the default Steam library:
   ```bash
   bridge/install/install_macos.sh ["/path/to/The Farmer Was Replaced"]
   ```
   It does five things:
   1. It downloads BepInEx 5.4.23.6 and checks its SHA-256.
   2. It unpacks BepInEx into the game, then copies `BepInEx/core` into `bridge/vendor/BepInEx/core` for the build.
   3. It builds the plugin against the game's own DLLs.
   4. It adds `steam_appid.txt` and sets the BepInEx entrypoint.
   5. It re-signs the app ad hoc, because the hardened runtime blocks Doorstop's library injection. It backs up the original signature to `~/.cache/tfwr-bridge/original-signature/` first.
3. In Steam, open the game's Properties → General → Launch Options. Paste the line the installer printed:
   `"<game folder>/run_bepinex.sh" %command%`
4. Start the game from Steam.
5. Check the bridge:
   ```bash
   python3 bridge/client/tfwr_bridge.py ping
   ```

### Windows

1. Clone the skill and enter it:
   ```powershell
   git clone https://github.com/arcataroger/claude-plays-farmer-was-replaced farmer-was-replaced
   cd farmer-was-replaced
   ```
2. Run the installer. It finds the game through Steam's library list; pass `-GameDir` when it can't:
   ```powershell
   powershell -ExecutionPolicy Bypass -File bridge\install\install_windows.ps1 [-GameDir "D:\SteamLibrary\steamapps\common\The Farmer Was Replaced"]
   ```
   It downloads and checks BepInEx 5.4.23.6, unpacks it into the game, fills `bridge\vendor\BepInEx\core`, builds the plugin and copies it in.
3. Start the game from Steam as usual. BepInEx loads through `winhttp.dll`, so Steam needs no launch option.
4. Check the bridge:
   ```powershell
   python bridge\client\tfwr_bridge.py ping
   ```

After a restart on Windows:
1. A launch through Steam can stall on a one-time Big Picture notice (for example, the On-Screen Keyboard notice). Steam's `logs/console_log.txt` then shows `waiting for user response to ShowInterstitials`. Dismiss it with Enter in the Big Picture window. An unattended restart hangs here.
2. If `ping` works but `status` says `in_game` is false, run `tfwr_bridge.py play`.

### Build by hand

The installers build for you. To build alone, run this from the skill folder:

```bash
dotnet build bridge/plugin/TfwrBridge.csproj -c Release -p:GameDir="/path/to/The Farmer Was Replaced"
```

The build references `bridge/vendor/BepInEx/core`. When that folder is missing, it uses the game's own `BepInEx/core` instead.
Without either, the build stops with a message that names the missing folder.

### Redeploy after a change (macOS)

1. Run `bridge/install/redeploy_macos.sh --check`. It builds and verifies without touching the game.
2. Run `bridge/install/redeploy_macos.sh`. It builds, and restarts the game only when the DLL changed.

It keeps the old DLL as `TfwrBridge.dll.prev` and restores it if the bridge never answers.
A redeploy takes 2–5 s, and you can pipe its output safely: the relaunched game holds none of the script's file descriptors.
If the old game ignores the quit for 10 s, the script force-kills it. That way the bridge ping reaches only the new instance.
Last, it runs `tidy` to restore the camera and window layout, which every restart resets.

### Uninstall

1. Remove the Steam launch option (macOS only).
2. In Steam, choose Properties → Installed Files → Verify integrity of game files. Steam restores the original app and signature.
3. Delete the `BepInEx` folder, `run_bepinex.sh`, `libdoorstop.dylib` (macOS) or `winhttp.dll` and `doorstop_config.ini` (Windows).

## Use

Run `python3 bridge/client/tfwr_bridge.py <command>`.
Exit codes: 0 ok, 2 bridge unreachable, 4 the watchdog stopped the run, 5 timeout, 6 warning, 7 script or bridge error, 8 `farm` stalled, 9 `farm`'s script ended.

| Command | What it does |
|---|---|
| `ping` | Plugin and Unity versions, platform, the game's data folder |
| `play` | Presses Start on the title screen |
| `save` | Presses the menu's Save button |
| `status` | In game? Executing? Which window? Did the watchdog stop the last run? |
| `windows` | Every code window: name, executing, minimized, position |
| `inventory` | Every item count, as the top bar shows them |
| `run FILE [--window W] [--timeout S] [--ff N] [--ff-after S] [--wait-for TEXT] [--goal ITEM=N]` | Puts the file's code in window W, runs it, waits, prints the new output. After S real seconds (default 10) it ramps the clock up to N times (default 128; 1 = off). The status line shows TEXT, and with a goal, the item's progress and time left. A watchdog stop ends with a `Watchdog: REASON` line and exit code 4 |
| `farm FILE --goal ITEM=N` | Runs an endless farming script in 15 s check-ins until the goal; stops on an error, a warning, a stall or a watchdog stop |
| `set FILE [--window W]` | Puts code in a window without running it |
| `stop` | Stops the running program |
| `buy UNLOCK` | Clicks that tech-tree node, as the player would (spends items) |
| `saves` | Lists save slots and the active one |
| `new-run [NAME]` | Saves, creates a new slot, loads it and takes it over |
| `load-save NAME` | Saves, then loads another slot |
| `output` | The whole output log |
| `error W` | The window's current error message |
| `check` | Parses every code window and lists the ones with errors |
| `takeover` | Backs up saves, consolidates everything into one agent window, frames the farm |
| `tidy` | Frames the farm, stacks windows in a left column, checks every window |
| `arrange [COLUMN]` | Stacks windows in a left column (a screen fraction, default 0.45) |
| `farm-rect` | The farm's on-screen rectangle in pixels |
| `center-farm [FX FY]` | Pans until the farm's centre sits at that screen fraction |
| `zoom Z` | Sets the camera zoom, pivoting on the screen centre |
| `follow [--off]` | Re-centres the farm whenever it drifts far off-centre |
| `fast-forward N` | Runs the game clock N times faster (1-128; 1 = real time) |
| `lb` | Leaderboard state: running, result screen open, title, finish time, result text, live clock |
| `lb-ok` | Presses OK on the leaderboard result screen (the button's own handler), so the workspace returns |
| `speed play\|ff\|auto\|status` | The player's speed buttons (bottom right of the game): ▶ 1x, fast-forward cycling 2x to 128x, and Auto. While manual, the plugin ignores `fast_forward` and `farm` pauses its stall clock. |
| `watchdog [--limit S] [--stall S] [--items a,b]` | Reads or sets the plugin watchdog for the next run (see "Watchdog") |
| `intervene "NOTE"` | Writes a human-intervention marker to the audit log and shows it as a caption |
| `say TEXT [--seconds S]` | Shows a caption at the bottom of the game, over the farm (0 = until replaced) |
| `wait [TEXT] [--timeout S] [--goal ITEM=N]` | Shows a live "Waiting for TEXT" status line: progress toward the goal, or elapsed time. No text clears it |
| `hover X Y` | The tooltip a player sees with the mouse at screen pixel X, Y (top-left origin). Read the fair-play audit first |
| `screenshot PATH` | Saves a PNG of the game's own frame, even when the window is hidden |
| `move W X Y`, `minimize W [--off]` | Moves or minimizes a window |
| `tidy_guard 1\|0 [KEEP]` (raw call) | Always on. Once a second, if a window covers the farm or the farm leaves the screen, the plugin restacks windows in the left column (keeping KEEP open, default `main`; "" keeps none), shrinks the zoom if needed, and centers the farm beside them. It waits while a mouse button is down. |

Two rules hold for every command:

1. `run` works only in `main`.
2. `set` opens other windows only with a `claude_` name, minimized, as staging files for `simulate`, `leaderboard_run` or `import`. The plugin closes them 15 s after the last run ends.

`scripts/lb.py BOARD PROGRAM_FILE [SPEEDUP] [MAX_SECONDS]` runs one leaderboard end to end on any OS. `scripts/lb.sh` calls it.

From Python:

```python
from tfwr_bridge import Bridge
b = Bridge()
out, secs, state = b.run_and_wait("main", 'quick_print(get_world_size())')
```

`state` is `finished`, `error`, `warning`, `goal`, `timeout` or `watchdog`. Treat `watchdog` as a failure, like `error`.

### Windows on screen
Only code windows stay on screen. Doc pages that `unlock()` opens close during bridge runs and on `arrange`. `status` reports them as `doc_windows`. The tidy guard also keeps zoom in step with the screen: it fits the farm and the code column together, so long code lines lower the zoom. Keep lines short; the linter warns over 100 characters.

### Watchdog

The plugin, not the client, ends a bridge run that runs too long or stops making progress.
A client that dies or forgets its checks can't leave a run going for hours.

1. It stops a run after `limit` real seconds (default 1200).
2. It stops a run after `stall` real seconds without progress (default 300).
3. It counts progress by this rule:
   1. With goal items set, only a rise in a goal item counts. Set them with `watchdog --items a,b`; `run --goal` and `farm` add their goal item.
   2. With no goal item, a rise in any item counts, except water and fertilizer, which refill on their own.
      A by-product can then hide a stall, so set goal items for every long run.
   3. A fall never counts: the drone burns power even while it idles.
4. A run takes the settings when it starts. The next run gets the defaults again, so a forgotten call can't carry over.
   A change during a run applies to that run.
5. Every minute the run appends a check-in line to `claude_watchdog.log` in the game's data folder.
   `status` reports `watchdog_stopped`, and the client returns the state `watchdog` (exit code 4).

For `simulate()` benches and leaderboard runs, follow these steps:

1. Expect item counts to come from the sandbox: they restart at the sandbox's starting items and rise as the simulated drone works.
   We measured this on 2026-11-03: hay climbed from 0 during a `simulate()` call.
   We expect `leaderboard_run()` to behave the same, since the leaderboard clock runs during `simulate()` too [unverified].
2. If the simulated script produces a goal item steadily, set `watchdog --items ITEM` and keep a stall limit.
3. If it pays out only at the end (a timing bench that only prints), run `watchdog --stall 0` and rely on `--limit`.

### Audit log

The plugin appends JSON lines to `claude_audit.jsonl` in the game's data folder (`ping` prints the folder).
Every line has `t` (UTC, ISO 8601) and `event`.

| `event` | When | Fields |
|---|---|---|
| `plugin_start` | The game loads the plugin | `plugin`, `unity`, `platform` |
| `command` | A bridge command finishes | `cmd`, `args`, `ok`, `error` |
| `run_start` | A run starts in `main` | `run_id`, `window`, `sha256`, `len`, `limit_s`, `stall_s`, `counts` (the progress rule), `save` |
| `run_end` | That run ends | `run_id`, `window`, `sha256`, `len`, `real_s`, `farm_s` (game seconds), `speed_s` (real seconds at each clock factor), `watchdog` (the stop reason, or null), `error`, `save` |
| `unlock` | `buy` unlocks a node | `unlock`, `level`, `cost` |
| `intervention` | `intervene "NOTE"` | `note`, `run_id`, `running` |
| `speed_button` | A person clicks a speed button | `mode`, `time_factor` |

The log follows these rules:

1. Code never appears in the log. `set` and `run` code shows as `{"sha256", "len"}`.
2. The plugin saves each distinct code body once, as `claude_runs/<sha256>.py` in the data folder. A run of code already in `main` gets archived too.
3. We leave out read-only polls: `status`, `error`, `output`, `windows`, `lb`, `farm_rect`, `ping`, `speed status` and `watchdog` without arguments. `run_and_wait` polls `status` ten times a second, so logging them would add about 40,000 lines an hour.
4. `run_id` restarts at 1 when the game restarts; a `plugin_start` line marks each restart.

The log can't see what a person does in the game's own UI: typing in the editor, pressing Execute, buying a node.
Log those with `intervene "what you did"`, so a reviewer can tell them apart from the agent's work.

### What `takeover` changes

1. It copies the game's whole `Saves` folder to `~/.tfwr-bridge/backups/<timestamp>/`.
2. It closes doc pages and the agent's old scratch windows (names starting `claude_` or `bridge_`).
   Closing a code window deletes its file, so it never closes your windows.
3. It minimizes your code windows. Their files stay as they are.
4. It opens the agent's window (`main`), fits the farm into the right of the screen, and stacks the windows on the left.

## How it works

- `plugin/BridgePlugin.cs` runs a loopback `TcpListener` on a background thread.
  Each request is one tab-separated line; code travels as base64. Each reply is one JSON line.
- Requests queue up and run on Unity's main thread in `Update()`, because Unity objects aren't thread-safe.
- `run` calls the window's own `PressExecuteOrStop()`, the Execute button's handler.
- `run` returns a run id, and the plugin counts finished runs each frame. A fast script can start and end between two polls, so the client waits for the finished count to reach its id. The player's code needs no marker.
- `buy` calls the tech-tree node's own `UnlockBox.ButtonClicked()`. It refuses a node whose button the player couldn't press.
- `new-run` mirrors the save chooser: `Saver.CreateNewSaveGame`, then set the active slot and reload scene 0.
- The 3D farm camera follows the workspace scroll position. `center-farm` projects the farm's corners like the game's `FarmRenderer` does. It then pans with a learned per-axis gain until the error drops under 6 px.
- `screenshot` captures at end of frame with `ScreenCapture.CaptureScreenshotAsTexture`. It works while the game sits behind other windows.

We inspected the game's UI classes to find these handlers. You need none of that to build or use the bridge.

## Credits

- [BepInEx](https://github.com/BepInEx/BepInEx) (LGPL-2.1): the plugin loader.
- [UnityDoorstop](https://github.com/NeighTools/UnityDoorstop) (LGPL-2.1): loads BepInEx into the game process.
- [HarmonyX](https://github.com/BepInEx/HarmonyX) (MIT): ships with BepInEx; the bridge doesn't patch any methods.
- [ILSpy](https://github.com/icsharpcode/ILSpy) (MIT): we used it to find the UI handlers the bridge calls.
