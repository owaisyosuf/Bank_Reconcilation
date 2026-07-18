"""Tests for the shared parse_amount / parse_date helpers in app/parsers/base.py."""

import datetime
from decimal import Decimal

import pytest

from app.parsers.base import parse_amount, parse_date


class TestParseAmount:
    def test_comma_thousands(self):
        assert parse_amount("1,234,567.89") == Decimal("1234567.89")

    def test_comma_thousands_no_decimals(self):
        assert parse_amount("50,000") == Decimal("50000")

    def test_parentheses_negative(self):
        assert parse_amount("(5,000.00)") == Decimal("-5000.00")

    def test_dr_suffix(self):
        assert parse_amount("5,000.00 DR") == Decimal("-5000.00")

    def test_cr_suffix(self):
        assert parse_amount("5,000.00 CR") == Decimal("5000.00")

    def test_dr_suffix_no_space(self):
        assert parse_amount("500DR") == Decimal("-500")

    def test_blank_string(self):
        assert parse_amount("") == Decimal("0")

    def test_dash_placeholder(self):
        assert parse_amount("-") == Decimal("0")

    def test_none(self):
        assert parse_amount(None) == Decimal("0")

    def test_nan(self):
        assert parse_amount(float("nan")) == Decimal("0")

    def test_plain_float(self):
        assert parse_amount(1234.5) == Decimal("1234.5")

    def test_plain_int(self):
        assert parse_amount(1234) == Decimal("1234")

    def test_rs_prefix(self):
        assert parse_amount("Rs. 1,000.00") == Decimal("1000.00")

    def test_never_uses_float_precision_loss(self):
        # 0.1 + 0.2 style drift would show up if float() were used anywhere
        # in the conversion path.
        assert parse_amount("1234567.89") == Decimal("1234567.89")


class TestParseDate:
    def test_ddmmyyyy_slash(self):
        assert parse_date("31/01/2026") == datetime.date(2026, 1, 31)

    def test_ddmmyyyy_dash(self):
        assert parse_date("31-01-2026") == datetime.date(2026, 1, 31)

    def test_dd_mmm_yyyy_dash(self):
        assert parse_date("31-Jan-2026") == datetime.date(2026, 1, 31)

    def test_dd_mmm_yyyy_space(self):
        assert parse_date("31 Jan 2026") == datetime.date(2026, 1, 31)

    def test_dd_mm_yyyy_dot(self):
        assert parse_date("31.01.2026") == datetime.date(2026, 1, 31)

    def test_day_first_never_us_style(self):
        # 03/04/2026 must be 3rd April, never March 4th.
        assert parse_date("03/04/2026") == datetime.date(2026, 4, 3)

    def test_excel_serial_int(self):
        # Excel serial 45658 == 2025-01-01 (with the classic 1900 leap bug epoch).
        assert parse_date(45658) == datetime.date(2025, 1, 1)

    def test_excel_serial_float(self):
        assert parse_date(45658.0) == datetime.date(2025, 1, 1)

    def test_excel_serial_as_string(self):
        assert parse_date("45658") == datetime.date(2025, 1, 1)

    def test_native_date_passthrough(self):
        d = datetime.date(2026, 5, 17)
        assert parse_date(d) == d

    def test_native_datetime_passthrough(self):
        dt = datetime.datetime(2026, 5, 17, 10, 30)
        assert parse_date(dt) == datetime.date(2026, 5, 17)

    def test_unparseable_raises(self):
        with pytest.raises(ValueError):
            parse_date("not a date")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_date("")

    def test_none_raises(self):
        with pytest.raises(ValueError):
            parse_date(None)
