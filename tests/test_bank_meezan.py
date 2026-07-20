"""Tests for app/parsers/bank_meezan.py (Meezan Bank PDF statement adapter).

Fixtures used (tests/fixtures/):
- bank_meezan_sample.pdf: 1-page synthetic, fully fabricated statement
  reproducing Meezan's online-banking "Account Statement (Online)" export
  structure: a pre-header garbage row (page metadata + opening-balance
  line merged into one cell), the real "Date(DD/MM)" header row, 6
  transaction blocks (one deliberately with NO balance row, matching the
  real-world quirk that Meezan doesn't print a running balance after every
  transaction), multi-row/multi-line particulars, and a
  "<= C L O S I N G - B A L A N C E =>" row at the end.
- bank_meezan_password_protected.pdf: same content, encrypted with user
  password "1234".
"""

import datetime
import os
from decimal import Decimal

import pytest

from app.parsers.base import PasswordProtectedPdfError
from app.parsers.bank_meezan import MeezanBankParser

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(filename: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, filename), "rb") as f:
        return f.read()


class TestHappyPath:
    def setup_method(self):
        self.parser = MeezanBankParser()
        self.filename = "bank_meezan_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_transaction_count(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # 6 transaction blocks; the garbage pre-header row and the
        # closing-balance row are not transactions.
        assert len(txns) == 6

    def test_dates_parsed_correctly(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert txns[0].date == datetime.date(2025, 7, 2)
        assert txns[-1].date == datetime.date(2025, 7, 12)

    def test_sorted_by_date_then_source_row(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        keys = [(t.date, t.source_row) for t in txns]
        assert keys == sorted(keys)

    def test_amounts_are_decimal_type(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert isinstance(t.debit, Decimal)
            assert isinstance(t.credit, Decimal)
            assert t.balance is None or isinstance(t.balance, Decimal)

    def test_source_row_traceability(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert all(t.source_row > 0 for t in txns)
        # source_row encodes page number (page_index * 100_000 + row) --
        # all rows on this single-page fixture share the same page prefix.
        for t in txns:
            assert t.source_row // 100_000 == 1


class TestCommaAmountsAndDrCrNormalization:
    def setup_method(self):
        self.parser = MeezanBankParser()
        self.filename = "bank_meezan_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_comma_formatted_credit_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        remittance = next(t for t in txns if t.description.startswith("Inward Remittance"))
        assert remittance.credit == Decimal("100000.00")
        assert remittance.debit == Decimal("0")
        assert isinstance(remittance.credit, Decimal)

    def test_comma_formatted_debit_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        atm = next(t for t in txns if t.description == "ATM Withdrawal")
        assert atm.debit == Decimal("10000.00")
        assert atm.credit == Decimal("0")

    def test_every_transaction_has_exactly_one_nonzero_side(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert (t.debit == Decimal("0")) != (t.credit == Decimal("0"))

    def test_comma_formatted_balance_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        deposit = next(t for t in txns if t.description.startswith("Cheque Deposit"))
        assert deposit.balance == Decimal("2070840.00")


class TestBalanceIntermittency:
    """Meezan's export does not print a running balance after every single
    transaction -- some blocks have no balance row at all."""

    def setup_method(self):
        self.parser = MeezanBankParser()
        self.filename = "bank_meezan_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_block_without_balance_row_yields_none(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        atm = next(t for t in txns if t.description == "ATM Withdrawal")
        assert atm.balance is None

    def test_block_with_balance_row_yields_parsed_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        transfer = next(t for t in txns if t.description.startswith("Internet Funds Transfer"))
        assert transfer.balance == Decimal("2055840.00")
        assert isinstance(transfer.balance, Decimal)


class TestMultiRowDescriptionConcatenation:
    def setup_method(self):
        self.parser = MeezanBankParser()
        self.filename = "bank_meezan_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_wrapped_particulars_become_single_transaction(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        transfers = [t for t in txns if t.description.startswith("Internet Funds Transfer")]
        assert len(transfers) == 1

    def test_continuation_lines_concatenated_into_description(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        txn = next(t for t in txns if t.description.startswith("Internet Funds Transfer"))
        assert "Money Received from ALI RAZA KHAN A/C 0111-" in txn.description
        assert "TO:GLOBAL TRADERS CORP" in txn.description
        assert "STAN (112233)" in txn.description

    def test_remittance_multiline_concatenation(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        txn = next(t for t in txns if t.description.startswith("Inward Remittance"))
        assert "FROM: SANA TARIQ" in txn.description
        assert "REF: RM998877" in txn.description


class TestJunkRowExclusion:
    def setup_method(self):
        self.parser = MeezanBankParser()
        self.filename = "bank_meezan_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_no_junk_leaks_into_any_description(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        junk_markers = [
            "Meezan Bank Limited Account Statement",
            "IBAN:",
            "OPENING-BALANCE",
            "Date(DD/MM)",
        ]
        for t in txns:
            for marker in junk_markers:
                assert marker.lower() not in t.description.lower()

    def test_closing_balance_row_excluded_from_transactions(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert "closing" not in t.description.lower()
            assert "balance" not in t.description.lower()

    def test_closing_balance_not_appended_to_last_transaction_description(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        last = txns[-1]
        assert last.description == "Bank Service Charges"

    def test_transaction_count_excludes_garbage_and_closing_rows(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # If the pre-header garbage row or the closing-balance row leaked
        # through as a transaction this would be != 6.
        assert len(txns) == 6


class TestPasswordProtection:
    def setup_method(self):
        self.parser = MeezanBankParser()
        self.filename = "bank_meezan_password_protected.pdf"
        self.file_bytes = _load(self.filename)

    def test_no_password_raises(self):
        with pytest.raises(PasswordProtectedPdfError):
            self.parser.parse(self.file_bytes, self.filename)

    def test_wrong_password_raises(self):
        with pytest.raises(PasswordProtectedPdfError):
            self.parser.parse(self.file_bytes, self.filename, password="0000")

    def test_correct_password_succeeds(self):
        txns = self.parser.parse(self.file_bytes, self.filename, password="1234")
        assert len(txns) == 6
        assert txns[0].date == datetime.date(2025, 7, 2)


class TestCanParse:
    def test_can_parse_true_for_unencrypted_fixture(self):
        parser = MeezanBankParser()
        file_bytes = _load("bank_meezan_sample.pdf")
        assert parser.can_parse(file_bytes, "bank_meezan_sample.pdf") is True

    def test_can_parse_false_for_non_pdf(self):
        parser = MeezanBankParser()
        assert parser.can_parse(b"not a pdf", "statement.csv") is False

    def test_can_parse_false_for_encrypted_without_password(self):
        parser = MeezanBankParser()
        file_bytes = _load("bank_meezan_password_protected.pdf")
        # Best-effort: encrypted PDFs can't be text-extracted without a
        # password at detection time, so can_parse is allowed to say False.
        assert parser.can_parse(file_bytes, "bank_meezan_password_protected.pdf") is False
