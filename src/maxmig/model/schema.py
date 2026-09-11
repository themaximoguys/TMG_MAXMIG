"""Pydantic models for module YAML files under config/modules/ and config/custom_objects/."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ApiMode = Literal["oslc", "rest_mbo"]

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


class RelationDef(BaseModel):
    """A child object reached through a parent's relation attribute."""

    model_config = _STRICT

    name: str
    maximo_object: str | None = None
    relation_attribute: str
    foreign_key: str | list[str]
    key_fields: list[str] = Field(min_length=1)
    fields: list[str]
    children: list[RelationDef] = Field(default_factory=list)


# Self-referential children only resolve once the class exists in the module namespace.
RelationDef.model_rebuild()


class ObjectDef(BaseModel):
    """A top-level Maximo object structure, extracted and loaded as one CSV plus one per child."""

    model_config = _STRICT

    name: str
    maximo_object: str | None = None
    access: AccessDef
    key_fields: list[str] = Field(min_length=1)
    fields: list[str]
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
