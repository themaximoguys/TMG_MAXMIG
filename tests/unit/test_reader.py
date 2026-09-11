from pathlib import Path

from maxmig.load.reader import read_csv_rows


def test_read_csv_rows_yields_dicts_keyed_by_header(tmp_path):
    path = tmp_path / "company.csv"
    path.write_text("company,orgid,name\nACME,EAGLENA,Acme Corp\n", encoding="utf-8")

    rows = list(read_csv_rows(path))

    assert rows == [{"company": "ACME", "orgid": "EAGLENA", "name": "Acme Corp"}]


def test_read_csv_rows_header_only_yields_nothing(tmp_path):
    path = tmp_path / "company.csv"
    path.write_text("company,orgid,name\n", encoding="utf-8")

    assert list(read_csv_rows(path)) == []


def test_read_csv_rows_preserves_empty_string_values(tmp_path):
    path = tmp_path / "company.csv"
    path.write_text("company,orgid,name\nACME,,Acme Corp\n", encoding="utf-8")

    rows = list(read_csv_rows(path))

    assert rows[0]["orgid"] == ""


def test_read_csv_rows_accepts_a_plain_str_path(tmp_path):
    path = tmp_path / "company.csv"
    path.write_text("company\nACME\n", encoding="utf-8")

    assert list(read_csv_rows(str(path))) == [{"company": "ACME"}]


def test_read_csv_rows_multiple_rows_preserve_order(tmp_path):
    path = tmp_path / "company.csv"
    path.write_text("company\nACME\nBETA\nGLOBEX\n", encoding="utf-8")

    assert [r["company"] for r in read_csv_rows(path)] == ["ACME", "BETA", "GLOBEX"]
