from app.csv_validation import parse_csv


def parse(text, max_rows=20):
    raw = text if isinstance(text, bytes) else text.encode()
    return parse_csv(raw, max_rows=max_rows, max_bytes=1_000_000)


def msgs(errors):
    return [e.message for e in errors]


# --- valid input ------------------------------------------------------------------------


def test_valid_with_and_without_phone():
    p = parse("name,address,phone\nA,1 St,555\nB,2 St,\n")
    assert not p.errors and not p.invalid
    assert [(r.name, r.phone) for r in p.rows] == [("A", "555"), ("B", None)]


def test_phone_column_optional():
    p = parse("name,address\nA,1 St\n")
    assert not p.errors and p.rows[0].phone is None


def test_header_case_whitespace_and_bom():
    p = parse(b"\xef\xbb\xbf Name , ADDRESS ,Phone\nA,1 St,1\n")
    assert not p.errors and len(p.rows) == 1


def test_blank_lines_skipped_and_row_numbers_stable():
    p = parse("name,address\nA,1\n\n,\nB,2\n")
    assert not p.errors
    assert [r.row for r in p.rows] == [1, 2]


def test_quoted_commas():
    p = parse('name,address\n"Smith, John Hospital","1 Main St, Ste 2"\n')
    assert not p.errors and p.rows[0].name == "Smith, John Hospital"


# --- file-level problems: reject the whole upload -----------------------------------------


def test_missing_required_column():
    p = parse("name,phone\nA,1\n")
    assert any("address" in m for m in msgs(p.errors)) and not p.rows


def test_duplicate_columns_rejected():
    p = parse("name,name,address\nA,A,B\n")
    assert any("Duplicate" in m for m in msgs(p.errors))


def test_empty_file_and_header_only():
    assert msgs(parse("").errors) == ["File is empty"]
    assert "no data rows" in msgs(parse("name,address\n").errors)[0]


def test_non_utf8_and_oversize():
    assert "UTF-8" in msgs(parse(b"name,address\n\xff\xfe,x\n").errors)[0]
    p = parse_csv(b"name,address\nA,B\n", max_rows=20, max_bytes=5)
    assert "exceeds" in msgs(p.errors)[0]


def test_csv_error_mid_file_rejects_the_file():
    # e.g. an unclosed quote swallowing the rest of the file into one huge field: the csv
    # module raises once the field passes its size limit (128 KB). Later rows can't be
    # trusted, so the whole file is rejected even though row 1 was fine.
    p = parse('name,address\nA,1\nB,"' + "x" * 200_000 + "\nC,3\n")
    assert p.errors and "Malformed CSV near data row 2" in p.errors[0].message
    assert not p.rows


def test_every_row_invalid_is_a_file_error():
    p = parse("name,address\nA,\n,2\n")
    assert msgs(p.errors) == ["No valid rows: every row has a problem"]
    assert len(p.invalid) == 2  # still reported, so the user sees why


# --- row-level problems: record the row, process the rest (D19) ---------------------------


def test_bad_rows_are_separated_from_good_ones():
    p = parse("name,address\nGood,1 St\nA,\n,2\nAlso good,3 St\n")
    assert not p.errors
    assert [r.row for r in p.rows] == [1, 4]
    assert [(r.row, [e.field for e in r.errors]) for r in p.invalid] == [(2, ["address"]), (3, ["name"])]
    assert p.invalid[0].message == "Row data is not correct: 'address' is required"
    assert p.total == 4


def test_row_keeps_its_raw_values():
    p = parse("name,address,phone\nOK,1 St,\nNo Address,,555\n")
    bad = p.invalid[0]
    assert (bad.name, bad.address, bad.phone) == ("No Address", "", "555")


def test_extra_values_with_data_make_the_row_invalid():
    # an unquoted comma shifts every later value: never truncate (D18)
    p = parse("name,address,phone\nGeneral Hospital, Inc,1 Main St,555-1234\nOK,1 St,1\n")
    assert [r.row for r in p.rows] == [2]
    assert "4 values for 3 columns" in p.invalid[0].message


def test_trailing_empty_values_are_accepted():
    p = parse("name,address,phone\nA,1 St,555,,\n")
    assert not p.invalid and p.rows[0].phone == "555"


def test_short_row_missing_optional_phone_is_accepted():
    p = parse("name,address,phone\nA,1 St\n")
    assert not p.invalid and p.rows[0].phone is None


def test_unknown_columns_ignored_with_a_warning():
    p = parse("name,email,address\nA,a@x.org,1 St\n")
    assert not p.errors and p.rows[0].address == "1 St"
    assert p.warnings == ["Ignored column(s): email"]


# --- row limit ----------------------------------------------------------------------------


def _n_rows(n):
    return "name,address\n" + "".join(f"H{i},addr\n" for i in range(n))


def test_row_limit():
    assert any("maximum is 20" in m for m in msgs(parse(_n_rows(21)).errors))
    assert not parse(_n_rows(21), max_rows=21).errors


def test_row_limit_counts_invalid_rows_too():
    text = _n_rows(20) + "Bad,\n"
    assert any("21 rows; maximum is 20" in m for m in msgs(parse(text).errors))


def test_zero_means_no_row_limit():
    p = parse(_n_rows(500), max_rows=0)
    assert len(p.rows) == 500 and not p.errors


def test_exactly_at_limit_ok():
    p = parse(_n_rows(20))
    assert len(p.rows) == 20 and not p.errors
