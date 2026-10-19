"""MxLoader engine port: values, sheet, node conversion, data dictionary."""

import base64
import json

import pytest
import requests

from maxmig.mxl.convert import build_select, members_to_rows, sheet_to_nodes
from maxmig.mxl.datadict import DataDictionary, fetch_from_maximo
from maxmig.mxl.sheet import (
    MxSheet,
    SheetFormatError,
    load_sheet,
    parse_header,
)
from maxmig.mxl.values import ValueConversionError, format_date, to_json_value

BASE_URL = "https://mx.example.com/maximo"
HEADERS = {"apikey": "k", "Accept": "application/json"}


def make_sheet(headers, rows, action="AddChange", service="MXAPIPO", obj="PO", **kw):
    return MxSheet(
        object_service=service,
        object_name=obj,
        action=action,
        columns=[parse_header(i, h) for i, h in enumerate(headers)],
        data=[list(r) for r in rows],
        **kw,
    )


def write_csv_sheet(path, config, header, rows):
    import csv

    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(config)
        w.writerow(header)
        w.writerows(rows)
    return path


# --------------------------------------------------------------------------- values


@pytest.mark.parametrize(
    "maxtype,raw,expected",
    [
        ("INTEGER", "42", 42),
        ("BIGINT", "7", 7),
        ("DECIMAL", "2.5", 2.5),
        ("AMOUNT", "2,5", 2.5),  # MxLoader's decimal-comma tolerance
        ("FLOAT", "3", 3),
        ("YORN", "1", True),
        ("YORN", "false", False),
        ("ALN", "00123", "00123"),  # string types keep leading zeros
        (None, "anything", "anything"),  # unknown type -> string
    ],
)
def test_to_json_value_types_by_maxtype(maxtype, raw, expected):
    value = to_json_value("attr", raw, maxtype)
    assert value == expected and type(value) is type(expected)


def test_to_json_value_null_token_sends_explicit_blank():
    assert to_json_value("description", "~NULL~", "ALN") == ""
    assert to_json_value("orderqty", "~NULL~", "DECIMAL") == ""


def test_yorn_rejects_values_outside_mxloader_set():
    with pytest.raises(ValueConversionError, match="YORN attribute ROTATING"):
        to_json_value("rotating", "maybe", "YORN")


def test_integer_rejects_fraction_and_text():
    with pytest.raises(ValueConversionError, match="whole number"):
        to_json_value("polinenum", "1.5", "INTEGER")
    with pytest.raises(ValueConversionError, match="numeric"):
        to_json_value("orderqty", "abc", "DECIMAL")


@pytest.mark.parametrize(
    "maxtype,raw,expected",
    [
        ("DATETIME", "2026-10-18T09:30:00", "2026-10-18T09:30:00"),
        ("DATETIME", "2026-10-18T09:30:00-04:00", "2026-10-18T09:30:00-04:00"),
        ("DATETIME", "10/18/2026 09:30", "2026-10-18T09:30:00"),
        ("DATE", "2026-10-18", "2026-10-18T00:00:00"),
        ("DATE", "2026-10-18T13:14:15", "2026-10-18T00:00:00"),
        ("TIME", "07:05", "1970-01-01T07:05:00"),
    ],
)
def test_dates_normalize_like_adjustval(maxtype, raw, expected):
    assert format_date(raw, maxtype) == expected


def test_datetime_default_timezone_only_when_missing():
    assert format_date("2026-10-18 09:30", "DATETIME", timezone="+00:00") == "2026-10-18T09:30:00+00:00"
    assert format_date("2026-10-18T09:30:00+02:00", "DATETIME", timezone="+00:00").endswith("+02:00")


def test_bad_date_raises():
    with pytest.raises(ValueConversionError, match="date format"):
        format_date("not a date", "DATE", "targstartdate")


def test_file_tags_inline_content_relative_to_sheet(tmp_path):
    (tmp_path / "ld.txt").write_text("long \"description\"", encoding="utf-8")
    (tmp_path / "img.bin").write_bytes(b"\x00\x01\xff")
    assert to_json_value("description_longdescription", "FileTxt:/./ld.txt", None, tmp_path) == 'long "description"'
    assert to_json_value("documentdata", "FileEnc:/./img.bin", None, tmp_path) == base64.b64encode(b"\x00\x01\xff").decode()
    with pytest.raises(ValueConversionError, match="not found"):
        to_json_value("x", "FileTxt:/./missing.txt", None, tmp_path)


# --------------------------------------------------------------------------- sheet


def test_load_csv_sheet_reads_row1_config_and_row2_header(tmp_path):
    path = write_csv_sheet(
        tmp_path / "s.csv",
        ["MXAPIASSET", "ASSET", "addchange", 'siteid="BEDFORD"', "+assetnum", "savedQuery=X"],
        ["ASSETNUM", "SITEID", "EX_CRITICALITY", "[ASSETUID]", "[RESULT]"],
        [["1001", "BEDFORD", "HIGH", "", ""]],
    )
    sheet = load_sheet(path)
    assert (sheet.object_service, sheet.object_name, sheet.action) == ("MXAPIASSET", "ASSET", "AddChange")
    assert sheet.where == 'siteid="BEDFORD"' and sheet.order_by == "+assetnum" and sheet.oslc_directives == "savedQuery=X"
    assert [c.attr for c in sheet.attr_columns] == ["assetnum", "siteid", "ex_criticality"]
    assert [c.attr for c in sheet.id_columns] == ["assetuid"]
    assert sheet.result_column.index == 4


def test_xlsx_round_trip(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Assets"
    ws.append(["MXAPIASSET", "ASSET", "AddChange"])
    ws.append(["ASSETNUM", "SITEID", "[RESULT]"])
    ws.append(["1001", "BEDFORD", None])
    wb.save(path)

    sheet = load_sheet(path, "Assets")
    assert sheet.sheet_name == "Assets" and sheet.data[0][:2] == ["1001", "BEDFORD"]
    sheet.set_result(3, "OK")
    sheet.save()
    assert openpyxl.load_workbook(path)["Assets"]["C3"].value == "OK"


@pytest.mark.parametrize(
    "config,match",
    [
        (["", "PO", "AddChange"], "A1"),
        (["MXAPIPO", "", "AddChange"], "B1"),
        (["MXAPIPO", "PO", ""], "C1"),
        (["MXAPIPO", "PO", "Upsert"], "unsupported action"),
    ],
)
def test_sheet_rejects_missing_config(tmp_path, config, match):
    path = write_csv_sheet(tmp_path / "s.csv", config, ["PONUM"], [])
    with pytest.raises(SheetFormatError, match=match):
        load_sheet(path)


def test_sheet_rejects_duplicate_headers():
    with pytest.raises(SheetFormatError, match="duplicate header 'PONUM'"):
        make_sheet(["PONUM", "ponum"], [])


def test_ensure_result_column_adds_once_and_set_id_fills_main_object_ids():
    sheet = make_sheet(["PONUM", "[POID]"], [["1001", ""]])
    column = sheet.ensure_result_column()
    assert column.header == "[RESULT]" and sheet.ensure_result_column() is column
    sheet.set_result(3, "OK")
    sheet.set_id(3, "POID", 99)
    assert sheet.data[0] == ["1001", 99, "OK"]


# --------------------------------------------------------------------------- convert


PO_HEADER = ["PONUM", "SITEID", "POLINE.POLINENUM", "POLINE.ITEMNUM", "POTERM.SEQNUM"]


def test_consecutive_identical_parent_rows_merge_into_one_parent():
    sheet = make_sheet(
        PO_HEADER,
        [
            ["1001", "BEDFORD", "1", "A", ""],
            ["1001", "BEDFORD", "2", "B", ""],
            ["1002", "BEDFORD", "1", "C", ""],
        ],
    )
    nodes, next_index = sheet_to_nodes(sheet)
    assert next_index == 3
    assert [n.find_attr("ponum") for n in nodes] == ["1001", "1002"]
    assert nodes[0].rows == [3, 4]
    assert [c.find_attr("polinenum") for c in nodes[0].children["poline"]] == ["1", "2"]


def test_sibling_relations_nest_under_the_parent_not_each_other():
    """MxLoader's single-cursor parser nests POTERM under POLINE; this port doesn't."""
    sheet = make_sheet(PO_HEADER, [["1001", "BEDFORD", "1", "A", "10"], ["1001", "BEDFORD", "2", "B", "10"]])
    (po,), _ = sheet_to_nodes(sheet)
    payload = po.to_payload()
    assert [line["polinenum"] for line in payload["poline"]] == ["1", "2"]
    assert payload["poterm"] == [{"seqnum": "10"}]
    assert "poterm" not in payload["poline"][0]


def test_empty_child_cells_create_no_child():
    sheet = make_sheet(PO_HEADER, [["1001", "BEDFORD", "", "", ""]])
    (po,), _ = sheet_to_nodes(sheet)
    assert po.to_payload() == {"ponum": "1001", "siteid": "BEDFORD"}


def test_grandchildren_nest_two_levels():
    sheet = make_sheet(
        ["ITEMNUM", "ITEMSPEC.ASSETATTRID", "ITEMSPEC.ITEMSPECVAL.VALUE"],
        [["A", "HEIGHT", "1"], ["A", "HEIGHT", "2"], ["A", "WIDTH", "3"]],
        obj="ITEM",
    )
    (item,), _ = sheet_to_nodes(sheet)
    specs = item.to_payload()["itemspec"]
    assert [s["assetattrid"] for s in specs] == ["HEIGHT", "WIDTH"]
    assert [v["value"] for v in specs[0]["itemspecval"]] == ["1", "2"]


def test_batch_size_limits_top_level_objects_and_never_splits_a_parent():
    rows = [["1", "S", "1", "A", ""], ["1", "S", "2", "B", ""], ["2", "S", "1", "C", ""], ["3", "S", "1", "D", ""]]
    sheet = make_sheet(PO_HEADER, rows)
    first, idx = sheet_to_nodes(sheet, 0, batch_size=2)
    assert [n.find_attr("ponum") for n in first] == ["1", "2"] and idx == 3
    second, idx = sheet_to_nodes(sheet, idx, batch_size=2)
    assert [n.find_attr("ponum") for n in second] == ["3"]
    everything, _ = sheet_to_nodes(sheet, 0, batch_size=0)
    assert len(everything) == 3


def test_parse_stops_at_first_empty_row():
    sheet = make_sheet(["PONUM"], [["1"], [""], ["2"]])
    nodes, idx = sheet_to_nodes(sheet)
    assert [n.find_attr("ponum") for n in nodes] == ["1"] and idx == 1


def test_custom_attributes_in_header_are_typed_from_the_dictionary():
    dd = DataDictionary()
    dd.add("PO", "EX_BUDGET", "AMOUNT")
    dd.add("POLINE", "EX_ISCRITICAL", "YORN")
    sheet = make_sheet(["PONUM", "EX_BUDGET", "POLINE.EX_ISCRITICAL"], [["1", "1500.25", "1"]])
    from maxmig.mxl.bridge import sheet_resolver

    (po,), _ = sheet_to_nodes(sheet, resolve=sheet_resolver(sheet, dd))
    assert po.to_payload() == {"ponum": "1", "ex_budget": 1500.25, "poline": [{"ex_iscritical": True}]}


def test_build_select_mirrors_build_attr_list():
    sheet = make_sheet(PO_HEADER + ["[POID]", "[RESULT]"], [], action="Query")
    assert build_select(sheet) == "ponum,siteid,poid,poline{polinenum,itemnum},poterm{seqnum}"


def test_members_to_rows_repeats_parent_values_per_child():
    sheet = make_sheet(["PONUM", "POLINE.POLINENUM", "[RESULT]"], [], action="Query")
    members = [{"ponum": "1", "poline": [{"polinenum": 1}, {"polinenum": 2}]}, {"ponum": "2"}]
    assert list(members_to_rows(members, sheet)) == [["1", 1, None], ["1", 2, None], ["2", None, None]]


# --------------------------------------------------------------------------- data dictionary


def test_datadict_csv_round_trip_and_custom_attributes(tmp_path):
    dd = DataDictionary()
    dd.add("asset", "ex_criticality", "aln", user_defined=True)
    dd.add("ASSET", "PRIORITY", "INTEGER")
    path = tmp_path / "dd.csv"
    dd.to_csv(path)
    loaded = DataDictionary.from_csv(path)
    assert loaded.type_of("Asset", "priority") == "INTEGER"
    assert loaded.custom_attributes("asset") == ["ex_criticality"]


def test_datadict_reads_mxloader_mxattrcfg_layout_without_userdefined(tmp_path):
    path = tmp_path / "MxAttrCfg.csv"
    path.write_text("OBJECTNAME,ATTRIBUTENAME,MAXTYPE,SCALE\nPOLINE,ORDERQTY,DECIMAL,2\n", encoding="utf-8")
    assert DataDictionary.from_csv(path).lookup("POLINE", "ORDERQTY").scale == 2


def test_datadict_fetch_from_maximo_filters_by_object_and_keeps_userdefined(requests_mock):
    route = requests_mock.get(
        f"{BASE_URL}/api/os/mxl_maxattributecfg",
        json={
            "member": [
                {"objectname": "ASSET", "attributename": "EX_COST", "maxtype": "AMOUNT", "scale": 2, "userdefined": True},
                {"objectname": "ASSET", "attributename": "ASSETNUM", "maxtype": "UPPER", "userdefined": False},
            ],
            "responseInfo": {},
        },
    )
    dd = fetch_from_maximo(requests.Session(), BASE_URL, HEADERS, ["asset"])
    # requests_mock lowercases parsed query strings; the raw URL keeps the case sent
    assert "%22ASSET%22" in route.last_request.url and route.last_request.qs["oslc.where"] == ['objectname in ["asset"]']
    assert dd.type_of("ASSET", "EX_COST") == "AMOUNT"
    assert dd.custom_attributes("ASSET") == ["ex_cost"]


def test_datadict_merge_later_wins():
    a, b = DataDictionary(), DataDictionary()
    a.add("X", "Y", "ALN")
    b.add("X", "Y", "INTEGER")
    assert a.merge(b).type_of("X", "Y") == "INTEGER"


def test_payload_is_valid_json_with_action_first():
    sheet = make_sheet(["PONUM", "DESCRIPTION"], [["1", 'quote " and \\ backslash']])
    (po,), _ = sheet_to_nodes(sheet)
    po.action = "AddChange"
    payload = po.to_payload()
    assert list(payload)[0] == "_action"
    assert json.loads(json.dumps(payload))["description"] == 'quote " and \\ backslash'
