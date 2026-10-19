"""Load scripts — the Python equivalent of MxLoader's VBA macro layer.

MxLoader is an Excel workbook, so anything its sheets can't express is done
in VBA: a macro that massages rows before a run, a user-defined worksheet
function used in a formula, a post-processing step over the results. MaxMig
keeps the same extension point, in Python: a plain `.py` file passed with
`--script`, defining any of these optional hooks:

```python
from maxmig.mxl.scripting import formula

@formula("PADNUM")                    # usable in fill formulas: =PADNUM([Asset], 5)
def padnum(value, width):
    return str(value).zfill(int(width))

def transform_row(row, ctx):          # fill: each source row (dict, upper-cased keys)
    if row["STATUS"] == "SCRAPPED":   #   return None to drop the row,
        return None                   #   or a (possibly modified) dict
    return row

def before_run(ctx): ...              # load: once, before the first batch
def before_record(payload, ctx):      # load: each top-level object's JSON payload
    payload.setdefault("siteid", "BEDFORD")
    return payload                    #   None skips the object (status "skipped")
def after_record(outcome, ctx): ...   # load: each object's outcome (status, message, ids)
def after_run(summary, ctx): ...      # load: once, with the RunSummary
```

A script is code the operator chose to run — like enabling macros in
MxLoader, it runs with the operator's permissions and is never loaded
implicitly; only `--script` loads one. Exceptions raised by `transform_row`,
`before_record` or a formula function fail that row/object (reported in
`[RESULT]`/`[ERROR]`), not the whole run; exceptions from `before_run` and
`after_run` abort, since they usually mean the script itself is broken.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HOOK_NAMES = ("transform_row", "before_run", "before_record", "after_record", "after_run")

_FORMULA_ATTR = "__maxmig_formula__"


class ScriptError(RuntimeError):
    """A script that can't be loaded, or whose run-level hook failed."""


def formula(name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as a formula UDF (VBA's Public Function used in a cell)."""

    def mark(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, _FORMULA_ATTR, (name or fn.__name__).upper())
        return fn

    return mark


@dataclass
class ScriptContext:
    """Passed to every hook. `state` persists across hooks within one run, so a
    script can count, cache, or collect things (VBA module-level variables)."""

    env: str | None = None
    sheet: Any = None  # the MxSheet being filled/run, when there is one
    action: str | None = None
    dry_run: bool = True
    state: dict[str, Any] = field(default_factory=dict)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("maxmig.script"))


@dataclass
class Script:
    path: Path | None = None
    hooks: dict[str, Callable[..., Any]] = field(default_factory=dict)
    functions: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def has(self, hook: str) -> bool:
        return hook in self.hooks

    def call(self, hook: str, *args: Any) -> Any:
        fn = self.hooks.get(hook)
        return None if fn is None else fn(*args)

    def run_level(self, hook: str, *args: Any) -> None:
        """before_run / after_run: a failure here aborts with the script's name."""
        try:
            self.call(hook, *args)
        except Exception as exc:
            raise ScriptError(f"{self.path or 'script'}: {hook} failed: {exc}") from exc


NO_SCRIPT = Script()


def load_script(path: Path) -> Script:
    path = Path(path)
    if not path.is_file():
        raise ScriptError(f"script not found: {path}")
    module_name = f"maxmig_script_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ScriptError(f"not a loadable Python file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # so dataclasses/pickling inside the script work
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ScriptError(f"{path}: failed to load: {exc}") from exc

    script = Script(path=path)
    for hook in HOOK_NAMES:
        fn = getattr(module, hook, None)
        if fn is not None:
            if not callable(fn):
                raise ScriptError(f"{path}: {hook} must be a function")
            script.hooks[hook] = fn
    for value in vars(module).values():
        name = getattr(value, _FORMULA_ATTR, None)
        if name and callable(value):
            script.functions[name] = value
    extra = getattr(module, "FUNCTIONS", None)  # plain-dict alternative to @formula
    if isinstance(extra, dict):
        script.functions.update({str(k).upper(): v for k, v in extra.items()})
    return script
