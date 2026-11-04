# Claude plays The Farmer Was Replaced

A Claude Code skill for the programming game *The Farmer Was Replaced* and its mining DLC. It teaches an agent **how** to play well, not **what** to play. It learns each system from the in-game docs and logged experiments. It measures and improves its own drone scripts, and it runs a whole campaign on its own. That campaign maxes the tech tree, unlocks the revealed achievements, then climbs the leaderboards.

The skill holds no puzzle solutions on purpose. Every algorithm the agent uses, it has to discover by reading, experimenting, researching and measuring. That keeps the game fun, and it lets you compare agents. Run different models or harnesses on the same skill from a fresh save, and see how each does.

Unofficial fan project. It is not affiliated with or endorsed by the game's developer or publisher. You need your own copy of the game (and the DLC for the mining content).

## What's here
- `SKILL.md`: the skill. The agent reads it first.
- `references/`: the language (`dialect.md`), a builtin index (`api.md`) and known enum names. No strategy.
- `scripts/`: doc and tooltip reader, tech-tree planner, achievements reader, linter, precheck, economy driver, campaign wake-up, journal.
- `bridge/`: a BepInEx plugin plus a Python client. They let the agent run code, buy unlocks, read the inventory and frame the camera without touching your mouse or keyboard. See `bridge/README.md`.

## Install
Prerequisites:
- the game on Steam. Tested on Windows. macOS: worked before the November 2026 game update, and we haven't confirmed it since; rerun the installer after each update. Linux: untested;
- Python 3.9 or newer (`python3` on macOS and Linux, `python` on Windows);
- the .NET SDK, to build the bridge;
- Claude Code, or another agent harness that loads skills.

1. Clone this repo into your skills folder:
   `git clone https://github.com/arcataroger/claude-plays-farmer-was-replaced ~/.claude/skills/farmer-was-replaced`
2. Install the bridge: follow `bridge/README.md`.
3. Generate the tech-tree file from your own install: see `SKILL.md`, section 3. The repo ships no game data.
4. Start the game, then ask the agent to play. For a full unattended campaign, ask it to start the campaign loop (`SKILL.md`, section 4).

## Using it as a benchmark
1. Start every agent from a fresh save (`tfwr_bridge.py new-run`) with the same prompt and budgets.
2. Score what the game measures: tech-tree nodes maxed, and each leaderboard program's seeded `simulate()` time. Achievements are played but not scored, because Steam keeps them across saves.
3. Count human help: the bridge logs every `intervene` marker, and its audit log records every command and every program run.
4. Keep each agent's journal (`scripts/journal.py`) as its record.

## Leaderboards
Leaderboard runs post to the real Steam boards under the player's account. Ask first if that matters to you.

## Your journal
`scripts/journal.py` records each playthrough as a git repo, one commit per milestone. Keep journals outside this repo, for example under `~/tfwr-journal/`. They hold your own solutions; publishing them would spoil the game for other agents.

## License
CC0 1.0 Universal: the authors dedicate this work to the public domain (see `LICENSE`). Dependencies keep their own licenses; the bridge installer downloads BepInEx and does not bundle it.

## Credits
- BepInEx, HarmonyX and Doorstop make the plugin possible.
- UnityPy reads the tech-tree records from the game's asset files.
- ILSpy helped us find the game's UI handlers for the bridge.
