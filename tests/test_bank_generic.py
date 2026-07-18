"""Tests for app/parsers/bank_generic.py (the "Other/Auto-detect" fallback)."""

import datetime
import os
from decimal import Decimal

import pytest

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


class TestHeaderOnlyEmptyFile:
    """A file with a valid header row and zero data rows should return []
    rather than crash (M7 edge-case audit)."""

    def test_csv_header_only_returns_empty_list(self):
        parser = GenericParser()
        file_bytes = _load("generic_header_only.csv")
        assert parser.can_parse(file_bytes, "generic_header_only.csv") is True
        txns = parser.parse(file_bytes, "generic_header_only.csv")
        assert txns == []

    def test_xlsx_header_only_returns_empty_list(self):
        parser = GenericParser()
        file_bytes = _load("generic_header_only.xlsx")
        assert parser.can_parse(file_bytes, "generic_header_only.xlsx") is True
        txns = parser.parse(file_bytes, "generic_header_only.xlsx")
        assert txns == []

    def test_completely_empty_csv_fails_gracefully(self):
        # No header at all (zero bytes) -- can_parse must say False, and a
        # direct parse() call must raise a clear ValueError, never crash
        # with an unhandled IndexError/etc.
        parser = GenericParser()
        assert parser.can_parse(b"", "empty.csv") is False
        with pytest.raises(ValueError):
            parser.parse(b"", "empty.csv")


class TestUnicodeDescriptions:
    """Urdu-script descriptions must round-trip without mangling."""

    def test_urdu_description_round_trips_in_csv(self):
        parser = GenericParser()
        file_bytes = _load("generic_unicode.csv")
        txns = parser.parse(file_bytes, "generic_unicode.csv")
        descriptions = [t.description for t in txns]
        assert "Payment to کریم ٹریڈرز" in descriptions
        assert "Received from عالم برادران" in descriptions

    def test_urdu_description_amounts_still_correct(self):
        parser = GenericParser()
        file_bytes = _load("generic_unicode.csv")
        txns = parser.parse(file_bytes, "generic_unicode.csv")
        payment = next(t for t in txns if "کریم" in t.description)
        assert payment.debit == Decimal("5000.00")
        receipt = next(t for t in txns if "عالم" in t.description)
        assert receipt.credit == Decimal("20000.00")


class TestExtraUnrelatedColumns:
    """A header row with extra unrelated columns (Branch, Account Type)
    mixed in must not false-match, and the relevant columns must still be
    correctly detected."""

    def setup_method(self):
        self.parser = GenericParser()
        self.filename = "generic_extra_columns.csv"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_row_count_and_values(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert len(txns) == 3
        deposit = next(t for t in txns if t.description == "Cash Deposit")
        assert deposit.credit == Decimal("50000.00")
        assert deposit.debit == Decimal("0")
        assert deposit.balance == Decimal("150000.00")

    def test_branch_and_account_type_not_treated_as_recognized_columns(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        withdrawal = next(t for t in txns if t.description == "Cheque Withdrawal")
        # "Branch"/"Account Type" values must still be present in raw (not
        # dropped), but must not have been mistaken for debit/credit/date.
        assert withdrawal.raw["Branch"] == "Lahore Branch"
        assert withdrawal.raw["Account Type"] == "Savings"
        assert withdrawal.debit == Decimal("25000.00")
        assert withdrawal.date == datetime.date(2026, 1, 3)


class TestSourceRowTraceabilityWithMidFileJunk:
    """source_row numbers must stay correct (no collisions/gaps) when junk
    rows are skipped in the *middle* of a file, not just before the header."""

    def test_source_rows_skip_junk_correctly(self):
        parser = GenericParser()
        file_bytes = _load("generic_mid_junk.csv")
        txns = parser.parse(file_bytes, "generic_mid_junk.csv")
        # File rows (1-based): 1=header, 2/3=real, 4=blank junk, 5=real,
        # 6=unparseable-date junk, 7=real. Junk rows 4 and 6 must be
        # dropped without shifting the source_row of surviving rows.
        source_rows = sorted(t.source_row for t in txns)
        assert source_rows == [2, 3, 5, 7]

    def test_descriptions_match_expected_rows(self):
        parser = GenericParser()
        file_bytes = _load("generic_mid_junk.csv")
        txns = parser.parse(file_bytes, "generic_mid_junk.csv")
        by_row = {t.source_row: t.description for t in txns}
        assert by_row[2] == "Opening Balance"
        assert by_row[3] == "Cash Deposit"
        assert by_row[5] == "Cheque Withdrawal"
        assert by_row[7] == "ATM Withdrawal"
