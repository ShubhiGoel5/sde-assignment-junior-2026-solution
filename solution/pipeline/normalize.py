"""Value cleaning rules from the spec. Parsers return the clean value, or None if invalid."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_HEADER_RUN = re.compile(r"[_\s]+")
_NON_DIGIT = re.compile(r"\D")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_DATE_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_DMY = re.compile(r"(\d{1,2})([/-])(\d{1,2})\2(\d{4})")      # DD/MM/YYYY or DD-MM-YYYY
_DATE_DMON = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")       # DD-Mon-YYYY

# optional currency prefix, then digits with commas and an optional fraction
_DECIMAL = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d*)?|\.\d+)", re.IGNORECASE)


def normalize_header(header: str) -> str:
    """Lower-case, turn runs of _ or whitespace into one space, trim."""
    return _HEADER_RUN.sub(" ", header.lower()).strip()


def parse_phone(raw: object) -> str | None:
    """Indian mobile number -> +91XXXXXXXXXX, or None."""
    if raw is None:
        return None
    digits = _NON_DIGIT.sub("", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def parse_date(raw: str) -> str | None:
    """Day-first date in one of the four formats -> YYYY-MM-DD."""
    s = raw.strip()
    if m := _DATE_ISO.fullmatch(s):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := _DATE_DMY.fullmatch(s):
        d, mo, y = int(m[1]), int(m[3]), int(m[4])
    elif m := _DATE_DMON.fullmatch(s):
        mo = _MONTHS.get(m[2].lower())
        if mo is None:
            return None
        d, y = int(m[1]), int(m[3])
    else:
        return None
    try:
        return date(y, mo, d).isoformat()   # rejects things like 31/02
    except ValueError:
        return None


def parse_decimal(raw: str) -> str | None:
    """Amount with optional ₹/Rs/Rs./INR prefix and commas -> "1234.50"."""
    m = _DECIMAL.fullmatch(raw.strip())
    if not m:
        return None
    try:
        value = Decimal(m[1].replace(",", ""))
    except InvalidOperation:
        return None
    # no sign in the regex, so never negative
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_enum(raw: str, values: list[str]) -> str | None:
    v = raw.strip().lower()
    return v if v in {x.lower() for x in values} else None


def parse_value(ftype: str, raw: str, values: list[str] | None = None) -> str | None:
    """Clean one trimmed, non-empty cell by its schema type."""
    if ftype == "string":
        return raw
    if ftype == "phone":
        return parse_phone(raw)
    if ftype == "date":
        return parse_date(raw)
    if ftype == "decimal":
        return parse_decimal(raw)
    if ftype == "enum":
        return parse_enum(raw, values or [])
    raise ValueError(f"unknown field type: {ftype}")


def parse_timestamp(raw: object) -> datetime | None:
    """UTC, offset or naive (= IST) timestamp -> UTC datetime."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def to_db_utc(dt: datetime) -> str:
    """UTC text form used in the database. Sorts correctly and works with SQLite date functions."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
