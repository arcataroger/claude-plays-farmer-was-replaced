# The in-game language: how it differs from Python

The game's language looks like Python but isn't Python. Python instinct produces most failed runs. Each rule below cites its source: a doc page (`doc NAME`) or an error string (`errors TEXT`), both read through `scripts/tfwr.py`.

## Contents
1. What exists
2. What does not exist
3. Values, numbers and conditions
4. Collections
5. Functions, scope and imports
6. Drones (parallelism)
7. Timing model (ticks and seconds)
8. Progressive unlocks

## 1. What exists
- Statements: `if/elif/else`, `while`, `for x in seq`, `break`, `continue`, `pass`, `return`, `def` (with defaults), `global`, `import f`, `from f import x`, `from f import *`.
- Operators: `+ - * / // % **`, `== != < > <= >=`, `not and or`, `in`, and assignment forms like `+=`.
- Literals: numbers, strings, `True/False/None`, lists `[]`, tuples `a, b`, dicts `{k: v}`, sets `{a, b}`, and `set()` for an empty set (`{}` makes a dict).
- Tuple unpacking (`x, y = measure()`), nested unpacking (`t, (x, y) = get_companion()`), indexing, and list slicing.
- Line continuation with `\` or open brackets.
- `__name__ == "__main__"` for import-safe entry points (doc `import`).

## 2. What does not exist
The linter flags all of these.
- `class`, `lambda`, `try/except`, `with`, `raise`, `assert`, `del`, `is`, `nonlocal`, `yield`, async, and decorators.
- The Python stdlib (`import math`, `random`, `typing`, ...). `import` loads only the player's own code files.
- Most builtins: `sum`, `sorted`, `enumerate`, `zip`, `int`, `round`, `any`, `all`, `map`, `isinstance`. The game provides only `len range str list dict set min max abs random print quick_print` plus the game API.
- Most methods. Lists have `append insert remove pop`. Sets have `add remove`. Dicts have `pop`. You get no `.keys()`, `.items()`, `.sort()`, `.split()` or `.join()` (`error_unknown_method`). Iterate a dict directly: `for k in d: v = d[k]`.
- `in` on strings (`error_in_string`). Loop instead: `for ch in s:`.
- Undocumented, so avoid them: comprehensions, f-strings, `a if c else b`, chained comparisons, keyword arguments, `*args`, and triple-quoted strings. Some can work, but nothing promises it, and one failed run costs more than a plain loop.

## 3. Values, numbers and conditions
- All numbers are floats (doc `operators`). `//` floors. Use `x // 1` to truncate, because `int()` doesn't exist.
- `random()` returns a float in [0, 1). For an index: `random() * len(l) // 1`.
- The game rejects an enum constant used as a condition (`if Grounds.Dirt:`), with errors `error_ground_condition` and the like.
- The game rejects a function object used as a condition (`if can_harvest:`). That gives `error_function_as_condition`.
- `x == 5 or 6` is an error (`error_nonsensical_or`).
- Sensors can return `None` or `0` for real, distinct states. Write `!= None` and `> 0` explicitly, and never rely on truthiness for sensor values. Check each sensor's tooltip for what `None` means.
- The bridge's output cuts each line at 1000 characters (measured 2026-11-04); `output.txt` keeps the whole line. Print long data in chunks.
- `print()` needs at least one argument and works from the start. `quick_print()` writes only to the output log, costs nothing, and needs the Debug unlock.

## 4. Collections
- Lists use reference semantics: `b = a` aliases the list (doc `lists`).
- Tuples are immutable and work as dict keys. Use `(x, y)` or `(x, y, z)` keys for maps of the world.
- Dicts and sets keep no order, so never depend on iteration order.
- Never change a collection's size while you `for`-loop over it (`error_collection_changed_size_during_iteration`). Iterate a copy you built, or use `while` with an index.
- Dict and set lookups cost extra ticks for large keys. Tuples of small numbers stay cheap.

## 5. Functions, scope and imports
- `def` runs like an assignment, so call a function only after its `def` has run (`error_call_before_def`).
- Assignment inside a function creates a local. To write a global, declare `global x` first. Otherwise `x += 1` fails (`error_use_before_assign`).
- Loops and `if` blocks create no scope.
- Functions are values: you can pass them, return them, and define them inside other functions. Closures capture loop variables; doc `megafarm` relies on this.
- On import, the game runs the whole file once and caches it. Guard top-level code with `if __name__ == "__main__":` so imports cause no side effects.
- Prefer `import f` and call `f.g()`. `from f import *` breaks on import cycles and is illegal inside functions.
- File names must be valid identifiers. The game shows the file name at the top of each code window, with no `.py`.

## 6. Drones (parallelism, after the Megafarm unlock)
- `spawn_drone(fn, *args)` starts a drone at the caller's position running `fn(*args)`. It returns a handle, or `None` when every drone is busy. The idiom is `if not spawn_drone(task): task()`.
- Drones share no memory. Each drone gets copies of globals and arguments. Return values and collect them with `wait_for(handle)`.
- Drones never collide. Two drones acting on one tile in the same tick create race conditions (doc `megafarm`).
- `spawn_drone` takes your own function, never a builtin (`error_spawn_drone_builtin`).
- Some functions and hats carry per-drone limits. Read the tooltip and doc page before you hand one to a spawned drone.
- `clear()` destroys all other drones.

## 7. Timing model (doc `timing`)
- 400 ticks per second at base speed. Speed upgrades and power raise the rate.
- Drone actions (`move`, `harvest`, `plant`, `till`, `place`, `push`, `hover`, `use_item`, `spawn_drone`, `change_hat`) cost 200 ticks on success and 1 tick on failure.
- Sensors (`get_*`, `measure`, `can_*`, `num_items`, `prospect_*`) cost 1 tick. `get_time`, `get_tick_count` and `quick_print` cost nothing.
- Binary operators cost 1 tick each, and so does an `if`. Unary `-`, `not`, calls, variable reads and writes, `return`, `break` and `continue` cost nothing. A `def` costs 1 tick. `pass` costs 1 tick, which makes it a precise delay.
- Not every action follows the tick model; some take fixed time. Time an unfamiliar action with `get_time()` before you plan around it.
- Measure before you optimize: `t = get_time()` ... `quick_print(get_time() - t)`.

## 8. Progressive unlocks
The language arrives piece by piece: loops, then if, operators, variables, functions, lists, dicts and import. Some features come from unexpected unlocks (confirmed in-game):
- `for` loops need the **second Expand level** (doc `expand_2`). With Expand 1 alone, use `while` with a counter.
- On a fresh save, `get_time()` and `get_tick_count()` need **Timing**, `quick_print()` needs **Debug**, `num_items()` needs **Senses**, and assigning a variable needs **Variables** (verified in-game, 2026-11-04).
- List literals need **Lists**, and `can_move` needs **Mazes** or **Dinosaurs**, although its tooltip doesn't say so. The linter flags both when you give it `--save-dir`.
- The game can report a missing-unlock error on the line after the call that caused it. When an error line looks innocent, check the line above it.
- The game reports a missing unlock as `Unlocks.X must be unlocked before this can be used.` Before you use a feature, confirm the player has it. Check the save's unlock list (`tfwr.py status`) or call `num_unlocked(Unlocks.X) > 0` at runtime. Calling something locked fails with `error_missing_unlock`. Inside `simulate()`, all programming features stay unlocked.
