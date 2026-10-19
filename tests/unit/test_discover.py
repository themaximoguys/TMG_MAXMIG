import pytest
import requests
import yaml

from maxmig.discover import (
    DiscoveryResult,
    ProbeAttempt,
    draft_module_yaml,
    probe_object,
    write_draft,
)
from maxmig.model.schema import ModuleDef

BASE_URL = "https://masdev.example.com/maximo"
HEADERS = {"apikey": "fake", "Accept": "application/json"}

UNKNOWN_OBJECT = {"Error": {"reasonCode": "BMXAA4216E", "message": "BMXAA4216E - Unknown Object", "statusCode": "404"}}

PO_SAMPLE = {
    "member": [
        {
            "href": f"{BASE_URL}/api/os/mxapipo/_UE8vMTAwMA--",
            "_rowstamp": "12345",
            "ponum": "1000",
            "siteid": "BEDFORD",
            "orgid": "EAGLENA",
            "description": "Pumps",
            "totalcost": 120.5,
            "poline_collectionref": f"{BASE_URL}/api/os/mxapipo/_X/poline",
            "localref": "x",
            "doclinks": [],
            "poline": [
                {"polinenum": 1, "itemnum": "PUMP100", "orderqty": 2, "href": "h", "localref": "y"},
                {"polinenum": 2, "itemnum": "PUMP200", "unitcost": 10, "_rowstamp": "1"},
            ],
        }
    ],
    "responseInfo": {},
}


def _session():
    return requests.Session()


def test_mxapi_hit_stops_after_first_attempt(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapipo", json=PO_SAMPLE)

    result = probe_object(_session(), BASE_URL, HEADERS, "po")

    assert result.found
    assert [a.resource for a in result.attempts] == ["mxapipo"]
    assert result.working.api == "oslc"
    qs = requests_mock.last_request.qs
    assert qs["lean"] == ["1"] and qs["oslc.pagesize"] == ["1"] and qs["oslc.select"] == ["*"]


def test_oslc_attribute_extraction_drops_plumbing_and_detects_children(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapipo", json=PO_SAMPLE)

    result = probe_object(_session(), BASE_URL, HEADERS, "po")

    assert result.attributes == ["ponum", "siteid", "orgid", "description", "totalcost"]
    assert [c.name for c in result.children] == ["poline"]
    assert result.children[0].attributes == ["polinenum", "itemnum", "orderqty", "unitcost"]


def test_single_dict_value_is_a_child_relation(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/api/os/mxapiasset",
        json={"member": [{"assetnum": "A1", "siteid": "S", "assetmeter": {"metername": "RUNHRS"}}]},
    )

    result = probe_object(_session(), BASE_URL, HEADERS, "asset")

    assert result.attributes == ["assetnum", "siteid"]
    assert result.children[0].name == "assetmeter"
    assert result.children[0].attributes == ["metername"]


def test_raw_name_fallback_after_mxapi_404(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapimxl_wo", status_code=404, text="Not Found")
    requests_mock.get(f"{BASE_URL}/api/os/MXL_WO", json={"member": [{"wonum": "1", "siteid": "S"}]})

    result = probe_object(_session(), BASE_URL, HEADERS, "MXL_WO")

    assert [(a.resource, a.status) for a in result.attempts] == [("mxapimxl_wo", "not_found"), ("MXL_WO", "ok")]
    assert result.working.resource == "MXL_WO"


def test_bmxaa4216e_body_classified_not_found_even_on_500(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapiwidget", status_code=500, json=UNKNOWN_OBJECT)
    requests_mock.get(f"{BASE_URL}/api/os/widget", status_code=400, json={"Error": {"message": "Unknown Object widget"}})
    requests_mock.get(f"{BASE_URL}/rest/mbo/widget", status_code=404)

    result = probe_object(_session(), BASE_URL, HEADERS, "widget")

    assert [a.status for a in result.attempts] == ["not_found", "not_found", "not_found"]
    assert not result.found


def test_rest_mbo_fallback(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapiinvcost", status_code=404)
    requests_mock.get(f"{BASE_URL}/api/os/invcost", status_code=404)
    requests_mock.get(
        f"{BASE_URL}/rest/mbo/invcost",
        json={
            "INVCOSTMboSet": {
                "rsCount": 1,
                "INVCOST": [{"Attributes": {"ITEMNUM": {"content": "X"}, "SITEID": {"content": "S"}, "AVGCOST": {"content": 1}}}],
            }
        },
    )

    result = probe_object(_session(), BASE_URL, HEADERS, "invcost")

    assert result.working.api == "rest_mbo"
    assert result.attributes == ["itemnum", "siteid", "avgcost"]
    assert requests_mock.last_request.qs == {"_format": ["json"], "_maxitems": ["1"]}


def test_all_fail_keeps_status_codes_and_messages(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapiwo", status_code=500, json={"Error": {"message": "boom"}})
    requests_mock.get(f"{BASE_URL}/api/os/wo", exc=requests.exceptions.ConnectTimeout)
    requests_mock.get(f"{BASE_URL}/rest/mbo/wo", status_code=403, text="Forbidden")

    result = probe_object(_session(), BASE_URL, HEADERS, "wo")

    assert not result.found
    assert [(a.status, a.status_code) for a in result.attempts] == [("error", 500), ("error", None), ("error", 403)]
    assert result.attempts[0].message == "boom"
    assert "ConnectTimeout" in result.attempts[1].message
    with pytest.raises(ValueError, match="nothing to draft"):
        draft_module_yaml(result, "m", "wo")


def test_name_already_prefixed_mxapi_is_not_probed_twice(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapipersongroup", status_code=404)
    requests_mock.get(f"{BASE_URL}/rest/mbo/mxapipersongroup", status_code=404)

    result = probe_object(_session(), BASE_URL, HEADERS, "mxapipersongroup")

    assert [a.api for a in result.attempts] == ["oslc", "rest_mbo"]


def test_draft_yaml_validates_against_current_schema(requests_mock):
    requests_mock.get(f"{BASE_URL}/api/os/mxapipo", json=PO_SAMPLE)
    result = probe_object(_session(), BASE_URL, HEADERS, "po")

    text = draft_module_yaml(result, "purchasing_draft", "purchase_order")

    assert "DRAFT" in text and "TODO: confirm natural key" in text
    assert "custom_fields" not in text
    module = ModuleDef.model_validate(yaml.safe_load(text))
    obj = module.objects[0]
    assert obj.key_fields == ["ponum", "siteid", "orgid"]
    assert obj.access.oslc_resource == "mxapipo" and obj.access.prefer == "oslc"
    child = obj.children[0]
    assert child.relation_attribute == "poline"
    assert child.foreign_key == ["ponum", "siteid", "orgid"]
    assert child.key_fields == ["ponum", "siteid", "orgid", "polinenum"]
    assert module.load_order == ["purchase_order"]


def test_draft_yaml_for_rest_mbo_validates():
    attempt = ProbeAttempt("rest_mbo", "invcost", "u", "ok", 200, None, {})
    result = DiscoveryResult("invcost", [attempt], attempt, ["itemnum", "siteid", "avgcost"])

    module = ModuleDef.model_validate(yaml.safe_load(draft_module_yaml(result, "m", "invcost")))

    assert module.objects[0].access.prefer == "rest_mbo"
    assert module.objects[0].access.oslc_resource is None
    assert module.objects[0].key_fields == ["itemnum", "siteid"]


def test_custom_attributes_emitted_under_custom_fields(requests_mock):
    sample = {"member": [{"ponum": "1", "siteid": "S", "zz_costcode": "A", "poline": [{"polinenum": 1, "zz_lineflag": 1}]}]}
    requests_mock.get(f"{BASE_URL}/api/os/mxapipo", json=sample)

    result = probe_object(_session(), BASE_URL, HEADERS, "po", custom_attributes={"ZZ_COSTCODE", "zz_lineflag"})
    text = draft_module_yaml(result, "m", "purchase_order")

    assert result.attributes == ["ponum", "siteid"]
    assert result.custom_attributes == ["zz_costcode"]
    assert result.children[0].custom_attributes == ["zz_lineflag"]
    assert "custom_fields: [zz_costcode]" in text
    assert "custom_fields: [zz_lineflag]" in text


def test_write_draft_refuses_overwrite(tmp_path):
    path = write_draft(tmp_path, "po", "a: 1\n")
    assert path.name == "po.discovered.yaml"

    with pytest.raises(FileExistsError):
        write_draft(tmp_path, "po", "a: 2\n")
    assert path.read_text() == "a: 1\n"

    write_draft(tmp_path, "po", "a: 2\n", overwrite=True)
    assert path.read_text() == "a: 2\n"
