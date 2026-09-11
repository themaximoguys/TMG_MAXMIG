import pytest
import requests

from maxmig.api.oslc import build_oslc_select, iter_oslc, iter_oslc_skip_errors

BASE_URL = "https://masdev.example.com/maximo"
HEADERS = {"maxauth": "fake", "Accept": "application/json"}


def test_iter_oslc_follows_next_page_until_absent(requests_mock):
    first_url = f"{BASE_URL}/api/os/mxapicompany"
    next_url = f"{BASE_URL}/api/os/mxapicompany?_page=2"

    requests_mock.get(
        first_url,
        json={
            "member": [{"company": "ACME"}, {"company": "GLOBEX"}],
            "responseInfo": {"totalCount": 3, "nextPage": next_url},
        },
    )
    requests_mock.get(
        next_url,
        json={
            "member": [{"company": "INITECH"}],
            "responseInfo": {"totalCount": 3},
        },
    )

    session = requests.Session()
    rows = list(iter_oslc(session, BASE_URL, HEADERS, "mxapicompany", page_size=2))

    assert [r["company"] for r in rows] == ["ACME", "GLOBEX", "INITECH"]
    assert requests_mock.call_count == 2
    # Second call followed nextPage verbatim, no re-added query params.
    assert requests_mock.request_history[1].url == next_url


def test_iter_oslc_follows_next_page_as_lean_href_dict(requests_mock):
    """Real Maximo (lean=1) nests nextPage as {"href": "<url>"}, not a bare
    string -- this is the shape the mocked tests above don't cover."""
    first_url = f"{BASE_URL}/api/os/mxapicompany"
    next_url = f"{BASE_URL}/api/os/mxapicompany?pageno=2&lean=1"

    requests_mock.get(
        first_url,
        json={
            "member": [{"company": "ACME"}],
            "responseInfo": {"nextPage": {"href": next_url}},
        },
    )
    requests_mock.get(
        next_url,
        json={"member": [{"company": "GLOBEX"}], "responseInfo": {}},
    )

    session = requests.Session()
    rows = list(iter_oslc(session, BASE_URL, HEADERS, "mxapicompany"))

    assert [r["company"] for r in rows] == ["ACME", "GLOBEX"]
    assert requests_mock.request_history[1].url == next_url


def test_iter_oslc_single_page_no_next_page(requests_mock):
    url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(
        url,
        json={"member": [{"company": "ACME"}], "responseInfo": {"totalCount": 1}},
    )

    session = requests.Session()
    rows = list(iter_oslc(session, BASE_URL, HEADERS, "mxapicompany"))

    assert rows == [{"company": "ACME"}]
    assert requests_mock.call_count == 1


class _FakeRelation:
    def __init__(self, relation_attribute, children=None):
        self.relation_attribute = relation_attribute
        self.children = children or []


def test_build_oslc_select_returns_star_for_no_children():
    """Real Maximo returns next to nothing (just `href`) when oslc.select is
    omitted entirely -- `*` (not None/omitted) is what actually gets the
    object's own fields back."""
    assert build_oslc_select([]) == "*"


def test_build_oslc_select_one_level():
    select = build_oslc_select([_FakeRelation("poline")])
    assert select == "*,poline{*}"


def test_build_oslc_select_recurses_into_grandchildren():
    """A child with its own children (e.g. a PO line's cost distribution) must
    still get its nested relation included, not silently dropped one level deep."""
    grandchild = _FakeRelation("costline")
    child = _FakeRelation("poline", children=[grandchild])
    select = build_oslc_select([child])
    assert select == "*,poline{*,costline{*}}"


def test_iter_oslc_empty_result_yields_nothing(requests_mock):
    url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(url, json={"member": [], "responseInfo": {"totalCount": 0}})

    session = requests.Session()
    rows = list(iter_oslc(session, BASE_URL, HEADERS, "mxapicompany"))

    assert rows == []


def _skippable_error(reason_code="BMXAA1825E"):
    return {
        "status_code": 400,
        "json": {"Error": {"reasonCode": reason_code, "message": "boom"}},
    }


def test_iter_oslc_skip_errors_recovers_one_bad_row_and_keeps_going(requests_mock):
    """A page (size 3) fails wholesale -> recovered row-by-row, skipping only
    the one bad row -> the *next* full page then proceeds normally."""
    url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(
        url,
        [
            _skippable_error(),  # pageno=1, size=3 -> whole page fails
            {"json": {"member": [{"company": "A"}], "responseInfo": {}}},  # pageno=1, size=1
            _skippable_error(),  # pageno=2, size=1 -> the actual bad row
            {"json": {"member": [{"company": "C"}], "responseInfo": {}}},  # pageno=3, size=1
            {"json": {"member": [{"company": "D"}, {"company": "E"}], "responseInfo": {}}},  # pageno=2, size=3 (short -> end)
        ],
    )

    session = requests.Session()
    skipped = []
    rows = list(
        iter_oslc_skip_errors(
            session, BASE_URL, HEADERS, "mxapicompany", page_size=3, on_skip=skipped.append
        )
    )

    assert [r["company"] for r in rows] == ["A", "C", "D", "E"]
    assert len(skipped) == 1
    assert skipped[0].pageno == 2
    assert skipped[0].resource == "mxapicompany"
    assert skipped[0].reason_code == "BMXAA1825E"


def test_iter_oslc_skip_errors_reraises_a_non_skippable_error(requests_mock):
    url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(url, status_code=500)

    session = requests.Session()
    with pytest.raises(requests.exceptions.HTTPError):
        list(iter_oslc_skip_errors(session, BASE_URL, HEADERS, "mxapicompany", page_size=3))


def test_iter_oslc_skip_errors_reraises_a_non_skippable_reason_code(requests_mock):
    url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(url, status_code=400, json={"Error": {"reasonCode": "BMXAA9999E", "message": "nope"}})

    session = requests.Session()
    with pytest.raises(requests.exceptions.HTTPError):
        list(iter_oslc_skip_errors(session, BASE_URL, HEADERS, "mxapicompany", page_size=3))


def test_iter_oslc_skip_errors_page_not_found_on_first_page_means_no_rows(requests_mock):
    """An exact multiple of page_size -- the very next page throws Maximo's own
    'page not found' error rather than an empty member list; that must end the
    iteration cleanly, not raise."""
    url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(url, status_code=400, json={"Error": {"reasonCode": "BMXAA8732E", "message": "no such page"}})

    session = requests.Session()
    rows = list(iter_oslc_skip_errors(session, BASE_URL, HEADERS, "mxapicompany", page_size=3))

    assert rows == []


def test_iter_oslc_skip_errors_recovery_stops_at_true_end_of_table(requests_mock):
    """The bad page is also the last (partial) page -- recovery must stop at
    the real end (a 'page not found' during the row-by-row scan), not keep
    requesting rows that don't exist."""
    url = f"{BASE_URL}/api/os/mxapicompany"
    requests_mock.get(
        url,
        [
            _skippable_error(),  # pageno=1, size=3 -> whole page fails
            {"json": {"member": [{"company": "A"}], "responseInfo": {}}},  # pageno=1, size=1
            _skippable_error(),  # pageno=2, size=1 -> bad row, skipped
            {"status_code": 400, "json": {"Error": {"reasonCode": "BMXAA8732E", "message": "no such page"}}},  # pageno=3, size=1 -> table only has 1 real row here
        ],
    )

    session = requests.Session()
    skipped = []
    rows = list(
        iter_oslc_skip_errors(
            session, BASE_URL, HEADERS, "mxapicompany", page_size=3, on_skip=skipped.append
        )
    )

    assert [r["company"] for r in rows] == ["A"]
    assert len(skipped) == 1
    assert requests_mock.call_count == 4  # never tried a pageno=2,size=3 full-page fetch after this
