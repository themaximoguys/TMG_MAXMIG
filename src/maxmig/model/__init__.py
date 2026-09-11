"""Object-definition layer: module YAML schema and the registry that loads it."""

from maxmig.model.registry import DEFAULT_MODULE_DIRS, ModuleLoadError, ObjectRegistry
from maxmig.model.schema import (
    AccessDef,
    ApiMode,
    ModuleDef,
    ObjectDef,
    RelationDef,
    load_module_yaml,
)

__all__ = [
    "DEFAULT_MODULE_DIRS",
    "AccessDef",
    "ApiMode",
    "ModuleDef",
    "ModuleLoadError",
    "ObjectDef",
    "ObjectRegistry",
    "RelationDef",
    "load_module_yaml",
]
