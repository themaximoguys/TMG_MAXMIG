import pytest
import requests

from maxmig.load.importer import (
    MissingKeyFieldError,
    create_record,
    find_existing_href,
    import_row,
    natural_key_where,
    update_record,
)

BASE_URL = "https://masdev.example.com/maximo"
HEADERS = {"maxauth": "fake", "Accept": "application/json"}


def test_natural_key_where_ands_key_fields_in_order():
    row = {"company": "ACME", "orgid": "EAGLENA", "name": "Acme Corp"}
    assert natural_key_where(row, ["company", "orgid"]) == 'company="ACME" and orgid="EAGLENA"'


def test_natural_key_where_raises_on_missing_key_field():
    row = {"company": "ACME", "orgid": ""}
    with pytest.raises(MissingKeyFieldError, match="orgid"):
        natural_key_where(row, ["company", "orgid"])


def test_natural_key_where_raises_on_absent_key_field():
    row = {"company": "ACME"}
    with pytest.raises(MissingKeyFieldError, match="orgid"):
        natural_key_where(row, ["company", "orgid"])


def test_find_existing_href_returns_none_when_no_match(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapicompany", json={"member": [], "responseInfo": {}})
    session = requests.Session()

    href = find_existing_href(session, BASE_URL, HEADERS, "mxapicompany", 'company="ACME"')

    assert href is None


def test_find_existing_href_returns_href_of_the_match(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/api/os/mxapicompany",
        json={"member": [{"href": f"{BASE_URL}/api/os/mxapicompany/_ABC"}], "responseInfo": {}},
    )
    session = requests.Session()

    href = find_existing_href(session, BASE_URL, HEADERS, "mxapicompany", 'company="ACME"')

    assert href == f"{BASE_URL}/api/os/mxapicompany/_ABC"


def test_create_record_uses_location_header(requests_mock):
    requests_mock.post(
        f"{BASE_URL}/api/os/mxapicompany",
        status_code=201,
        headers={"Location": f"{BASE_URL}/api/os/mxapicompany/_NEW"},
    )
    session = requests.Session()

    href = create_record(session, BASE_URL, HEADERS, "mxapicompany", {"company": "ACME"})

    assert href == f"{BASE_URL}/api/os/mxapicompany/_NEW"


def test_create_record_falls_back_to_body_href_when_no_location_header(requests_mock):
    requests_mock.post(
        f"{BASE_URL}/api/os/mxapicompany",
        status_code=201,
        json={"href": f"{BASE_URL}/api/os/mxapicompany/_NEW"},
    )
    session = requests.Session()

    href = create_record(session, BASE_URL, HEADERS, "mxapicompany", {"company": "ACME"})

    assert href == f"{BASE_URL}/api/os/mxapicompany/_NEW"


def test_create_record_raises_when_no_href_available_at_all(requests_mock):
    requests_mock.post(f"{BASE_URL}/api/os/mxapicompany", status_code=201, text="")
    session = requests.Session()

    with pytest.raises(RuntimeError, match="no href"):
        create_record(session, BASE_URL, HEADERS, "mxapicompany", {"company": "ACME"})


def test_update_record_sends_method_override_patch_header(requests_mock):
    mocked = requests_mock.post(f"{BASE_URL}/api/os/mxapicompany/_ABC", status_code=200)
    session = requests.Session()

    update_record(session, HEADERS, f"{BASE_URL}/api/os/mxapicompany/_ABC", {"name": "New Name"})

    assert mocked.last_request.headers["x-method-override"] == "PATCH"


# --- import_row orchestration (injected find_href/create/update fakes) ---


def _make_fakes(existing_href=None, create_href="new-href", create_raises=None, update_raises=None):
    calls = {"find": 0, "create": 0, "update": 0}

    def find_href(session, base_url, headers, resource, where):
        calls["find"] += 1
        return existing_href

    def create(session, base_url, headers, resource, body):
        calls["create"] += 1
        if create_raises:
            raise create_raises
        return create_href

    def update(session, headers, href, body):
        calls["update"] += 1
        if update_raises:
            raise update_raises

    return calls, find_href, create, update


def test_import_row_dry_run_reports_would_create_without_writing():
    calls, find_href, create, update = _make_fakes(existing_href=None)
    row = {"company": "ACME", "orgid": "EAGLENA", "name": "Acme"}

    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=False, find_href=find_href, create=create, update=update,
    )

    assert result.action == "would_create"
    assert calls == {"find": 1, "create": 0, "update": 0}


def test_import_row_dry_run_reports_would_update_when_a_match_exists():
    calls, find_href, create, update = _make_fakes(existing_href="existing-href")
    row = {"company": "ACME", "orgid": "EAGLENA", "name": "Acme"}

    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=False, find_href=find_href, create=create, update=update,
    )

    assert result.action == "would_update"
    assert result.href == "existing-href"
    assert calls["create"] == 0 and calls["update"] == 0


def test_import_row_execute_creates_when_no_match():
    calls, find_href, create, update = _make_fakes(existing_href=None, create_href="new-href")
    row = {"company": "ACME", "orgid": "EAGLENA", "name": "Acme"}

    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert result.action == "created"
    assert result.href == "new-href"
    assert calls == {"find": 1, "create": 1, "update": 0}


def test_import_row_execute_updates_when_a_match_exists():
    calls, find_href, create, update = _make_fakes(existing_href="existing-href")
    row = {"company": "ACME", "orgid": "EAGLENA", "name": "Acme"}

    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert result.action == "updated"
    assert result.href == "existing-href"
    assert calls["create"] == 0 and calls["update"] == 1


def test_import_row_missing_key_field_fails_without_any_http_call():
    calls, find_href, create, update = _make_fakes()
    row = {"company": "ACME", "orgid": ""}

    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert result.action == "failed"
    assert "orgid" in result.error
    assert calls == {"find": 0, "create": 0, "update": 0}


def test_import_row_update_failure_is_reported_not_raised():
    err = requests.exceptions.HTTPError("500 boom")
    calls, find_href, create, update = _make_fakes(existing_href="existing-href", update_raises=err)
    row = {"company": "ACME", "orgid": "EAGLENA"}

    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert result.action == "failed"
    assert result.href == "existing-href"
    assert "boom" in result.error


def test_import_row_create_failure_rechecks_and_reports_created_if_it_actually_landed():
    """A create can time out on the client side even though it succeeded on the
    server -- import_row must re-check by natural key rather than assume
    failure, so a caller re-running the import never double-creates."""
    err = requests.exceptions.HTTPError("timeout")
    find_calls = []

    def find_href(session, base_url, headers, resource, where):
        find_calls.append(where)
        # First call (pre-create check): no match. Second call (post-failure
        # recheck): the create actually landed.
        return None if len(find_calls) == 1 else "landed-href"

    def create(session, base_url, headers, resource, body):
        raise err

    def update(session, headers, href, body):
        raise AssertionError("should never be called")

    row = {"company": "ACME", "orgid": "EAGLENA"}
    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert result.action == "created"
    assert result.href == "landed-href"
    assert len(find_calls) == 2


def test_import_row_create_failure_reports_failed_when_recheck_confirms_nothing_landed():
    err = requests.exceptions.HTTPError("500 boom")
    calls, find_href, create, update = _make_fakes(existing_href=None, create_raises=err)
    row = {"company": "ACME", "orgid": "EAGLENA"}

    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert result.action == "failed"
    assert "boom" in result.error


def test_import_row_update_body_excludes_key_fields_and_empty_values():
    captured_body = {}

    def find_href(session, base_url, headers, resource, where):
        return "existing-href"

    def create(session, base_url, headers, resource, body):
        raise AssertionError("should never be called")

    def update(session, headers, href, body):
        captured_body.update(body)

    row = {"company": "ACME", "orgid": "EAGLENA", "name": "Acme", "type": ""}
    import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert captured_body == {"name": "Acme"}


def test_import_row_create_body_includes_key_fields_but_not_empty_values():
    captured_body = {}

    def find_href(session, base_url, headers, resource, where):
        return None

    def create(session, base_url, headers, resource, body):
        captured_body.update(body)
        return "new-href"

    def update(session, headers, href, body):
        raise AssertionError("should never be called")

    row = {"company": "ACME", "orgid": "EAGLENA", "name": "Acme", "type": ""}
    import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=True, find_href=find_href, create=create, update=update,
    )

    assert captured_body == {"company": "ACME", "orgid": "EAGLENA", "name": "Acme"}


def test_import_row_lookup_failure_reports_failed():
    def find_href(session, base_url, headers, resource, where):
        raise requests.exceptions.HTTPError("401")

    row = {"company": "ACME", "orgid": "EAGLENA"}
    result = import_row(
        None, BASE_URL, HEADERS, "mxapicompany", row, ["company", "orgid"],
        execute=False, find_href=find_href,
    )

    assert result.action == "failed"
    assert "401" in result.error
