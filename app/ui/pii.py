"""PII masking helper for the UI layer (CLAUDE.md Hard Rule #5, REQUIREMENTS F5.1).

`mask_pii()` is the single, shared function every table/metric/export in
`app/ui/` (and, later, `app/export/`) must run description/detail text
through before it is ever rendered or written out. Full account numbers and
CNICs must never reach the screen or a file — only the last 4 digits.

This module is pure (no Streamlit import, no I/O) so it can be unit tested
directly (see tests/test_pii.py) even though the rest of `app/ui/` is not
unit-tested per CLAUDE.md.

Detection strategy
-------------------
We don't try to distinguish "this is definitely a CNIC" from "this is
definitely a bank account number" — Pakistani account numbers vary widely in
length (8-20 digits) and CNICs are a fixed 13 digits, often written with
dashes (`XXXXX-XXXXXXX-X`). Instead we find any run of digits (optionally
broken up by dashes/spaces, e.g. CNIC dashes or spaced IBAN-style numbers)
that contains >= 8 total digits, and mask every digit except the last 4.

To avoid mangling ordinary dates (`15-07-2026`, `2026-07-15`) — which are
also digit runs joined by dashes — we skip masking when the matched run is
exactly a plausible `D[D]-M[M]-YYYY` / `YYYY-M[M]-D[D]` style date (8 total
digits, 3 groups, first/last group a valid day-or-year and middle a valid
month). Slash- or dot- separated dates (`15/07/2026`, `15.07.2026`) are
naturally unaffected since `/` and `.` are not part of the matched
character class.
"""

from __future__ import annotations

import re

# A run of digits, optionally interspersed with single dashes/spaces, that
# both starts and ends on a digit (so we don't eat trailing/leading
# punctuation that isn't part of the number itself).
_DIGIT_RUN_RE = re.compile(r"\d[\d\-\s]{6,}\d")

# Plausible "day-month-year" or "year-month-day" style date made of exactly
# 3 dash/space separated groups totaling 8 digits, e.g. "15-07-2026",
# "2026-07-15", "15 07 2026".
_DATE_LIKE_RE = re.compile(r"^(\d{1,4})[\-\s](\d{1,2})[\-\s](\d{1,4})$")

_MIN_PII_DIGITS = 8


def _looks_like_date(match_str: str) -> bool:
    m = _DATE_LIKE_RE.match(match_str)
    if not m:
        return False
    a, b, c = m.groups()
    if len(a) + len(b) + len(c) != 8:
        return False
    # Whichever group is 4 digits should look like a plausible year, and the
    # remaining two groups should look like a valid day (1-31) and month
    # (1-12) in either order.
    groups = [a, b, c]
    four_digit = [g for g in groups if len(g) == 4]
    if len(four_digit) != 1:
        return False
    year = int(four_digit[0])
    if not (1900 <= year <= 2100):
        return False
    others = [int(g) for g in groups if len(g) != 4]
    if len(others) != 2:
        return False
    day_or_month_1, day_or_month_2 = others
    valid_as_day_month = (1 <= day_or_month_1 <= 31) and (1 <= day_or_month_2 <= 12)
    valid_as_month_day = (1 <= day_or_month_1 <= 12) and (1 <= day_or_month_2 <= 31)
    return valid_as_day_month or valid_as_month_day


def _mask_match(match: re.Match) -> str:
    matched = match.group(0)
    if _looks_like_date(matched):
        return matched

    digits = re.sub(r"\D", "", matched)
    if len(digits) < _MIN_PII_DIGITS:
        return matched

    visible = digits[-4:]
    return "*" * (len(digits) - 4) + visible


def mask_pii(text: str | None) -> str:
    """Mask account-number-like and CNIC-like digit runs to their last 4 digits.

    - CNIC (13 digits, e.g. "42101-1234567-1" or "4210112345671") ->
      "*********5671"
    - Account numbers (8-20 digit runs) -> last 4 digits kept, rest starred.
    - Short numbers (< 8 digits — cheque numbers, invoice numbers, amounts,
      phone extensions, etc.) and ordinary dates pass through unchanged.
    - `None` / empty input passes through unchanged.

    Safe to call on any free-text field (description, narration, remarks,
    raw-row values) before it is displayed or exported.
    """
    if text is None:
        return text
    if not isinstance(text, str):
        text = str(text)
    if text == "":
        return text
    return _DIGIT_RUN_RE.sub(_mask_match, text)
