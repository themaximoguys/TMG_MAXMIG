"""Pydantic models for module YAML files under config/modules/ and config/custom_objects/."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ApiMode = Literal["oslc", "rest_mbo"]

# Maximo MAXATTRIBUTE.MAXTYPE values a `field_types` override may name. Mirrors
# the data types MxLoader's AdjustVal/MxNodeAttr.ToJson branch on (numbers sent
# unquoted, YORN as a boolean, dates normalized) plus the string-ish types that
# are sent verbatim — see maxmig.mxl.values.
MAXTYPES = frozenset(
    {
        "ALN", "UPPER", "LOWER", "LONGALN", "CLOB", "GL", "CRYPTO", "CRYPTOX", "BLOB",
        "BIGINT", "INTEGER", "SMALLINT", "DECIMAL", "FLOAT", "AMOUNT", "DURATION",
        "DATE", "DATETIME", "TIME", "YORN",
    }
)

# extra="forbid" turns a typo'd YAML key ("key_field:") into a named error instead of a silent default.
_STRICT = ConfigDict(extra="forbid")


class AccessDef(BaseModel):
    """How one object is reached: OSLC resource, REST MBO table, or both with a preference."""

    model_config = _STRICT

    oslc_resource: str | None = None
    rest_mbo_table: str | None = None
    prefer: ApiMode

    @model_validator(mode="after")
    def _check_endpoints(self) -> AccessDef:
        if self.oslc_resource is None and self.rest_mbo_table is None:
            raise ValueError(
                "access requires at least one of oslc_resource or rest_mbo_table to be set"
            )
        if self.prefer == "oslc" and self.oslc_resource is None:
            raise ValueError("access.prefer is 'oslc' but oslc_resource is not set")
        if self.prefer == "rest_mbo" and self.rest_mbo_table is None:
            raise ValueError("access.prefer is 'rest_mbo' but rest_mbo_table is not set")
        return self


def _validate_custom_fields(fields: list[str], custom_fields: list[str], field_types: dict[str, str]) -> None:
    overlap = sorted(set(fields) & set(custom_fields))
    if overlap:
        raise ValueError(
            f"attribute(s) listed in both fields and custom_fields: {', '.join(overlap)} — list each once"
        )
    bad = {name: t for name, t in field_types.items() if t.upper() not in MAXTYPES}
    if bad:
        raise ValueError(
            f"unknown Maximo type(s) in field_types: {bad}; expected one of {', '.join(sorted(MAXTYPES))}"
        )
    for name in list(field_types):
        field_types[name] = field_types[name].upper()


class _AttributeSet(BaseModel):
    """Attribute-list options shared by objects and child relations."""

    model_config = _STRICT

    fields: list[str]
    # Custom (user-defined, non-OOB) attributes — e.g. `ex_criticality` added via
    # Database Configuration. Kept apart from `fields` so a module YAML documents
    # which columns are client-specific; written as CSV columns right after
    # `fields`, exported and imported exactly like any other attribute.
    custom_fields: list[str] = Field(default_factory=list)
    # Per-attribute Maximo type overrides (MAXTYPE, e.g. {ex_cost: AMOUNT}) for
    # attributes the data dictionary doesn't know — typically custom ones when no
    # live dictionary is available. Drives JSON typing on load, see maxmig.mxl.values.
    field_types: dict[str, str] = Field(default_factory=dict)
    # Export every attribute the API returns, not just the declared ones — new
    # columns (custom attributes nobody listed yet) are appended after the
    # declared ones, sorted by name.
    capture_all_attributes: bool = False

    @model_validator(mode="after")
    def _check_custom_fields(self):
        _validate_custom_fields(self.fields, self.custom_fields, self.field_types)
        return self

    def all_fields(self) -> list[str]:
        """Declared standard fields followed by declared custom fields."""
        return self.fields + [f for f in self.custom_fields if f not in self.fields]


class RelationDef(_AttributeSet):
    """A child object reached through a parent's relation attribute."""

    model_config = _STRICT

    name: str
    maximo_object: str | None = None
    relation_attribute: str
    foreign_key: str | list[str]
    key_fields: list[str] = Field(min_length=1)
    # Key fields that may legitimately be blank (e.g. lotnum for non-lotted items):
    # still part of the key, but a blank value isn't an integrity error.
    optional_key_fields: list[str] = Field(default_factory=list)
    children: list[RelationDef] = Field(default_factory=list)



# Self-referential children only resolve once the class exists in the module namespace.
RelationDef.model_rebuild()


class ObjectDef(_AttributeSet):
    """A top-level Maximo object structure, extracted and loaded as one CSV plus one per child."""

    model_config = _STRICT

    name: str
    maximo_object: str | None = None
    access: AccessDef
    key_fields: list[str] = Field(min_length=1)
    # Key fields that may legitimately be blank (e.g. lotnum for non-lotted items):
    # still part of the key, but a blank value isn't an integrity error.
    optional_key_fields: list[str] = Field(default_factory=list)
    where: str | None = None
    children: list[RelationDef] = Field(default_factory=list)



class ModuleDef(BaseModel):
    """One module YAML file: a named set of object structures plus their extract/load ordering."""

    model_config = _STRICT

    module: str
    description: str | None = None
    objects: list[ObjectDef] = Field(min_length=1)
    extract_order: list[str] = Field(default_factory=list)
    load_order: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_orders_reference_known_objects(self) -> ModuleDef:
        names = [obj.name for obj in self.objects]

        counts = Counter(names)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(
                f"module '{self.module}' declares duplicate object names: {', '.join(duplicates)}"
            )

        known = set(names)

        def _check_order(order: list[str], field_name: str) -> None:
            for entry in order:
                if entry not in known:
                    raise ValueError(
                        f"{field_name} references unknown object '{entry}' in module "
                        f"'{self.module}'; known objects: {', '.join(sorted(known))}"
                    )

        _check_order(self.extract_order, "extract_order")
        _check_order(self.load_order, "load_order")
        return self

    def object_names(self) -> list[str]:
        return [obj.name for obj in self.objects]


def with_extra_fields(obj_def: ObjectDef, specs: list[str]) -> ObjectDef:
    """Return a copy of `obj_def` with run-time custom attributes added — the
    `--extra-field` CLI escape hatch for a custom attribute nobody has put in
    the YAML yet. `attr` targets the object itself; `relation.attr` (relation
    *names*, dotted to any depth, e.g. `po_line.ex_field`) targets a child.
    Attributes already declared are left alone rather than duplicated."""
    if not specs:
        return obj_def
    updated = obj_def.model_copy(deep=True)
    for spec in specs:
        *path, attr = spec.strip().lower().split(".")
        if not attr:
            raise ValueError(f"malformed extra field {spec!r}; expected attr or relation.attr")
        node = updated
        for rel_name in path:
            match = next((child for child in node.children if child.name == rel_name), None)
            if match is None:
                known = ", ".join(child.name for child in node.children) or "(none)"
                raise ValueError(f"extra field {spec!r}: no relation named '{rel_name}' (known: {known})")
            node = match
        if attr not in node.all_fields() and attr not in node.key_fields:
            node.custom_fields.append(attr)
    return updated


def with_capture_all(node):
    """Copy of an object/relation tree with `capture_all_attributes` set on
    every node — the `--all-attributes` export flag."""
    return node.model_copy(
        update={"capture_all_attributes": True, "children": [with_capture_all(child) for child in node.children]}
    )


def load_module_yaml(path: Path) -> ModuleDef:
    """Read and validate one module YAML file, letting pydantic's ValidationError propagate."""
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(
            f"expected a YAML mapping at the top level of {path}, got {type(raw).__name__}"
        )

    return ModuleDef.model_validate(raw)
