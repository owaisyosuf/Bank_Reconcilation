"""Tests for app/parsers/bank_alfalah.py (Bank Alfalah PDF statement adapter).

Fixtures used (tests/fixtures/):
- bank_alfalah_sample.pdf: 2-page synthetic, fully fabricated statement
  replicating the whitespace-aligned (no grid) layout, including a
  multi-line continuation description, "-" placeholders on the
  non-applicable Debit/Credit side, and Opening/Closing Balance lines.
- bank_alfalah_password_protected.pdf: same content, encrypted with user
  password "1234".
"""

import datetime
import os
from decimal import Decimal

import pytest

from app.parsers.base import PasswordProtectedPdfError
from app.parsers.bank_alfalah import BankAlfalahParser

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(filename: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, filename), "rb") as f:
        return f.read()


class TestHappyPath:
    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "bank_alfalah_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_transaction_count(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # 5 transactions on page 1 + 4 on page 2 = 9 (Opening/Closing
        # Balance lines are not transactions).
        assert len(txns) == 9

    def test_dates_parsed_correctly(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert txns[0].date == datetime.date(2026, 6, 1)
        assert txns[-1].date == datetime.date(2026, 6, 30)

    def test_sorted_by_date_then_source_row(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        keys = [(t.date, t.source_row) for t in txns]
        assert keys == sorted(keys)

    def test_credit_transaction_values(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        cash_deposit = next(t for t in txns if t.description == "Cash Deposit")
        assert cash_deposit.debit == Decimal("0")
        assert cash_deposit.credit == Decimal("50000.00")
        assert cash_deposit.balance == Decimal("13550000.00")
        assert cash_deposit.date == datetime.date(2026, 6, 1)

    def test_debit_transaction_values(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        atm = next(t for t in txns if t.description == "ATM Withdrawal")
        assert atm.debit == Decimal("20000.00")
        assert atm.credit == Decimal("0")
        assert atm.balance == Decimal("13780000.00")

    def test_comma_formatted_amounts_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        salary = next(t for t in txns if t.description == "Salary Disbursement")
        assert salary.debit == Decimal("300000.00")
        assert isinstance(salary.debit, Decimal)
        assert isinstance(salary.balance, Decimal)

    def test_amounts_are_decimal_type(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert isinstance(t.debit, Decimal)
            assert isinstance(t.credit, Decimal)
            assert t.balance is None or isinstance(t.balance, Decimal)

    def test_source_row_traceability(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert all(t.source_row > 0 for t in txns)
        # Page-2 transactions must have a strictly larger source_row than
        # page-1 ones (source_row encodes page number).
        page1_max = max(t.source_row for t in txns if t.date <= datetime.date(2026, 6, 20))
        page2_min = min(t.source_row for t in txns if t.date >= datetime.date(2026, 6, 22))
        assert page2_min > page1_max


class TestMultiLineContinuation:
    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "bank_alfalah_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_wrapped_description_becomes_single_transaction(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        fund_transfers = [t for t in txns if t.description.startswith("Fund Transfer")]
        assert len(fund_transfers) == 1

    def test_continuation_lines_concatenated_into_description(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        txn = next(t for t in txns if t.description.startswith("Fund Transfer"))
        assert "FROM: Zenith Apparel Ltd" in txn.description
        assert "TRANS.ID: FT20260603998877" in txn.description
        assert "REMITTING BANK: Meezan Bank Ltd" in txn.description
        assert txn.credit == Decimal("250000.00")

    def test_second_continuation_example_on_page_two(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        txn = next(t for t in txns if t.description.startswith("Online Transfer"))
        assert "REF: OT998877 ABC Traders" in txn.description
        assert txn.credit == Decimal("75000.00")


class TestHeaderFooterStripping:
    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "bank_alfalah_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_no_junk_leaks_into_any_description(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        junk_markers = [
            "MONTHLY",
            "Statement of Account",
            "From Date",
            "To Date",
            "Cheq/Inst#",
            "Please notify your branch",
            "For Information & queries",
            "bankalfalah.com",
            "Opening Balance",
            "Closing Balance",
        ]
        for t in txns:
            for marker in junk_markers:
                assert marker.lower() not in t.description.lower()

    def test_no_spurious_transaction_from_header_or_footer(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        descriptions = [t.description for t in txns]
        assert "Date Description Cheq/Inst# Debit Credit Balance" not in descriptions
        assert not any(d.strip() == "" for d in descriptions)


class TestOpeningClosingBalanceDropped:
    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "bank_alfalah_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_opening_and_closing_balance_not_returned_as_transactions(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert not t.description.strip().lower().startswith("opening balance")
            assert not t.description.strip().lower().startswith("closing balance")

    def test_transaction_count_excludes_balance_lines(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # 9 real transactions; if Opening/Closing Balance leaked through as
        # rows this would be 11.
        assert len(txns) == 9


class TestDashPlaceholderNormalization:
    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "bank_alfalah_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_dash_in_debit_column_becomes_zero(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        cash_deposit = next(t for t in txns if t.description == "Cash Deposit")
        assert cash_deposit.debit == Decimal("0")

    def test_dash_in_credit_column_becomes_zero(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        atm = next(t for t in txns if t.description == "ATM Withdrawal")
        assert atm.credit == Decimal("0")

    def test_every_transaction_has_exactly_one_nonzero_side(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert (t.debit == Decimal("0")) != (t.credit == Decimal("0"))


class TestPasswordProtection:
    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "bank_alfalah_password_protected.pdf"
        self.file_bytes = _load(self.filename)

    def test_no_password_raises(self):
        with pytest.raises(PasswordProtectedPdfError):
            self.parser.parse(self.file_bytes, self.filename)

    def test_wrong_password_raises(self):
        with pytest.raises(PasswordProtectedPdfError):
            self.parser.parse(self.file_bytes, self.filename, password="0000")

    def test_correct_password_succeeds(self):
        txns = self.parser.parse(self.file_bytes, self.filename, password="1234")
        assert len(txns) == 9
        assert txns[0].date == datetime.date(2026, 6, 1)


class TestBlankCellNoPlaceholder:
    """A transaction row where the Debit/Credit column is *entirely* blank
    (no word at all in that column bucket, not even a literal "-"
    placeholder) must still resolve to Decimal("0") without crashing."""

    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "bank_alfalah_blank_cell.pdf"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_row_count(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert len(txns) == 3

    def test_entirely_blank_debit_resolves_to_zero(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        reversal = next(t for t in txns if t.description == "Bank Charges Reversed")
        assert reversal.debit == Decimal("0")
        assert reversal.credit == Decimal("50.00")

    def test_entirely_blank_credit_resolves_to_zero(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        atm = next(t for t in txns if t.description == "ATM Withdrawal")
        assert atm.credit == Decimal("0")
        assert atm.debit == Decimal("20000.00")

    def test_dash_placeholder_still_works_alongside_blank_cells(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        deposit = next(t for t in txns if t.description == "Cash Deposit")
        assert deposit.debit == Decimal("0")
        assert deposit.credit == Decimal("50000.00")


class TestNoHeaderLineFoundOnAnyPage:
    """A user can pick the wrong bank adapter for their file (get_parser()
    lets them choose freely). If the Bank Alfalah adapter is force-fed a
    PDF from a different bank whose column-header line never matches
    HEADER_WORDS on any page, it must fail gracefully -- either an empty
    result or a clear exception -- never an unhandled crash like
    IndexError."""

    def setup_method(self):
        self.parser = BankAlfalahParser()
        self.filename = "wrong_bank_no_header.pdf"
        self.file_bytes = _load(self.filename)

    def test_can_parse_is_false_for_wrong_bank(self):
        # Best-effort bank identification correctly declines this file.
        assert self.parser.can_parse(self.file_bytes, self.filename) is False

    def test_forced_parse_raises_clear_error_not_a_crash(self):
        # Even if a caller bypasses can_parse() and forces this adapter on
        # the wrong file, parse() must not raise IndexError/AttributeError
        # etc. Since no Alfalah-style header line was ever found to anchor
        # column boundaries, it raises a clear, actionable ValueError rather
        # than silently returning an empty list (which would look like "this
        # statement has zero transactions" instead of "wrong bank selected").
        with pytest.raises(ValueError, match="Bank Alfalah"):
            self.parser.parse(self.file_bytes, self.filename)


class TestCanParse:
    def test_can_parse_true_for_unencrypted_fixture(self):
        parser = BankAlfalahParser()
        file_bytes = _load("bank_alfalah_sample.pdf")
        assert parser.can_parse(file_bytes, "bank_alfalah_sample.pdf") is True

    def test_can_parse_false_for_non_pdf(self):
        parser = BankAlfalahParser()
        assert parser.can_parse(b"not a pdf", "statement.csv") is False

    def test_can_parse_false_for_encrypted_without_password(self):
        parser = BankAlfalahParser()
        file_bytes = _load("bank_alfalah_password_protected.pdf")
        # Best-effort: encrypted PDFs can't be text-extracted without a
        # password at detection time, so can_parse is allowed to say False.
        assert parser.can_parse(file_bytes, "bank_alfalah_password_protected.pdf") is False
