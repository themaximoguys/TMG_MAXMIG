import json

import pytest
from click.testing import CliRunner

from maxmig.cli import main

BASE_URL = "https://masdev.example.com/maximo"
OSLC_URL = f"{BASE_URL}/api/os/mxapicompany"
REST_MBO_URL = f"{BASE_URL}/rest/mbo/company"

MODULE_YAML = """
module: purchasing
description: "Test fixture module."
objects:
  - name: company
    maximo_object: COMPANY
    access:
      oslc_resource: mxapicompany
      rest_mbo_table: company
      prefer: oslc
    key_fields: [company, orgid]
    fields: [name, company, orgid, type]
    where: null
    children: []
  - name: purchase_order
    maximo_object: PO
    access:
      oslc_resource: mxapipo
      rest_mbo_table: po
      prefer: oslc
    key_fields: [ponum]
    fields: [ponum, status]
    where: null
    children:
      - name: po_line
        maximo_object: POLINE
        relation_attribute: poline
        foreign_key: [ponum]
        key_fields: [ponum, polinenum]
        fields: [polinenum, itemnum]
        children: []
extract_order: [company, purchase_order]
load_order: [company, purchase_order]
"""

OSLC_ROWS = [
    {"company": "ACME", "orgid": "EAGLENA", "name": "Acme, Inc", "type": "V"},
    {"company": "BETA", "orgid": "EAGLENA", "name": "Beta Ltd", "type": "M"},
]


@pytest.fixture
def config_dir(tmp_path):
    directory = tmp_path / "config" / "modules"
    directory.mkdir(parents=True)
    (directory / "purchasing.yaml").write_text(MODULE_YAML, encoding="utf-8")
    return directory


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env.masdev"
    path.write_text(
        f"MASDEV_MANAGE_URL={BASE_URL}\n"
        "MASDEV_MANAGE_USERNAME=maxadmin\n"
        "MASDEV_MANAGE_PASSWORD=secret\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def oslc_ok(requests_mock):
    return requests_mock.get(OSLC_URL, json={"member": OSLC_ROWS, "responseInfo": {}})


def base_args(env_file, config_dir, out_dir):
    return [
        "export",
        "--module", "purchasing",
        "--object", "company",
        "--env", "MASDEV",
        "--env-file", str(env_file),
        "--config-dir", str(config_dir),
        "--out", str(out_dir),
    ]


def run_export(env_file, config_dir, out_dir, *extra):
    return CliRunner().invoke(main, base_args(env_file, config_dir, out_dir) + list(extra))


def object_dir(out_dir, module, object_name):
    """Every object/relation gets its own stable {out}/{module}/{object}/ folder."""
    return out_dir / module / object_name


def _single_glob(directory, pattern):
    (path,) = sorted(directory.glob(pattern))
    return path


def csv_path(out_dir, module, object_name):
    return _single_glob(object_dir(out_dir, module, object_name), f"{object_name}_*.csv")


def manifest_path(out_dir, module="purchasing", object_name="company"):
    return _single_glob(object_dir(out_dir, module, object_name), f"{object_name}_*.manifest.json")


def read_csv_lines(out_dir, module="purchasing", object_name="company"):
    return csv_path(out_dir, module, object_name).read_text(encoding="utf-8").splitlines()


def test_export_writes_csv_with_key_fields_first_and_no_duplicate_columns(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    result = run_export(env_file, config_dir, out_dir)

    assert result.exit_code == 0, result.output
    assert read_csv_lines(out_dir) == [
        "company,orgid,name,type",
        'ACME,EAGLENA,"Acme, Inc",V',
        "BETA,EAGLENA,Beta Ltd,M",
    ]


def test_export_csv_filename_embeds_the_run_timestamp(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    run_export(env_file, config_dir, out_dir)

    name = csv_path(out_dir, "purchasing", "company").stem
    assert name.startswith("company_")
    timestamp = name.removeprefix("company_")
    assert len(timestamp) == 16 and timestamp[8] == "T" and timestamp.endswith("Z")


def test_export_writes_manifest_with_row_count_and_csv_path(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    run_export(env_file, config_dir, out_dir)

    manifest = json.loads(manifest_path(out_dir).read_text(encoding="utf-8"))

    assert manifest["module"] == "purchasing"
    assert manifest["object"] == "company"
    assert manifest["env_prefix"] == "MASDEV"
    assert manifest["row_count"] == 2
    assert manifest["csv_path"].endswith(".csv")
    assert "purchasing/company/company_" in manifest["csv_path"]
    assert manifest["started_at"] <= manifest["finished_at"]


def test_export_prints_row_count_and_path_on_success(tmp_path, env_file, config_dir, oslc_ok):
    out_dir = tmp_path / "exports"
    result = run_export(env_file, config_dir, out_dir)

    assert "2" in result.stdout
    assert "company_" in result.stdout
    assert ".csv" in result.stdout


def test_export_with_no_rows_writes_header_only_and_row_count_zero(
    tmp_path, env_file, config_dir, requests_mock
):
    requests_mock.get(OSLC_URL, json={"member": [], "responseInfo": {}})
    out_dir = tmp_path / "exports"

    result = run_export(env_file, config_dir, out_dir)

    assert result.exit_code == 0, result.output
    assert read_csv_lines(out_dir) == ["company,orgid,name,type"]
    manifest = json.loads(manifest_path(out_dir).read_text(encoding="utf-8"))
    assert manifest["row_count"] == 0


def test_filter_options_are_anded_into_the_oslc_where(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    result = run_export(
        env_file, config_dir, out_dir, "--site", "BEDFORD", "--filter", "type=V"
    )

    assert result.exit_code == 0, result.output
    where = oslc_ok.last_request.qs["oslc.where"][0]
    assert 'siteid="bedford"' in where  # requests_mock lowercases parsed query values
    assert 'type="v"' in where
    assert " and " in where


def test_raw_where_overrides_the_structured_filters(tmp_path, env_file, config_dir, oslc_ok):
    out_dir = tmp_path / "exports"
    result = run_export(
        env_file, config_dir, out_dir, "--site", "BEDFORD", "--where", 'status!="OBSOLETE"'
    )

    assert result.exit_code == 0, result.output
    where = oslc_ok.last_request.qs["oslc.where"][0]
    assert "siteid" not in where
    assert "obsolete" in where


def test_filter_value_containing_an_equals_sign_splits_on_the_first_one_only(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    result = run_export(env_file, config_dir, out_dir, "--filter", "description=a=b")

    assert result.exit_code == 0, result.output
    assert 'description="a=b"' in oslc_ok.last_request.qs["oslc.where"][0]


def test_conflicting_site_and_siteid_filter_exits_nonzero_without_a_traceback(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    result = run_export(
        env_file, config_dir, out_dir, "--site", "BEDFORD", "--filter", "siteid=MAIN"
    )

    assert result.exit_code != 0
    assert "conflicting site filter" in result.stderr
    assert "Traceback" not in result.stderr
    assert not oslc_ok.called


def test_malformed_filter_without_an_equals_sign_exits_nonzero(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    result = run_export(env_file, config_dir, out_dir, "--filter", "typeV")

    assert result.exit_code != 0
    assert "typeV" in result.stderr
    assert not oslc_ok.called
    assert not out_dir.exists()


def test_missing_required_env_var_exits_nonzero_without_a_traceback(
    tmp_path, config_dir, oslc_ok
):
    incomplete = tmp_path / ".env.masdev"
    incomplete.write_text(f"MASDEV_MANAGE_URL={BASE_URL}\n", encoding="utf-8")
    out_dir = tmp_path / "exports"

    result = run_export(incomplete, config_dir, out_dir)

    assert result.exit_code != 0
    assert "MASDEV_MANAGE_USERNAME" in result.stderr
    assert "Traceback" not in result.stderr


def test_unknown_module_exits_nonzero_with_a_clear_message(tmp_path, env_file, config_dir):
    out_dir = tmp_path / "exports"
    result = CliRunner().invoke(
        main,
        [
            "export",
            "--module", "nosuchmodule",
            "--object", "company",
            "--env", "MASDEV",
            "--env-file", str(env_file),
            "--config-dir", str(config_dir),
            "--out", str(out_dir),
        ],
    )

    assert result.exit_code != 0
    assert "nosuchmodule" in result.stderr
    assert "Traceback" not in result.stderr


def test_unknown_object_exits_nonzero_with_a_clear_message(tmp_path, env_file, config_dir):
    out_dir = tmp_path / "exports"
    result = CliRunner().invoke(
        main,
        [
            "export",
            "--module", "purchasing",
            "--object", "nosuchobject",
            "--env", "MASDEV",
            "--env-file", str(env_file),
            "--config-dir", str(config_dir),
            "--out", str(out_dir),
        ],
    )

    assert result.exit_code != 0
    assert "nosuchobject" in result.stderr
    assert "Traceback" not in result.stderr


def test_api_failure_exits_nonzero_with_export_failed_message(
    tmp_path, env_file, config_dir, requests_mock
):
    """401 rather than 500: non-retryable, so this exercises the failure path
    without waiting out the retry/backoff budget."""
    requests_mock.get(OSLC_URL, status_code=401)
    requests_mock.get(REST_MBO_URL, status_code=401)
    out_dir = tmp_path / "exports"

    result = run_export(env_file, config_dir, out_dir)

    assert result.exit_code != 0
    assert "export of 'company' failed" in result.stderr
    assert "Traceback" not in result.stderr


def test_nonexistent_env_file_exits_nonzero(tmp_path, config_dir):
    out_dir = tmp_path / "exports"
    result = run_export(tmp_path / "nope.env", config_dir, out_dir)

    assert result.exit_code != 0
    assert "Traceback" not in result.stderr


def test_env_file_defaults_to_env_dot_env_lowercase_prefix_under_cwd(
    tmp_path, config_dir, requests_mock, monkeypatch
):
    requests_mock.get(OSLC_URL, json={"member": OSLC_ROWS, "responseInfo": {}})
    cwd = tmp_path / "workdir"
    (cwd / "env").mkdir(parents=True)
    (cwd / "env" / ".env.masdev").write_text(
        f"MASDEV_MANAGE_URL={BASE_URL}\n"
        "MASDEV_MANAGE_USERNAME=maxadmin\n"
        "MASDEV_MANAGE_PASSWORD=secret\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd)

    result = CliRunner().invoke(
        main,
        [
            "export",
            "--module", "purchasing",
            "--object", "company",
            "--env", "MASDEV",
            "--config-dir", str(config_dir),
            "--out", "exports",
        ],
    )

    assert result.exit_code == 0, result.output
    assert list((cwd / "exports" / "purchasing" / "company").glob("company_*.csv"))


def test_export_writes_a_separate_csv_per_child_relation(tmp_path, env_file, config_dir, requests_mock):
    po_url = f"{BASE_URL}/api/os/mxapipo"
    requests_mock.get(
        po_url,
        json={
            "member": [
                {"ponum": "PO1", "status": "APPR", "poline": [{"polinenum": 1, "itemnum": "0-0031"}]},
            ],
            "responseInfo": {},
        },
    )
    out_dir = tmp_path / "exports"

    result = CliRunner().invoke(
        main,
        [
            "export", "--module", "purchasing", "--object", "purchase_order",
            "--env", "MASDEV", "--env-file", str(env_file),
            "--config-dir", str(config_dir), "--out", str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert csv_path(out_dir, "purchasing", "purchase_order").read_text(encoding="utf-8").splitlines() == [
        "ponum,status",
        "PO1,APPR",
    ]
    assert csv_path(out_dir, "purchasing", "po_line").read_text(encoding="utf-8").splitlines() == [
        "ponum,polinenum,itemnum",
        "PO1,1,0-0031",
    ]
    manifest = json.loads(manifest_path(out_dir, "purchasing", "purchase_order").read_text(encoding="utf-8"))
    assert manifest["row_count"] == 1
    assert manifest["children"]["po_line"]["row_count"] == 1
    assert "1" in result.stdout  # child row count echoed too


def test_export_without_object_exports_every_object_in_extract_order(
    tmp_path, env_file, config_dir, requests_mock
):
    requests_mock.get(OSLC_URL, json={"member": OSLC_ROWS, "responseInfo": {}})
    requests_mock.get(
        f"{BASE_URL}/api/os/mxapipo",
        json={"member": [{"ponum": "PO1", "status": "APPR"}], "responseInfo": {}},
    )
    out_dir = tmp_path / "exports"

    result = CliRunner().invoke(
        main,
        [
            "export", "--module", "purchasing",
            "--env", "MASDEV", "--env-file", str(env_file),
            "--config-dir", str(config_dir), "--out", str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert csv_path(out_dir, "purchasing", "company").exists()
    assert csv_path(out_dir, "purchasing", "purchase_order").exists()
    assert "module 'purchasing': exported 2 objects, 3 total rows" in result.output


def test_export_with_explicit_object_does_not_print_module_summary_line(
    tmp_path, env_file, config_dir, oslc_ok
):
    out_dir = tmp_path / "exports"
    result = run_export(env_file, config_dir, out_dir)

    assert result.exit_code == 0, result.output
    assert "module 'purchasing'" not in result.output


def test_export_without_object_stops_at_the_first_failing_object(
    tmp_path, env_file, config_dir, requests_mock
):
    requests_mock.get(OSLC_URL, json={"member": OSLC_ROWS, "responseInfo": {}})
    requests_mock.get(f"{BASE_URL}/api/os/mxapipo", status_code=401)
    requests_mock.get(f"{BASE_URL}/rest/mbo/po", status_code=401)
    out_dir = tmp_path / "exports"

    result = CliRunner().invoke(
        main,
        [
            "export", "--module", "purchasing",
            "--env", "MASDEV", "--env-file", str(env_file),
            "--config-dir", str(config_dir), "--out", str(out_dir),
        ],
    )

    assert result.exit_code != 0
    assert "export of 'purchase_order' failed" in result.stderr
    assert csv_path(out_dir, "purchasing", "company").exists()  # already-succeeded object is kept


def test_flat_flag_skips_child_relations_entirely(tmp_path, env_file, config_dir, requests_mock):
    po_url = f"{BASE_URL}/api/os/mxapipo"
    request = requests_mock.get(
        po_url,
        json={"member": [{"ponum": "PO1", "status": "APPR"}], "responseInfo": {}},
    )
    out_dir = tmp_path / "exports"

    result = CliRunner().invoke(
        main,
        [
            "export", "--module", "purchasing", "--object", "purchase_order",
            "--env", "MASDEV", "--env-file", str(env_file),
            "--config-dir", str(config_dir), "--out", str(out_dir), "--flat",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not object_dir(out_dir, "purchasing", "po_line").exists()
    assert request.last_request.qs["oslc.select"] == ["*"]


def test_skip_errors_flag_writes_a_skip_log_and_reports_it_in_the_manifest(
    tmp_path, env_file, config_dir, requests_mock
):
    """MaximoClient's default page_size is 250, so a recovery scan here has to
    end itself quickly via a 'page not found' response rather than actually
    walking all 250 row slots -- see `_recover_page_row_by_row`'s early break."""
    company_url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(
        company_url,
        [
            {"status_code": 400, "json": {"Error": {"reasonCode": "BMXAA1825E", "message": "boom"}}},  # pageno=1,size=250
            {"json": {"member": [{"company": "ACME", "orgid": "EAGLENA", "name": "Acme", "type": "V"}], "responseInfo": {}}},  # pageno=1,size=1
            {"status_code": 400, "json": {"Error": {"reasonCode": "BMXAA1825E", "message": "boom"}}},  # pageno=2,size=1 (the skipped row)
            {"status_code": 400, "json": {"Error": {"reasonCode": "BMXAA8732E", "message": "no such page"}}},  # pageno=3,size=1 -> end
        ],
    )
    out_dir = tmp_path / "exports"

    result = run_export(env_file, config_dir, out_dir, "--skip-errors")

    assert result.exit_code == 0, result.output
    (skip_log,) = sorted(object_dir(out_dir, "purchasing", "company").glob("company_*.skipped.jsonl"))
    entry = json.loads(skip_log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["reason_code"] == "BMXAA1825E"
    assert entry["resource"] == "mxapicompany"

    manifest = json.loads(manifest_path(out_dir).read_text(encoding="utf-8"))
    assert manifest["skipped_count"] == 1
    assert manifest["skipped_log"].endswith(".skipped.jsonl")
    assert manifest["row_count"] == 1
    assert "skipped 1 rows" in result.output


def test_no_skip_log_written_when_skip_errors_flag_is_absent(tmp_path, env_file, config_dir, oslc_ok):
    out_dir = tmp_path / "exports"
    result = run_export(env_file, config_dir, out_dir)

    assert result.exit_code == 0, result.output
    assert not list(object_dir(out_dir, "purchasing", "company").glob("*.skipped.jsonl"))
    manifest = json.loads(manifest_path(out_dir).read_text(encoding="utf-8"))
    assert manifest["skipped_count"] == 0
    assert manifest["skipped_log"] is None


LEGACY_ONLY_MODULE_YAML = """
module: legacy
description: "Fixture module for import's OSLC-only rejection."
objects:
  - name: widget
    maximo_object: WIDGET
    access:
      rest_mbo_table: widget
      prefer: rest_mbo
    key_fields: [widgetid]
    fields: [widgetid, name]
    where: null
    children: []
extract_order: [widget]
load_order: [widget]
"""


def _write_company_csv(directory, timestamp, rows):
    import csv as _csv

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"company_{timestamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = _csv.writer(fh)
        writer.writerow(["company", "orgid", "name", "type"])
        writer.writerows(rows)
    return path


def import_args(env_file, config_dir, out_dir, *extra):
    return [
        "import",
        "--module", "purchasing",
        "--object", "company",
        "--env", "MASDEV",
        "--env-file", str(env_file),
        "--config-dir", str(config_dir),
        "--out", str(out_dir),
    ] + list(extra)


def test_import_dry_run_reports_would_create_and_never_posts(
    tmp_path, env_file, config_dir, requests_mock
):
    out_dir = tmp_path / "exports"
    _write_company_csv(
        out_dir / "purchasing" / "company", "20260911T000000Z",
        [["ACME", "EAGLENA", "Acme, Inc", "V"]],
    )
    requests_mock.get(OSLC_URL, json={"member": [], "responseInfo": {}})

    result = CliRunner().invoke(main, import_args(env_file, config_dir, out_dir))

    assert result.exit_code == 0, result.output
    assert "would_create: 1" in result.output
    assert not any(req.method == "POST" for req in requests_mock.request_history)


def test_import_execute_creates_a_new_record(tmp_path, env_file, config_dir, requests_mock):
    out_dir = tmp_path / "exports"
    _write_company_csv(
        out_dir / "purchasing" / "company", "20260911T000000Z",
        [["ACME", "EAGLENA", "Acme, Inc", "V"]],
    )
    requests_mock.get(OSLC_URL, json={"member": [], "responseInfo": {}})
    requests_mock.post(OSLC_URL, status_code=201, headers={"Location": f"{OSLC_URL}/_NEW"})

    result = CliRunner().invoke(main, import_args(env_file, config_dir, out_dir, "--execute"))

    assert result.exit_code == 0, result.output
    assert "created: 1" in result.output

    (log_path,) = sorted(object_dir(out_dir, "purchasing", "company").glob("company_*.import-log.jsonl"))
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["action"] == "created"
    assert entry["href"] == f"{OSLC_URL}/_NEW"

    (result_path,) = sorted(object_dir(out_dir, "purchasing", "company").glob("company_*.import-result.json"))
    manifest = json.loads(result_path.read_text(encoding="utf-8"))
    assert manifest["dry_run"] is False
    assert manifest["counts"] == {"created": 1}


def test_import_execute_updates_an_existing_record(tmp_path, env_file, config_dir, requests_mock):
    out_dir = tmp_path / "exports"
    _write_company_csv(
        out_dir / "purchasing" / "company", "20260911T000000Z",
        [["ACME", "EAGLENA", "Acme, Inc", "V"]],
    )
    existing_href = f"{OSLC_URL}/_EXISTING"
    requests_mock.get(OSLC_URL, json={"member": [{"href": existing_href}], "responseInfo": {}})
    patch_mock = requests_mock.post(existing_href, status_code=200)

    result = CliRunner().invoke(main, import_args(env_file, config_dir, out_dir, "--execute"))

    assert result.exit_code == 0, result.output
    assert "updated: 1" in result.output
    assert patch_mock.last_request.headers["x-method-override"] == "PATCH"
    assert patch_mock.last_request.json() == {"name": "Acme, Inc", "type": "V"}  # keys excluded


def test_import_auto_locates_the_newest_csv(tmp_path, env_file, config_dir, requests_mock):
    out_dir = tmp_path / "exports"
    _write_company_csv(
        out_dir / "purchasing" / "company", "20260101T000000Z",
        [["OLD", "EAGLENA", "Old Corp", "V"]],
    )
    _write_company_csv(
        out_dir / "purchasing" / "company", "20260911T000000Z",
        [["NEW", "EAGLENA", "New Corp", "V"]],
    )
    requests_mock.get(OSLC_URL, json={"member": [], "responseInfo": {}})

    result = CliRunner().invoke(main, import_args(env_file, config_dir, out_dir))

    assert result.exit_code == 0, result.output
    manifest_path = sorted(object_dir(out_dir, "purchasing", "company").glob("company_*.import-result.json"))[-1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["csv_file"].endswith("company_20260911T000000Z.csv")


def test_import_explicit_csv_file_overrides_auto_locate(tmp_path, env_file, config_dir, requests_mock):
    out_dir = tmp_path / "exports"
    _write_company_csv(
        out_dir / "purchasing" / "company", "20260911T000000Z",
        [["WRONG", "EAGLENA", "Wrong Corp", "V"]],
    )
    explicit_csv = tmp_path / "hand_picked.csv"
    explicit_csv.write_text("company,orgid,name,type\nRIGHT,EAGLENA,Right Corp,V\n", encoding="utf-8")
    requests_mock.get(OSLC_URL, json={"member": [], "responseInfo": {}})

    result = CliRunner().invoke(
        main, import_args(env_file, config_dir, out_dir, "--csv-file", str(explicit_csv))
    )

    assert result.exit_code == 0, result.output
    assert 'RIGHT' in requests_mock.request_history[0].qs["oslc.where"][0].upper()
    manifest_path = sorted(object_dir(out_dir, "purchasing", "company").glob("company_*.import-result.json"))[-1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["csv_file"] == str(explicit_csv)


def test_import_no_csv_found_exits_nonzero_with_a_clear_message(tmp_path, env_file, config_dir):
    out_dir = tmp_path / "exports"
    result = CliRunner().invoke(main, import_args(env_file, config_dir, out_dir))

    assert result.exit_code != 0
    assert "no exported CSV found" in result.stderr


def test_import_row_failure_exits_nonzero_and_reports_count(tmp_path, env_file, config_dir, requests_mock):
    out_dir = tmp_path / "exports"
    _write_company_csv(
        out_dir / "purchasing" / "company", "20260911T000000Z",
        [["ACME", "EAGLENA", "Acme, Inc", "V"]],
    )
    requests_mock.get(OSLC_URL, status_code=401)

    result = CliRunner().invoke(main, import_args(env_file, config_dir, out_dir))

    assert result.exit_code != 0
    assert "1 row(s) failed" in result.stderr


def test_import_rejects_an_object_with_no_oslc_resource(tmp_path, env_file):
    config_dir = tmp_path / "config" / "modules"
    config_dir.mkdir(parents=True)
    (config_dir / "legacy.yaml").write_text(LEGACY_ONLY_MODULE_YAML, encoding="utf-8")
    out_dir = tmp_path / "exports"

    result = CliRunner().invoke(
        main,
        [
            "import", "--module", "legacy", "--object", "widget",
            "--env", "MASDEV", "--env-file", str(env_file),
            "--config-dir", str(config_dir), "--out", str(out_dir),
        ],
    )

    assert result.exit_code != 0
    assert "oslc_resource" in result.stderr


def test_missing_default_env_file_exits_nonzero_naming_the_expected_path(
    tmp_path, config_dir, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "export",
            "--module", "purchasing",
            "--object", "company",
            "--env", "MASDEV",
            "--config-dir", str(config_dir),
        ],
    )

    assert result.exit_code != 0
    assert ".env.masdev" in result.stderr
    assert "Traceback" not in result.stderr
