"""Excel-style fill formulas (MxLoader's Excel transformation layer)."""

import pytest

from maxmig.mxl.formula import FUNCTIONS, Formula, FormulaContext, FormulaError, LookupTable

LOC = LookupTable("LOCMAP", ["OLD", "NEW", "SITE"], [["BLK1000", "BLK-1000", "BEDFORD"], ["BR210", "BR-210", "NASHUA"]])
ROW = {"ASSET": "AS_P100", "DESCRIPTION": "PUMP 100", "LOCATION": " blk1000 ", "QTY": "3", "BLANK": "", "ASSET NAME": "x"}


def ev(text, row=ROW, functions=None):
    return Formula.parse(text).evaluate(FormulaContext(functions or FUNCTIONS, {"LOCMAP": LOC}, row))


# --------------------------------------------------------------------------- the user-guide example


@pytest.mark.parametrize(
    "formula,expected",
    [
        ('=CONCATENATE("PUMP", RIGHT([Asset],3))', "PUMP100"),
        ("=PROPER([Description])", "Pump 100"),
        ("=[Location]", " blk1000 "),
        ('="BEDFORD"', "BEDFORD"),
    ],
)
def test_user_guide_asset_example(formula, expected):
    assert ev(formula) == expected


def test_bare_and_bracketed_column_names_are_case_insensitive():
    assert ev("=asset") == ev("=[ASSET]") == "AS_P100"
    assert ev("=[Asset Name]") == "x"


def test_unknown_column_is_a_name_error():
    with pytest.raises(FormulaError, match="#NAME"):
        ev("=[Nope]")


# --------------------------------------------------------------------------- operators


@pytest.mark.parametrize(
    "formula,expected",
    [
        ("=1+2*3", 7),
        ("=(1+2)*3", 9),
        ("=2-1-1", 0),
        ("=8/2/2", 2),
        ("=-QTY+1", -2),
        ('=1+2&"x"', "3x"),
        ('="a"&"b"&QTY', "ab3"),
        ("=QTY*2>5", True),
        ('="abc"="ABC"', True),
        ("=QTY<>3", False),
        ("=10>9", True),
        ('="10">"9"', True),  # numeric text compares numerically
        ("=7/2", 3.5),
    ],
)
def test_operators_follow_excel_precedence(formula, expected):
    assert ev(formula) == expected


def test_division_by_zero():
    with pytest.raises(FormulaError, match="DIV/0"):
        ev("=1/0")


def test_arithmetic_on_text_is_value_error():
    with pytest.raises(FormulaError, match="#VALUE"):
        ev("=[Asset]+1")


# --------------------------------------------------------------------------- functions


@pytest.mark.parametrize(
    "formula,expected",
    [
        ('=LEFT("PUMP100",4)', "PUMP"),
        ('=MID("ABCDEF",2,3)', "BCD"),
        ('=LEN("abc")', 3),
        ('=UPPER("x")&LOWER("Y")', "Xy"),
        ('=TRIM("  a   b ")', "a b"),
        ('=REPLACE("ABCDEF",2,3,"x")', "AxEF"),
        ('=SUBSTITUTE("a-b-c","-","/")', "a/b/c"),
        ('=SUBSTITUTE("a-b-c","-","/",2)', "a-b/c"),
        ('=FIND("C","ABCC")', 3),
        ('=SEARCH("c","ABCC")', 3),
        ('=TEXT(42,"00000")', "00042"),
        ('=TEXT(3.14159,"0.00")', "3.14"),
        ('=TEXT("2026-03-04","dd/mm/yyyy")', "04/03/2026"),
        ('=DATEVALUE("03/04/2026")', "2026-03-04"),
        ("=ROUND(2.5)", 3),
        ("=ROUND(-2.5)", -3),
        ("=ROUND(1.2345,2)", 1.23),
        ('=VALUE("12")+1', 13),
        ('=TEXTJOIN("-",TRUE,"a","","b")', "a-b"),
        ('=COALESCE([Blank],"",[Asset])', "AS_P100"),
        ("=ISBLANK([Blank])", True),
        ("=ISNUMBER(QTY)", True),
        ('=AND(QTY>1, NOT(ISBLANK(ASSET)))', True),
        ("=MAX(1,QTY,2)", 3),
        ("=NULL()", "~NULL~"),
    ],
)
def test_builtin_functions(formula, expected):
    assert ev(formula) == expected


def test_unknown_function():
    with pytest.raises(FormulaError, match="unknown function NOPE"):
        ev("=NOPE(1)")


# --------------------------------------------------------------------------- lazy branches


def test_if_only_evaluates_the_branch_taken():
    assert ev('=IF(ISBLANK([Blank]), "", VLOOKUP([Blank], "LOCMAP", 2, FALSE))') == ""


def test_iferror_catches_lookup_misses():
    assert ev('=IFERROR(VLOOKUP("NOPE","LOCMAP",2,FALSE),"?")') == "?"


def test_ifs_and_switch():
    assert ev('=IFS(QTY>5,"big",QTY>1,"mid")') == "mid"
    assert ev('=SWITCH(QTY,1,"a",3,"c","z")') == "c"
    assert ev('=SWITCH(QTY,1,"a","z")') == "z"
    with pytest.raises(FormulaError, match="SWITCH has no match"):
        ev('=SWITCH(QTY,1,"a")')


# --------------------------------------------------------------------------- VLOOKUP


def test_vlookup_by_index_and_header_trimmed_and_case_insensitive():
    assert ev('=VLOOKUP([Location],"LOCMAP",2,FALSE)') == "BLK-1000"
    assert ev('=VLOOKUP([Location],"locmap","SITE")') == "BEDFORD"


def test_vlookup_miss_is_na():
    with pytest.raises(FormulaError, match="#N/A"):
        ev('=VLOOKUP("X","LOCMAP",2,FALSE)')


def test_vlookup_rejects_approximate_match():
    with pytest.raises(FormulaError, match="exact match"):
        ev('=VLOOKUP([Location],"LOCMAP",2,TRUE)')


def test_vlookup_unknown_table_and_column():
    with pytest.raises(FormulaError, match="no lookup table"):
        ev('=VLOOKUP("a","OTHER",2)')
    with pytest.raises(FormulaError, match="#REF"):
        ev('=VLOOKUP([Location],"LOCMAP",9)')


# --------------------------------------------------------------------------- parsing / safety


@pytest.mark.parametrize("bad", ["=1+", '="open', "=FOO(1,", "=1 2", "=(1", "=@"])
def test_malformed_formulas_are_rejected_at_parse_time(bad):
    with pytest.raises(FormulaError):
        Formula.parse(bad)


def test_python_is_never_evaluated():
    # Looks like a function call; resolves only against the function table.
    with pytest.raises(FormulaError, match="unknown function __IMPORT__"):
        ev('=__import__("os")')


def test_escaped_quotes_and_columns_listing():
    f = Formula.parse('=[A] & """" & B & C(D)')
    assert f.columns() == {"A", "B", "D"}
    assert ev('="say ""hi"""') == 'say "hi"'


def test_udf_exceptions_become_row_errors():
    def boom(_):
        raise RuntimeError("bad")

    with pytest.raises(FormulaError, match="BOOM failed: bad"):
        ev("=BOOM(1)", functions={**FUNCTIONS, "BOOM": boom})
