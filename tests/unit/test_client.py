import pytest
import requests

from maxmig.api.client import MaximoAuthError, MaximoClient, build_auth_header
from maxmig.api.filter import Filter
from maxmig.config import EnvConfig

BASE_URL = "https://masdev.example.com/maximo"


class FakeAccess:
    def __init__(self, prefer, oslc_resource=None, rest_mbo_table=None):
        self.prefer = prefer
        self.oslc_resource = oslc_resource
        self.rest_mbo_table = rest_mbo_table


class FakeObjectDef:
    def __init__(self, access, fields, key_fields, where=None, children=None):
        self.access = access
        self.fields = fields
        self.key_fields = key_fields
        self.where = where
        self.children = children or []


def _env():
    return EnvConfig(prefix="MASDEV", manage_url=BASE_URL, username="maxadmin", password="secret")


def test_client_uses_apikey_header_when_configured():
    env = EnvConfig(prefix="MAXDEMO", manage_url=BASE_URL, api_key="real-generated-key")
    client = MaximoClient(env)
    assert client.headers["apikey"] == "real-generated-key"
    assert "maxauth" not in client.headers


def test_client_prefers_apikey_over_maxauth_when_both_set():
    env = EnvConfig(
        prefix="MAXDEMO", manage_url=BASE_URL, username="maxadmin", password="secret", api_key="real-key"
    )
    client = MaximoClient(env)
    assert client.headers["apikey"] == "real-key"
    assert "maxauth" not in client.headers


def test_client_falls_back_to_maxauth_when_api_key_unset():
    client = MaximoClient(_env())
    assert "maxauth" in client.headers
    assert "apikey" not in client.headers


def test_client_raises_clear_error_when_neither_auth_mode_configured():
    env = EnvConfig(prefix="MAXDEMO", manage_url=BASE_URL)
    with pytest.raises(MaximoAuthError, match="MAXDEMO"):
        MaximoClient(env)


def test_build_auth_header_is_base64_of_user_colon_pass():
    header = build_auth_header("maxadmin", "secret")
    import base64

    assert base64.b64decode(header).decode() == "maxadmin:secret"


def test_client_uses_preferred_oslc_api_when_it_succeeds(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/api/os/mxapicompany",
        json={"member": [{"company": "ACME"}], "responseInfo": {"totalCount": 1}},
    )
    obj_def = FakeObjectDef(
        access=FakeAccess("oslc", oslc_resource="mxapicompany", rest_mbo_table="company"),
        fields=["company"],
        key_fields=["company"],
    )

    client = MaximoClient(_env())
    rows = list(client.fetch_object(obj_def))

    assert rows == [{"company": "ACME"}]
    assert requests_mock.call_count == 1  # never touched rest_mbo fallback


def test_client_falls_back_to_rest_mbo_when_oslc_first_page_errors(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapicompany", status_code=500)
    requests_mock.get(
        f"{BASE_URL}/rest/mbo/company",
        json={
            "COMPANYMboSet": {
                "rsCount": 1,
                "rsTotal": 1,
                "COMPANY": [{"Attributes": {"COMPANY": {"content": "ACME"}}}],
            }
        },
    )
    obj_def = FakeObjectDef(
        access=FakeAccess("oslc", oslc_resource="mxapicompany", rest_mbo_table="company"),
        fields=["company"],
        key_fields=["company"],
    )

    client = MaximoClient(_env())
    rows = list(client.fetch_object(obj_def))

    assert rows == [{"company": "ACME"}]


def test_client_does_not_fall_back_to_rest_mbo_when_where_filter_would_be_dropped(requests_mock):
    """REST MBO can't express an oslc.where boolean expression — falling back to it
    for a filtered object would silently return unfiltered (wrong) rows, so the
    client must raise the original error instead of masking it with a fallback."""
    requests_mock.get(f"{BASE_URL}/api/os/mxapilocations", status_code=500)
    rest_mbo_mock = requests_mock.get(
        f"{BASE_URL}/rest/mbo/locations",
        json={"LOCATIONSMboSet": {"rsCount": 0, "rsTotal": 0, "LOCATIONS": []}},
    )
    obj_def = FakeObjectDef(
        access=FakeAccess("oslc", oslc_resource="mxapilocations", rest_mbo_table="locations"),
        fields=["location", "siteid"],
        key_fields=["location", "siteid"],
        where='type="STOREROOM"',
    )

    client = MaximoClient(_env())
    with pytest.raises(requests.exceptions.HTTPError):
        list(client.fetch_object(obj_def))

    assert not rest_mbo_mock.called  # never silently fell back and dropped the filter


def test_client_empty_result_does_not_trigger_fallback(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/api/os/mxapicompany",
        json={"member": [], "responseInfo": {"totalCount": 0}},
    )
    obj_def = FakeObjectDef(
        access=FakeAccess("oslc", oslc_resource="mxapicompany", rest_mbo_table="company"),
        fields=["company"],
        key_fields=["company"],
    )

    client = MaximoClient(_env())
    rows = list(client.fetch_object(obj_def))

    assert rows == []
    assert requests_mock.call_count == 1  # no fallback attempted for a legitimately empty set


def test_client_passes_filter_conditions_through_to_oslc_where(requests_mock):
    mocked = requests_mock.get(
        f"{BASE_URL}/api/os/mxapicompany",
        json={"member": [{"company": "ACME"}], "responseInfo": {"totalCount": 1}},
    )
    obj_def = FakeObjectDef(
        access=FakeAccess("oslc", oslc_resource="mxapicompany", rest_mbo_table="company"),
        fields=["company"],
        key_fields=["company"],
    )

    client = MaximoClient(_env())
    list(client.fetch_object(obj_def, filter=Filter(site="BEDFORD")))

    assert mocked.last_request.qs["oslc.where"] == ['siteid="bedford"']  # requests_mock lowercases qs values too


def test_client_raises_when_rest_mbo_is_primary_and_default_where_is_set(requests_mock):
    """access.prefer=rest_mbo with a configured obj_def.where is a genuine
    misconfiguration (REST MBO can't express an OSLC where expression) — it
    must raise loudly, not silently ignore the object's own default filter."""
    from maxmig.api.filter import UnsupportedFilterError

    obj_def = FakeObjectDef(
        access=FakeAccess("rest_mbo", oslc_resource=None, rest_mbo_table="locations"),
        fields=["location", "siteid"],
        key_fields=["location", "siteid"],
        where='type="STOREROOM"',
    )

    client = MaximoClient(_env())
    with pytest.raises(UnsupportedFilterError):
        list(client.fetch_object(obj_def))

    assert not requests_mock.called  # fails before ever making an HTTP call


def test_client_passes_filter_conditions_through_to_rest_mbo_params(requests_mock):
    mocked = requests_mock.get(
        f"{BASE_URL}/rest/mbo/company",
        json={
            "COMPANYMboSet": {
                "rsCount": 1,
                "rsTotal": 1,
                "COMPANY": [{"Attributes": {"COMPANY": {"content": "ACME"}}}],
            }
        },
    )
    obj_def = FakeObjectDef(
        access=FakeAccess("rest_mbo", oslc_resource=None, rest_mbo_table="company"),
        fields=["company"],
        key_fields=["company"],
    )

    client = MaximoClient(_env())
    list(client.fetch_object(obj_def, filter=Filter(conditions={"status": "ACTIVE"})))

    assert mocked.last_request.qs["status"] == ["active"]  # confirms REST MBO now receives real filter params


def test_client_skip_errors_recovers_a_bad_row_via_oslc(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/api/os/mxapicompany",
        [
            {"status_code": 400, "json": {"Error": {"reasonCode": "BMXAA1825E", "message": "boom"}}},  # pageno=1,size=3
            {"json": {"member": [{"company": "A"}], "responseInfo": {}}},  # pageno=1,size=1
            {"status_code": 400, "json": {"Error": {"reasonCode": "BMXAA1825E", "message": "boom"}}},  # pageno=2,size=1 (bad row)
            {"json": {"member": [{"company": "C"}], "responseInfo": {}}},  # pageno=3,size=1
            {"json": {"member": [{"company": "D"}], "responseInfo": {}}},  # pageno=2,size=3 (short -> end)
        ],
    )
    obj_def = FakeObjectDef(
        access=FakeAccess("oslc", oslc_resource="mxapicompany", rest_mbo_table="company"),
        fields=["company"],
        key_fields=["company"],
    )

    client = MaximoClient(_env(), page_size=3)
    skipped = []
    rows = list(client.fetch_object(obj_def, skip_errors=True, on_skip=skipped.append))

    assert [r["company"] for r in rows] == ["A", "C", "D"]
    assert len(skipped) == 1


def test_client_skip_errors_rejects_rest_mbo(requests_mock):
    obj_def = FakeObjectDef(
        access=FakeAccess("rest_mbo", oslc_resource=None, rest_mbo_table="company"),
        fields=["company"],
        key_fields=["company"],
    )

    client = MaximoClient(_env())
    with pytest.raises(ValueError, match="OSLC"):
        list(client.fetch_object(obj_def, skip_errors=True))
    assert not requests_mock.called
