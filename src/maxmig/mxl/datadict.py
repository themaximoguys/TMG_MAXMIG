"""Maximo data dictionary — port of MxLoader's `MxAttrCfg` sheet.

MxLoader keeps a table of (OBJECTNAME, ATTRIBUTENAME, MAXTYPE, SCALE) rows,
refreshed from the server's MAXATTRIBUTE table via a small custom object
structure (MXL_MAXATTRIBUTECFG), and uses it to type every cell before
sending it (see `maxmig.mxl.values`). This module does the same, with three
sources merged in priority order:

1. YAML `field_types` overrides (per object/relation) — highest priority, and
   the way to type a custom attribute when no live dictionary is at hand.
2. A dictionary CSV (`OBJECTNAME,ATTRIBUTENAME,MAXTYPE,SCALE[,USERDEFINED]` —
   MxLoader's MxAttrCfg column layout), typically cached from (3).
3. A live pull from MAXATTRIBUTE through any object structure that exposes it.

Because the live pull reads MAXATTRIBUTE itself, user-defined attributes added
through Database Configuration come back exactly like OOB ones — custom
attributes need no special handling once the dictionary is refreshed. The
USERDEFINED flag, when the object structure exposes it, is kept so callers
(`migrate discover`) can tell custom attributes apart.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests

from maxmig.api.oslc import iter_oslc

# MxLoader's own name for the object structure over MAXATTRIBUTE, created by
# its "Create Custom Object Structure" ribbon button — reused as the default
# so an environment already prepared for MxLoader works here unchanged.
DEFAULT_DICTIONARY_OS = "MXL_MAXATTRIBUTECFG"
CSV_COLUMNS = ["OBJECTNAME", "ATTRIBUTENAME", "MAXTYPE", "SCALE", "USERDEFINED"]


@dataclass(frozen=True)
class AttrType:
    maxtype: str
    scale: int = 0
    user_defined: bool = False


@dataclass
class DataDictionary:
    entries: dict[tuple[str, str], AttrType] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.entries)

    def add(self, obj: str, attr: str, maxtype: str, scale: int = 0, user_defined: bool = False) -> None:
        self.entries[(obj.upper(), attr.upper())] = AttrType(maxtype.upper(), int(scale or 0), bool(user_defined))

    def lookup(self, obj: str | None, attr: str) -> AttrType | None:
        if not obj:
            return None
        return self.entries.get((obj.upper(), attr.upper()))

    def type_of(self, obj: str | None, attr: str) -> str | None:
        found = self.lookup(obj, attr)
        return found.maxtype if found else None

    def custom_attributes(self, obj: str) -> list[str]:
        """Lowercased user-defined attribute names for `obj`, sorted."""
        obj = obj.upper()
        return sorted(a.lower() for (o, a), t in self.entries.items() if o == obj and t.user_defined)

    def merge(self, other: "DataDictionary") -> "DataDictionary":
        """New dictionary: self's entries, overridden by `other`'s."""
        return DataDictionary({**self.entries, **other.entries})

    @classmethod
    def from_csv(cls, path: Path) -> "DataDictionary":
        """Read an MxAttrCfg-style CSV. Header names are matched
        case-insensitively; SCALE and USERDEFINED are optional."""
        result = cls()
        with Path(path).open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                row = {(k or "").strip().upper(): (v or "").strip() for k, v in raw.items()}
                if not row.get("OBJECTNAME") or not row.get("ATTRIBUTENAME") or not row.get("MAXTYPE"):
                    continue
                result.add(
                    row["OBJECTNAME"],
                    row["ATTRIBUTENAME"],
                    row["MAXTYPE"],
                    int(row.get("SCALE") or 0),
                    row.get("USERDEFINED", "").upper() in ("1", "TRUE", "Y"),
                )
        return result

    def to_csv(self, path: Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            for (obj, attr), t in sorted(self.entries.items()):
                writer.writerow([obj, attr, t.maxtype, t.scale, 1 if t.user_defined else 0])
        return len(self.entries)


def _first(row: dict, *names: str):
    lowered = {k.lower(): v for k, v in row.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


# Out-of-the-box object structure over MAXOBJECT with MAXATTRIBUTE as a child —
# present on stock Manage (confirmed on MAXDEMO), so no MxLoader-created OS is needed.
OOB_DICTIONARY_OS = "MXAPIMAXOBJECT"
_NESTED_FIELDS = ("attributename", "maxtype", "scale", "userdefined")


def fetch_from_maximo(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    objects: Iterable[str] | None = None,
    resource: str = DEFAULT_DICTIONARY_OS,
    page_size: int = 1000,
) -> DataDictionary:
    """Pull MAXATTRIBUTE rows (optionally only for `objects`) through
    `resource`. All attribute types are fetched — not only the numeric/date
    ones MxLoader's own query filters to — so USERDEFINED is known for every
    custom attribute, string-typed ones included."""
    where = None
    names = sorted({o.upper() for o in objects or [] if o})
    if names:
        where = "objectname in [" + ",".join(f'"{n}"' for n in names) + "]"
    nested = resource.upper() == OOB_DICTIONARY_OS
    select = f"objectname,maxattribute{{{','.join(_NESTED_FIELDS)}}}" if nested else "*"
    result = DataDictionary()
    for row in iter_oslc(session, base_url, headers, resource, page_size=page_size, where=where, select=select):
        # MXAPIMAXOBJECT nests the attributes under each object; MxLoader's own
        # MXL_MAXATTRIBUTECFG returns one flat MAXATTRIBUTE row per attribute.
        children = row.get("maxattribute")
        attr_rows = [dict(c, objectname=_first(row, "objectname")) for c in children] if isinstance(children, list) else [row]
        for attr_row in attr_rows:
            obj, attr, maxtype = _first(attr_row, "objectname"), _first(attr_row, "attributename"), _first(attr_row, "maxtype")
            if obj and attr and maxtype:
                user_defined = _first(attr_row, "userdefined")
                result.add(obj, attr, maxtype, _first(attr_row, "scale") or 0, user_defined in (True, 1, "1", "true", "Y"))
    return result
