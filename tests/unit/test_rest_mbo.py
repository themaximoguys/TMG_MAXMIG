import requests

from maxmig.api.rest_mbo import iter_rest_mbo

BASE_URL = "https://masdev.example.com/maximo"
HEADERS = {"maxauth": "fake", "Accept": "application/json"}


def _mbo_response(rows, rs_count, rs_total):
    return {
        "COMPANYMboSet": {
            "rsStart": 0,
            "rsCount": rs_count,
            "rsTotal": rs_total,
            "COMPANY": rows,
        }
    }


def test_iter_rest_mbo_paginates_until_last_page(requests_mock):
    url = f"{BASE_URL}/rest/mbo/company"
    page1 = _mbo_response(
        [
            {"Attributes": {"COMPANY": {"content": "ACME"}, "NAME": {"content": "Acme Corp"}}},
            {"Attributes": {"COMPANY": {"content": "GLOBEX"}, "NAME": {"content": "Globex Inc"}}},
        ],
        rs_count=2,
        rs_total=3,
    )
    page2 = _mbo_response(
        [{"Attributes": {"COMPANY": {"content": "INITECH"}, "NAME": {"content": "Initech"}}}],
        rs_count=1,
        rs_total=3,
    )
    requests_mock.get(
        url,
        [
            {"json": page1},
            {"json": page2},
        ],
    )

    session = requests.Session()
    rows = list(
        iter_rest_mbo(session, BASE_URL, HEADERS, "company", fields=["company", "name"], page_size=2)
    )

    assert [r["company"] for r in rows] == ["ACME", "GLOBEX", "INITECH"]
    assert requests_mock.call_count == 2
    first_qs = requests_mock.request_history[0].qs
    assert first_qs["_rsstart"] == ["0"]
    assert first_qs["_maxitems"] == ["2"]
    second_qs = requests_mock.request_history[1].qs
    assert second_qs["_rsstart"] == ["2"]


def test_iter_rest_mbo_stops_on_short_page_even_if_rstotal_higher(requests_mock):
    """A page shorter than page_size ends pagination even without hitting rsTotal exactly."""
    url = f"{BASE_URL}/rest/mbo/company"
    page1 = _mbo_response(
        [{"Attributes": {"COMPANY": {"content": "ACME"}}}], rs_count=1, rs_total=50
    )
    requests_mock.get(url, json=page1)

    session = requests.Session()
    rows = list(iter_rest_mbo(session, BASE_URL, HEADERS, "company", fields=["company"], page_size=10))

    assert len(rows) == 1
    assert requests_mock.call_count == 1


def test_iter_rest_mbo_empty_result_yields_nothing(requests_mock):
    url = f"{BASE_URL}/rest/mbo/company"
    requests_mock.get(url, json=_mbo_response([], rs_count=0, rs_total=0))

    session = requests.Session()
    rows = list(iter_rest_mbo(session, BASE_URL, HEADERS, "company", fields=["company"]))

    assert rows == []
