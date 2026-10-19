"""Value typing — port of MxLoader's `AdjustVal` (MxUtils.bas) and
`MxNodeAttr.ToJson`.

A sheet/CSV cell is always text. Whether it goes to Maximo as a JSON number,
boolean, ISO timestamp or string depends on the attribute's Maximo type
(MAXATTRIBUTE.MAXTYPE), looked up in the data dictionary
(`maxmig.mxl.datadict`). That lookup is what lets custom attributes load
correctly: a user-defined `ex_cost AMOUNT` column is sent as a number exactly
like OOB `unitcost`, as long as the dictionary (or a YAML `field_types`
override) knows its type. An attribute of unknown type is sent as a string,
which Maximo itself coerces for ALN-family types.

Special cell values, same tokens as MxLoader:

- `~NULL~`        — explicitly blank the attribute (an empty cell means "leave as is").
- `FileTxt:/path` — inline a UTF-8 text file's content (e.g. a long description).
- `FileEnc:/path` — inline a binary file, base64-encoded (e.g. an image/doclink).
  A path starting with `.` is relative to the sheet's own directory.
"""

from __future__ import annotations

import base64
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

NULL_TOKEN = "~NULL~"
FILE_TXT = "FileTxt:/"
FILE_ENC = "FileEnc:/"

INTEGER_TYPES = frozenset({"BIGINT", "INTEGER", "SMALLINT"})
DECIMAL_TYPES = frozenset({"DECIMAL", "FLOAT", "AMOUNT", "DURATION"})
DATE_TYPES = frozenset({"DATE", "DATETIME", "TIME"})

_YORN_TRUE = frozenset({"1", "TRUE", "Y", "YES"})
_YORN_FALSE = frozenset({"0", "FALSE", "N", "NO"})

# Accepted inbound date/time spellings, tried in order. ISO first (what the
# API itself returns on export, so a round-trip never reformats anything),
# then the forms people actually type into a spreadsheet.
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)
_TIME_FORMATS = ("%H:%M:%S", "%H:%M")
_TZ_SUFFIX = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


class ValueConversionError(ValueError):
    """A cell can't be converted to its attribute's Maximo type."""


def _to_number(text: str, maxtype: str, name: str) -> int | float:
    # MxLoader: Replace(val, ",", ".") — tolerate a European decimal comma.
    candidate = text.strip().replace(",", ".") if text.count(",") == 1 and "." not in text else text.strip()
    try:
        number = Decimal(candidate)
    except InvalidOperation:
        raise ValueConversionError(f"{name.upper()} ({maxtype}) must be numeric, got {text!r}") from None
    if maxtype in INTEGER_TYPES:
        if number != number.to_integral_value():
            raise ValueConversionError(f"{name.upper()} ({maxtype}) must be a whole number, got {text!r}")
        return int(number)
    return int(number) if number == number.to_integral_value() and "." not in candidate else float(number)


def _to_bool(text: str, name: str) -> bool:
    upper = text.strip().upper()
    if upper in _YORN_TRUE:
        return True
    if upper in _YORN_FALSE:
        return False
    # Same rule (and message shape) as MxNodeAttr.ToJson.
    raise ValueConversionError(
        f"YORN attribute {name.upper()} must have one of the following values: 0, 1, FALSE, TRUE (got {text!r})"
    )


def _parse_datetime(text: str, name: str, maxtype: str) -> tuple[datetime, str]:
    raw = text.strip()
    tz = ""
    match = _TZ_SUFFIX.search(raw)
    if match and "T" in raw:
        tz = match.group(1)
        raw = raw[: match.start()]
    raw = raw.split(".")[0]  # drop fractional seconds
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt), tz
        except ValueError:
            continue
    raise ValueConversionError(f"Cannot convert {text!r} to date format for {name.upper()} ({maxtype})")


def format_date(value: Any, maxtype: str, name: str = "", timezone: str | None = None) -> str:
    """Normalize to the ISO shapes MxLoader's AdjustVal produces:
    DATETIME `yyyy-mm-ddThh:mm:ss[tz]`, DATE `yyyy-mm-ddT00:00:00`, TIME
    `1970-01-01Thh:mm:ss`. A timezone already present on the input wins over
    `timezone` (the sheet-level default, e.g. "+02:00")."""
    if isinstance(value, datetime):
        parsed, tz = value, ""
    elif isinstance(value, date):
        parsed, tz = datetime.combine(value, time()), ""
    elif isinstance(value, time):
        parsed, tz = datetime.combine(date(1970, 1, 1), value), ""
    elif maxtype == "TIME":
        parsed, tz = None, ""
        for fmt in _TIME_FORMATS:
            try:
                parsed = datetime.combine(date(1970, 1, 1), datetime.strptime(str(value).strip(), fmt).time())
                break
            except ValueError:
                continue
        if parsed is None:
            parsed, tz = _parse_datetime(str(value), name, maxtype)
    else:
        parsed, tz = _parse_datetime(str(value), name, maxtype)

    if maxtype == "DATE":
        return parsed.strftime("%Y-%m-%dT00:00:00")
    if maxtype == "TIME":
        return "1970-01-01T" + parsed.strftime("%H:%M:%S")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S") + (tz or timezone or "")


def _resolve_file(tagged: str, prefix: str, base_dir: Path | None) -> Path:
    relative = tagged[len(prefix):]
    path = Path(relative)
    if relative.startswith(".") and base_dir is not None:
        path = base_dir / relative
    if not path.is_file():
        raise ValueConversionError(f"file referenced by {tagged!r} not found at {path}")
    return path


def to_json_value(
    name: str,
    value: Any,
    maxtype: str | None,
    base_dir: Path | None = None,
    timezone: str | None = None,
) -> Any:
    """Convert one cell to the JSON value sent to Maximo. Callers skip empty
    cells entirely (an empty cell never reaches here) — MxLoader semantics:
    blank means "don't touch", `~NULL~` means "set to blank"."""
    if isinstance(value, bool):
        value = "1" if value else "0"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and (maxtype or "") not in DATE_TYPES:
        value = repr(value) if isinstance(value, float) else str(value)

    if value == NULL_TOKEN:
        return ""

    mtype = (maxtype or "").upper()
    if mtype in INTEGER_TYPES or mtype in DECIMAL_TYPES:
        return _to_number(str(value), mtype, name)
    if mtype == "YORN":
        return _to_bool(str(value), name)
    if mtype in DATE_TYPES:
        return format_date(value, mtype, name, timezone)

    text = str(value)
    if text.startswith(FILE_TXT):
        return _resolve_file(text, FILE_TXT, base_dir).read_text(encoding="utf-8")
    if text.startswith(FILE_ENC):
        return base64.b64encode(_resolve_file(text, FILE_ENC, base_dir).read_bytes()).decode("ascii")
    return text


def is_blank(value: Any) -> bool:
    """MxLoader's "skip this attribute" test — None or an empty/whitespace cell."""
    return value is None or (isinstance(value, str) and value.strip() == "")
