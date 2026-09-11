import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from maxmig.model.registry import ModuleLoadError, ObjectRegistry  # noqa: E402
from maxmig.model.schema import ModuleDef, ObjectDef  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "modules"

VALID_YAML = """\
module: alpha
objects:
  - name: thing
    maximo_object: THING
    access:
      oslc_resource: mxapithing
      prefer: oslc
    key_fields: [thingid]
    fields: [thingid, description]
    children: []
"""

BROKEN_YAML = """\
module: beta
objects:
  - name: busted
    maximo_object: BUSTED
    access:
      oslc_resource: mxapibusted
      prefer: oslc
    key_fields: []
    fields: [bustedid]
"""


@pytest.fixture
def registry() -> ObjectRegistry:
    return ObjectRegistry([FIXTURE_DIR])


def test_lists_modules_from_fixture_dir(registry: ObjectRegistry) -> None:
    assert registry.list_modules() == ["sample"]


def test_lists_objects_for_module(registry: ObjectRegistry) -> None:
    assert registry.list_objects("sample") == ["widget", "gadget"]


def test_get_object_returns_matching_object_def(registry: ObjectRegistry) -> None:
    widget = registry.get_object("sample", "widget")

    assert isinstance(widget, ObjectDef)
    assert widget.name == "widget"
    assert widget.maximo_object == "WIDGET"
    assert widget.access.prefer == "oslc"
    assert widget.children[0].name == "widget_line"


def test_get_object_unknown_name_raises_keyerror(registry: ObjectRegistry) -> None:
    with pytest.raises(KeyError) as excinfo:
        registry.get_object("sample", "nope")

    message = str(excinfo.value)
    assert "nope" in message
    assert "sample" in message


def test_get_object_unknown_module_raises_keyerror(registry: ObjectRegistry) -> None:
    with pytest.raises(KeyError) as excinfo:
        registry.get_object("nosuchmodule", "widget")

    assert "nosuchmodule" in str(excinfo.value)


def test_get_module_returns_module_def(registry: ObjectRegistry) -> None:
    module = registry.get_module("sample")

    assert isinstance(module, ModuleDef)
    assert module.module == "sample"
    assert module.extract_order == ["gadget", "widget"]


def test_get_module_unknown_module_raises_keyerror(registry: ObjectRegistry) -> None:
    with pytest.raises(KeyError) as excinfo:
        registry.get_module("nosuchmodule")

    assert "nosuchmodule" in str(excinfo.value)


def test_list_objects_unknown_module_raises_keyerror(registry: ObjectRegistry) -> None:
    with pytest.raises(KeyError) as excinfo:
        registry.list_objects("nosuchmodule")

    assert "nosuchmodule" in str(excinfo.value)


def test_broken_yaml_raises_module_load_error_naming_the_file(tmp_path: Path) -> None:
    (tmp_path / "valid.yaml").write_text(VALID_YAML, encoding="utf-8")
    broken = tmp_path / "broken.yaml"
    broken.write_text(BROKEN_YAML, encoding="utf-8")

    with pytest.raises(ModuleLoadError) as excinfo:
        ObjectRegistry([tmp_path])

    message = str(excinfo.value)
    assert str(broken) in message
    assert "key_fields" in message
    assert excinfo.value.path == broken
    assert excinfo.value.original_exception is not None


def test_malformed_yaml_syntax_raises_module_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "unparseable.yaml"
    bad.write_text("module: oops\nobjects: [ unclosed\n", encoding="utf-8")

    with pytest.raises(ModuleLoadError) as excinfo:
        ObjectRegistry([tmp_path])

    assert str(bad) in str(excinfo.value)


def test_empty_directory_does_not_raise(tmp_path: Path) -> None:
    registry = ObjectRegistry([tmp_path])

    assert registry.list_modules() == []


def test_missing_directory_does_not_raise(tmp_path: Path) -> None:
    registry = ObjectRegistry([tmp_path / "does_not_exist"])

    assert registry.list_modules() == []


def test_loads_from_multiple_directories(tmp_path: Path) -> None:
    (tmp_path / "alpha.yaml").write_text(VALID_YAML, encoding="utf-8")

    registry = ObjectRegistry([FIXTURE_DIR, tmp_path])

    assert registry.list_modules() == ["alpha", "sample"]
    assert registry.get_object("alpha", "thing").maximo_object == "THING"


def test_underscore_prefixed_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "alpha.yaml").write_text(VALID_YAML, encoding="utf-8")
    (tmp_path / "_template.yaml").write_text(VALID_YAML.replace("alpha", "template"), encoding="utf-8")

    registry = ObjectRegistry([tmp_path])

    assert registry.list_modules() == ["alpha"]


def test_underscore_prefixed_broken_file_is_not_validated(tmp_path: Path) -> None:
    (tmp_path / "_scratch.yaml").write_text(BROKEN_YAML, encoding="utf-8")

    registry = ObjectRegistry([tmp_path])

    assert registry.list_modules() == []


def test_duplicate_module_name_across_files_raises_module_load_error(tmp_path: Path) -> None:
    first = tmp_path / "a_alpha.yaml"
    second = tmp_path / "b_alpha_again.yaml"
    first.write_text(VALID_YAML, encoding="utf-8")
    second.write_text(VALID_YAML.replace("name: thing", "name: other"), encoding="utf-8")

    with pytest.raises(ModuleLoadError) as excinfo:
        ObjectRegistry([tmp_path])

    message = str(excinfo.value)
    assert str(second) in message
    assert str(first) in message
    assert "alpha" in message


def test_duplicate_object_name_within_module_raises_module_load_error(tmp_path: Path) -> None:
    dupe = tmp_path / "dupe.yaml"
    dupe.write_text(VALID_YAML + VALID_YAML.split("objects:\n")[1], encoding="utf-8")

    with pytest.raises(ModuleLoadError) as excinfo:
        ObjectRegistry([tmp_path])

    assert "duplicate object names" in str(excinfo.value)
    assert "thing" in str(excinfo.value)


def test_pilot_config_modules_load_cleanly() -> None:
    config_dir = Path(__file__).resolve().parents[2] / "config" / "modules"

    registry = ObjectRegistry([config_dir])

    assert registry.list_modules() == ["inventory", "purchasing"]
    assert registry.get_object("purchasing", "purchase_order").children[0].name == "po_line"
    assert registry.get_object("inventory", "inventory_balance").access.prefer == "oslc"
