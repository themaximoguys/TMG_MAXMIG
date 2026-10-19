"""`migrate export --checkpoint / --resume`: continue a large export after a failure."""

import csv
import json
from functools import partialmethod

import pytest
from click.testing import CliRunner

from maxmig.api.client import MaximoClient
from maxmig.cli import main
from maxmig.extract.checkpoint import CheckpointError, ExportCheckpoint, find_latest

BASE_URL = "https://masdev.example.com/maximo"
PO_URL = f"{BASE_URL}/api/os/mxapipo"
REST_URL = f"{BASE_URL}/rest/mbo/po"
PAGE = 2

MODULE_YAML = """
module: purchasing
objects:
  - name: purchase_order
    maximo_object: PO
    access: {oslc_resource: mxapipo, rest_mbo_table: po, prefer: oslc}
    key_fields: [ponum]
    fields: [ponum, status]
    children:
      - name: po_line
        maximo_object: POLINE
        relation_attribute: poline
        foreign_key: [ponum]
        key_fields: [ponum, polinenum]
        fields: [polinenum, itemnum]
        children: []
  - name: company
    maximo_object: COMPANY
    access: {oslc_resource: mxapicompany, prefer: oslc}
    key_fields: [company]
    fields: [company, name]
    children: []
extract_order: [company, purchase_order]
"""

POS = [
    {"ponum": f"PO{i}", "status": "APPR", "poline": [{"polinenum": 1, "itemnum": f"I{i}"}, {"polinenum": 2, "itemnum": f"J{i}"}]}
    for i in range(1, 6)
]


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr(MaximoClient, "__init__", partialmethod(MaximoClient.__init__, page_size=PAGE))


@pytest.fixture
def setup(tmp_path):
    config = tmp_path / "config" / "modules"
    config.mkdir(parents=True)
    (config / "purchasing.yaml").write_text(MODULE_YAML, encoding="utf-8")
    env = tmp_path / ".env.masdev"
    env.write_text(f"MASDEV_MANAGE_URL={BASE_URL}\nMASDEV_MANAGE_APIKEY=k\n", encoding="utf-8")
    return config, env, tmp_path / "out"


def oslc_pages(rows, fail_on=None, error=None):
    """requests-mock callback serving `rows` by explicit pageno; page `fail_on` fails with a
    non-retryable 401, so the failure path doesn't wait out the retry/backoff budget."""
    calls = []

    def callback(request, context):
        pageno = int(request.qs.get("pageno", ["1"])[0])
        size = int(request.qs["oslc.pagesize"][0])
        calls.append(pageno)
        if pageno == fail_on:
            context.status_code = 401
            return error or {"Error": {"message": "connection reset", "reasonCode": "BMXAA0000E"}}
        start = (pageno - 1) * size
        if start >= len(rows) and pageno > 1:
            context.status_code = 400
            return {"Error": {"message": "page not found", "reasonCode": "BMXAA8732E"}}
        info = {}
        if start + size < len(rows):  # plain (non-checkpointed) exports follow nextPage links
            info["nextPage"] = {"href": f"{PO_URL}?pageno={pageno + 1}&oslc.pageSize={size}"}
        return {"member": rows[start : start + size], "responseInfo": info}

    callback.calls = calls
    return callback


def export(setup, *extra, obj="purchase_order"):
    config, env, out = setup
    args = ["export", "--module", "purchasing", "--env", "MASDEV", "--env-file", str(env),
            "--config-dir", str(config), "--out", str(out)]
    if obj:
        args += ["--object", obj]
    return CliRunner().invoke(main, args + list(extra))


def read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def only(directory, pattern):
    found = sorted(directory.glob(pattern))
    assert len(found) == 1, found
    return found[0]


# --------------------------------------------------------------------------- happy path


def test_checkpointed_run_matches_plain_run_and_cleans_up(setup, requests_mock, tmp_path):
    requests_mock.get(PO_URL, json=oslc_pages(POS))
    plain = export(setup)
    assert plain.exit_code == 0, plain.output
    out = setup[2]
    plain_po = read(only(out / "purchasing" / "purchase_order", "*.csv"))
    plain_lines = read(only(out / "purchasing" / "po_line", "*.csv"))

    second = setup[0], setup[1], tmp_path / "out2"
    result = export(second, "--checkpoint")
    assert result.exit_code == 0, result.output
    po_dir = second[2] / "purchasing" / "purchase_order"
    assert read(only(po_dir, "*.csv")) == plain_po
    assert read(only(second[2] / "purchasing" / "po_line", "*.csv")) == plain_lines
    assert not list(po_dir.glob("*.checkpoint.json")) and not list(po_dir.glob("*.partial"))
    manifest = json.loads(only(po_dir, "*.manifest.json").read_text())
    assert manifest["checkpointed"] and manifest["resumed"] is False and manifest["row_count"] == 5


# --------------------------------------------------------------------------- failure + resume


def test_failure_keeps_partials_and_checkpoint_then_resume_completes(setup, requests_mock):
    failing = oslc_pages(POS, fail_on=2)
    requests_mock.get(PO_URL, json=failing)
    result = export(setup, "--checkpoint")
    assert result.exit_code != 0
    assert "--resume" in result.output and "after 1 page(s)" in result.output

    out = setup[2]
    po_dir = out / "purchasing" / "purchase_order"
    checkpoint = ExportCheckpoint.load(find_latest(po_dir, "purchase_order"))
    assert (checkpoint.api, checkpoint.cursor, checkpoint.pages) == ("oslc", 2, 1)
    assert checkpoint.nodes["purchase_order"]["count"] == 2 and checkpoint.nodes["po_line"]["count"] == 4
    assert len(read(only(po_dir, "*.csv.partial"))) == 3  # header + page 1

    resumed = oslc_pages(POS)
    requests_mock.get(PO_URL, json=resumed)
    result = export(setup, "--resume")
    assert result.exit_code == 0, result.output
    assert "resuming purchase_order from oslc cursor 2 (2 rows already written)" in result.output
    assert resumed.calls[0] == 2  # page 1 was never fetched again

    rows = read(only(po_dir, "*.csv"))
    assert rows[0] == ["ponum", "status"] and [r[0] for r in rows[1:]] == ["PO1", "PO2", "PO3", "PO4", "PO5"]
    lines = read(only(out / "purchasing" / "po_line", "*.csv"))
    assert len(lines) == 11 and lines[0] == ["ponum", "polinenum", "itemnum"]
    manifest = json.loads(only(po_dir, "*.manifest.json").read_text())
    assert manifest["resumed"] is True and manifest["resume_count"] == 1 and manifest["row_count"] == 5
    assert manifest["children"]["po_line"]["row_count"] == 10
    assert not list(po_dir.glob("*.checkpoint.json")) and not list(po_dir.glob("*.partial"))


def test_resume_truncates_half_written_page(setup, requests_mock):
    requests_mock.get(PO_URL, json=oslc_pages(POS, fail_on=3))
    export(setup, "--checkpoint")
    po_dir = setup[2] / "purchasing" / "purchase_order"
    partial = only(po_dir, "*.csv.partial")
    with partial.open("a", encoding="utf-8") as fh:
        fh.write("PO3,APP")  # a crash mid-row after the last checkpoint

    requests_mock.get(PO_URL, json=oslc_pages(POS))
    assert export(setup, "--resume").exit_code == 0
    assert [r[0] for r in read(only(po_dir, "*.csv"))[1:]] == ["PO1", "PO2", "PO3", "PO4", "PO5"]


def test_failure_on_first_page_leaves_nothing_behind(setup, requests_mock):
    requests_mock.get(PO_URL, json=oslc_pages(POS, fail_on=1))
    requests_mock.get(REST_URL, status_code=401)
    assert export(setup, "--checkpoint").exit_code != 0
    po_dir = setup[2] / "purchasing" / "purchase_order"
    assert not list(po_dir.glob("*.partial")) and find_latest(po_dir, "purchase_order") is None


def test_resume_without_checkpoint_starts_fresh(setup, requests_mock):
    requests_mock.get(PO_URL, json=oslc_pages(POS))
    result = export(setup, "--resume")
    assert result.exit_code == 0, result.output
    assert "no checkpoint" in result.output


def test_resume_rejects_a_different_query(setup, requests_mock):
    requests_mock.get(PO_URL, json=oslc_pages(POS, fail_on=2))
    export(setup, "--checkpoint")
    result = export(setup, "--resume", "--site", "BEDFORD")
    assert result.exit_code != 0 and "different query" in result.output


def test_checkpoint_load_rejects_unknown_version(tmp_path):
    path = tmp_path / "x_1.checkpoint.json"
    path.write_text(json.dumps({"version": 99}))
    with pytest.raises(CheckpointError, match="version"):
        ExportCheckpoint.load(path)


# --------------------------------------------------------------------------- REST MBO path


def test_rest_mbo_resume_continues_from_rs_start(setup, requests_mock):
    config, env, out = setup
    (config / "purchasing.yaml").write_text(MODULE_YAML.replace("prefer: oslc}", "prefer: rest_mbo}", 1))
    flat = [{"Attributes": {"PONUM": {"content": p["ponum"]}, "STATUS": {"content": "APPR"}}} for p in POS]
    starts = []

    def mbo(request, context, fail_at=None):
        start = int(request.qs["_rsstart"][0])
        starts.append(start)
        if start == fail_at:
            context.status_code = 401
            return {}
        return {"POMboSet": {"PO": flat[start : start + PAGE], "rsCount": len(flat[start : start + PAGE]), "rsTotal": len(flat)}}

    requests_mock.get(REST_URL, json=lambda r, c: mbo(r, c, fail_at=4))
    assert export(setup, "--checkpoint", "--flat").exit_code != 0
    po_dir = out / "purchasing" / "purchase_order"
    assert ExportCheckpoint.load(find_latest(po_dir, "purchase_order")).cursor == 4

    starts.clear()
    requests_mock.get(REST_URL, json=mbo)
    result = export(setup, "--resume", "--flat")
    assert result.exit_code == 0, result.output
    assert starts == [4]
    assert [r[0] for r in read(only(po_dir, "*.csv"))[1:]] == ["PO1", "PO2", "PO3", "PO4", "PO5"]


# --------------------------------------------------------------------------- capture-all spool


def test_capture_all_resume_keeps_extras_seen_before_the_failure(setup, requests_mock):
    rows = [dict(p, **({"ex_custom": "C1"} if p["ponum"] == "PO1" else {})) for p in POS]
    requests_mock.get(PO_URL, json=oslc_pages(rows, fail_on=2))
    assert export(setup, "--checkpoint", "--flat", "--all-attributes").exit_code != 0
    po_dir = setup[2] / "purchasing" / "purchase_order"
    assert list(po_dir.glob("*.spool.jsonl"))

    requests_mock.get(PO_URL, json=oslc_pages(rows))
    assert export(setup, "--resume", "--flat", "--all-attributes").exit_code == 0
    result = read(only(po_dir, "*.csv"))
    assert result[0] == ["ponum", "status", "ex_custom"]
    assert result[1][2] == "C1" and len(result) == 6
    assert not list(po_dir.glob("*.spool.jsonl"))


# --------------------------------------------------------------------------- skip-errors


def test_skip_errors_survive_a_resume(setup, requests_mock):
    def callback(request, context):
        pageno = int(request.qs["pageno"][0])
        size = int(request.qs["oslc.pagesize"][0])
        if size == PAGE and pageno == 1:  # page 1 has a bad record -> recovered row by row
            context.status_code = 400
            return {"Error": {"message": "rotating", "reasonCode": "BMXAA1825E"}}
        if size == 1:
            if pageno == 2:
                context.status_code = 400
                return {"Error": {"message": "rotating", "reasonCode": "BMXAA1825E"}}
            return {"member": POS[pageno - 1 : pageno]}
        if pageno == callback.fail:
            context.status_code = 401
            return {"Error": {"message": "boom"}}
        start = (pageno - 1) * size
        return {"member": POS[start : start + size]}

    callback.fail = 2
    requests_mock.get(PO_URL, json=callback)
    assert export(setup, "--checkpoint", "--flat", "--skip-errors").exit_code != 0
    callback.fail = None
    result = export(setup, "--resume", "--flat", "--skip-errors")
    assert result.exit_code == 0, result.output
    po_dir = setup[2] / "purchasing" / "purchase_order"
    assert [r[0] for r in read(only(po_dir, "*.csv"))[1:]] == ["PO1", "PO3", "PO4", "PO5"]
    manifest = json.loads(only(po_dir, "*.manifest.json").read_text())
    assert manifest["skipped_count"] == 1


# --------------------------------------------------------------------------- whole module


def test_whole_module_resume_skips_finished_objects(setup, requests_mock):
    company = requests_mock.get(f"{BASE_URL}/api/os/mxapicompany", json={"member": [{"company": "A", "name": "a"}]})
    requests_mock.get(PO_URL, json=oslc_pages(POS, fail_on=2))
    assert export(setup, "--checkpoint", obj=None).exit_code != 0
    assert company.call_count == 1

    requests_mock.get(PO_URL, json=oslc_pages(POS))
    result = export(setup, "--resume", obj=None)
    assert result.exit_code == 0, result.output
    assert "company: already exported" in result.output and company.call_count == 1
    out = setup[2] / "purchasing"
    stamp = only(out / "company", "*.csv").stem.split("_", 1)[1]
    assert only(out / "purchase_order", "*.csv").stem.endswith(stamp)  # one run timestamp across the module
