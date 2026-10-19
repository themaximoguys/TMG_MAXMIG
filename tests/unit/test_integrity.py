import csv
from types import SimpleNamespace

from maxmig.integrity import check_tree


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _tree():
    """po -> poline (composite FK ponum+siteid) -> pocost (FK ponum+siteid+polinenum)."""
    pocost = SimpleNamespace(
        name="pocost",
        foreign_key=["ponum", "siteid", "polinenum"],
        key_fields=["ponum", "siteid", "polinenum", "costlinenum"],
        children=[],
    )
    poline = SimpleNamespace(
        name="poline",
        foreign_key=["ponum", "siteid"],
        key_fields=["ponum", "siteid", "polinenum"],
        children=[pocost],
    )
    return SimpleNamespace(name="po", key_fields=["ponum", "siteid"], children=[poline])


def _files(tmp_path, po_rows=None, poline_rows=None, pocost_rows=None):
    return {
        "po": _write(tmp_path / "po.csv", ["ponum", "siteid", "description"],
                     po_rows if po_rows is not None else [["1000", "BEDFORD", "a"], ["1001", "BEDFORD", "b"]]),
        "poline": _write(tmp_path / "poline.csv", ["ponum", "siteid", "polinenum", "itemnum"],
                         poline_rows if poline_rows is not None else [["1000", "BEDFORD", "1", "X"], ["1001", "BEDFORD", "1", "Y"]]),
        "pocost": _write(tmp_path / "pocost.csv", ["ponum", "siteid", "polinenum", "costlinenum"],
                         pocost_rows if pocost_rows is not None else [["1000", "BEDFORD", "1", "1"]]),
    }


def test_clean_tree_is_ok(tmp_path):
    report = check_tree(_tree(), _files(tmp_path))

    assert report.ok
    assert report.rows_checked == {"po": 2, "poline": 2, "pocost": 1}
    assert report.summary_lines()[0].startswith("integrity OK")


def test_composite_fk_orphan_detected_when_only_one_column_matches(tmp_path):
    # ponum 1000 exists, but only at BEDFORD — 1000/NASHUA is an orphan.
    paths = _files(tmp_path, poline_rows=[["1000", "BEDFORD", "1", "X"], ["1000", "NASHUA", "1", "Y"]])

    report = check_tree(_tree(), paths)

    assert not report.ok
    assert len(report.orphans) == 1
    orphan = report.orphans[0]
    assert (orphan.relation, orphan.row, orphan.values) == ("poline", 2, ("1000", "NASHUA"))
    assert orphan.foreign_key == ("ponum", "siteid")
    assert orphan.path == paths["poline"]


def test_grandchild_orphan_checked_against_immediate_parent(tmp_path):
    # PO line 2 doesn't exist for 1000, even though PO 1000 does.
    paths = _files(tmp_path, pocost_rows=[["1000", "BEDFORD", "1", "1"], ["1000", "BEDFORD", "2", "1"]])

    report = check_tree(_tree(), paths)

    assert [(o.relation, o.row, o.values) for o in report.orphans] == [("pocost", 2, ("1000", "BEDFORD", "2"))]


def test_single_string_foreign_key(tmp_path):
    child = SimpleNamespace(name="spec", foreign_key="itemnum", key_fields=["itemnum", "assetattrid"], children=[])
    item = SimpleNamespace(name="item", key_fields=["itemnum"], children=[child])
    paths = {
        "item": _write(tmp_path / "item.csv", ["itemnum"], [["A"]]),
        "spec": _write(tmp_path / "spec.csv", ["itemnum", "assetattrid"], [["A", "COLOR"], ["B", "COLOR"]]),
    }

    report = check_tree(item, paths)

    assert [(o.row, o.values) for o in report.orphans] == [(2, ("B",))]


def test_duplicate_natural_keys_reported(tmp_path):
    paths = _files(tmp_path, po_rows=[["1000", "BEDFORD", "a"], ["1001", "BEDFORD", "b"], ["1000", "BEDFORD", "c"]])

    report = check_tree(_tree(), paths)

    assert len(report.duplicates) == 1
    dup = report.duplicates[0]
    assert (dup.relation, dup.row, dup.first_row, dup.values) == ("po", 3, 1, ("1000", "BEDFORD"))
    assert not report.ok


def test_blank_key_fields_reported(tmp_path):
    paths = _files(tmp_path, po_rows=[["1000", "BEDFORD", "a"], ["1001", "  ", "b"], ["", "", "c"]],
                   poline_rows=[["1000", "BEDFORD", "1", "X"]])

    report = check_tree(_tree(), paths)

    assert [(b.row, b.blank_fields) for b in report.blank_keys] == [(2, ("siteid",)), (3, ("ponum", "siteid"))]
    assert any("blank" in line for line in report.summary_lines())


def test_missing_child_file_is_recorded_not_raised(tmp_path):
    paths = _files(tmp_path)
    paths["poline"].unlink()

    report = check_tree(_tree(), paths)

    assert not report.ok
    assert [(m.relation, m.reason) for m in report.missing] == [("poline", "file not found")]
    # grandchild still scanned for dups/blanks, but its FK can't be checked
    assert report.rows_checked["pocost"] == 1
    assert report.orphans == []


def test_relation_absent_from_mapping_and_missing_column(tmp_path):
    paths = _files(tmp_path)
    del paths["pocost"]
    _write(paths["poline"], ["ponum", "polinenum"], [["1000", "1"]])

    report = check_tree(_tree(), paths)

    reasons = {m.relation: m.reason for m in report.missing}
    assert reasons["pocost"] == "no CSV path given"
    assert "siteid" in reasons["poline"]
