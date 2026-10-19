"""MxNode — port of MxLoader's MxNode.cls / MxNodeAttr.cls.

One node per Maximo object in a payload: its attributes (name, raw cell value,
Maximo type) plus named child arrays. `to_payload()` is MxNode.ToJson, except
it builds a dict for `requests` to serialize instead of hand-concatenating a
JSON string, so escaping can't go wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maxmig.mxl.values import to_json_value


@dataclass
class MxAttr:
    name: str
    value: Any
    maxtype: str | None = None


@dataclass
class MxNode:
    name: str
    attrs: list[MxAttr] = field(default_factory=list)
    # Relation name (lowercase, as it appears in the OSLC JSON) -> child objects.
    children: dict[str, list["MxNode"]] = field(default_factory=dict)
    # Sheet rows (1-based) this object was parsed from — every row, so results
    # and returned IDs can be written to all of them.
    rows: list[int] = field(default_factory=list)
    action: str | None = None

    def add_attr(self, name: str, value: Any, maxtype: str | None = None) -> None:
        self.attrs.append(MxAttr(name.lower(), value, maxtype))

    def same_values(self, other: "MxNode") -> bool:
        """MxNode.Equals: same object name and identical attribute values, in
        order. This is the whole of MxLoader's parent-grouping rule —
        consecutive rows whose parent cells are identical are one parent."""
        if self.name != other.name or len(self.attrs) != len(other.attrs):
            return False
        return all(a.name == b.name and a.value == b.value for a, b in zip(self.attrs, other.attrs))

    def find_attr(self, name: str) -> Any:
        name = name.lower()
        return next((a.value for a in self.attrs if a.name == name), None)

    def child_list(self, relation: str) -> list["MxNode"]:
        return self.children.setdefault(relation.lower(), [])

    def to_payload(self, base_dir: Path | None = None, timezone: str | None = None) -> dict[str, Any]:
        """JSON body for one object (and, recursively, its children), with
        `_action` first when set — as MxNode.ToJson writes it."""
        body: dict[str, Any] = {}
        if self.action:
            body["_action"] = self.action
        for attr in self.attrs:
            body[attr.name] = to_json_value(attr.name, attr.value, attr.maxtype, base_dir, timezone)
        for relation, nodes in self.children.items():
            if nodes:
                body[relation] = [node.to_payload(base_dir, timezone) for node in nodes]
        return body
