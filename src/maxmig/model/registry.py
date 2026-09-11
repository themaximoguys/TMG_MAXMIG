"""Loads module YAML files into an in-memory registry of object definitions."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from maxmig.model.schema import ModuleDef, ObjectDef, load_module_yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_MODULE_DIRS: tuple[Path, ...] = (
    _PROJECT_ROOT / "config" / "modules",
    _PROJECT_ROOT / "config" / "custom_objects",
)


def _is_active_module_file(path: Path) -> bool:
    """A leading underscore marks a skeleton/scratch file (e.g. _template.yaml) — not an active module."""
    return not path.name.startswith("_")


class ModuleLoadError(Exception):
    """A module YAML file exists but could not be read or validated."""

    def __init__(self, path: Path, original_exception: Exception) -> None:
        self.path = Path(path)
        self.original_exception = original_exception
        super().__init__(
            f"failed to load module definition {self.path}: {original_exception}"
        )


class ObjectRegistry:
    """Module definitions loaded from one or more config directories, keyed by module and object."""

    def __init__(self, directories: Iterable[Path] | None = None) -> None:
        self.directories: tuple[Path, ...] = tuple(
            Path(d) for d in (DEFAULT_MODULE_DIRS if directories is None else directories)
        )
        self._modules: dict[str, ModuleDef] = {}
        self._sources: dict[str, Path] = {}
        self._load()

    def _load(self) -> None:
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.yaml")):
                if _is_active_module_file(path):
                    self._load_file(path)

    def _load_file(self, path: Path) -> None:
        try:
            module = load_module_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise ModuleLoadError(path, exc) from exc

        if module.module in self._modules:
            raise ModuleLoadError(
                path,
                ValueError(
                    f"module '{module.module}' is already defined by {self._sources[module.module]}"
                ),
            )

        self._modules[module.module] = module
        self._sources[module.module] = path

    def list_modules(self) -> list[str]:
        return sorted(self._modules)

    def list_objects(self, module: str) -> list[str]:
        return self.get_module(module).object_names()

    def get_module(self, module: str) -> ModuleDef:
        try:
            return self._modules[module]
        except KeyError:
            raise KeyError(
                f"unknown module '{module}'; loaded modules: "
                f"{', '.join(self.list_modules()) or '(none)'}"
            ) from None

    def get_object(self, module: str, name: str) -> ObjectDef:
        module_def = self.get_module(module)
        for obj in module_def.objects:
            if obj.name == name:
                return obj
        raise KeyError(
            f"unknown object '{name}' in module '{module}'; objects in that module: "
            f"{', '.join(module_def.object_names()) or '(none)'}"
        )
