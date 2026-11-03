# Log convention for drone scripts

Every script that joins a loop prints the same two kinds of lines. The driver, `precheck.py` and you then read every script the same way.

## PHASE lines

1. Split the script into phases: one sweep, one pass, one solve, one round.
2. At the end of each phase, print one line in this exact shape:

   `PHASE <name> t <get_time()> work <successful actions> left <remaining work>`

3. Make `<name>` one word with no spaces, e.g. `sweep` or `round3`.
4. Count only actions that succeeded. Most actions return a bool, so add 1 when one returns True.
5. Set `left` to the work your script still sees: cells to visit, items still to make, steps to the goal. Print `-1` if the script cannot know it.
6. Use `quick_print`, not `print`: it writes only to the log and costs no game time.

A neutral example, for a phase that visits cells and acts on some of them:

```python
done = 0
for i in range(n):
	if act():
		done += 1
	step()
quick_print("PHASE sweep t", get_time(), "work", done, "left", n - done)
```

## Zero work is an anomaly

A phase that reports `work 0` did nothing. Treat it as a bug until you can explain it.
1. `precheck.py` fails a script on any `work 0` line.
2. `economy_driver.py` writes a `CHECK` line to its log for each one and keeps the slice output.
3. When a phase can legitimately do nothing, end the phase earlier instead, or renew the resource it needs.

## SUMMARY line

The game keeps only the most recent output lines. A long run pushes its early lines out.
1. End every run with one line that starts with `SUMMARY`, e.g. `SUMMARY t 812.4 phases 40 work 3120`.
2. For an endless loop, print a `SUMMARY` line every N phases instead.
3. Keep it on one line, so it survives with the last few lines of the buffer.

## Who reads the lines

1. `scripts/precheck.py FILE` runs the script in `simulate()` and fails on errors, warnings, `work 0`, or no lines at all.
2. `scripts/economy_driver.py` archives each slice's output under `<workdir>/slices/` and flags `work 0` lines.
3. `scripts/lint_tfwr.py` warns when a script farms or moves in a loop but never prints a `PHASE` or `SUMMARY` line.
