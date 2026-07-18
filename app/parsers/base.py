"""Shared schema and helpers for all bank-statement / ledger parsers.

Every parser in `app/parsers/` MUST:
- output `list[StandardTransaction]` (see CLAUDE.md "Standard Internal Schema")
- use `parse_amount` / `parse_date` from this module instead of inlining
  their own number/date parsing logic.

See skills/bank-statement-parser/SKILL.md for the full spec this module
implements.
"""

from __future__ import annotations

import datetime
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


class ScannedPdfError(Exception):
    """Raised when a PDF has no extractable text (i.e. it's a scanned image).

    OCR is out of scope for v1 (REQUIREMENTS.md F1.4) — callers should show
    the message on this exception to the user, suggesting a CSV/Excel export
    instead.
    """

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or (
                "This PDF appears to be a scanned image with no extractable "
                "text. Please export/download the statement as CSV or Excel "
                "from your bank's portal instead."
            )
        )


@dataclass
class StandardTransaction:
    """The internal schema every parser normalizes into.

    Exactly as specified in CLAUDE.md's "Standard Internal Schema".
    """

    date: datetime.date
    description: str
    debit: Decimal
    credit: Decimal
    balance: Decimal | None
    source_row: int
    raw: dict = field(default_factory=dict)


class BaseParser(ABC):
    """Interface every bank adapter (and the generic/ledger parsers) implements."""

    bank_name: str = "Unknown"

    @abstractmethod
    def can_parse(self, file_bytes: bytes, filename: str) -> bool:
        """Return True if this adapter recognizes the file format/layout."""
        raise NotImplementedError

    @abstractmethod
    def parse(self, file_bytes: bytes, filename: str) -> list["StandardTransaction"]:
        """Parse the file into a list of StandardTransaction."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Strips anything that isn't a digit, dot, or minus sign.
_NON_NUMERIC_RE = re.compile(r"[^0-9.\-]")

# Excel's epoch (with the classic 1900 leap-year bug baked in, matching
# what pandas/openpyxl report for serial dates).
_EXCEL_EPOCH = datetime.date(1899, 12, 30)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_FORMATS = [
    "%d/%m/%Y", "%d/%m/%y",
    "%d-%m-%Y", "%d-%m-%y",
    "%d-%b-%Y", "%d-%b-%y",
    "%d %b %Y", "%d %b %y",
    "%d.%m.%Y", "%d.%m.%y",
    "%d-%B-%Y", "%d %B %Y",
]


def _is_nan(value: Any) -> bool:
    try:
        return isinstance(value, float) and math.isnan(value)
    except Exception:
        return False


def parse_amount(raw: Any) -> Decimal:
    """Parse a money value into a Decimal.

    Handles: comma thousands ("1,234,567.89"), parentheses-negative
    ("(5,000.00)"), DR/CR suffixes ("5,000.00 DR"), and blank/None/NaN
    (-> Decimal("0")). Never uses float() in the conversion path.
    """
    if raw is None or _is_nan(raw):
        return Decimal("0")

    if isinstance(raw, Decimal):
        return raw

    if isinstance(raw, int):
        return Decimal(raw)

    if isinstance(raw, float):
        # Convert via str() (repr-safe for typical spreadsheet floats) rather
        # than doing float arithmetic ourselves.
        return Decimal(str(raw))

    text = str(raw).strip()
    if text == "" or text in {"-", "--", "NaN", "nan", "None"}:
        return Decimal("0")

    negative = False

    # Parentheses = negative, e.g. "(5,000.00)"
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    # Strip a leading currency marker, e.g. "Rs. 1,000.00", "PKR 500", "₨50".
    text = re.sub(r"(?i)^\s*(rs\.?|pkr|₨)\s*", "", text).strip()

    # DR/CR suffix or prefix -> DR is negative (a debit reduces balance),
    # CR is positive. Handles both spaced ("5,000.00 DR") and unspaced
    # ("500DR") markers, at either end of the string.
    marker = None
    lower = text.lower()
    if lower.startswith("dr") and len(text) > 2:
        marker = "dr"
        text = text[2:].strip()
    elif lower.startswith("cr") and len(text) > 2:
        marker = "cr"
        text = text[2:].strip()
    else:
        if lower.endswith("dr") and len(text) > 2:
            marker = "dr"
            text = text[:-2].strip()
        elif lower.endswith("cr") and len(text) > 2:
            marker = "cr"
            text = text[:-2].strip()

    if marker == "dr":
        negative = True

    # Leading minus sign
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    text = text.replace(",", "")
    text = _NON_NUMERIC_RE.sub("", text)

    if text == "" or text == "." or text == "-":
        return Decimal("0")

    try:
        value = Decimal(text)
    except InvalidOperation:
        return Decimal("0")

    if negative:
        value = -abs(value)

    return value


def _excel_serial_to_date(serial: float) -> datetime.date:
    return _EXCEL_EPOCH + datetime.timedelta(days=int(serial))


def parse_date(raw: Any) -> datetime.date:
    """Parse a date value, always interpreting ambiguous numeric dates as
    day-first (DD/MM/YYYY), never month-first.

    Accepts: DD/MM/YYYY, DD-MM-YYYY, DD-MMM-YYYY, DD MMM YYYY, DD.MM.YYYY,
    and Excel serial numbers (int/float).
    """
    if raw is None or _is_nan(raw):
        raise ValueError("Cannot parse empty date value")

    if isinstance(raw, datetime.datetime):
        return raw.date()

    if isinstance(raw, datetime.date):
        return raw

    if isinstance(raw, (int, float)):
        # Excel serial date number.
        return _excel_serial_to_date(float(raw))

    text = str(raw).strip()
    if text == "":
        raise ValueError("Cannot parse empty date value")

    # Excel serial number stored as a string, e.g. "45678".
    if re.fullmatch(r"\d{4,6}(\.0+)?", text):
        return _excel_serial_to_date(float(text))

    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    # Manual fallback for formats like "31 January 2026" with 3-letter month
    # lookup, or separators strptime's %b/%B might not match locale-independently.
    match = re.match(
        r"^(\d{1,2})[\s\-./]+([A-Za-z]+)[\s\-./]+(\d{2,4})$", text
    )
    if match:
        day_s, month_s, year_s = match.groups()
        month = _MONTH_MAP.get(month_s.lower()[:4]) or _MONTH_MAP.get(month_s.lower()[:3])
        if month:
            day = int(day_s)
            year = int(year_s)
            if year < 100:
                year += 2000
            return datetime.date(year, month, day)

    # Numeric day-first fallback, e.g. "3/4/2026" -> handled by formats above
    # already, but guard against 2-digit years or odd separators.
    match = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", text)
    if match:
        day_s, month_s, year_s = match.groups()
        year = int(year_s)
        if year < 100:
            year += 2000
        return datetime.date(year, int(month_s), int(day_s))

    raise ValueError(f"Unrecognized date format: {raw!r}")
