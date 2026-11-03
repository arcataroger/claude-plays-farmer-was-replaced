#!/usr/bin/env python3
"""Static checker for The Farmer Was Replaced scripts.

The in-game language looks like Python but differs. This linter catches the Python habits
that break in-game, plus stale identifiers from older game versions.

usage: lint_tfwr.py FILE.py [FILE2.py ...] [--save-dir DIR] [--quiet]

  --save-dir DIR  folder holding the player's other code files; used to resolve imports
                  and to load a fresher __builtins__.py stub if the game wrote one.

Exit code: 0 = no errors (warnings allowed), 1 = errors found, 2 = could not parse.
Every finding cites the in-game error string it mirrors, where one exists.

Two method rules hold no game errors; they enforce the skill's own conventions:
  no-log       WARNING when a loop plants, harvests, digs, places or moves (directly or
               through your own functions) but the file has no PHASE or SUMMARY string.
               See scripts/tfwr_log.md. We skip library files: files whose top level
               holds only defs, imports and assignments, because their caller logs.
  literal-pos  WARNING when a call to a movement helper passes two integer literals,
               e.g. go(12, 7). A movement helper is your own function that calls move()
               (directly or through your functions) and takes 2+ parameters. Fair play
               forbids hardcoding positions seen on screen. We skip (0, 0), the origin,
               and negative literals, which read as offsets.
"""
import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENUMS_PATH = os.path.join(HERE, "..", "references", "enums.json")

BUILTIN_FUNCS = {"reset",
    # farming
    "harvest", "can_harvest", "plant", "swap", "till", "use_item", "place", "clear", "change_hat",
    # movement / senses
    "move", "can_move", "get_pos_x", "get_pos_y", "get_pos_z", "hover", "get_hover", "get_world_size",
    "get_entity_type", "get_ground_type", "get_water", "num_items", "get_companion", "measure",
    # mining
    "dig", "get_hardness", "get_stability", "prospect_iron", "prospect_quartz", "push", "can_push", "jump",
    # drones
    "spawn_drone", "wait_for", "has_finished", "max_drones", "num_drones",
    # debug / timing / sim
    "get_time", "get_tick_count", "set_execution_speed", "set_world_size", "simulate", "leaderboard_run",
    # unlocks
    "get_cost", "unlock", "num_unlocked",
    # utilities / language
    "random", "min", "max", "abs", "print", "quick_print", "do_a_flip", "pet_the_piggy",
    "range", "len", "str", "list", "dict", "set",
}
BUILTIN_CONSTS = {"North", "East", "South", "West", "True", "False", "None", "__name__",
                  "Items", "Entities", "Grounds", "Unlocks", "Hats", "Leaderboards"}
ENUM_CATS = ["Grounds", "Items", "Entities", "Unlocks", "Hats", "Leaderboards"]

# (min_args, max_args) for builtins with fixed arity.
ARITY = {
    "harvest": (0, 0), "can_harvest": (0, 0), "till": (0, 0), "clear": (0, 0), "dig": (0, 0),
    "plant": (1, 1), "swap": (1, 1), "place": (1, 1), "change_hat": (1, 1), "move": (1, 1),
    "can_move": (1, 1), "hover": (1, 1), "push": (1, 1), "can_push": (1, 1), "jump": (1, 1),
    "get_pos_x": (0, 0), "get_pos_y": (0, 0), "get_hover": (0, 0), "get_world_size": (0, 0),
    "get_pos_z": (0, 1), "get_entity_type": (0, 1), "get_ground_type": (0, 1), "measure": (0, 1),
    "get_hardness": (0, 1), "get_stability": (0, 1),
    "get_water": (0, 0), "num_items": (1, 1), "get_companion": (0, 0),
    "prospect_iron": (0, 0), "prospect_quartz": (0, 0),
    "use_item": (1, 2), "wait_for": (1, 1), "has_finished": (1, 1), "max_drones": (0, 0), "num_drones": (0, 0),
    "get_time": (0, 0), "get_tick_count": (0, 0), "set_execution_speed": (1, 1), "set_world_size": (1, 1),
    "simulate": (6, 6), "leaderboard_run": (3, 3), "get_cost": (1, 2), "unlock": (1, 1), "num_unlocked": (1, 1),
    "random": (0, 0), "abs": (1, 1), "do_a_flip": (0, 0), "pet_the_piggy": (0, 0),
    "len": (1, 1), "str": (1, 1), "range": (1, 3),
}

# Python builtins that do NOT exist in-game, with what to do instead.
MISSING_PY = {
    "sum": "loop and add", "sorted": "write your own sort (lists have no .sort())",
    "enumerate": "use a counter variable", "zip": "index with range(len(...))",
    "int": "use x // 1 (all numbers are floats)", "float": "numbers are already floats",
    "round": "use (x + 0.5) // 1", "isinstance": "not available", "type": "not available",
    "any": "loop with a flag", "all": "loop with a flag", "reversed": "use range(len(l) - 1, -1, -1)",
    "sqrt": "use x ** 0.5", "pow": "use **", "input": "not available", "open": "not available",
    "tuple": "build tuples with commas: a, b", "bool": "compare explicitly", "map": "use a for loop",
    "filter": "use a for loop", "exit": "return from main() or break", "quit": "break out of the loop",
    "divmod": "use // and %", "hash": "not available", "id": "not available", "iter": "not available",
    "next": "not available", "chr": "not available", "ord": "not available", "format": "use str() and +",
}
LIST_METHODS = {"append", "remove", "insert", "pop"}
SET_METHODS = {"add", "remove"}
DICT_METHODS = {"pop"}
ALL_METHODS = LIST_METHODS | SET_METHODS | DICT_METHODS
# Builtins whose result can be 0 or None for a valid reading, so a bare truth test is a logic bug.
NUMERIC_OR_NONE = {"measure", "prospect_quartz", "prospect_iron", "get_hardness", "get_stability",
                   "get_pos_x", "get_pos_y", "get_pos_z", "num_items", "get_water", "get_entity_type",
                   "get_ground_type", "get_companion", "get_hover", "num_unlocked", "num_drones"}
SHADOW_WARN = {"min", "max", "abs", "len", "range", "list", "dict", "set", "str", "print", "random",
               "move", "harvest", "measure", "dig", "plant"}
# The Python stdlib and typing modules do not exist in-game.
FAKE_MODULES = {"math", "random", "typing", "collections", "itertools", "functools", "time", "os", "sys"}


class Finding:
    def __init__(self, level, line, msg, ref=None):
        self.level, self.line, self.msg, self.ref = level, line, msg, ref

    def __str__(self):
        ref = "  [game: %s]" % self.ref if self.ref else ""
        return "%s:%s: %s%s" % (self.level.upper(), self.line, self.msg, ref)


def load_enums(save_dir):
    with open(ENUMS_PATH) as f:
        data = json.load(f)
    known = {c: set(data.get(c, [])) for c in ENUM_CATS}
    stale = data.get("_stale", {})
    uncertain = data.get("_uncertain", {})
    # Merge in a game-written stub if it is fresher than our snapshot.
    stub = os.path.join(save_dir, "__builtins__.py") if save_dir else None
    if stub and os.path.exists(stub) and os.path.getmtime(stub) > os.path.getmtime(ENUMS_PATH):
        text = open(stub, errors="replace").read()
        for c in ENUM_CATS:
            m = re.search(r"class %s\b.*?:(.*?)(?=\nclass |\n# -{10,}\n(?:def|class)|\Z)" % c, text, re.S)
            if m:
                names = set(re.findall(r"^\s+(\w+)\s*:\s*\w+\s*$", m.group(1), re.M))
                names |= set(re.findall(r"def (\w+)\(self\)", m.group(1)))
                for n in names:
                    if not n.startswith("_"):
                        known[c].add(n)
                        stale.pop("%s.%s" % (c, n), None)
    return known, stale, uncertain


class Linter(ast.NodeVisitor):
    def __init__(self, src, path, save_dir, enums):
        self.src, self.path, self.save_dir = src, path, save_dir
        self.known, self.stale, self.uncertain = enums
        self.f = []
        self.defined = set()
        self.modules = set()
        self.star_import = False
        self.func_stack = []
        self.loop_iters = []
        self.jump_calls = 0
        self.spawned_funcs = set()

    def add(self, level, node, msg, ref=None):
        self.f.append(Finding(level, getattr(node, "lineno", "?"), msg, ref))

    # ---------- pre-pass: collect definitions ----------
    def collect(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                self.defined.add(node.name)
                for a in node.args.args:
                    self.defined.add(a.arg)
            elif isinstance(node, (ast.Name,)) and isinstance(node.ctx, ast.Store):
                self.defined.add(node.id)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    self.modules.add(a.asname or a.name)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == "*":
                        self.star_import = True
                    else:
                        self.defined.add(a.asname or a.name)
            elif isinstance(node, ast.Global):
                self.defined.update(node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "spawn_drone":
                if node.args and isinstance(node.args[0], ast.Name):
                    self.spawned_funcs.add(node.args[0].id)

    # ---------- unsupported syntax ----------
    def generic_unsupported(self, node, what, level="error", ref=None):
        self.add(level, node, what, ref)
        self.generic_visit(node)

    def visit_ClassDef(self, n): self.generic_unsupported(n, "`class` does not exist in-game. Use dicts/tuples and functions.")
    def visit_Lambda(self, n): self.generic_unsupported(n, "`lambda` does not exist in-game. Use a nested `def`.")
    def visit_Try(self, n): self.generic_unsupported(n, "`try/except` does not exist in-game. Check return values instead (move/dig/plant return bools).")
    def visit_With(self, n): self.generic_unsupported(n, "`with` does not exist in-game.")
    def visit_Assert(self, n): self.generic_unsupported(n, "`assert` does not exist in-game. Use if + print.")
    def visit_Delete(self, n): self.generic_unsupported(n, "`del` does not exist in-game. Use list.pop(i) / dict.pop(k) / set.remove(x).")
    def visit_Raise(self, n): self.generic_unsupported(n, "`raise` does not exist in-game.")
    def visit_Nonlocal(self, n): self.generic_unsupported(n, "`nonlocal` does not exist in-game. Only `global` exists.")
    def visit_Yield(self, n): self.generic_unsupported(n, "Generators (`yield`) do not exist in-game.")
    def visit_YieldFrom(self, n): self.visit_Yield(n)
    def visit_AsyncFunctionDef(self, n): self.generic_unsupported(n, "async does not exist in-game. Use spawn_drone() for parallelism.")
    def visit_ListComp(self, n): self.generic_unsupported(n, "List comprehensions are undocumented in-game. Build the list with a for loop and append().", "warning")
    def visit_SetComp(self, n): self.generic_unsupported(n, "Set comprehensions are undocumented in-game. Use a for loop and add().", "warning")
    def visit_DictComp(self, n): self.generic_unsupported(n, "Dict comprehensions are undocumented in-game. Use a for loop.", "warning")
    def visit_GeneratorExp(self, n): self.generic_unsupported(n, "Generator expressions do not exist in-game. Use a for loop.")
    def visit_JoinedStr(self, n): self.generic_unsupported(n, "f-strings are undocumented in-game. Use print(a, b) or str(x) + ' '.", "warning")
    def visit_IfExp(self, n): self.generic_unsupported(n, "Conditional expressions (`a if c else b`) are undocumented in-game. Use an if statement.", "warning")
    def visit_NamedExpr(self, n): self.generic_unsupported(n, "The walrus operator `:=` does not exist in-game.")
    def visit_Starred(self, n): self.generic_unsupported(n, "Star unpacking (`*x`) is undocumented in-game.", "warning")

    def visit_Constant(self, n):
        if isinstance(n.value, str):
            seg = ast.get_source_segment(self.src, n) or ""
            if seg.startswith(('"""', "'''")) and not self._is_docstring(n):
                self.add("warning", n, "Triple-quoted strings are undocumented in-game. Use # comments.")

    def _is_docstring(self, n):
        return False

    def visit_Expr(self, n):
        # A bare string statement (docstring) - flag once, it is a no-op at best.
        if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            seg = ast.get_source_segment(self.src, n.value) or ""
            if seg.startswith(('"""', "'''")):
                self.add("warning", n, "Docstrings/triple-quoted strings are undocumented in-game. Use # comments.")
                return
        self.generic_visit(n)

    def visit_Compare(self, n):
        for op in n.ops:
            if isinstance(op, (ast.Is, ast.IsNot)):
                self.add("error", n, "`is` / `is not` do not exist in-game. Use `==` / `!=` (e.g. `x != None`).")
        for op, right in zip(n.ops, n.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, ast.Constant) and isinstance(right.value, str):
                self.add("error", n, "`in` on a string fails in-game. Iterate with `for ch in s:` instead.", "error_in_string")
        if len(n.ops) > 1:
            self.add("warning", n, "Chained comparisons (a < b < c) are undocumented in-game. Use `a < b and b < c`.")
        # Comparing an enum constant with a number
        for side in [n.left] + n.comparators:
            cat = self._enum_cat(side)
            if cat in ("Items", "Entities", "Unlocks") and any(
                    isinstance(o, (ast.Lt, ast.Gt, ast.LtE, ast.GtE)) for o in n.ops):
                self.add("error", n, "Comparing %s with a number. Use num_items()/num_unlocked()/get_entity_type()." % cat,
                         "error_compared_item_with_number")
        self.generic_visit(n)

    def visit_BoolOp(self, n):
        if isinstance(n.op, ast.Or):
            for v in n.values[1:]:
                if isinstance(v, ast.Constant) and not isinstance(v.value, bool):
                    self.add("error", n, "`or <constant>` is always true. Did you mean `x == a or x == b`?", "error_nonsensical_or")
        self.generic_visit(n)

    # ---------- conditions ----------
    def _check_condition(self, test, node):
        t = test.operand if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) else test
        cat = self._enum_cat(t)
        if cat:
            self.add("error", node, "%s constant used as a condition. Compare it, e.g. `get_ground_type() == %s`." % (cat, ast.unparse(t)),
                     "error_%s_condition" % cat.lower().rstrip("s"))
        if isinstance(t, ast.Name) and (t.id in BUILTIN_FUNCS or t.id in self._func_names) and t.id not in self._assigned_non_func:
            self.add("error", node, "Function object `%s` used as a condition. Call it: `%s()`." % (t.id, t.id), "error_function_as_condition")
        if isinstance(t, ast.Call) and isinstance(t.func, ast.Name) and t.func.id in ("get_ground_type", "get_entity_type"):
            self.add("warning", node, "`%s()` returns a type, not a bool. Compare it: `%s() == ...` or `!= None`." % (t.func.id, t.func.id))
        elif isinstance(t, ast.Call) and isinstance(t.func, ast.Name) and t.func.id in NUMERIC_OR_NONE:
            self.add("warning", node, "`%s()` can return 0 or None for a real reading. Compare explicitly (`!= None`, `> 0`)." % t.func.id)

    def visit_If(self, n):
        self._check_condition(n.test, n)
        self.generic_visit(n)

    def visit_While(self, n):
        self._check_condition(n.test, n)
        if n.orelse:
            self.add("warning", n, "`while ... else` is undocumented in-game.")
        self.loop_iters.append(None)
        self.generic_visit(n)
        self.loop_iters.pop()

    def visit_For(self, n):
        if n.orelse:
            self.add("warning", n, "`for ... else` is undocumented in-game.")
        it = ast.unparse(n.iter) if isinstance(n.iter, (ast.Name, ast.Attribute)) else None
        self.loop_iters.append(it)
        self.generic_visit(n)
        self.loop_iters.pop()

    # ---------- functions / scope ----------
    def visit_FunctionDef(self, n):
        a = n.args
        if a.vararg or a.kwarg or a.kwonlyargs:
            self.add("warning", n, "*args/**kwargs/keyword-only params are undocumented in-game.")
        if n.decorator_list:
            self.add("error", n, "Decorators do not exist in-game.")
        if n.returns or any(x.annotation for x in a.args):
            self.add("warning", n, "Type annotations are undocumented in-game. Remove them.")
        if n.name in self.spawned_funcs:
            self._check_drone_globals(n)
        self.func_stack.append(n)
        self._check_augassign_globals(n)
        self.generic_visit(n)
        self.func_stack.pop()

    def _check_augassign_globals(self, fn):
        params = {x.arg for x in fn.args.args}
        declared_global = set()
        assigned = set(params)
        for node in ast.walk(fn):
            if isinstance(node, ast.Global):
                declared_global.update(node.names)
        # walk in source order, top-level statements of this function only (good enough heuristic)
        for node in sorted((x for x in ast.walk(fn) if hasattr(x, "lineno")), key=lambda x: (x.lineno, x.col_offset)):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    for nm in ast.walk(t):
                        if isinstance(nm, ast.Name):
                            assigned.add(nm.id)
            elif isinstance(node, ast.For):
                for nm in ast.walk(node.target):
                    if isinstance(nm, ast.Name):
                        assigned.add(nm.id)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                nm = node.target.id
                if nm not in assigned and nm not in declared_global and nm in self._module_globals:
                    self.add("error", node, "`%s += ...` inside %s() reads global `%s` without `global %s`." % (
                        nm, fn.name, nm, nm), "error_use_before_assign")

    def _check_drone_globals(self, fn):
        for node in ast.walk(fn):
            if isinstance(node, ast.Global):
                self.add("warning", node, "%s() runs on a spawned drone. Its global writes stay on that drone; "
                         "return a value and read it with wait_for()." % fn.name)

    def visit_Global(self, n):
        if not self.func_stack:
            self.add("warning", n, "`global` at module level does nothing.")
        self.generic_visit(n)

    def visit_Assign(self, n):
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id in SHADOW_WARN and not self.func_stack:
                self.add("warning", n, "Assigning to `%s` shadows the builtin `%s()`." % (t.id, t.id), "error_not_a_function")
            if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Tuple):
                self.add("error", n, "Tuples are immutable.", "error_index_on_tuple")
        self.generic_visit(n)

    # ---------- imports ----------
    def visit_Import(self, n):
        for a in n.names:
            self._check_module(n, a.name)
            if a.asname:
                self.add("warning", n, "`import x as y` is undocumented in-game.")
        self.generic_visit(n)

    def visit_ImportFrom(self, n):
        if n.module == "__builtins__":
            return  # editor-only helper; harmless
        self._check_module(n, n.module or "")
        if self.func_stack and any(a.name == "*" for a in n.names):
            self.add("error", n, "`from x import *` is not allowed inside functions.", "error_wildcard_imports_not_allowed_in_function")
        self.generic_visit(n)

    def _check_module(self, node, name):
        if name in FAKE_MODULES:
            self.add("error", node, "Module `%s` does not exist in-game. Imports load the player's own code files only." % name,
                     "error_module_not_found")
            return
        if "." in name or not re.match(r"^[A-Za-z_]\w*$", name):
            self.add("error", node, "Import names must be plain file names that are valid identifiers.", "error_invalid_import")
            return
        if self.save_dir:
            base = os.path.dirname(os.path.abspath(self.path))
            if not (os.path.exists(os.path.join(self.save_dir, name + ".py")) or os.path.exists(os.path.join(base, name + ".py"))):
                self.add("error", node, "No code file named `%s.py` in the save folder." % name, "error_module_not_found")

    # ---------- calls / attributes ----------
    def visit_Call(self, n):
        if n.keywords:
            self.add("warning", n, "Keyword arguments (f(x=1)) are undocumented in-game. Pass arguments by position.")
        if isinstance(n.func, ast.Name):
            name = n.func.id
            if name == "reset" and name not in self.defined:
                self.add("error", n, "DANGER: reset() wipes the farm to a 1x1 plot and locks most unlocks. Never call it; "
                         "use clear() to return to the surface.")
            elif name in MISSING_PY and name not in self.defined and not self.star_import:
                self.add("error", n, "`%s()` does not exist in-game: %s." % (name, MISSING_PY[name]),
                         "error_name_not_defined")
            elif (name not in BUILTIN_FUNCS and name not in self.defined and name not in BUILTIN_CONSTS
                  and not self.star_import):
                self.add("error", n, "`%s` is never defined in this file or imported." % name, "error_name_not_defined")
            if name in ARITY and name not in self._user_funcs:
                lo, hi = ARITY[name]
                k = len(n.args)
                if not (lo <= k <= hi):
                    self.add("error" if name != "dig" else "warning", n,
                             "%s() takes %s argument(s); got %d." % (name, lo if lo == hi else "%d-%d" % (lo, hi), k),
                             "error_wrong_number_args")
            if name == "print" and not n.args:
                self.add("error", n, "print() needs at least one argument.", "error_empty_print")
            if name == "jump":
                self.jump_calls += 1
                if self.func_stack and self.func_stack[-1].name in self.spawned_funcs:
                    self.add("error", n, "Only the first drone can jump().", "error_only_first_drone_can_jump")
            if name == "spawn_drone" and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in BUILTIN_FUNCS \
                    and n.args[0].id not in self._user_funcs:
                self.add("error", n, "spawn_drone() needs your own function, not a builtin.", "error_spawn_drone_builtin")
            if name == "spawn_drone" and n.args and isinstance(n.args[0], ast.Constant):
                self.add("error", n, "spawn_drone() takes a function, not a file name string.")
            if name == "use_item" and len(n.args) == 2 and isinstance(n.args[1], ast.Constant) and \
                    isinstance(n.args[1].value, (int, float)) and n.args[1].value < 1:
                self.add("error", n, "use_item quantity must be at least 1.", "error_negative_use_item")
        elif isinstance(n.func, ast.Attribute):
            obj, meth = n.func.value, n.func.attr
            is_module = isinstance(obj, ast.Name) and obj.id in self.modules
            is_enum = isinstance(obj, ast.Name) and obj.id in ENUM_CATS
            if not is_module and not is_enum and meth not in ALL_METHODS:
                hint = {"sort": "write a sort", "extend": "loop + append", "index": "loop to find it",
                        "keys": "iterate the dict directly: for k in d", "items": "for k in d: v = d[k]",
                        "values": "for k in d: v = d[k]", "get": "use `if k in d:` first",
                        "split": "iterate the string with for", "join": "concatenate with +",
                        "copy": "build a new list with a loop", "clear": "assign a new [] / {} / set()",
                        "discard": "check `in` then remove", "update": "loop and assign",
                        "count": "loop and count", "upper": "not available", "lower": "not available",
                        "reverse": "build a reversed copy with a loop"}.get(meth, "only append/insert/remove/pop/add exist")
                self.add("error", n, "Method `.%s()` does not exist in-game (%s)." % (meth, hint), "error_unknown_method")
            if meth in ("remove", "append", "insert", "pop", "add") and self.loop_iters:
                target = ast.unparse(obj)
                if target in [x for x in self.loop_iters if x]:
                    self.add("error", n, "Changing `%s` while a for loop iterates over it." % target,
                             "error_collection_changed_size_during_iteration")
        self.generic_visit(n)

    def visit_Attribute(self, n):
        cat = n.value.id if isinstance(n.value, ast.Name) else None
        if cat in ENUM_CATS:
            key = "%s.%s" % (cat, n.attr)
            if key in self.stale:
                fix = self.stale[key]
                self.add("error", n, "`%s` is a stale name from an older game version.%s" % (
                    key, " Use `%s`." % fix if fix else " It no longer exists."), "error_invalid_const")
            elif key in self.uncertain:
                self.add("warning", n, "`%s`: %s" % (key, self.uncertain[key]))
            elif n.attr not in self.known[cat]:
                self.add("warning", n, "`%s` is not in the known %s list. Check the in-game docs (tfwr.py doc)." % (key, cat),
                         "error_invalid_const")
        self.generic_visit(n)

    def _enum_cat(self, node):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ENUM_CATS:
            return node.value.id
        return None

    def run(self):
        try:
            tree = ast.parse(self.src, self.path)
        except TabError as e:
            self.f.append(Finding("error", e.lineno, "Tabs and spaces are mixed.", "error_mixed_indentation"))
            return 2
        except SyntaxError as e:
            self.f.append(Finding("error", e.lineno, "Syntax error: %s" % e.msg))
            return 2
        self.collect(tree)
        self._user_funcs = {x.name for x in ast.walk(tree) if isinstance(x, ast.FunctionDef)}
        self._func_names = self._user_funcs
        self._assigned_non_func = set()
        self._module_globals = set()
        for st in tree.body:
            for nm in ast.walk(st) if isinstance(st, (ast.Assign, ast.AugAssign, ast.For)) else []:
                if isinstance(nm, ast.Name) and isinstance(nm.ctx, ast.Store):
                    self._module_globals.add(nm.id)
        # Mixed indentation check (ast tolerates some mixes the game rejects).
        indents = {m.group(1)[0] for m in re.finditer(r"^([ \t]+)\S", self.src, re.M)}
        if len(indents) > 1:
            self.f.append(Finding("error", "?", "File mixes tab and space indentation.", "error_mixed_indentation"))
        self.visit(tree)
        if self.save_dir:
            user = {x.id for x in ast.walk(tree) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
            self.f += unlock_findings(tree, self.save_dir, user | self._user_funcs)
        self.f += method_findings(tree, _owns(self.save_dir, "debug"))
        if self.jump_calls > 1:
            self.f.append(Finding("warning", "?", "jump() appears %d times; it works once per program run." % self.jump_calls,
                                  "error_can_only_jump_once"))
        return 1 if any(x.level == "error" for x in self.f) else 0


# --- Method rules: log lines and literal positions (see the module docstring) ---
ACTIONS = {"plant", "harvest", "dig", "place", "move"}
LOG_TAGS = ("PHASE", "SUMMARY")


def _called_names(node):
    return {c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}


def _reaching(funcs, targets):
    """Names of user functions that call a target builtin, directly or through other user functions."""
    calls = {name: _called_names(fn) for name, fn in funcs.items()}
    hit = {name for name, c in calls.items() if c & targets}
    grew = True
    while grew:
        grew = False
        for name, c in calls.items():
            if name not in hit and c & hit:
                hit.add(name)
                grew = True
    return hit


def _is_library(tree):
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
            continue
        return False
    return True


def _int_literal(node):
    return isinstance(node, ast.Constant) and type(node.value) in (int, float) and node.value >= 0 \
        and float(node.value).is_integer()


def _owns(save_dir, unlock_id):
    """True when the save owns the unlock, or when no save was given (assume a late game)."""
    if not save_dir:
        return True
    try:
        with open(os.path.join(save_dir, "save.json"), encoding="utf-8") as f:
            ids = json.load(f).get("unlocks", [])
    except (OSError, ValueError):
        return True
    return any(u == unlock_id or re.match(re.escape(unlock_id) + r"_\d+$", u) for u in ids)


def method_findings(tree, debug_owned=True):
    found = []
    funcs = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)}
    # no-log: one warning per file, at the first acting loop.
    has_tag = any(isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.lstrip().startswith(LOG_TAGS)
                  for c in ast.walk(tree))
    if not has_tag and debug_owned and not _is_library(tree):
        acting = ACTIONS | _reaching(funcs, ACTIONS)
        loops = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))), key=lambda n: n.lineno)
        for lp in loops:
            if _called_names(lp) & acting:
                found.append(Finding("warning", lp.lineno,
                                     "This loop farms or moves, but the file prints no PHASE or SUMMARY line. "
                                     "Log each phase: see scripts/tfwr_log.md."))
                break
    # literal-pos: two integer literals passed to a movement helper.
    movers = {n for n in _reaching(funcs, {"move"}) if len(funcs[n].args.args) >= 2}
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in movers and len(c.args) >= 2:
            x, y = c.args[0], c.args[1]
            if _int_literal(x) and _int_literal(y) and (x.value, y.value) != (0, 0):
                found.append(Finding("warning", c.lineno,
                                     "%s(%g, %g) passes a literal position. Fair play forbids hardcoding positions "
                                     "seen on screen; derive it from get_world_size() or the drone's sensors."
                                     % (c.func.id, x.value, y.value)))
    return found


# --- Unlock check: is every name and syntax feature unlocked in this save? ---
# The tech tree lists what each unlock grants (names and keywords, some only at a
# higher level). The save lists what the player owns. Syntax without a keyword
# grant follows the game's own errors: comparisons and arithmetic need Operators.
SYNTAX = [  # (AST node types, grant name that enables them, what to call it)
    ((ast.BinOp, ast.Compare, ast.AugAssign), "and", "operators (comparisons and arithmetic)"),
    ((ast.If, ast.IfExp), "if", "`if`"),
    ((ast.While,), "while", "`while`"),
    ((ast.Break,), "break", "`break`"),
    ((ast.Continue,), "continue", "`continue`"),
    ((ast.For,), "for", "`for`"),
    ((ast.FunctionDef,), "def", "`def`"),
    ((ast.Return,), "return", "`return`"),
    ((ast.Global,), "global", "`global`"),
    ((ast.List, ast.Subscript), "list", "lists"),
    ((ast.Dict, ast.Set), "dict", "dicts and sets"),
    ((ast.ImportFrom,), "from", "`from ... import`"),
]


def unlock_findings(tree, save_dir, user_names):
    tree_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references", "tech_tree.json")
    save_path = os.path.join(save_dir, "save.json")
    if not (os.path.exists(tree_path) and os.path.exists(save_path)) or os.path.getsize(save_path) == 0:
        return []
    with open(save_path) as f:
        owned_raw = json.load(f).get("unlocks", [])  # metadata only, never the map
    with open(tree_path) as f:
        nodes = json.load(f)["unlocks"]
    node_ids = {v["id"] for v in nodes}
    level = {}
    for u in owned_raw:
        m = None if u in node_ids else re.match(r"(.+)_(\d+)$", u)  # debug_2 is an id
        uid, n = (m.group(1), int(m.group(2))) if m else (u, 1)
        level[uid] = max(level.get(uid, 0), n)
    grants, enum, reqs = {}, {}, {}
    for v in nodes:
        enum[v["id"]] = v.get("enum_name_guess") or v["id"].title()
        reqs[v["id"]] = [(r["id"], r.get("level", 1)) for r in (v.get("requires") or [])]
        for g in v.get("grants") or []:
            grants.setdefault(g["name"].lower(), []).append((v["id"], g["level"]))
    # Variables and Import grant no names; they gate assignment and `import`.
    grants.setdefault("=", []).append(("variables", 1))
    grants.setdefault("import", []).append(("import", 1))

    def missing(name):
        """None if owned or unknown, else the cheapest visible (unlock id, level) that grants it.

        Several nodes can grant one name. Fair play: name only a node the player can
        see (owned, or every prerequisite met); otherwise return ("?", 0).
        """
        name = name.lower()
        if name in level:
            return None
        ways = grants.get(name)
        if not ways or any(level.get(uid, 0) >= lv for uid, lv in ways):
            return None
        seen_ways = [w for w in ways if level.get(w[0], 0) > 0
                     or all(level.get(r, 0) >= rl for r, rl in reqs.get(w[0], []))]
        return min(seen_ways, key=lambda w: w[1]) if seen_ways else ("?", 0)

    found, seen = [], set()

    def report(node, what, need):
        if need and what not in seen:
            seen.add(what)
            if need[0] == "?":
                msg = "%s needs an unlock you can't see in the tech tree yet." % what
            else:
                lv = " level %d" % need[1] if need[1] > 1 else ""
                msg = "%s needs Unlocks.%s%s, which this save doesn't have yet." % (what, enum.get(need[0], need[0]), lv)
            found.append(Finding("error", getattr(node, "lineno", "?"), msg, "error_not_unlocked"))

    for node in ast.walk(tree):
        for types, gname, what in SYNTAX:
            if isinstance(node, types):
                report(node, what, missing(gname))
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) for t in node.targets):
            report(node, "assigning a variable", missing("="))
        if isinstance(node, (ast.Import,)):
            report(node, "`import`", missing("import"))
        if isinstance(node, ast.Tuple):
            report(node, "tuples", missing("tuples"))
        if isinstance(node, ast.BoolOp) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
            report(node, "`and`/`or`/`not`", missing("and"))
        if isinstance(node, ast.Name) and node.id not in user_names:
            report(node, "`%s`" % node.id, missing(node.id))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            report(node, "`%s.%s`" % (node.value.id, node.attr), missing(node.attr))
    return found


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    save_dir = None
    quiet = "--quiet" in args
    if "--save-dir" in args:
        i = args.index("--save-dir")
        save_dir = args[i + 1]
        del args[i:i + 2]
    files = [a for a in args if not a.startswith("--")]
    worst = 0
    for path in files:
        src = open(path, errors="replace").read()
        enums = load_enums(save_dir or os.path.dirname(os.path.abspath(path)))
        lt = Linter(src, path, save_dir, enums)
        code = lt.run()
        worst = max(worst, code)
        errs = sum(1 for x in lt.f if x.level == "error")
        warns = len(lt.f) - errs
        print("%s: %d error(s), %d warning(s)" % (path, errs, warns))
        for x in sorted(lt.f, key=lambda x: (x.level != "error", str(x.line))):
            if quiet and x.level != "error":
                continue
            print("  " + str(x))
    sys.exit(worst)


if __name__ == "__main__":
    main()
