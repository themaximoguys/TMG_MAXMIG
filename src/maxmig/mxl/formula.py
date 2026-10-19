"""Excel-style formulas — the transformation layer MxLoader gets from Excel.

MxLoader has no transformation engine of its own: users keep reference data
in one worksheet, write Excel formulas in the MxLoader sheet's first data row
(`=CONCATENATE("PUMP", RIGHT('RefData'!A2,3))`, `=PROPER(...)`, `VLOOKUP`…),
and the *FillData* macro drags that row down once per source row. This module
is the headless equivalent: the same formula syntax, evaluated per source row
without Excel.

Syntax (a practical subset of Excel's):

- literals: `"text"` (`""` escapes a quote), `12`, `3.5`, `TRUE`, `FALSE`
- source columns: `[Asset Name]` (any header text), or a bare name when the
  header is a plain identifier (`Asset`, `site_id`); case-insensitive
- operators: `&` (concatenate), `+ - * /`, unary `-`, `= <> < > <= >=`, `( )`
- function calls: `NAME(arg, ...)` — see `FUNCTIONS`, plus any user-defined
  function (UDF) registered from a script (see `maxmig.mxl.scripting`)

Formulas are parsed once into a tree and evaluated per row, and nothing is
ever passed to Python's `eval` — a mapping file can come from anyone.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping


class FormulaError(ValueError):
    """A formula that can't be parsed, or fails on a particular row."""


# --------------------------------------------------------------------------- tokens

_TOKEN = re.compile(
    r"""
    \s*(?:
      (?P<str>"(?:[^"]|"")*")
    | (?P<num>\d+(?:\.\d*)?|\.\d+)
    | (?P<col>\[[^\]]*\])
    | (?P<op><>|<=|>=|[-+*/&=<>(),])
    | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
    )""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: Any
    pos: int


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FormulaError(f"unexpected character {text[pos:].strip()[:1]!r} at position {pos + 1}")
        kind = m.lastgroup
        raw = m.group(kind)
        if kind == "str":
            value: Any = raw[1:-1].replace('""', '"')
        elif kind == "num":
            value = float(raw) if "." in raw else int(raw)
        elif kind == "col":
            value = raw[1:-1].strip()
        else:
            value = raw
        tokens.append(_Tok(kind, value, m.start(kind)))
        pos = m.end()
    tokens.append(_Tok("end", None, len(text)))
    return tokens


# --------------------------------------------------------------------------- tree


@dataclass(frozen=True)
class _Lit:
    value: Any


@dataclass(frozen=True)
class _Col:
    name: str


@dataclass(frozen=True)
class _Call:
    name: str
    args: tuple


@dataclass(frozen=True)
class _BinOp:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class _Neg:
    operand: Any


# Lowest to highest, as in Excel: comparison < concatenation < additive < multiplicative.
_PRECEDENCE = {"=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1, "&": 2, "+": 3, "-": 3, "*": 4, "/": 4}


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _tokenize(text)
        self.i = 0

    def peek(self) -> _Tok:
        return self.tokens[self.i]

    def take(self) -> _Tok:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, value: str) -> None:
        tok = self.take()
        if tok.value != value or tok.kind != "op":
            raise FormulaError(f"expected {value!r} at position {tok.pos + 1}")

    def parse(self) -> Any:
        tree = self.expression(0)
        tok = self.peek()
        if tok.kind != "end":
            raise FormulaError(f"unexpected {tok.value!r} at position {tok.pos + 1}")
        return tree

    def expression(self, min_prec: int) -> Any:
        left = self.unary()
        while True:
            tok = self.peek()
            prec = _PRECEDENCE.get(tok.value) if tok.kind == "op" else None
            if prec is None or prec < min_prec:
                return left
            self.take()
            right = self.expression(prec + 1)  # left-associative
            left = _BinOp(tok.value, left, right)

    def unary(self) -> Any:
        tok = self.peek()
        if tok.kind == "op" and tok.value in ("-", "+"):
            self.take()
            operand = self.unary()
            return _Neg(operand) if tok.value == "-" else operand
        return self.primary()

    def primary(self) -> Any:
        tok = self.take()
        if tok.kind in ("str", "num"):
            return _Lit(tok.value)
        if tok.kind == "col":
            return _Col(tok.value)
        if tok.kind == "op" and tok.value == "(":
            inner = self.expression(0)
            self.expect(")")
            return inner
        if tok.kind == "name":
            upper = tok.value.upper()
            nxt = self.peek()
            if nxt.kind == "op" and nxt.value == "(":
                self.take()
                args: list[Any] = []
                if not (self.peek().kind == "op" and self.peek().value == ")"):
                    while True:
                        args.append(self.expression(0))
                        sep = self.take()
                        if sep.kind == "op" and sep.value == ")":
                            break
                        if not (sep.kind == "op" and sep.value == ","):
                            raise FormulaError(f"expected ',' or ')' at position {sep.pos + 1}")
                else:
                    self.take()
                return _Call(upper, tuple(args))
            if upper in ("TRUE", "FALSE"):
                return _Lit(upper == "TRUE")
            return _Col(tok.value)
        raise FormulaError(f"unexpected {tok.value!r} at position {tok.pos + 1}" if tok.kind != "end" else "formula ends too early")


# --------------------------------------------------------------------------- coercion helpers


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _number(value: Any) -> float | int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    text = _text(value).strip().replace(",", "")
    if text == "":
        return 0
    try:
        return int(text) if re.fullmatch(r"[-+]?\d+", text) else float(text)
    except ValueError:
        raise FormulaError(f"#VALUE! {value!r} is not a number") from None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = _text(value).strip().upper()
    if text in ("TRUE", "Y", "YES", "1"):
        return True
    if text in ("FALSE", "N", "NO", "0", ""):
        return False
    raise FormulaError(f"#VALUE! {value!r} is not TRUE/FALSE")


def _compare(op: str, left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        a, b = _number(left), _number(right)
    else:
        a, b = _text(left).upper(), _text(right).upper()  # Excel text comparison is case-insensitive
    return {
        "=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b,
    }[op]


def _tidy(value: float | int) -> float | int:
    return int(value) if isinstance(value, float) and value.is_integer() else value


# --------------------------------------------------------------------------- built-in functions


def _proper(text: Any) -> str:
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), _text(text))


def _mid(text: Any, start: Any, length: Any) -> str:
    s, n = int(_number(start)), int(_number(length))
    if s < 1 or n < 0:
        raise FormulaError("#VALUE! MID start must be >= 1 and length >= 0")
    return _text(text)[s - 1 : s - 1 + n]


def _replace(text: Any, start: Any, length: Any, new: Any) -> str:
    t, s, n = _text(text), int(_number(start)), int(_number(length))
    if s < 1:
        raise FormulaError("#VALUE! REPLACE start must be >= 1")
    return t[: s - 1] + _text(new) + t[s - 1 + n :]


def _substitute(text: Any, old: Any, new: Any, instance: Any = None) -> str:
    t, o, n = _text(text), _text(old), _text(new)
    if not o:
        return t
    if instance is None:
        return t.replace(o, n)
    which = int(_number(instance))
    pos = -1
    for _ in range(which):
        pos = t.find(o, pos + 1)
        if pos < 0:
            return t
    return t[:pos] + n + t[pos + len(o) :]


def _round(value: Any, digits: Any = 0) -> float | int:
    d = int(_number(digits))
    factor = 10**d
    v = _number(value) * factor
    rounded = math.floor(abs(v) + 0.5) * (1 if v >= 0 else -1)  # Excel rounds half away from zero
    return _tidy(rounded / factor)


def _text_fmt(value: Any, fmt: Any) -> str:
    """TEXT(value, format) for the formats migration sheets actually use:
    zero-padding ("00000"), fixed decimals ("0.00"), and dates ("yyyy-mm-dd")."""
    f = _text(fmt)
    if re.fullmatch(r"0+(\.0+)?", f):
        decimals = len(f.split(".")[1]) if "." in f else 0
        width = len(f.split(".")[0])
        n = _number(value)
        body = f"{n:.{decimals}f}"
        int_part, _, frac = body.lstrip("-").partition(".")
        return ("-" if n < 0 else "") + int_part.zfill(width) + ("." + frac if frac else "")
    parsed = value if isinstance(value, (date, datetime)) else _parse_date(value)
    py = (
        f.replace("yyyy", "%Y").replace("yy", "%y").replace("mm", "%m").replace("dd", "%d")
        .replace("hh", "%H").replace("nn", "%M").replace("ss", "%S")
    )
    return parsed.strftime(py)


_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%d.%m.%Y", "%Y%m%d")


def _parse_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = _text(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise FormulaError(f"#VALUE! {value!r} is not a recognised date")


def _datevalue(value: Any) -> str:
    return _parse_date(value).strftime("%Y-%m-%d")


def _concat(*args: Any) -> str:
    return "".join(_text(a) for a in args)


def _textjoin(sep: Any, ignore_empty: Any, *args: Any) -> str:
    parts = [_text(a) for a in args]
    if _truthy(ignore_empty):
        parts = [p for p in parts if p != ""]
    return _text(sep).join(parts)


def _find(needle: Any, haystack: Any, start: Any = 1) -> int:
    pos = _text(haystack).find(_text(needle), int(_number(start)) - 1)
    if pos < 0:
        raise FormulaError(f"#VALUE! {needle!r} not found")
    return pos + 1


def _search(needle: Any, haystack: Any, start: Any = 1) -> int:
    return _find(_text(needle).upper(), _text(haystack).upper(), start)


def _left(text: Any, n: Any = 1) -> str:
    return _text(text)[: max(int(_number(n)), 0)]


def _right(text: Any, n: Any = 1) -> str:
    count = max(int(_number(n)), 0)
    return _text(text)[-count:] if count else ""


FUNCTIONS: dict[str, Callable[..., Any]] = {
    # text
    "CONCATENATE": _concat,
    "CONCAT": _concat,
    "TEXTJOIN": _textjoin,
    "LEFT": _left,
    "RIGHT": _right,
    "MID": _mid,
    "LEN": lambda t: len(_text(t)),
    "UPPER": lambda t: _text(t).upper(),
    "LOWER": lambda t: _text(t).lower(),
    "PROPER": _proper,
    "TRIM": lambda t: " ".join(_text(t).split()),
    "REPLACE": _replace,
    "SUBSTITUTE": _substitute,
    "REPT": lambda t, n: _text(t) * int(_number(n)),
    "FIND": _find,
    "SEARCH": _search,
    "EXACT": lambda a, b: _text(a) == _text(b),
    "TEXT": _text_fmt,
    "VALUE": lambda t: _tidy(_number(t)),
    # numbers
    "ROUND": _round,
    "INT": lambda v: math.floor(_number(v)),
    "ABS": lambda v: abs(_number(v)),
    "MAX": lambda *a: _tidy(max(_number(v) for v in a)),
    "MIN": lambda *a: _tidy(min(_number(v) for v in a)),
    "SUM": lambda *a: _tidy(sum(_number(v) for v in a)),
    # dates
    "TODAY": lambda: date.today().isoformat(),
    "NOW": lambda: datetime.now().replace(microsecond=0).isoformat(),
    "DATEVALUE": _datevalue,
    # logic / blanks
    "AND": lambda *a: all(_truthy(v) for v in a),
    "OR": lambda *a: any(_truthy(v) for v in a),
    "NOT": lambda v: not _truthy(v),
    "ISBLANK": lambda v: _text(v).strip() == "",
    "ISNUMBER": lambda v: _is_number(v),
    "COALESCE": lambda *a: next((v for v in a if _text(v).strip() != ""), ""),
    "NULL": lambda: "~NULL~",  # MxLoader's explicit-empty marker
}

# Lazily-evaluated: only the branch taken is evaluated, so IF(ISBLANK(x), "", VLOOKUP(x, ...))
# and IFERROR(VLOOKUP(...), "?") don't fail on the branch they're guarding against.
_LAZY = {"IF", "IFERROR", "IFS", "SWITCH"}
# Need the evaluation context (lookup tables) — not plain functions of their arguments.
_CONTEXTUAL = {"VLOOKUP", "XLOOKUP", "LOOKUP"}


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        _number(value)
    except FormulaError:
        return False
    return _text(value).strip() != ""


# --------------------------------------------------------------------------- lookup tables


@dataclass
class LookupTable:
    """A reference table for VLOOKUP — MxLoader users' 'RefData' worksheet.
    Column 1 is the key; keys match case-insensitively and trimmed."""

    name: str
    header: list[str]
    rows: list[list[Any]]

    def __post_init__(self) -> None:
        self._index: dict[str, list[Any]] = {}
        for row in self.rows:
            if row:
                self._index.setdefault(_text(row[0]).strip().upper(), row)

    def column_index(self, column: Any) -> int:
        if isinstance(column, (int, float)) and not isinstance(column, bool):
            index = int(column)
        else:
            wanted = _text(column).strip().upper()
            try:
                index = [h.strip().upper() for h in self.header].index(wanted) + 1
            except ValueError:
                if re.fullmatch(r"\d+", wanted):
                    index = int(wanted)
                else:
                    raise FormulaError(f"#REF! lookup table {self.name!r} has no column {column!r}") from None
        if not 1 <= index <= len(self.header):
            raise FormulaError(f"#REF! lookup table {self.name!r} has {len(self.header)} columns, not {index}")
        return index

    def find(self, key: Any, column: Any) -> Any:
        row = self._index.get(_text(key).strip().upper())
        if row is None:
            raise FormulaError(f"#N/A {key!r} not found in lookup table {self.name!r}")
        index = self.column_index(column)
        return row[index - 1] if index - 1 < len(row) else ""


# --------------------------------------------------------------------------- evaluation


@dataclass
class FormulaContext:
    functions: Mapping[str, Callable[..., Any]]
    lookups: Mapping[str, LookupTable]
    row: Mapping[str, Any]  # source column name (upper-cased) -> value


class Formula:
    """A parsed formula. `Formula.parse("=PROPER([Description])")`."""

    def __init__(self, source: str, tree: Any) -> None:
        self.source = source
        self.tree = tree

    @classmethod
    def parse(cls, text: str) -> "Formula":
        body = text.strip()
        if body.startswith("="):
            body = body[1:]
        try:
            return cls(text, _Parser(body).parse())
        except FormulaError as exc:
            raise FormulaError(f"{text!r}: {exc}") from None

    def columns(self) -> set[str]:
        """Source columns this formula reads (upper-cased)."""
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, _Col):
                found.add(node.name.upper())
            elif isinstance(node, _Call):
                for arg in node.args:
                    walk(arg)
            elif isinstance(node, _BinOp):
                walk(node.left)
                walk(node.right)
            elif isinstance(node, _Neg):
                walk(node.operand)

        walk(self.tree)
        return found

    def evaluate(self, ctx: FormulaContext) -> Any:
        return _eval(self.tree, ctx)


def _eval(node: Any, ctx: FormulaContext) -> Any:
    if isinstance(node, _Lit):
        return node.value
    if isinstance(node, _Col):
        key = node.name.upper()
        if key not in ctx.row:
            raise FormulaError(f"#NAME? no source column {node.name!r}")
        value = ctx.row[key]
        return "" if value is None else value
    if isinstance(node, _Neg):
        return _tidy(-_number(_eval(node.operand, ctx)))
    if isinstance(node, _BinOp):
        left, right = _eval(node.left, ctx), _eval(node.right, ctx)
        op = node.op
        if op == "&":
            return _text(left) + _text(right)
        if op in ("+", "-", "*", "/"):
            a, b = _number(left), _number(right)
            if op == "/" and b == 0:
                raise FormulaError("#DIV/0!")
            return _tidy({"+": a + b, "-": a - b, "*": a * b, "/": a / b if op == "/" else 0}[op])
        return _compare(op, left, right)
    if isinstance(node, _Call):
        return _call(node, ctx)
    raise FormulaError(f"cannot evaluate {node!r}")  # pragma: no cover


def _call(node: _Call, ctx: FormulaContext) -> Any:
    name, args = node.name, node.args
    if name in _LAZY:
        return _lazy(name, args, ctx)
    values = [_eval(a, ctx) for a in args]
    if name in _CONTEXTUAL:
        return _lookup(name, values, ctx)
    fn = ctx.functions.get(name)
    if fn is None:
        raise FormulaError(f"#NAME? unknown function {name}")
    try:
        return fn(*values)
    except FormulaError:
        raise
    except TypeError as exc:
        raise FormulaError(f"#VALUE! {name}: {exc}") from None
    except Exception as exc:  # a UDF failing is a row error, not a crash
        raise FormulaError(f"{name} failed: {exc}") from None


def _lazy(name: str, args: tuple, ctx: FormulaContext) -> Any:
    if name == "IF":
        if len(args) not in (2, 3):
            raise FormulaError("IF takes 2 or 3 arguments")
        if _truthy(_eval(args[0], ctx)):
            return _eval(args[1], ctx)
        return _eval(args[2], ctx) if len(args) == 3 else False
    if name == "IFERROR":
        if len(args) != 2:
            raise FormulaError("IFERROR takes 2 arguments")
        try:
            return _eval(args[0], ctx)
        except FormulaError:
            return _eval(args[1], ctx)
    if name == "IFS":
        if len(args) % 2:
            raise FormulaError("IFS takes condition/value pairs")
        for cond, value in zip(args[::2], args[1::2]):
            if _truthy(_eval(cond, ctx)):
                return _eval(value, ctx)
        raise FormulaError("#N/A no IFS condition was TRUE")
    # SWITCH(expr, match1, value1, ..., [default])
    if len(args) < 3:
        raise FormulaError("SWITCH takes an expression and at least one match/value pair")
    subject = _eval(args[0], ctx)
    rest = args[1:]
    for match, value in zip(rest[::2], rest[1::2]):
        if _compare("=", subject, _eval(match, ctx)):
            return _eval(value, ctx)
    if len(rest) % 2:
        return _eval(rest[-1], ctx)
    raise FormulaError(f"#N/A SWITCH has no match for {subject!r}")


def _lookup(name: str, values: list[Any], ctx: FormulaContext) -> Any:
    """VLOOKUP(key, "table", column[, exact]) — `column` is a 1-based index or
    a header name. Only exact matching is supported (approximate VLOOKUP on
    unsorted migration data is a classic source of silently wrong loads), so a
    TRUE 4th argument is rejected rather than ignored. LOOKUP/XLOOKUP are
    accepted as aliases with the same arguments."""
    if len(values) not in (3, 4):
        raise FormulaError(f"{name} takes (value, table, column[, FALSE])")
    if len(values) == 4 and _truthy(values[3]):
        raise FormulaError(f"{name}: only exact match (FALSE) is supported")
    table_name = _text(values[1]).strip()
    table = ctx.lookups.get(table_name.upper())
    if table is None:
        raise FormulaError(f"#REF! no lookup table {table_name!r} (have: {', '.join(sorted(ctx.lookups)) or 'none'})")
    return table.find(values[0], values[2])


def evaluate_cell(cell: Any, ctx: FormulaContext) -> Any:
    """A template cell: text starting with `=` is a formula, anything else is a
    constant copied to every row (MxLoader FillData's drag-down semantics)."""
    if isinstance(cell, Formula):
        return cell.evaluate(ctx)
    return cell
