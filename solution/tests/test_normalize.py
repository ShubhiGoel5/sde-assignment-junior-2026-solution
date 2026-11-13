import pytest

from solution.pipeline.normalize import (normalize_header, parse_date, parse_decimal, parse_enum,
                                         parse_phone, parse_timestamp, to_db_utc)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210",
    "+919876543210", " 98765-43210 ",
])
def test_phone_formats_normalise_to_one_canonical_form(raw):
    assert parse_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",          # 9 digits
    "5876543210",         # must start with 6-9
    "022-2345-6789",      # landline
    "N/A", "", None,
    "+1 415 555 0134",
    "0919876543210",      # only one prefix is stripped
    "919876543",          # starts with 91 but not 12 digits
])
def test_invalid_phones(raw):
    assert parse_phone(raw) is None


def test_ten_digit_number_starting_with_91_keeps_its_digits():
    assert parse_phone("9123456789") == "+919123456789"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-dEC-2026", "2026-12-01"),
    ("05/11/2026", "2026-11-05"),     # day first
    ("29/02/2028", "2028-02-29"),     # leap year
])
def test_dates(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "soon", "TBD", "29/02/2026",
                                 "01-Foo-2026", "12/2026", "01/12/26", "2026/12/01"])
def test_invalid_dates(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("4737.14", "4737.14"), ("18678", "18678.00"), ("₹28,397", "28397.00"),
    ("Rs. 24787", "24787.00"), ("rs 100", "100.00"), ("Rs.100", "100.00"),
    ("INR 18678.3", "18678.30"), ("inr1,00,000.5", "100000.50"), ("0", "0.00"),
    ("2.345", "2.35"), ("2.344", "2.34"),
])
def test_decimals(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-5", "$5", "1e3", "Rs.", ","])
def test_invalid_decimals(raw):
    assert parse_decimal(raw) is None


def test_enum_is_case_insensitive_and_stored_lower_case():
    assert parse_enum("HI", ["en", "hi"]) == "hi"
    assert parse_enum("Hindi", ["en", "hi"]) is None


@pytest.mark.parametrize("raw,expected", [
    ("Policy_No", "policy no"), ("  Mobile   No ", "mobile no"), ("customer__name", "customer name"),
    ("EXPIRY_ \tDate", "expiry date"),
])
def test_header_normalisation(raw, expected):
    assert normalize_header(raw) == expected


@pytest.mark.parametrize("raw", [
    "2026-10-03T04:31:00Z", "2026-10-03T10:01:00+05:30", "2026-10-03 10:01:00",
])
def test_three_timestamp_forms_are_the_same_instant(raw):
    assert to_db_utc(parse_timestamp(raw)) == "2026-10-03 04:31:00"


def test_bad_timestamps():
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(None) is None
