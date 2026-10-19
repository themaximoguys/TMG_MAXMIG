"""MxLoader engine port: REST BULK service and the sync/query run loop."""

import json

import pytest
import requests

from maxmig.mxl.convert import sheet_to_nodes
from maxmig.mxl.runner import RunOptions, run_nodes, run_query, run_sheet
from maxmig.mxl.service import MxRestService, parse_bulk_response
from maxmig.mxl.sheet import MxSheet, SheetFormatError, parse_header

BASE_URL = "https://mx.example.com/maximo"
OS_URL = f"{BASE_URL}/api/os/mxapiasset"
HEADERS = {"apikey": "k", "Accept": "application/json"}


def asset_sheet(rows, headers=("ASSETNUM", "SITEID", "[ASSETUID]"), action="AddChange"):
    return MxSheet(
        object_service="MXAPIASSET",
        object_name="ASSET",
        action=action,
        columns=[parse_header(i, h) for i, h in enumerate(headers)],
        data=[list(r) for r in rows],
    )


def service():
    return MxRestService(requests.Session(), BASE_URL, HEADERS)


def ok(status=201, **data):
    return {"_responsemeta": {"status": str(status)}, "_responsedata": data}


def err(message, status=400):
    return {"_responsemeta": {"status": str(status)}, "_responsedata": {"Error": {"message": message}}}


# --------------------------------------------------------------------------- service


def test_sync_posts_bulk_with_action_properties_and_rest_params(requests_mock):
    route = requests_mock.post(OS_URL, json=[ok(201, assetuid=7)])
    results = service().sync("MXAPIASSET", "AddChange", [{"_action": "AddChange", "assetnum": "1"}], ["assetuid"])

    request = route.last_request
    assert request.headers["x-method-override"] == "BULK"
    assert request.headers["properties"] == "assetuid"
    assert request.headers["apikey"] == "k"
    assert request.qs["lean"] == ["1"] and request.qs["mxlaction"] == ["addchange"]
    assert request.json() == [{"_action": "AddChange", "assetnum": "1"}]
    assert results[0].ok and results[0].created and results[0].data == {"assetuid": 7}


def test_parse_bulk_response_maps_statuses_like_mxrunsync():
    results = parse_bulk_response([ok(201), ok(204), ok(200), err("BMXAA4129E - bad site")], 4)
    assert [r.status for r in results] == ["201", "204", "200", "400"]
    assert [r.ok for r in results] == [True, True, True, False]
    assert results[3].message == "BMXAA4129E - bad site"


def test_parse_bulk_response_pads_missing_records():
    results = parse_bulk_response([ok()], 2)
    assert not results[1].ok and "no result" in results[1].message


def test_whole_request_http_error_fails_every_record(requests_mock):
    requests_mock.post(OS_URL, status_code=401, json={"Error": {"message": "BMXAA0021E unauthorized"}})
    results = service().sync("MXAPIASSET", "Add", [{"a": 1}, {"a": 2}])
    assert [r.message for r in results] == ["BMXAA0021E unauthorized"] * 2


def test_transport_error_is_reported_not_raised(requests_mock):
    requests_mock.post(OS_URL, exc=requests.exceptions.ConnectTimeout)
    (result,) = service().sync("MXAPIASSET", "Add", [{"a": 1}])
    assert not result.ok and "request failed" in result.message


def test_query_follows_next_page_and_honours_max_rows(requests_mock):
    requests_mock.get(
        OS_URL,
        [
            {"json": {"member": [{"assetnum": "1"}, {"assetnum": "2"}], "responseInfo": {"nextPage": {"href": f"{OS_URL}?page=2"}}}},
            {"json": {"member": [{"assetnum": "3"}], "responseInfo": {}}},
        ],
    )
    members = list(service().query("MXAPIASSET", "assetnum", where='siteid="X"', directives="savedQuery=Q", page_size=2))
    assert [m["assetnum"] for m in members] == ["1", "2", "3"]
    first = requests_mock.request_history[0]
    assert first.qs["oslc.where"] == ['siteid="x"'] and first.qs["savedquery"] == ["q"]

    requests_mock.reset_mock()
    assert len(list(service().query("MXAPIASSET", "assetnum", page_size=2, max_rows=1))) == 1


# --------------------------------------------------------------------------- runner


def test_dry_run_builds_payloads_and_never_posts(requests_mock):
    sheet = asset_sheet([["1", "BEDFORD", ""], ["2", "BEDFORD", ""]])
    summary = run_sheet(sheet, service(), RunOptions(execute=False))
    assert summary.dry_run and summary.validated == 2 and summary.errors == 0
    assert summary.outcomes[0].payload == {"_action": "AddChange", "assetnum": "1", "siteid": "BEDFORD"}
    assert requests_mock.request_history == []
    assert sheet.result_column is None  # sheet untouched on a dry run


def test_execute_writes_result_and_returned_ids_back_to_every_row(requests_mock):
    requests_mock.post(OS_URL, json=[ok(201, assetuid=11), err("BMXAA4153E site invalid")])
    sheet = asset_sheet([["1", "BEDFORD", ""], ["2", "NOPE", ""]])
    summary = run_sheet(sheet, service(), RunOptions(execute=True, batch_size=0, stop_on_error=False))

    assert (summary.created, summary.errors, summary.batches) == (1, 1, 1)
    assert sheet.data[0] == ["1", "BEDFORD", 11, "OK"]
    assert sheet.data[1][3] == "BMXAA4153E site invalid"


def test_batch_size_controls_requests(requests_mock):
    route = requests_mock.post(OS_URL, [{"json": [ok(), ok()]}, {"json": [ok()]}])
    sheet = asset_sheet([["1", "S", ""], ["2", "S", ""], ["3", "S", ""]])
    summary = run_sheet(sheet, service(), RunOptions(execute=True, batch_size=2))
    assert route.call_count == 2 and summary.batches == 2
    assert [len(r.json()) for r in route.request_history] == [2, 1]


def test_stop_on_error_halts_after_failing_batch_and_reports_resume_row(requests_mock):
    route = requests_mock.post(OS_URL, [{"json": [ok(), err("boom")]}, {"json": [ok()]}])
    sheet = asset_sheet([["1", "S", ""], ["2", "S", ""], ["3", "S", ""]])
    summary = run_sheet(sheet, service(), RunOptions(execute=True, batch_size=2, stop_on_error=True))
    assert route.call_count == 1 and summary.stopped and summary.resume_row == 4


def test_start_row_resumes_mid_sheet(requests_mock):
    route = requests_mock.post(OS_URL, json=[ok()])
    sheet = asset_sheet([["1", "S", ""], ["2", "S", ""]])
    run_sheet(sheet, service(), RunOptions(execute=True, start_row=4))
    assert route.last_request.json() == [{"_action": "AddChange", "assetnum": "2", "siteid": "S"}]


def test_type_error_fails_only_that_object_in_dry_run():
    sheet = asset_sheet([["1", "abc"], ["2", "5"]], headers=("ASSETNUM", "PRIORITY"))
    summary = run_sheet(
        sheet, None, RunOptions(stop_on_error=False), resolve=lambda path, attr: "INTEGER" if attr == "priority" else None
    )
    assert summary.errors == 1 and summary.validated == 1
    assert "PRIORITY (INTEGER) must be numeric" in summary.outcomes[0].message


@pytest.mark.parametrize("action", ["Sync", "Query"])
def test_load_runner_rejects_sync_and_query(action):
    with pytest.raises(SheetFormatError):
        run_sheet(asset_sheet([["1", "S", ""]], action=action), None, RunOptions())


def test_run_query_replaces_sheet_rows(requests_mock):
    requests_mock.get(OS_URL, json={"member": [{"assetnum": "9", "siteid": "S", "assetuid": 3}], "responseInfo": {}})
    sheet = asset_sheet([["old", "row", ""]], action="Query")
    assert run_query(sheet, service(), RunOptions()) == 1
    assert sheet.data == [["9", "S", 3]]
    assert requests_mock.last_request.qs["oslc.select"] == ["assetnum,siteid,assetuid"]


def test_run_nodes_sends_nested_children_in_one_record(requests_mock):
    route = requests_mock.post(f"{BASE_URL}/api/os/mxapipo", json=[ok()])
    sheet = MxSheet(
        "MXAPIPO", "PO", "AddChange",
        [parse_header(i, h) for i, h in enumerate(["PONUM", "POLINE.POLINENUM"])],
        [["1", "1"], ["1", "2"]],
    )
    nodes, _ = sheet_to_nodes(sheet)
    run_nodes([nodes], service(), "MXAPIPO", "AddChange", RunOptions(execute=True))
    assert json.loads(route.last_request.text) == [
        {"_action": "AddChange", "ponum": "1", "poline": [{"polinenum": "1"}, {"polinenum": "2"}]}
    ]
