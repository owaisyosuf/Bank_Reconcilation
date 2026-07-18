"""Tests for app/parsers/bank_generic.py (the "Other/Auto-detect" fallback)."""

import datetime
import os
from decimal import Decimal

from app.parsers.bank_generic import GenericParser

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(filename: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, filename), "rb") as f:
        return f.read()


class TestHappyPath:
    """Clean headers, comma-formatted amounts."""

    def setup_method(self):
        self.parser = GenericParser()
        self.filename = "generic_clean_headers.csv"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_row_count(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert len(txns) == 4

    def test_comma_amounts_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        cash_deposit = next(t for t in txns if t.description == "Cash Deposit")
        assert cash_deposit.credit == Decimal("50000.00")
        assert cash_deposit.debit == Decimal("0")
        assert cash_deposit.balance == Decimal("150000.00")

    def test_debit_row(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        atm = next(t for t in txns if t.description == "ATM Withdrawal")
        assert atm.debit == Decimal("1234.56")
        assert atm.credit == Decimal("0")

    def test_dates_parsed_day_first(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert txns[0].date == datetime.date(2026, 1, 1)
        assert txns[-1].date == datetime.date(2026, 1, 4)

    def test_sorted_by_date_then_source_row(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        dates = [t.date for t in txns]
        assert dates == sorted(dates)

    def test_source_row_traceability(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # Row 1 = header, so first data row is file row 2.
        assert txns[0].source_row == 2
        assert txns[-1].source_row == 5

    def test_raw_preserved(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert txns[0].raw["Description"] == "Opening Balance"


class TestJunkRowsBeforeHeader:
    """XLSX with metadata rows above the real header, blank rows, and an
    unparseable-date-and-zero-amount row that must be dropped."""

    def setup_method(self):
        self.parser = GenericParser()
        self.filename = "generic_junk_rows.xlsx"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_skips_junk_header_rows_and_drops_bad_rows(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # Only the 2 genuine transaction rows should survive; the blank
        # row and the no-date/zero-amount continuation row must be dropped.
        assert len(txns) == 2

    def test_values_extracted_correctly(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        salary = next(t for t in txns if t.description == "Salary Credit")
        assert salary.date == datetime.date(2026, 1, 5)
        assert salary.credit == Decimal("75000.00")
        assert salary.debit == Decimal("0")
        assert salary.balance == Decimal("200000.00")

        utility = next(t for t in txns if t.description == "Utility Bill Payment")
        assert utility.date == datetime.date(2026, 1, 6)
        assert utility.debit == Decimal("4500.50")
        assert utility.credit == Decimal("0")

    def test_source_row_points_past_junk_header(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # Header row is file row 6 (1-based), so first data row is row 7.
        salary = next(t for t in txns if t.description == "Salary Credit")
        assert salary.source_row == 7


class TestSingleAmountDrCrNormalization:
    def setup_method(self):
        self.parser = GenericParser()
        self.filename = "generic_single_amount_drcr.csv"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_row_count(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert len(txns) == 3

    def test_dr_normalized_to_debit(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        pos_purchase = next(t for t in txns if t.description == "POS Purchase")
        assert pos_purchase.debit == Decimal("1500.00")
        assert pos_purchase.credit == Decimal("0")

    def test_cr_normalized_to_credit(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        transfer_in = next(t for t in txns if t.description == "Fund Transfer In")
        assert transfer_in.credit == Decimal("20000.00")
        assert transfer_in.debit == Decimal("0")

    def test_second_dr_row(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        charges = next(t for t in txns if t.description == "Service Charges")
        assert charges.debit == Decimal("150.00")
        assert charges.credit == Decimal("0")
