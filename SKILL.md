---
name: farmer-was-replaced
description: Play the programming game The Farmer Was Replaced (and its mining DLC, The Miner Was Replaced) the way a strong programmer-player does. It teaches how to learn the game's systems from in-game docs and logged experiments. It shows how to measure and improve your own drone scripts. It drives a whole campaign (tech tree, achievements, leaderboards) on its own, with no guides or posted solutions. Use it when the user mentions this game, TFWR, "the farmer/miner was replaced", their drone, farm, tech tree or a leaderboard. Use it when they paste game code or an in-game error.
---

# The Farmer Was Replaced: a method for playing well

You are an expert programmer playing this game. You write drone scripts, run them, read the results and improve them. This skill teaches a method, not answers. It holds no puzzle solutions on purpose: discovering them is the game, and the skill doubles as a benchmark of how well an agent learns. Never look for solutions outside the game; find them by reading, experimenting, researching and measuring.

## 1. Fair play
The line runs between **game metadata** and **world state**. Metadata is what a player can learn from the game's own UI and docs. World state is this run's hidden layout; only the drone's sensors can reveal it.

**Allowed (metadata):**
1. Doc pages (spoiler hints included), tooltips, error and warning strings: `scripts/tfwr.py` reads them from the install.
2. The visible tech tree: nodes whose prerequisites you own, with their costs and effects. Never decode hidden (`????`) nodes; the tools hide them.
3. Visible Steam achievements. Hidden ones stay hidden until unlocked; the tools hide their names too.
4. The builtins stubs, your own code files and `output.txt`.
5. The save's `unlocks` and `items` lists (they match the screen), or the bridge's `inventory` command.
6. General computer science and math: algorithms, papers, textbooks, open-source code that does not mention this game.

**Not allowed (world state and solutions):**
1. World-generation logic or parameters: seeds, RNG, spawn rules, layer depths, maze or puzzle generation. Read no game code beyond what a UI handler needs.
2. The save JSON's `grounds` and `entities` keys: they hold blocks the drone never sensed.
3. Guides, wikis (the official one included), forums, videos or posted solutions for this game. Skip any search result that names it. Wikis lag behind game updates and miss the DLC; the game's own docs, tooltips and your experiments give the same facts, always current.
4. Learning a layout from a fixed `simulate()` seed and hardcoding it. Fixed seeds serve only for fair A/B timing.
5. Hardcoded coordinates you saw in a screenshot. The linter warns on literal coordinates.

**Two kinds of eyes.** You are the player; the drone is your robot.
1. You can look at the screen (`tfwr_bridge.py screenshot shot.png`, then Read it) and hover tiles (`hover X Y`) to watch runs and learn rules.
2. The drone senses only through its own calls (`get_ground_type`, `measure`, `get_pos_z(dir)`, ...). Never feed what you saw or hovered into a drone script.

## 2. Tools
Run these from the skill directory. Each prints help with `-h`. On Windows, type `python` where this skill writes `python3`.
1. `python3 scripts/tfwr.py status|doc [page]|fn NAME|errors [text]|output`: save summary, doc pages, builtin tooltips, error strings, the output log.
2. `python3 scripts/unlocks.py next|info|levels|plan|probe|parse`: buyable nodes with costs and shortfall, node details, budget plans. `next` judges each node alone, so one purchase can make the next line unaffordable. They read `references/tech_tree.json`, which you generate locally (section 3) and which shows only visible nodes.
3. `python3 scripts/unlocks.py can`: what this save can use right now (language features and builtins), and what each visible node would add. Run it before you write your first script, and again after every `new-run` or `load-save`: a fresh save lacks even variables.
4. `python3 scripts/achievements.py [--json]`: your Steam achievements, unlocked and locked, with hidden ones hidden.
5. `python3 scripts/lint_tfwr.py FILE.py --save-dir SLOT_DIR`: Python habits the game rejects, names and features this save hasn't unlocked, loops without log lines, literal coordinates. Fix every ERROR; justify or fix every WARNING.
6. `python3 scripts/precheck.py FILE.py --seeds 3,8`: lint, then run the script in `simulate()` on fixed seeds, and fail on errors, warnings or zero-work phases. Pass it before any script joins a loop.
7. `python3 scripts/economy_driver.py --registry economy.json`: runs your farms in time slices, always the one whose short item finishes last, and buys greedily with reserves. You write the registry; the driver holds no game knowledge.
8. `python3 scripts/campaign.py`: the outside-the-turn wake-up for the campaign loop (section 4). It prints the one next step.
9. `scripts/tfwr_log.md`: the log-line convention every drone script follows.
10. `python3 bridge/client/tfwr_bridge.py ...`: drives the game (section 8).
11. `python3 scripts/journal.py init|step|fail|log`: records the run as a git repo, one commit per milestone (section 9).

References:
1. `references/dialect.md`: the language and its timing model. Read it before your first script.
2. `references/api.md`: the builtin index and name authority.
3. `references/enums.json`: known names; the linter uses it.

## 3. Setup
1. Install the bridge: follow `bridge/README.md` (macOS and Windows).
2. Generate the tech-tree file once per game version:
   1. Create a venv with UnityPy: `python3 -m venv ~/.cache/tfwr/venv`, then install UnityPy with that venv's pip.
   2. Run the extractor with that venv's Python: `-I scripts/extract_tech_tree.py -o references/tech_tree.json --check`. It reads only the game's unlock records.
   3. The venv's programs live in `bin/` on macOS and Linux, and in `Scripts\` on Windows (`Scripts\python.exe`, `Scripts\pip.exe`).
   4. If a tool can't find the game or your saves, set `TFWR_LANGUAGES` or `TFWR_SAVES`. With the bridge running, the tools ask the game for its save folder first.
3. Run `python3 bridge/client/tfwr_bridge.py ping`. Exit code 2 means no bridge: check that the game runs with BepInEx (bridge README).
4. For a fresh campaign, run `tfwr_bridge.py new-run`, then `scripts/journal.py init`.
5. At the start of every session, and after any `new-run` or `load-save`, run `scripts/unlocks.py can`. Write only what it lists.

**After a game update, repair and repatch.** An update overwrites the patched game:
1. In Steam, run Verify integrity of game files.
2. Rerun the bridge installer.
3. On macOS, start the game with `run_bepinex.sh`, never Steam's Play button (bridge README).
4. Regenerate `references/tech_tree.json`.

## 4. The campaign loop
Play as one self-pacing loop with three phases. Apply sections 5–7 inside every phase.
1. **Phase 1: max the tech tree.** Buy every visible node to its max level. Drive the early game by hand: the economy driver and the log convention need Debug, Costs, Auto_Unlock and the core language unlocks first. The driver says which are missing. Look ahead only at visible nodes. Done when no unlock has a next-level cost (check in-game with `get_cost` over `Unlocks`).
2. **Phase 2: unlock every revealed achievement.** Run `scripts/achievements.py`. Take the cheap ones first, then the puzzles, then the volume goals (feed them to the economy driver as `targets`). Re-run the tool after each one to confirm Steam recorded it.
3. **Phase 3: climb the leaderboards.** Score your board programs by their seeded `simulate()` time, not by rank. Improve the board with the largest gap between your time and your best idea's estimate.
4. **Cycle.** A game update, new DLC, or a newly visible node or achievement sends the loop back to the earliest phase with work left.

**Keep the loop alive from outside your turn.**
1. Start a self-pacing `/loop` (or a scheduled task) whose prompt is: run `python3 scripts/campaign.py`, do the step it prints, and keep the game busy.
2. Re-arm it after every context compaction.
3. Before you end any turn, start the next run or a background waiter on it. The watchdog catches runaway runs; only the loop catches an idle agent.

**Standing permissions inside a campaign.** The user grants these by starting the campaign; do not ask again:
1. Buy any visible node; report each purchase in your next message.
2. Hover tiles and tech-tree nodes yourself; never ask the user to hover.
3. Read status yourself; never ask the user to confirm what you see.
4. Pause, edit and restart your own runs.
Still ask before `new-run`, `load-save`, deleting files, or touching the user's own code windows.

## 5. Learn each system before you optimize it
1. **Read first.** Read the system's doc page (`tfwr.py doc`) and the tooltip of every builtin it uses (`tfwr.py fn`). The docs win over anything you assume.
2. **Probe what the docs leave open.** Write a 10-line experiment that logs the sensors around one action. Print before and after. Take a screenshot when the log can't explain a change.
3. **Re-probe after you buy the related unlock.** The world can change with what you own, so an absence you observed before an unlock proves nothing afterwards.
4. **Fit hidden rules with a sweep, not one point.** Vary one parameter across 3–4 values and log every step. Fit the rule to all of them. Then test the fit once more. A model built from one observation fails on the next try.
5. **Name the general problem.** Before a big script, describe the task in general terms. Name the grid, what the drone senses, what each action costs and what pays. Then research that problem class (section 6.6).
6. **Ask what the rule's inputs are, and which you control.** Many limits depend on something you can change. Examples: the farm size, the drone count, the order of steps, a cheaper input.

## 6. Optimize first, and measure instead of guessing
The game is about inventing efficient solutions. Waiting and brute force come last.
1. **Treat a slow run as a signal.** Stop and look for a better algorithm in two cases. A farm runs under half its best measured rate, or one phase takes over half of a run's ticks.
2. **Ask what the game allows that your script doesn't use yet.** Candidates: more drones, sensors that read a neighbour (`dir` arguments), the world as shared state, `wait_for` return values, a different split of the work, a cheaper input.
3. **Never estimate time or savings from intuition.** LLMs judge time badly. Time phases with `get_time()` and `get_tick_count()`, or A/B the variants.
4. **A/B on fixed seeds.** Stage each variant (`set v.py --window claude_v_a`), run a bench in `main` that calls `simulate(name, unlocks, items, {}, seed, speedup)` on 2–3 seeds per variant, and compare game seconds. Keep a change only when every seed improves or the mean improves clearly.
5. **Profile before rewriting.** The part that looks slow on screen is often not the cost. Log each phase's time first.
6. **Research every new system before you write its farm.** Treat this as a trigger, not advice. `unlocks.py next` lists newly visible nodes. When a node or doc page reveals a new system, name its general problem (section 5.5). Then start background research and keep playing. If you judge that a system needs no research, record why in the journal:
   1. Brief at most two research agents at once.
   2. Describe the problem in general terms; never name this game in queries or briefs.
   3. Tell them to skip any page that mentions this game and to report general algorithms with sources.
   4. Have them write their report to a file in your journal at once; compaction erases transcripts.
7. **Stop optimizing a step** only when you have measured it near its limit, or when the remaining wait costs less than testing the next idea. Say which applies.
8. **Fast-forward only waits.** Speed-up never replaces a better algorithm. `get_time()` counts game time, so fast-forward changes your wait, not your numbers.

## 7. Log, simulate and audit every loop
No loop is trusted until its logs prove it works, and a running loop keeps proving it. These steps are mandatory from your first script, not only for long loops; quick early-game scripts need them most, because they run blind.
1. `run` and `farm` lint every script against the save and refuse on errors. Use `--no-lint` only for throwaway probes. From the moment you own Debug, every script logs.
2. Follow `scripts/tfwr_log.md`: each phase prints `PHASE <name> t <time> work <successful actions> left <remaining>`, and each run ends with one `SUMMARY` line. The game keeps only recent output lines, so summaries must come last.
3. Once you own Simulation, run `scripts/precheck.py` on every new or changed script. A PASS proves no errors, not payoff: read the gains too. A phase with `work 0` is a bug until you explain it.
4. Watch one real run on screen and compare it with the log.
5. In production, keep the log lines on. The economy driver archives every slice's output and stops on any error or warning.
6. Every few check-ins, read a sample of raw output, not just summaries. Look for zero-work phases, slow phases, unexpected ground or entity types, and drifting counts.
7. Treat a loop's summary as a claim, not proof.

**Script habits that prevent silent failure:**
1. Check every action's return value; a silent failure becomes an endless loop.
2. Guard every loop with a sensed exit or a budget, and budget waits in game time (`get_time()`), not loop counts.
3. Check inputs inside the loop that spends them, not only at its start.
4. Start every script from a known state; don't assume where the last script left the drone or the ground.
5. Make every helper drone return a result, check it with `wait_for`, and act on a shared structure only when every part reports ready.
6. Recheck the world before each step of a precomputed plan; the world can change mid-plan.
7. Log every item a script produces, not only its goal; read the inventory before you claim an absence.
8. When the log can't explain a failure, print what the drone senses as a small ASCII map. Print it before and after the failing step.
9. Gate features on unlocks at runtime (`num_unlocked(Unlocks.X) > 0`), and never hardcode sizes, costs or depths.

**Parallelism.** Drones share no memory (doc `megafarm`). They exchange data only through spawn arguments, `wait_for` return values, the world itself and global counts such as `num_items()`. Give every tile or task exactly one owner per round; two drones acting on one tile race.

## 8. The bridge
`bridge/` holds a BepInEx plugin plus a stdlib Python client. It drives the game over `127.0.0.1:7777` without moving the mouse. Read `bridge/README.md` for every command. The rules that matter most:
1. **Run everything through `main`.** The plugin refuses `run` in other windows. Stage files that `main` needs (imports, `simulate()` and `leaderboard_run()` targets) with `set FILE --window claude_NAME`; the plugin minimizes them and closes them after the run.
2. **One run:** `tfwr_bridge.py run FILE.py --timeout SECS --wait-for "why"`. Exit 0 finished, 4 watchdog stop, 5 timed out, 6 warning, 7 error. When you chain commands, stop on failure (`buy X && journal.py step ...`): a step that runs after a failed one records something that never happened.
3. **Long farming:** `tfwr_bridge.py farm FILE --goal item=N`. It starts the script once and checks in every chunk; it stops on the goal, an error, a warning or a stall. It checks the goal once per poll, so at high speed it can overshoot a small goal; that's fine.
4. **Watchdog:** the plugin stops any bridge run after its time limit, or when its goal items stop rising. Set goals with `--goal` or `watchdog --items a,b`; give `simulate()` benches `--stall 0`. Never disable it for an unattended run.
5. **Status line and captions:** a viewer sees only the game, so never leave the screen unexplained. Give every run a reason (`--wait-for`). End every batch of commands with a caption that stays up while you think: `tfwr_bridge.py say "Claude: <what you found, what you try next>" --seconds 0`. Post one at every decision: a result, a rejected idea, a new plan.
6. **Leaderboards:** `python3 scripts/lb.py BOARD PROGRAM.py [SPEEDUP] [MAX_SECONDS]` (`lb.sh` wraps it). Read each board's start items and goal in `tfwr.py doc unlocks/leaderboard`. Prints inside a board never reach the log, so debug the program in `simulate()` first.
7. **Speed:** leave it to the bridge. The client ramps fast-forward during runs, and the plugin returns to 1x when nothing runs. Never pass `--ff` or set a speed unless the user asks; `--ff 1` turns the ramp off and runs crawl. The player's own speed buttons override it.
8. **Camera:** the plugin keeps code windows off the farm by itself (`tidy_guard`). Code text follows the screen by default (`text-scale auto`: screen height / 540). Set `tfwr_bridge.py text-scale N` (1 to 8) to override it; the setting persists. Never hand-edit the game's `options.txt`: a bad value broke a save's load in testing.

## 9. Records for every run
A run must leave enough to replay, audit and score it.
1. Record milestones with `scripts/journal.py step --goal "..." --check '{...}' --script FILE --notes "..."`. Notes hold what a replayer needs: why the script looks as it does, and what you measured. `--buy` refuses a purchase whose level didn't rise; in `--check`, a plain name means "owned at any level", and `trees_2` means level 2.
2. Keep every script, bench result and research report in the journal, not in this skill. This skill stays free of solutions.
3. The bridge writes an audit log and archives every program it runs (bridge README, "Audit"). Mark any human help with `tfwr_bridge.py intervene "what the user did"`.
4. End each phase with a numbers report: nodes maxed, achievements unlocked, board times before and after, wall-clock and game time. List the research you started per system, or why a system needed none.

## 10. When something fails
1. Read the output for the game's error, then run `tfwr.py errors <word>` for the full text. If the quoted line looks wrong, take a screenshot: the code window marks the real line.
2. If a name fails, check `references/enums.json` (`_stale`) and the doc pages.
3. If behaviour surprises you, write a small logged experiment (section 5). Never guess at hidden rules.
4. If a run stalls or idles, read the watchdog and audit logs before you rerun anything.
