"""Tests for app/parsers/ledger_parser.py (user-mapped local ledger)."""

import datetime
import os
from decimal import Decimal

import pytest

from app.parsers.ledger_parser import LedgerParser

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(filename: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, filename), "rb") as f:
        return f.read()


COLUMN_MAPPING = {
    "date": "Txn Date",
    "debit": "Withdrawal",
    "credit": "Deposit",
    "description": "Narration",
}


class TestHappyPath:
    def setup_method(self):
        self.parser = LedgerParser()
        self.filename = "ledger_sample.csv"
        self.file_bytes = _load(self.filename)

    def test_row_count(self):
        txns = self.parser.parse(self.file_bytes, self.filename, COLUMN_MAPPING)
        # All 3 data rows kept, including the opening-balance row that has
        # zero debit/credit but a valid date.
        assert len(txns) == 3

    def test_opening_balance_row_kept(self):
        txns = self.parser.parse(self.file_bytes, self.filename, COLUMN_MAPPING)
        opening = next(t for t in txns if t.description == "Opening balance c/f")
        assert opening.debit == Decimal("0")
        assert opening.credit == Decimal("0")
        assert opening.date == datetime.date(2026, 3, 1)

    def test_comma_amount_debit(self):
        txns = self.parser.parse(self.file_bytes, self.filename, COLUMN_MAPPING)
        payment = next(t for t in txns if "Supplier ABC" in t.description)
        assert payment.debit == Decimal("10000.00")
        assert payment.credit == Decimal("0")

    def test_comma_amount_credit(self):
        txns = self.parser.parse(self.file_bytes, self.filename, COLUMN_MAPPING)
        receipt = next(t for t in txns if "Customer XYZ" in t.description)
        assert receipt.credit == Decimal("22500.75")
        assert receipt.debit == Decimal("0")

    def test_source_row_traceability(self):
        txns = self.parser.parse(self.file_bytes, self.filename, COLUMN_MAPPING)
        assert txns[0].source_row == 2
        assert txns[-1].source_row == 4

    def test_sorted_by_date(self):
        txns = self.parser.parse(self.file_bytes, self.filename, COLUMN_MAPPING)
        dates = [t.date for t in txns]
        assert dates == sorted(dates)


class TestColumnMappingValidation:
    def test_missing_date_key_raises(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_sample.csv")
        with pytest.raises(ValueError):
            parser.parse(file_bytes, "ledger_sample.csv", {"debit": "Withdrawal"})

    def test_missing_debit_and_credit_raises(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_sample.csv")
        with pytest.raises(ValueError):
            parser.parse(file_bytes, "ledger_sample.csv", {"date": "Txn Date"})

    def test_unknown_mapped_column_raises(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_sample.csv")
        bad_mapping = dict(COLUMN_MAPPING)
        bad_mapping["debit"] = "Does Not Exist"
        with pytest.raises(ValueError):
            parser.parse(file_bytes, "ledger_sample.csv", bad_mapping)


class TestHeaderOnlyEmptyFile:
    """A ledger file with a valid header row and zero data rows should
    return [] rather than crash (M7 edge-case audit)."""

    def test_csv_header_only_returns_empty_list(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_header_only.csv")
        txns = parser.parse(file_bytes, "ledger_header_only.csv", COLUMN_MAPPING)
        assert txns == []

    def test_fully_empty_file_returns_empty_list(self):
        # Zero bytes at all (not even a header row) -- must return [] since
        # _read_rows() returns [] and LedgerParser explicitly short-circuits
        # on "not rows", rather than crashing trying to read rows[0].
        parser = LedgerParser()
        txns = parser.parse(b"", "empty.csv", COLUMN_MAPPING)
        assert txns == []


class TestUnicodeDescriptions:
    """Urdu-script narration text must round-trip without mangling."""

    def test_urdu_description_round_trips_in_csv(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_unicode.csv")
        txns = parser.parse(file_bytes, "ledger_unicode.csv", COLUMN_MAPPING)
        descriptions = [t.description for t in txns]
        assert "Payment to کریم ٹریڈرز" in descriptions
        assert "Received from عالم برادران" in descriptions

    def test_urdu_description_amounts_still_correct(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_unicode.csv")
        txns = parser.parse(file_bytes, "ledger_unicode.csv", COLUMN_MAPPING)
        payment = next(t for t in txns if "کریم" in t.description)
        assert payment.debit == Decimal("10000.00")
        receipt = next(t for t in txns if "عالم" in t.description)
        assert receipt.credit == Decimal("22500.75")


class TestSourceRowTraceabilityWithMidFileJunk:
    """source_row numbers must stay correct (no collisions/gaps) when junk
    rows are skipped in the middle of a ledger file."""

    def test_source_rows_skip_junk_correctly(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_mid_junk.csv")
        txns = parser.parse(file_bytes, "ledger_mid_junk.csv", COLUMN_MAPPING)
        # File rows (1-based): 1=header, 2/3=real, 4=blank junk, 5=real,
        # 6=unparseable-date junk, 7=real.
        source_rows = sorted(t.source_row for t in txns)
        assert source_rows == [2, 3, 5, 7]

    def test_descriptions_match_expected_rows(self):
        parser = LedgerParser()
        file_bytes = _load("ledger_mid_junk.csv")
        txns = parser.parse(file_bytes, "ledger_mid_junk.csv", COLUMN_MAPPING)
        by_row = {t.source_row: t.description for t in txns}
        assert by_row[2] == "Opening balance"
        assert by_row[3] == "Payment to Supplier ABC"
        assert by_row[5] == "Received from Customer XYZ"
        assert by_row[7] == "Another Payment"
