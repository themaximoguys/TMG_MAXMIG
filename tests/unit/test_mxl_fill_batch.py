"""FillData, load scripts (the VBA-macro layer), [ERROR], and RunBatch."""

import json
import textwrap

import pytest
import requests
from click.testing import CliRunner

from maxmig.cli import main
from maxmig.mxl.batch import EntryReport, load_batch, new_batch, run_batch
from maxmig.mxl.fill import fill, load_mapping, read_table, table_rows, template_row
from maxmig.mxl.formula import Formula, LookupTable
from maxmig.mxl.runner import RunOptions, run_sheet
from maxmig.mxl.scripting import ScriptError, load_script
from maxmig.mxl.service import MxRestService
from maxmig.mxl.sheet import MxSheet, SheetFormatError, load_sheet, parse_header

BASE_URL = "https://mx.example.com/maximo"
OS_URL = f"{BASE_URL}/api/os/mxapiasset"


def ok(status=201, **data):
    return {"_responsemeta": {"status": str(status)}, "_responsedata": data}


def err(message, status=400):
    return {"_responsemeta": {"status": str(status)}, "_responsedata": {"Error": {"message": message}}}


def asset_sheet(rows=(), headers=("ASSETNUM", "SITEID", "[ASSETUID]"), action="AddChange"):
    return MxSheet("MXAPIASSET", "ASSET", action, [parse_header(i, h) for i, h in enumerate(headers)], [list(r) for r in rows])


def service():
    return MxRestService(requests.Session(), BASE_URL, {"apikey": "k"})


SOURCE = [
    {"Asset": "AS_P100", "Description": "PUMP 100", "Location": "BLK1000", "Status": "ACTIVE"},
    {"Asset": "AS_P101", "Description": "PUMP 101", "Location": "BR210", "Status": "SCRAPPED"},
    {"Asset": "AS_P102", "Description": "PUMP 102", "Location": "CSBRK571", "Status": "ACTIVE"},
]


def write(path, text):
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- fill


def test_fill_drags_template_down_one_row_per_source_row():
    sheet = asset_sheet(headers=("ASSETNUM", "SITEID", "DESCRIPTION", "LOCATION", "[ASSETUID]"))
    template = ['=CONCATENATE("PUMP", RIGHT([Asset],3))', "BEDFORD", "=PROPER([Description])", "=[Location]", ""]
    result = fill(sheet, template, SOURCE)
    assert result.errors == []
    assert sheet.data[0] == ["PUMP100", "BEDFORD", "Pump 100", "BLK1000", ""]
    assert len(sheet.data) == 3


def test_fill_collects_every_bad_row_instead_of_stopping():
    sheet = asset_sheet(headers=("ASSETNUM", "LOCATION"))
    lookups = {"LOC": LookupTable("LOC", ["OLD", "NEW"], [["BLK1000", "B-1"]])}
    result = fill(sheet, ["=[Asset]", '=VLOOKUP([Location],"LOC",2,FALSE)'], SOURCE, lookups)
    assert [(e.source_row, e.column) for e in result.errors] == [(2, "LOCATION"), (3, "LOCATION")]
    assert sheet.data == [["AS_P100", "B-1"]]


def test_fill_filter_drops_rows():
    sheet = asset_sheet(headers=("ASSETNUM",))
    result = fill(sheet, ["=[Asset]"], SOURCE, row_filter=Formula.parse('=[Status]<>"SCRAPPED"'))
    assert result.dropped == 1 and [r[0] for r in sheet.data] == ["AS_P100", "AS_P102"]


def test_fill_booleans_become_mxloader_yorn():
    sheet = asset_sheet(headers=("ASSETNUM", "ISRUNNING"))
    fill(sheet, ["=[Asset]", '=[Status]="ACTIVE"'], SOURCE[:2])
    assert [r[1] for r in sheet.data] == ["1", "0"]


# --------------------------------------------------------------------------- scripts


SCRIPT = """
from maxmig.mxl.scripting import formula

@formula("PADNUM")
def padnum(value, width):
    return str(value).zfill(int(width))

FUNCTIONS = {"shout": lambda s: str(s).upper() + "!"}

def transform_row(row, ctx):
    ctx.state["seen"] = ctx.state.get("seen", 0) + 1
    if row["STATUS"] == "SCRAPPED":
        return None
    row["ASSET"] = row["ASSET"].replace("AS_", "")
    return row

def before_run(ctx):
    ctx.state["log"] = ["start"]

def before_record(payload, ctx):
    if payload.get("assetnum") == "SKIPME":
        return None
    if payload.get("assetnum") == "BAD":
        raise ValueError("no BAD assets")
    payload["siteid"] = payload.get("siteid") or "BEDFORD"
    return payload

def after_record(outcome, ctx):
    ctx.state["log"].append(outcome.status)

def after_run(summary, ctx):
    ctx.state["log"].append("end")
"""


@pytest.fixture
def script(tmp_path):
    return load_script(write(tmp_path / "hooks.py", SCRIPT))


def test_script_registers_formula_udfs_and_hooks(script):
    assert set(script.functions) == {"PADNUM", "SHOUT"}
    assert set(script.hooks) == {"transform_row", "before_run", "before_record", "after_record", "after_run"}


def test_fill_uses_script_udfs_and_transform_row(script):
    sheet = asset_sheet(headers=("ASSETNUM", "DESCRIPTION"))
    result = fill(sheet, ["=PADNUM([Asset], 8)", "=SHOUT([Description])"], SOURCE, script=script)
    assert result.dropped == 1
    assert sheet.data == [["0000P100", "PUMP 100!"], ["0000P102", "PUMP 102!"]]


def test_load_hooks_run_in_order_and_shape_payloads(script, requests_mock):
    route = requests_mock.post(OS_URL, json=[ok(assetuid=1), ok(assetuid=2)])
    sheet = asset_sheet([["A1", ""], ["SKIPME", ""], ["BAD", ""], ["A2", "S2"]], headers=("ASSETNUM", "SITEID"))
    from maxmig.mxl.scripting import ScriptContext

    ctx = ScriptContext(sheet=sheet, action="AddChange", dry_run=False)
    summary = run_sheet(sheet, service(), RunOptions(execute=True, stop_on_error=False), script=script, ctx=ctx)
    sent = route.last_request.json()
    assert [p["assetnum"] for p in sent] == ["A1", "A2"]
    assert [p["siteid"] for p in sent] == ["BEDFORD", "S2"]
    assert summary.skipped == 1 and summary.errors == 1 and summary.created == 2
    assert ctx.state["log"][0] == "start" and ctx.state["log"][-1] == "end"
    assert sorted(ctx.state["log"][1:-1]) == ["created", "created", "error", "skipped"]
    assert sheet.data[2][-1].startswith("before_record: no BAD assets")


def test_before_record_error_counts_as_batch_failure_when_stopping(script, requests_mock):
    route = requests_mock.post(OS_URL, json=[ok()])
    sheet = asset_sheet([["BAD", ""], ["A2", ""]], headers=("ASSETNUM", "SITEID"))
    summary = run_sheet(sheet, service(), RunOptions(execute=True, batch_size=1), script=script)
    assert summary.stopped and summary.resume_row == 3 and not route.called


def test_broken_script_fails_to_load(tmp_path):
    with pytest.raises(ScriptError, match="failed to load"):
        load_script(write(tmp_path / "bad.py", "def oops(:\n"))
    with pytest.raises(ScriptError, match="must be a function"):
        load_script(write(tmp_path / "bad2.py", "before_run = 3\n"))
    with pytest.raises(ScriptError, match="not found"):
        load_script(tmp_path / "missing.py")


def test_before_run_failure_aborts(tmp_path):
    s = load_script(write(tmp_path / "h.py", "def before_run(ctx):\n    raise RuntimeError('nope')\n"))
    with pytest.raises(ScriptError, match="before_run failed: nope"):
        run_sheet(asset_sheet([["A", "", ""]]), None, RunOptions(), script=s)


# --------------------------------------------------------------------------- [ERROR]


def test_error_column_holds_errors_and_blanks_successes(requests_mock):
    requests_mock.post(OS_URL, json=[ok(assetuid=5), err("BMXAA4153E bad site")])
    sheet = asset_sheet([["A1", "S", ""], ["A2", "X", ""]], headers=("ASSETNUM", "SITEID", "[ERROR]"))
    sheet.data[0][2] = "stale error from last run"
    run_sheet(sheet, service(), RunOptions(execute=True, stop_on_error=False))
    error_col = sheet.error_column.index
    assert sheet.data[0][error_col] == ""
    assert "BMXAA4153E" in sheet.data[1][error_col]


# --------------------------------------------------------------------------- mapping files + CLI fill


@pytest.fixture
def fill_files(tmp_path):
    src = tmp_path / "src.csv"
    src.write_text("Asset,Description,Location,Status\n" + "\n".join(
        f"{r['Asset']},{r['Description']},{r['Location']},{r['Status']}" for r in SOURCE) + "\n", encoding="utf-8")
    write(tmp_path / "locs.csv", "OLD,NEW\nBLK1000,B-1000\nCSBRK571,C-571\nBR210,B-210\n")
    write(tmp_path / "hooks.py", SCRIPT)
    mapping = write(tmp_path / "assets.yaml", """
        object_service: MXAPIASSET
        object: ASSET
        action: AddChange
        source: src.csv
        lookups:
          LOCMAP: locs.csv
        filter: '=[Status] <> "SCRAPPED"'
        columns:
          ASSETNUM: '=CONCATENATE("PUMP", RIGHT([Asset], 3))'
          SITEID: BEDFORD
          DESCRIPTION: '=PROPER([Description])'
          LOCATION: '=VLOOKUP([Location], "LOCMAP", "NEW", FALSE)'
          "[ASSETUID]": ""
        """)
    return tmp_path, mapping


def test_load_mapping(fill_files):
    _, mapping_path = fill_files
    mapping = load_mapping(mapping_path)
    assert mapping.sheet.object_service == "MXAPIASSET" and mapping.sheet.action == "AddChange"
    assert [c.header for c in mapping.sheet.columns][-1] == "[ASSETUID]"
    assert "LOCMAP" in mapping.lookups and mapping.row_filter is not None


def test_cli_fill_from_mapping_writes_runnable_sheet(fill_files):
    tmp_path, mapping = fill_files
    out = tmp_path / "out.csv"
    result = CliRunner().invoke(main, ["mxl", "fill", str(mapping), "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert "filled 2 row(s) from 3 source row(s) (1 dropped" in result.output
    sheet = load_sheet(out)
    assert sheet.config_row()[:3] == ["MXAPIASSET", "ASSET", "AddChange"]
    assert sheet.data[1] == ["PUMP102", "BEDFORD", "Pump 102", "C-571", ""]


def test_cli_fill_reports_bad_rows_and_writes_nothing(fill_files):
    tmp_path, mapping = fill_files
    (tmp_path / "locs.csv").write_text("OLD,NEW\nBLK1000,B-1000\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    result = CliRunner().invoke(main, ["mxl", "fill", str(mapping), "--output", str(out)])
    assert result.exit_code != 0
    assert "source row 3 [LOCATION]: #N/A" in result.output
    assert not out.exists()


def test_cli_fill_from_csv_template_with_script_and_lookup(fill_files):
    tmp_path, _ = fill_files
    tpl = write(tmp_path / "tpl.csv", """
        MXAPIASSET,ASSET,AddChange,,,
        ASSETNUM,LOCATION
        "=PADNUM([Asset], 8)","=VLOOKUP([Location],""L"",2,FALSE)"
        """)
    out = tmp_path / "o.csv"
    result = CliRunner().invoke(main, [
        "mxl", "fill", str(tpl), "--source", str(tmp_path / "src.csv"),
        "--lookup", f"L={tmp_path / 'locs.csv'}", "--script", str(tmp_path / "hooks.py"), "--output", str(out),
    ])
    assert result.exit_code == 0, result.output
    assert load_sheet(out).data == [["0000P100", "B-1000"], ["0000P102", "C-571"]]


def test_xlsx_template_reads_formulas_d1_source_and_refdata_sheets(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Assets"
    ws.append(["MXAPIASSET", "ASSET", "AddChange", "RefData"])
    ws.append(["ASSETNUM", "LOCATION"])
    ws.append(['=CONCATENATE("PUMP", RIGHT([Asset],3))', '=VLOOKUP([Location],"LocMap",2,FALSE)'])
    ref = wb.create_sheet("RefData")
    ref.append(["Asset", "Location"])
    ref.append(["AS_P100", "BLK1000"])
    locs = wb.create_sheet("LocMap")
    locs.append(["OLD", "NEW"])
    locs.append(["BLK1000", "B-1000"])
    book = tmp_path / "book.xlsx"
    wb.save(book)

    assert template_row(load_sheet(book, "Assets"))[0].startswith("=CONCATENATE")
    out = tmp_path / "filled.xlsx"
    result = CliRunner().invoke(main, ["mxl", "fill", str(book), "--sheet", "Assets", "--output", str(out)])
    assert result.exit_code == 0, result.output
    filled = load_sheet(out)
    assert [r[:2] for r in filled.data] == [["PUMP100", "B-1000"]] and filled.where == ""


def test_read_table_skips_blank_rows(tmp_path):
    header, data = read_table(write(tmp_path / "t.csv", "A,B\n1,2\n,\n3,4\n"))
    assert header == ["A", "B"] and list(table_rows(header, data)) == [{"A": "1", "B": "2"}, {"A": "3", "B": "4"}]


# --------------------------------------------------------------------------- RunBatch


def make_sheets(tmp_path):
    write(tmp_path / "a.csv", "MXAPIASSET,ASSET,AddChange,,,\nASSETNUM,SITEID\nA1,S\n")
    write(tmp_path / "b.csv", "MXAPIASSET,ASSET,AddChange,,,\nASSETNUM,SITEID\nB1,S\n")
    write(tmp_path / "c.csv", "MXAPIASSET,ASSET,AddChange,,,\nASSETNUM,SITEID\nC1,S\n")


def test_load_batch_csv(tmp_path):
    path = write(tmp_path / "batch.csv", """
        RunBatch
        Sheet,Action,Status
        a.csv,,
        b.csv,skip,
        c.csv,exportcsv,
        """)
    batch = load_batch(path)
    assert [(e.row, e.sheet, e.action) for e in batch.entries] == [(3, "a.csv", ""), (4, "b.csv", "Skip"), (5, "c.csv", "ExportCsv")]


def test_load_batch_rejects_bad_marker_and_xml(tmp_path):
    with pytest.raises(SheetFormatError, match="RunBatch"):
        load_batch(write(tmp_path / "x.csv", "Nope\n"))
    with pytest.raises(SheetFormatError, match="ExportXml"):
        load_batch(write(tmp_path / "y.csv", "RunBatch\nSheet,Action\na.csv,ExportXml\n"))


def test_run_batch_stops_at_first_failing_sheet(tmp_path):
    make_sheets(tmp_path)
    batch = new_batch([("a.csv", ""), ("b.csv", "Skip"), ("c.csv", "Add"), ("a.csv", "")], tmp_path / "batch.csv")
    seen = []

    def run_one(sheet, action):
        seen.append((sheet.data[0][0], action))
        return EntryReport(sync=1, error="1 object(s) failed" if action == "Add" else "", stop=action == "Add")

    summary = run_batch(batch, run_one)
    assert seen == [("A1", "AddChange"), ("C1", "Add")]
    assert summary.stopped_at == 5 and summary.skipped == 1
    assert [e.status for e in batch.entries] == ["OK", "Skipped", "Error", ""]
    batch.save()
    assert load_batch(batch.path).entries[2].error == "1 object(s) failed"


def test_run_batch_continue_on_error_and_missing_sheet(tmp_path):
    make_sheets(tmp_path)
    batch = new_batch([("missing.csv", ""), ("a.csv", "")], tmp_path / "batch.csv")
    summary = run_batch(batch, lambda s, a: EntryReport(sync=1), continue_on_error=True)
    assert summary.failed == 1 and summary.ran == 2 and "missing.csv" in batch.entries[0].error


def test_run_batch_exportcsv(tmp_path):
    make_sheets(tmp_path)
    batch = new_batch([("a.csv", "ExportCsv")], tmp_path / "batch.csv")
    run_batch(batch, lambda s, a: pytest.fail("no Maximo call for ExportCsv"))
    assert (tmp_path / "Export" / "a.csv").exists() and batch.entries[0].status.startswith("OK")


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env.mx"
    path.write_text(f"MX_MANAGE_URL={BASE_URL}\nMX_MANAGE_APIKEY=k\n", encoding="utf-8")
    return path


def test_cli_batch_executes_sheets_in_order_and_writes_status(tmp_path, env_file, requests_mock):
    make_sheets(tmp_path)
    route = requests_mock.post(OS_URL, [{"json": [ok(assetuid=1)]}, {"json": [err("BMXAA boom")]}])
    write(tmp_path / "batch.csv", "RunBatch\nSheet,Action\na.csv,\nb.csv,\nc.csv,\n")
    result = CliRunner().invoke(main, [
        "mxl", "batch", str(tmp_path / "batch.csv"), "--env", "MX", "--env-file", str(env_file), "--execute",
    ])
    assert result.exit_code != 0 and "batch stopped at row 4" in result.output
    assert route.call_count == 2
    assert [json.loads(r.body)[0]["assetnum"] for r in route.request_history] == ["A1", "B1"]
    entries = load_batch(tmp_path / "batch.csv").entries
    assert [e.status for e in entries] == ["OK", "Error", ""]
    assert load_sheet(tmp_path / "b.csv").data[0][-1] == "BMXAA boom"


def test_cli_batch_dry_run_touches_nothing(tmp_path, env_file, requests_mock):
    make_sheets(tmp_path)
    write(tmp_path / "batch.csv", "RunBatch\nSheet,Action\na.csv,\nb.csv,\n")
    before = (tmp_path / "a.csv").read_text()
    result = CliRunner().invoke(main, ["mxl", "batch", str(tmp_path / "batch.csv"), "--env", "MX", "--env-file", str(env_file)])
    assert result.exit_code == 0, result.output
    assert "DRY RUN batch: 2 ran, 0 failed" in result.output
    assert not requests_mock.called and (tmp_path / "a.csv").read_text() == before


def test_cli_run_with_script(tmp_path, env_file, requests_mock):
    write(tmp_path / "hooks.py", SCRIPT)
    write(tmp_path / "s.csv", "MXAPIASSET,ASSET,AddChange,,,\nASSETNUM,SITEID\nA1,\nSKIPME,\n")
    route = requests_mock.post(OS_URL, json=[ok()])
    result = CliRunner().invoke(main, [
        "mxl", "run", str(tmp_path / "s.csv"), "--env", "MX", "--env-file", str(env_file),
        "--execute", "--script", str(tmp_path / "hooks.py"),
    ])
    assert result.exit_code == 0, result.output
    assert "skipped by script: 1" in result.output
    assert route.last_request.json() == [{"assetnum": "A1", "siteid": "BEDFORD", "_action": "AddChange"}]
