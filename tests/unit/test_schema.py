import sys
from pathlib import Path

import pytest
import yaml

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from maxmig.model.schema import (  # noqa: E402
    AccessDef,
    ModuleDef,
    ObjectDef,
    RelationDef,
    load_module_yaml,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "modules" / "sample_module.yaml"


def _sample_dict() -> dict:
    with FIXTURE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_load_sample_module_yaml_returns_module_def() -> None:
    module = load_module_yaml(FIXTURE)

    assert isinstance(module, ModuleDef)
    assert module.module == "sample"
    assert len(module.objects) == 2
    assert [obj.name for obj in module.objects] == ["widget", "gadget"]


def test_load_sample_module_yaml_parses_object_details() -> None:
    module = load_module_yaml(FIXTURE)
    widget = module.objects[0]

    assert widget.maximo_object == "WIDGET"
    assert widget.access.oslc_resource == "mxapiwidget"
    assert widget.access.rest_mbo_table == "widget"
    assert widget.access.prefer == "oslc"
    assert widget.key_fields == ["widgetnum", "siteid"]
    assert widget.where is None
    assert len(widget.children) == 1

    line = widget.children[0]
    assert line.name == "widget_line"
    assert line.relation_attribute == "widgetline"
    assert line.foreign_key == "widgetnum"
    assert line.children == []


def test_load_sample_module_yaml_parses_orders() -> None:
    module = load_module_yaml(FIXTURE)

    assert module.extract_order == ["gadget", "widget"]
    assert module.load_order == ["gadget", "widget"]


def test_extract_order_referencing_unknown_object_raises_naming_entry() -> None:
    data = _sample_dict()
    data["extract_order"] = ["gadget", "no_such_object"]

    with pytest.raises(ValueError) as excinfo:
        ModuleDef(**data)

    assert "no_such_object" in str(excinfo.value)
    assert "extract_order" in str(excinfo.value)


def test_load_order_referencing_unknown_object_raises_naming_entry() -> None:
    data = _sample_dict()
    data["load_order"] = ["bogus_thing"]

    with pytest.raises(ValueError) as excinfo:
        ModuleDef(**data)

    assert "bogus_thing" in str(excinfo.value)
    assert "load_order" in str(excinfo.value)


def test_object_def_with_empty_key_fields_is_invalid() -> None:
    with pytest.raises(ValueError) as excinfo:
        ObjectDef(
            name="widget",
            maximo_object="WIDGET",
            access=AccessDef(oslc_resource="mxapiwidget", rest_mbo_table="widget", prefer="oslc"),
            key_fields=[],
            fields=["widgetnum"],
        )

    assert "key_fields" in str(excinfo.value)


def test_module_def_with_no_objects_is_invalid() -> None:
    with pytest.raises(ValueError) as excinfo:
        ModuleDef(module="empty", objects=[])

    assert "objects" in str(excinfo.value)


def test_access_def_requires_at_least_one_endpoint() -> None:
    with pytest.raises(ValueError) as excinfo:
        AccessDef(oslc_resource=None, rest_mbo_table=None, prefer="oslc")

    assert "oslc_resource" in str(excinfo.value)
    assert "rest_mbo_table" in str(excinfo.value)


def test_access_def_accepts_rest_mbo_only() -> None:
    access = AccessDef(rest_mbo_table="matrectrans", prefer="rest_mbo")

    assert access.oslc_resource is None
    assert access.prefer == "rest_mbo"


def test_access_def_rejects_unknown_prefer_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        AccessDef(oslc_resource="mxapipo", prefer="soap")

    assert "prefer" in str(excinfo.value)


def test_relation_def_parses_two_levels_of_nested_children() -> None:
    nested = RelationDef(
        name="level_one",
        maximo_object="LEVELONE",
        relation_attribute="levelone",
        foreign_key="parentnum",
        key_fields=["parentnum", "l1num"],
        fields=["l1num", "description"],
        children=[
            RelationDef(
                name="level_two",
                maximo_object="LEVELTWO",
                relation_attribute="leveltwo",
                foreign_key=["parentnum", "l1num"],
                key_fields=["parentnum", "l1num", "l2num"],
                fields=["l2num", "description"],
                children=[],
            )
        ],
    )

    assert nested.children[0].name == "level_two"
    assert nested.children[0].foreign_key == ["parentnum", "l1num"]
    assert nested.children[0].children == []


def test_relation_def_parses_nested_children_from_raw_dicts() -> None:
    obj = ObjectDef(
        **{
            "name": "parent",
            "maximo_object": "PARENT",
            "access": {"oslc_resource": "mxapiparent", "prefer": "oslc"},
            "key_fields": ["parentnum"],
            "fields": ["parentnum"],
            "children": [
                {
                    "name": "child",
                    "relation_attribute": "child",
                    "foreign_key": "parentnum",
                    "key_fields": ["parentnum", "childnum"],
                    "fields": ["childnum"],
                    "children": [
                        {
                            "name": "grandchild",
                            "relation_attribute": "grandchild",
                            "foreign_key": ["parentnum", "childnum"],
                            "key_fields": ["parentnum", "childnum", "gcnum"],
                            "fields": ["gcnum"],
                        }
                    ],
                }
            ],
        }
    )

    grandchild = obj.children[0].children[0]
    assert isinstance(grandchild, RelationDef)
    assert grandchild.name == "grandchild"
    assert grandchild.children == []


def test_load_module_yaml_propagates_field_level_validation_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "module: broken\n"
        "objects:\n"
        "  - name: thing\n"
        "    access:\n"
        "      oslc_resource: mxapithing\n"
        "      prefer: oslc\n"
        "    key_fields: []\n"
        "    fields: [thingid]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_module_yaml(broken)

    assert "key_fields" in str(excinfo.value)
