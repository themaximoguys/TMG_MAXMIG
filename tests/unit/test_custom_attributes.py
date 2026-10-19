"""Custom (user-defined) attribute support: schema, export, bulk import, CLI."""

import csv
import json

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from maxmig.api.client import _rest_mbo_fields
from maxmig.cli import main
from maxmig.extract.writer import write_object_tree
from maxmig.model.schema import ModuleDef, with_extra_fields
from maxmig.mxl.bridge import TreeReader, export_tree_to_sheet, make_resolver, sheet_headers, template_sheet
from maxmig.mxl.datadict import DataDictionary

BASE_URL = "https://masdev.example.com/maximo"
PO_URL = f"{BASE_URL}/api/os/mxapipo"

MODULE = {
    "module": "purchasing",
    "objects": [
        {
            "name": "purchase_order",
            "maximo_object": "PO",
            "access": {"oslc_resource": "mxapipo", "rest_mbo_table": "po", "prefer": "oslc"},
            "key_fields": ["ponum", "siteid"],
            "fields": ["ponum", "siteid", "status"],
            "custom_fields": ["ex_budgetcode"],
            "field_types": {"ex_budget": "amount"},
            "children": [
                {
                    "name": "po_line",
                    "maximo_object": "POLINE",
                    "relation_attribute": "poline",
                    "foreign_key": ["ponum", "siteid"],
                    "key_fields": ["ponum", "siteid", "polinenum"],
                    "fields": ["polinenum", "orderqty"],
                    "custom_fields": ["ex_contractref"],
                    "field_types": {"ex_iscritical": "YORN"},
                }
            ],
        }
    ],
    "load_order": ["purchase_order"],
}


def po_def():
    return ModuleDef.model_validate(MODULE).objects[0]


# --------------------------------------------------------------------------- schema


def test_custom_fields_validate_and_field_types_normalize():
    obj = po_def()
    assert obj.all_fields() == ["ponum", "siteid", "status", "ex_budgetcode"]
    assert obj.field_types == {"ex_budget": "AMOUNT"}
    assert obj.children[0].all_fields() == ["polinenum", "orderqty", "ex_contractref"]


def test_custom_field_also_in_fields_is_rejected():
    bad = json.loads(json.dumps(MODULE))
    bad["objects"][0]["custom_fields"] = ["status"]
    with pytest.raises(ValidationError, match="both fields and custom_fields"):
        ModuleDef.model_validate(bad)


def test_unknown_field_type_is_rejected():
    bad = json.loads(json.dumps(MODULE))
    bad["objects"][0]["field_types"] = {"ex_x": "MONEY"}
    with pytest.raises(ValidationError, match="unknown Maximo type"):
        ModuleDef.model_validate(bad)


def test_with_extra_fields_targets_object_and_relation_without_duplicates():
    obj = with_extra_fields(po_def(), ["ex_new", "po_line.ex_lineflag", "status", "EX_NEW"])
    assert obj.custom_fields == ["ex_budgetcode", "ex_new"]
    assert obj.children[0].custom_fields == ["ex_contractref", "ex_lineflag"]
    assert po_def().custom_fields == ["ex_budgetcode"]  # original untouched


def test_with_extra_fields_unknown_relation_is_a_clear_error():
    with pytest.raises(ValueError, match="no relation named 'nope'"):
        with_extra_fields(po_def(), ["nope.attr"])


def test_rest_mbo_requests_custom_fields_or_everything():
    obj = po_def()
    assert _rest_mbo_fields(obj) == ["ponum", "siteid", "status", "ex_budgetcode"]
    assert _rest_mbo_fields(obj.model_copy(update={"capture_all_attributes": True})) is None


# --------------------------------------------------------------------------- export


def read(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


ROWS = [
    {
        "ponum": "1", "siteid": "S", "status": "APPR", "ex_budgetcode": "B1", "ex_undeclared": "U1",
        "href": "x", "_rowstamp": "1", "poline_collectionref": "y",
        "poline": [{"polinenum": 1, "orderqty": 2, "ex_contractref": "C", "ex_iscritical": True}],
    },
    {"ponum": "2", "siteid": "S", "status": "WAPPR", "ex_other": "O2", "poline": []},
]


def test_export_writes_declared_custom_fields_as_columns(tmp_path):
    results = write_object_tree(iter(ROWS), po_def(), tmp_path, "purchasing", "T")
    parent = read(results["purchase_order"].path)
    child = read(results["po_line"].path)
    assert parent[0] == ["ponum", "siteid", "status", "ex_budgetcode"]
    assert parent[1] == ["1", "S", "APPR", "B1"]
    assert child[0] == ["ponum", "siteid", "polinenum", "orderqty", "ex_contractref"]


def test_capture_all_attributes_adds_undeclared_columns_and_skips_plumbing(tmp_path):
    obj = po_def()
    obj = obj.model_copy(update={"capture_all_attributes": True})
    results = write_object_tree(iter(ROWS), obj, tmp_path, "purchasing", "T")
    header, first, second = read(results["purchase_order"].path)
    assert header == ["ponum", "siteid", "status", "ex_budgetcode", "ex_other", "ex_undeclared"]
    assert first == ["1", "S", "APPR", "B1", "", "U1"]
    assert second == ["2", "S", "WAPPR", "", "O2", ""]
    assert not list(tmp_path.rglob("*.spool.jsonl")) and not list(tmp_path.rglob("*.partial"))


def test_capture_all_cleans_up_spool_on_failure(tmp_path):
    def broken():
        yield ROWS[0]
        raise RuntimeError("page 2 failed")

    obj = po_def().model_copy(update={"capture_all_attributes": True})
    with pytest.raises(RuntimeError):
        write_object_tree(broken(), obj, tmp_path, "purchasing", "T")
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------- bridge


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture
def export_files(tmp_path):
    base = tmp_path / "exports" / "purchasing"
    parent = write_csv(
        base / "purchase_order" / "purchase_order_20261018T000000Z.csv",
        ["ponum", "siteid", "status", "ex_budgetcode", "ex_budget", "ex_handadded"],
        [["1", "S", "APPR", "B1", "10.50", "H"], ["2", "S", "WAPPR", "", "", ""]],
    )
    child = write_csv(
        base / "po_line" / "po_line_20261018T000000Z.csv",
        ["ponum", "siteid", "polinenum", "orderqty", "ex_contractref", "ex_iscritical"],
        [["1", "S", "1", "2", "C", "1"], ["1", "S", "2", "3", "", "0"], ["2", "S", "1", "4", "", ""]],
    )
    return {"purchase_order": parent, "po_line": child}


def test_tree_reader_nests_children_and_types_custom_attributes(export_files):
    dd = DataDictionary()
    dd.add("POLINE", "ORDERQTY", "DECIMAL")
    dd.add("POLINE", "POLINENUM", "INTEGER")
    nodes = list(TreeReader(po_def(), export_files, make_resolver(po_def(), dd)))
    first = nodes[0].to_payload()
    assert first == {
        "ponum": "1", "siteid": "S", "status": "APPR", "ex_budgetcode": "B1",
        "ex_budget": 10.5,  # field_types override
        "ex_handadded": "H",  # undeclared column still sent
        "poline": [
            {"polinenum": 1, "orderqty": 2, "ex_contractref": "C", "ex_iscritical": True},
            {"polinenum": 2, "orderqty": 3, "ex_iscritical": False},
        ],
    }
    assert nodes[1].to_payload()["poline"] == [{"polinenum": 1, "orderqty": 4}]


def test_tree_reader_counts_unmatched_child_rows(export_files, tmp_path):
    write_csv(export_files["po_line"], ["ponum", "siteid", "polinenum"], [["9", "S", "1"]])
    reader = TreeReader(po_def(), export_files, lambda p, a: None)
    list(reader)
    assert reader.unmatched == {"po_line": 1}


def test_template_and_headers_include_custom_fields():
    assert sheet_headers(po_def()) == [
        "PONUM", "SITEID", "STATUS", "EX_BUDGETCODE", "POLINE.POLINENUM", "POLINE.ORDERQTY", "POLINE.EX_CONTRACTREF",
    ]
    sheet = template_sheet(po_def())
    assert (sheet.object_service, sheet.object_name, sheet.action) == ("MXAPIPO", "PO", "AddChange")
    assert sheet.columns[-1].header == "[RESULT]"


def test_export_tree_to_sheet_joins_and_keeps_undeclared_columns(export_files):
    sheet = export_tree_to_sheet(po_def(), export_files)
    headers = [c.header for c in sheet.columns]
    assert "EX_HANDADDED" in headers and "POLINE.EX_ISCRITICAL" in headers and headers[-1] == "[RESULT]"
    assert len(sheet.data) == 3
    col = headers.index("POLINE.POLINENUM")
    assert [row[col] for row in sheet.data] == ["1", "2", "1"]


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env.masdev"
    path.write_text(f"MASDEV_MANAGE_URL={BASE_URL}\nMASDEV_MANAGE_APIKEY=k\n", encoding="utf-8")
    return path


@pytest.fixture
def config_dir(tmp_path):
    directory = tmp_path / "config" / "modules"
    directory.mkdir(parents=True)
    (directory / "purchasing.yaml").write_text(yaml.safe_dump(MODULE), encoding="utf-8")
    return directory


def cli(*args):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def common(env_file, config_dir, tmp_path):
    return ["--env", "MASDEV", "--env-file", str(env_file), "--config-dir", str(config_dir), "--out", str(tmp_path / "exports")]


def test_cli_export_extra_field_and_all_attributes(tmp_path, env_file, config_dir, requests_mock):
    requests_mock.get(PO_URL, json={"member": ROWS, "responseInfo": {}})
    result = cli("export", "--module", "purchasing", "--object", "purchase_order", *common(env_file, config_dir, tmp_path),
                 "--extra-field", "po_line.ex_iscritical", "--all-attributes")
    assert result.exit_code == 0, result.output
    (child_csv,) = (tmp_path / "exports" / "purchasing" / "po_line").glob("*.csv")
    assert read(child_csv)[0] == ["ponum", "siteid", "polinenum", "orderqty", "ex_contractref", "ex_iscritical"]
    (parent_csv,) = (tmp_path / "exports" / "purchasing" / "purchase_order").glob("*.csv")
    assert "ex_undeclared" in read(parent_csv)[0]


def test_cli_import_bulk_dry_run_nests_children_and_never_posts(tmp_path, env_file, config_dir, export_files, requests_mock):
    result = cli("import", "--module", "purchasing", "--object", "purchase_order", "--bulk", *common(env_file, config_dir, tmp_path))
    assert result.exit_code == 0, result.output
    assert "dry_run: 2" in result.output and "nested children from" in result.output
    assert requests_mock.request_history == []
    (log,) = (tmp_path / "exports" / "purchasing" / "purchase_order").glob("*.import-log.jsonl")
    first = json.loads(log.read_text().splitlines()[0])
    assert first["key"] == {"ponum": "1", "siteid": "S"} and first["children"] == {"poline": 2}


def test_cli_import_bulk_execute_sends_bulk_and_reports(tmp_path, env_file, config_dir, export_files, requests_mock):
    route = requests_mock.post(
        PO_URL,
        json=[
            {"_responsemeta": {"status": "201"}},
            {"_responsemeta": {"status": "400"}, "_responsedata": {"Error": {"message": "BMXAA bad"}}},
        ],
    )
    result = CliRunner().invoke(main, ["import", "--module", "purchasing", "--object", "purchase_order", "--bulk",
                                       "--execute", "--continue-on-error", *common(env_file, config_dir, tmp_path)])
    assert result.exit_code == 1 and "1 row(s) failed" in result.output
    body = route.last_request.json()
    assert route.last_request.headers["x-method-override"] == "BULK"
    assert body[0]["_action"] == "AddChange" and len(body[0]["poline"]) == 2
    (manifest,) = (tmp_path / "exports" / "purchasing" / "purchase_order").glob("*.import-result.json")
    data = json.loads(manifest.read_text())
    assert data["mode"] == "bulk" and data["counts"] == {"created": 1, "error": 1} and data["integrity_ok"] is True


def test_cli_import_bulk_start_row_skips_earlier_parents(tmp_path, env_file, config_dir, export_files, requests_mock):
    route = requests_mock.post(PO_URL, json=[{"_responsemeta": {"status": "204"}}])
    result = cli("import", "--module", "purchasing", "--object", "purchase_order", "--bulk", "--execute",
                 "--start-row", "2", *common(env_file, config_dir, tmp_path))
    assert result.exit_code == 0, result.output
    assert [r["ponum"] for r in route.last_request.json()] == ["2"]


def test_cli_import_whole_module_follows_load_order(tmp_path, env_file, config_dir, export_files, requests_mock):
    requests_mock.get(PO_URL, json={"member": [], "responseInfo": {}})
    result = cli("import", "--module", "purchasing", *common(env_file, config_dir, tmp_path))
    assert result.exit_code == 0, result.output
    assert "would_create: 2" in result.output


def test_cli_import_row_mode_sends_custom_columns(tmp_path, env_file, config_dir, export_files, requests_mock):
    requests_mock.get(PO_URL, json={"member": [], "responseInfo": {}})
    route = requests_mock.post(PO_URL, status_code=201, headers={"Location": f"{PO_URL}/_N"})
    result = cli("import", "--module", "purchasing", "--object", "purchase_order", "--execute",
                 "--start-row", "2", *common(env_file, config_dir, tmp_path)[:-2],
                 "--csv-file", str(export_files["purchase_order"]), "--out", str(tmp_path / "exports"))
    assert result.exit_code == 0, result.output
    assert route.last_request.json() == {"ponum": "2", "siteid": "S", "status": "WAPPR"}


def test_cli_check_reports_orphans(tmp_path, env_file, config_dir, export_files):
    write_csv(export_files["po_line"], ["ponum", "siteid", "polinenum"], [["9", "S", "1"]])
    result = CliRunner().invoke(main, ["check", "--module", "purchasing", "--object", "purchase_order",
                                       "--config-dir", str(config_dir), "--out", str(tmp_path / "exports")])
    assert result.exit_code == 1 and "orphan" in result.output


def test_cli_mxl_template_and_from_export(tmp_path, config_dir, export_files):
    out = tmp_path / "t.csv"
    result = cli("mxl", "template", "--module", "purchasing", "--object", "purchase_order",
                 "--extra-field", "ex_more", "--config-dir", str(config_dir), "--output", str(out))
    assert result.exit_code == 0, result.output
    assert read(out)[0][:3] == ["MXAPIPO", "PO", "AddChange"] and "EX_MORE" in read(out)[1]

    joined = tmp_path / "joined.xlsx"
    pytest.importorskip("openpyxl")
    result = cli("mxl", "from-export", "--module", "purchasing", "--object", "purchase_order",
                 "--config-dir", str(config_dir), "--out", str(tmp_path / "exports"), "--output", str(joined))
    assert result.exit_code == 0 and "3 row(s)" in result.output


def test_cli_mxl_run_dry_run_then_execute(tmp_path, env_file, requests_mock):
    sheet = tmp_path / "assets.csv"
    write_csv(sheet, ["MXAPIASSET", "ASSET", "AddChange"], [["ASSETNUM", "SITEID", "EX_CRIT", "[ASSETUID]"], ["A1", "S", "HIGH", ""]])
    route = requests_mock.post(f"{BASE_URL}/api/os/mxapiasset", json=[{"_responsemeta": {"status": "201"}, "_responsedata": {"assetuid": 5}}])

    args = ["mxl", "run", str(sheet), "--env", "MASDEV", "--env-file", str(env_file)]
    result = cli(*args)
    assert result.exit_code == 0 and "DRY RUN" in result.output and route.call_count == 0

    result = cli(*args, "--execute")
    assert result.exit_code == 0, result.output
    assert route.last_request.json() == [{"_action": "AddChange", "assetnum": "A1", "siteid": "S", "ex_crit": "HIGH"}]
    assert read(sheet)[1][-1] == "[RESULT]" and read(sheet)[2] == ["A1", "S", "HIGH", "5", "OK"]


def test_cli_mxl_run_query_fills_sheet(tmp_path, env_file, requests_mock):
    sheet = tmp_path / "q.csv"
    write_csv(sheet, ["MXAPIASSET", "ASSET", "Query", 'siteid="S"'], [["ASSETNUM", "EX_CRIT"]])
    requests_mock.get(f"{BASE_URL}/api/os/mxapiasset", json={"member": [{"assetnum": "A", "ex_crit": "LOW"}], "responseInfo": {}})
    result = cli("mxl", "run", str(sheet), "--env", "MASDEV", "--env-file", str(env_file))
    assert result.exit_code == 0, result.output
    assert read(sheet)[2] == ["A", "LOW"]


def test_cli_datadict_refresh_writes_csv(tmp_path, env_file, requests_mock):
    requests_mock.get(
        f"{BASE_URL}/api/os/mxl_maxattributecfg",
        json={"member": [{"objectname": "PO", "attributename": "EX_BUDGET", "maxtype": "AMOUNT", "userdefined": True}], "responseInfo": {}},
    )
    out = tmp_path / "dd.csv"
    result = cli("datadict", "refresh", "--env", "MASDEV", "--env-file", str(env_file), "--output", str(out))
    assert result.exit_code == 0 and "1 user-defined" in result.output
    assert DataDictionary.from_csv(out).custom_attributes("PO") == ["ex_budget"]
