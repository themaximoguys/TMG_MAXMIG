import csv
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from maxmig.extract.writer import write_csv, write_object_tree

COLUMNS = ["company", "orgid", "name"]


@dataclass
class _FakeRelation:
    name: str
    relation_attribute: str
    foreign_key: str | list[str]
    key_fields: list[str]
    fields: list[str]
    children: list["_FakeRelation"] = field(default_factory=list)


@dataclass
class _FakeObject:
    name: str
    key_fields: list[str]
    fields: list[str]
    children: list[_FakeRelation] = field(default_factory=list)


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _read_back(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_header_row_matches_columns_exactly(tmp_path):
    path = tmp_path / "company.csv"
    write_csv(iter([]), path, COLUMNS)

    assert _read_back(path) == [COLUMNS]


def test_returns_number_of_data_rows_written(tmp_path):
    path = tmp_path / "company.csv"
    rows = [
        {"company": "ACME", "orgid": "EAGLENA", "name": "Acme Corp"},
        {"company": "BETA", "orgid": "EAGLENA", "name": "Beta Ltd"},
    ]

    assert write_csv(iter(rows), path, COLUMNS) == 2


def test_writes_values_in_column_order_not_dict_order(tmp_path):
    path = tmp_path / "company.csv"
    write_csv(iter([{"name": "Acme Corp", "company": "ACME", "orgid": "EAGLENA"}]), path, COLUMNS)

    assert _read_back(path)[1] == ["ACME", "EAGLENA", "Acme Corp"]


def test_missing_field_and_none_value_become_empty_string_not_the_word_none(tmp_path):
    path = tmp_path / "company.csv"
    write_csv(iter([{"company": "ACME", "name": None}]), path, COLUMNS)

    body = path.read_text(encoding="utf-8")
    assert _read_back(path)[1] == ["ACME", "", ""]
    assert "None" not in body
    assert "null" not in body


def test_explicit_empty_string_stays_empty(tmp_path):
    path = tmp_path / "company.csv"
    write_csv(iter([{"company": "ACME", "orgid": "", "name": ""}]), path, COLUMNS)

    assert _read_back(path)[1] == ["ACME", "", ""]


def test_extra_fields_not_in_columns_are_dropped(tmp_path):
    path = tmp_path / "company.csv"
    write_csv(
        iter([{"company": "ACME", "orgid": "EAGLENA", "name": "Acme", "disabled": "0"}]),
        path,
        COLUMNS,
    )

    assert _read_back(path) == [COLUMNS, ["ACME", "EAGLENA", "Acme"]]


@pytest.mark.parametrize(
    "value",
    [
        "Acme, Incorporated",
        'He said "hello"',
        "line one\nline two",
        "trailing\r\ncarriage",
        "semi;colon and\ttab",
        "unicode ü ñ 中文",
    ],
)
def test_special_characters_round_trip_through_csv_reader(tmp_path, value):
    path = tmp_path / "company.csv"
    write_csv(iter([{"company": "ACME", "orgid": "EAGLENA", "name": value}]), path, COLUMNS)

    assert _read_back(path)[1][2] == value


def test_values_are_written_verbatim_without_reformatting(tmp_path):
    path = tmp_path / "company.csv"
    rows = [{"company": "0012300", "orgid": "2026-09-09T00:00:00+00:00", "name": "1.500"}]
    write_csv(iter(rows), path, COLUMNS)

    assert _read_back(path)[1] == ["0012300", "2026-09-09T00:00:00+00:00", "1.500"]


def test_file_is_utf8_without_a_bom(tmp_path):
    path = tmp_path / "company.csv"
    write_csv(iter([{"company": "ü"}]), path, COLUMNS)

    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "run" / "purchasing" / "company.csv"
    write_csv(iter([{"company": "ACME"}]), path, COLUMNS)

    assert path.exists()


def test_a_source_that_fails_mid_stream_leaves_no_file_at_the_final_path(tmp_path):
    """A failed export must never leave a truncated file sitting at the final
    path looking complete — that's the only thing an interrupted run's
    absent manifest.json can't signal on its own."""
    path = tmp_path / "company.csv"

    def rows():
        for i in range(3):
            yield {"company": f"C{i}", "orgid": "EAGLENA", "name": f"n{i}"}
        raise RuntimeError("connection reset by peer")

    with pytest.raises(RuntimeError):
        write_csv(rows(), path, COLUMNS)

    assert not path.exists()
    assert not path.with_name(path.name + ".partial").exists()  # cleaned up, not left behind


def test_only_rows_yielded_before_the_failure_are_ever_pulled_from_the_source():
    """The source is only consumed up to the point of failure — proves write_csv
    doesn't drain the whole iterator (e.g. via list(rows)) before writing a byte,
    which is what makes it safe for a source that can raise partway through."""
    pulled = []

    def rows():
        for i in range(3):
            pulled.append(i)
            yield {"company": f"C{i}"}
        raise RuntimeError("connection reset by peer")

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(RuntimeError):
            write_csv(rows(), Path(tmp) / "company.csv", COLUMNS)

    assert pulled == [0, 1, 2]


def test_accepts_a_lazy_generator_source(tmp_path):
    path = tmp_path / "company.csv"
    source = ({"company": f"C{i}"} for i in range(3))

    assert write_csv(source, path, COLUMNS) == 3
    assert [row[0] for row in _read_back(path)[1:]] == ["C0", "C1", "C2"]


def test_write_object_tree_with_no_children_behaves_like_write_csv(tmp_path):
    obj = _FakeObject(name="item", key_fields=["itemnum"], fields=["description"])
    rows = [{"itemnum": "0-0031", "description": "Elbow"}]

    results = write_object_tree(iter(rows), obj, tmp_path, "inventory", "20260911T000000Z")

    assert results["item"].row_count == 1
    expected_path = tmp_path / "inventory" / "item" / "item_20260911T000000Z.csv"
    assert results["item"].path == expected_path
    assert _read_csv(expected_path) == [
        ["itemnum", "description"],
        ["0-0031", "Elbow"],
    ]


def test_write_object_tree_writes_one_csv_per_child_with_foreign_key_merged_in(tmp_path):
    invbalances = _FakeRelation(
        name="invbalances",
        relation_attribute="invbalances",
        foreign_key=["itemnum", "siteid"],
        key_fields=["itemnum", "siteid", "binnum"],
        fields=["binnum", "curbal"],
    )
    obj = _FakeObject(
        name="inventory_balance",
        key_fields=["itemnum", "siteid"],
        fields=["curbal"],
        children=[invbalances],
    )
    rows = [
        {
            "itemnum": "0-0031",
            "siteid": "FLEET",
            "curbal": 3.0,
            # Real Maximo child payloads never repeat the parent's own key
            # fields (itemnum/siteid) -- only binnum/curbal are present here.
            "invbalances": [{"binnum": "D-9-4", "curbal": 3.0}],
        },
        {"itemnum": "0-0048", "siteid": "FLEET", "curbal": 1.0, "invbalances": []},
    ]

    results = write_object_tree(iter(rows), obj, tmp_path, "inventory", "20260911T000000Z")

    assert results["inventory_balance"].row_count == 2
    assert results["invbalances"].row_count == 1
    assert _read_csv(tmp_path / "inventory" / "inventory_balance" / "inventory_balance_20260911T000000Z.csv") == [
        ["itemnum", "siteid", "curbal"],
        ["0-0031", "FLEET", "3.0"],
        ["0-0048", "FLEET", "1.0"],
    ]
    assert _read_csv(tmp_path / "inventory" / "invbalances" / "invbalances_20260911T000000Z.csv") == [
        ["itemnum", "siteid", "binnum", "curbal"],
        ["0-0031", "FLEET", "D-9-4", "3.0"],
    ]


def test_write_object_tree_handles_a_single_child_dict_not_wrapped_in_a_list(tmp_path):
    child = _FakeRelation(
        name="invcost", relation_attribute="invcost", foreign_key="itemnum",
        key_fields=["itemnum", "invcostid"], fields=["invcostid", "avgcost"],
    )
    obj = _FakeObject(name="inventory_balance", key_fields=["itemnum"], fields=[], children=[child])
    rows = [{"itemnum": "0-0031", "invcost": {"invcostid": 305, "avgcost": 2.5}}]

    results = write_object_tree(iter(rows), obj, tmp_path, "inventory", "20260911T000000Z")

    assert results["inventory_balance"].row_count == 1
    assert results["invcost"].row_count == 1
    assert _read_csv(tmp_path / "inventory" / "invcost" / "invcost_20260911T000000Z.csv")[1] == ["0-0031", "305", "2.5"]


def test_write_object_tree_recurses_into_grandchildren(tmp_path):
    grandchild = _FakeRelation(
        name="costline", relation_attribute="costline", foreign_key="polinenum",
        key_fields=["polinenum", "glaccount"], fields=["glaccount", "amount"],
    )
    child = _FakeRelation(
        name="poline", relation_attribute="poline", foreign_key="ponum",
        key_fields=["ponum", "polinenum"], fields=["polinenum"], children=[grandchild],
    )
    obj = _FakeObject(name="purchase_order", key_fields=["ponum"], fields=[], children=[child])
    rows = [
        {
            "ponum": "PO1",
            "poline": [
                {"polinenum": 1, "costline": [{"glaccount": "6600", "amount": 10.0}]}
            ],
        }
    ]

    results = write_object_tree(iter(rows), obj, tmp_path, "purchasing", "20260911T000000Z")

    assert results["purchase_order"].row_count == 1
    assert results["poline"].row_count == 1
    assert results["costline"].row_count == 1
    assert _read_csv(tmp_path / "purchasing" / "costline" / "costline_20260911T000000Z.csv") == [
        ["polinenum", "glaccount", "amount"],
        ["1", "6600", "10.0"],
    ]


def test_write_object_tree_cleans_up_all_partial_files_on_mid_stream_failure(tmp_path):
    child = _FakeRelation(
        name="invbalances", relation_attribute="invbalances", foreign_key="itemnum",
        key_fields=["itemnum", "binnum"], fields=["binnum"],
    )
    obj = _FakeObject(name="inventory_balance", key_fields=["itemnum"], fields=[], children=[child])

    def rows():
        yield {"itemnum": "0-0031", "invbalances": [{"binnum": "D-9-4"}]}
        raise RuntimeError("connection reset by peer")

    with pytest.raises(RuntimeError):
        write_object_tree(rows(), obj, tmp_path, "inventory", "20260911T000000Z")

    ts = "20260911T000000Z"
    assert not (tmp_path / "inventory" / "inventory_balance" / f"inventory_balance_{ts}.csv").exists()
    assert not (tmp_path / "inventory" / "invbalances" / f"invbalances_{ts}.csv").exists()
    assert not (tmp_path / "inventory" / "inventory_balance" / f"inventory_balance_{ts}.csv.partial").exists()
    assert not (tmp_path / "inventory" / "invbalances" / f"invbalances_{ts}.csv.partial").exists()
