# Builtin API index

This page lists the builtins by family, with no behaviour notes. The game is the authority on what each one does:
1. Read the tooltip and stub: `python3 scripts/tfwr.py fn NAME`.
2. Read the doc page of the system it belongs to: `python3 scripts/tfwr.py doc` lists them.
3. Verify anything the text leaves open with a small logged experiment before you rely on it (SKILL.md, "Log, simulate and debug").

`dir` arguments take `North`, `East`, `South` or `West`.

**Name authority, best first:**
1. Doc pages and error strings (current for the DLC).
2. A game-written `Saves/SaveN/__builtins__.py` newer than the install.
3. `code_tooltips.txt`.
4. The install's community `builtins.py`. It lags behind DLC renames; `references/enums.json` maps the stale names it uses.

**Safety:** never call `reset()`. It wipes the farm back to the start and locks most unlocks.

## Actions
`move(dir)` · `harvest()` · `plant(Entities.X)` · `till()` · `swap(dir)` · `use_item(Items.X, n)` · `place(Grounds.X)` · `dig()` · `push(dir)` · `can_push(dir)` · `hover(h)` · `get_hover()` · `jump(Unlocks.X)` · `clear()` · `change_hat(Hats.X)` · `do_a_flip()` · `pet_the_piggy()`

## Senses
`get_pos_x()` · `get_pos_y()` · `get_pos_z(dir)` · `get_world_size()` · `get_entity_type(dir)` · `get_ground_type(dir)` · `get_hardness(dir)` · `get_stability(dir)` · `can_harvest()` · `can_move(dir)` · `get_water()` · `num_items(Items.X)` · `get_companion()` · `measure(dir)` · `prospect_iron()` · `prospect_quartz()`

## Drones
`spawn_drone(fn, *args)` · `wait_for(h)` · `has_finished(h)` · `max_drones()` · `num_drones()`

## Progress and meta
`get_cost(thing, level)` · `unlock(Unlocks.X)` · `num_unlocked(X)` · `simulate(file, unlocks, items, globals, seed, speedup)` · `leaderboard_run(Leaderboards.X, file, speedup)`

## Debug and timing
`print(...)` · `quick_print(...)` · `get_time()` · `get_tick_count()` · `set_execution_speed(s)` · `set_world_size(n)`

## Language builtins
`range` · `len` · `str` · `list` · `dict` · `set` · `min` · `max` · `abs` · `random` · `append` · `insert` · `pop` · `remove` · `add`

## Enum families
`Items`, `Entities`, `Grounds`, `Unlocks`, `Hats` and `Leaderboards`. A `for` loop iterates any of them. The linter's known names live in `references/enums.json`, with a `_stale` map of old names and their replacements.
