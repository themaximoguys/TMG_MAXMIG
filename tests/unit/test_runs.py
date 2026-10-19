import json
from pathlib import Path

from maxmig.model.registry import ObjectRegistry
from maxmig.runs import describe_modules, describe_objects, latest_runs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "modules"


def _registry():
    return ObjectRegistry([FIXTURES])


def test_describe_modules():
    rows = describe_modules(_registry())

    assert rows == [
        {
            "module": "sample",
            "description": "Minimal fixture module used only by unit tests.",
            "object_count": 2,
            "extract_order": ["gadget", "widget"],
            "load_order": ["gadget", "widget"],
        }
    ]


def test_describe_objects():
    rows = describe_objects(_registry(), "sample")

    widget, gadget = rows
    assert widget["name"] == "widget"
    assert widget["oslc_resource"] == "mxapiwidget"
    assert widget["rest_mbo_table"] == "widget"
    assert widget["prefer"] == "oslc"
    assert widget["key_fields"] == ["widgetnum", "siteid"]
    assert widget["field_count"] == 4
    assert widget["children"] == ["widget_line"]
    assert widget["custom_fields"] == []
    assert gadget["oslc_resource"] is None and gadget["children"] == []


def test_describe_objects_nested_children_as_dotted_paths():
    class Node:
        def __init__(self, name, children=()):
            self.name = name
            self.children = list(children)

    class Access:
        oslc_resource, rest_mbo_table, prefer = "mxapipo", None, "oslc"

    class Obj:
        name, maximo_object, access = "purchase_order", "PO", Access()
        key_fields, fields = ["ponum"], ["ponum", "description"]
        custom_fields = ["zz_code"]
        children = [Node("po_line", [Node("subchild")]), Node("po_term")]

    class Module:
        objects = [Obj()]

    class Registry:
        def get_module(self, name):
            return Module()

    (row,) = describe_objects(Registry(), "purchasing")
    assert row["children"] == ["po_line", "po_line.subchild", "po_term"]
    assert row["custom_fields"] == ["zz_code"]


def _manifest(path, **payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_latest_runs_picks_newest_by_filename_and_skips_bad_json(tmp_path):
    item_dir = tmp_path / "inventory" / "item"
    _manifest(item_dir / "item_20260101T000000Z.manifest.json", module="inventory", object="item",
              env_prefix="DEV", row_count=5, started_at="a", finished_at="b")
    newest_good = _manifest(item_dir / "item_20260301T000000Z.manifest.json", module="inventory", object="item",
                            env_prefix="DEV", row_count=9, started_at="c", finished_at="d")
    (item_dir / "item_20260401T000000Z.manifest.json").write_text("{not json", encoding="utf-8")
    imp = _manifest(item_dir / "item_20260201T000000Z.import-result.json", module="inventory", object="item",
                    env_prefix="TST", dry_run=True, row_count=9, counts={"create": 7, "update": 2})
    (item_dir / "item_20260201T000000Z.csv").write_text("x\n")

    runs = latest_runs(tmp_path)

    by_kind = {r.kind: r for r in runs}
    assert set(by_kind) == {"export", "import"}
    assert by_kind["export"].path == newest_good
    assert by_kind["export"].row_count == 9 and by_kind["export"].env_prefix == "DEV"
    assert by_kind["export"].dry_run is None
    assert by_kind["import"].path == imp
    assert by_kind["import"].counts == {"create": 7, "update": 2}
    assert by_kind["import"].dry_run is True


def test_latest_runs_module_filter_and_missing_dir(tmp_path):
    _manifest(tmp_path / "inventory" / "item" / "item_20260101T000000Z.manifest.json", row_count=1)
    _manifest(tmp_path / "purchasing" / "po" / "po_20260101T000000Z.manifest.json", row_count=2)

    runs = latest_runs(tmp_path, module="purchasing")

    assert [(r.module, r.object, r.row_count) for r in runs] == [("purchasing", "po", 2)]
    assert latest_runs(tmp_path, module="nope") == []
    assert latest_runs(tmp_path / "absent") == []


def test_latest_runs_skips_non_object_json(tmp_path):
    _manifest(tmp_path / "m" / "o" / "o_20260101T000000Z.manifest.json", row_count=1)
    (tmp_path / "m" / "o" / "o_20260201T000000Z.manifest.json").write_text("[1, 2]")

    (run,) = latest_runs(tmp_path)
    assert run.row_count == 1
