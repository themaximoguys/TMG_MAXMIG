import pytest

from maxmig.api.filter import Filter, UnsupportedFilterError, to_oslc_where, to_rest_mbo_params


def test_to_oslc_where_with_no_filter_returns_default():
    assert to_oslc_where('type="STOREROOM"', None) == 'type="STOREROOM"'


def test_to_oslc_where_combines_default_where_and_conditions():
    filt = Filter(conditions={"status": "APPR"})
    where = to_oslc_where('type="STOREROOM"', filt)
    assert where == '(type="STOREROOM") and status="APPR"'


def test_to_oslc_where_combines_site_and_conditions():
    filt = Filter(site="BEDFORD", conditions={"status": "APPR"})
    where = to_oslc_where(None, filt)
    assert where == 'siteid="BEDFORD" and status="APPR"'


def test_to_oslc_where_returns_none_when_nothing_set():
    assert to_oslc_where(None, Filter()) is None


def test_to_oslc_where_raw_where_overrides_everything():
    filt = Filter(site="BEDFORD", conditions={"status": "APPR"}, raw_where='ponum="1234"')
    where = to_oslc_where('type="STOREROOM"', filt)
    assert where == 'ponum="1234"'  # default_where, site, and conditions all ignored


def test_to_oslc_where_rejects_a_value_containing_a_double_quote():
    """A value like `BEDFORD" or "1"="1` must not be silently embedded — it
    would turn a scoping filter into an always-true predicate."""
    filt = Filter(site='BEDFORD" or "1"="1')
    with pytest.raises(ValueError, match="double quote"):
        to_oslc_where(None, filt)


def test_filter_rejects_conflicting_site_and_siteid_condition():
    with pytest.raises(ValueError, match="conflicting site filter"):
        Filter(site="BEDFORD", conditions={"siteid": "MAIN"})


def test_to_rest_mbo_params_includes_site_as_siteid():
    filt = Filter(site="BEDFORD")
    assert to_rest_mbo_params(None, filt) == {"SITEID": "BEDFORD"}


def test_to_rest_mbo_params_uppercases_condition_keys():
    filt = Filter(conditions={"status": "APPR", "orgid": "ACME"})
    assert to_rest_mbo_params(None, filt) == {"STATUS": "APPR", "ORGID": "ACME"}


def test_to_rest_mbo_params_with_no_filter_is_empty():
    assert to_rest_mbo_params(None, None) == {}


def test_to_rest_mbo_params_raises_on_raw_where():
    filt = Filter(raw_where='ponum="1234"')
    with pytest.raises(UnsupportedFilterError):
        to_rest_mbo_params(None, filt)


def test_to_rest_mbo_params_raises_on_default_where():
    """The object's own YAML `where:` (an OSLC boolean expression) can't be
    expressed via REST MBO's {FIELD}={VALUE} params either — it must raise,
    not silently drop the object's configured scope."""
    with pytest.raises(UnsupportedFilterError):
        to_rest_mbo_params('type="STOREROOM"', None)
