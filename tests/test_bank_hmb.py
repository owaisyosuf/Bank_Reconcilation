"""Tests for app/parsers/bank_hmb.py (Habib Metropolitan Bank PDF statement
adapter).

Fixtures used (tests/fixtures/):
- bank_hmb_sample.pdf: 2-page synthetic, fully fabricated statement
  reproducing HMB's real-ruling-line PDF export structure:
    * Page 1: an account-info box (branch/A-C-type/IBAN/currency/date-range
      boilerplate) that bleeds extra blank "filler" columns into
      pdfplumber's grid detection, producing a 9-column table shape
      (`[date, particulars, debit, None, credit, None, None, balance,
      None]`), a column-header row ("Date"/"Particulars"/"Debit"/
      "Credit"/"Balance"), a stray "Opening Balance" row (only the balance
      cell populated), and 4 transactions -- one with no balance cell
      (intermittent balance) and one with a multi-line wrapped
      description (embedded "\n" merged into a single cell by
      pdfplumber's extract_table()).
    * Page 2: a clean 5-column table (`[date, particulars, debit, credit,
      balance]`) with no repeated header row, 3 more transactions.
- bank_hmb_password_protected.pdf: same content, encrypted with user
  password "1234".
"""

import datetime
import os
from decimal import Decimal

import pytest

from app.parsers.base import PasswordProtectedPdfError
from app.parsers.bank_hmb import HabibMetropolitanParser

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(filename: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, filename), "rb") as f:
        return f.read()


class TestHappyPath:
    def setup_method(self):
        self.parser = HabibMetropolitanParser()
        self.filename = "bank_hmb_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_can_parse(self):
        assert self.parser.can_parse(self.file_bytes, self.filename) is True

    def test_transaction_count(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # 4 transactions on page 1 + 3 on page 2 = 7. The opening-balance
        # stray row and the column-header row are not transactions.
        assert len(txns) == 7

    def test_dates_parsed_correctly(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert txns[0].date == datetime.date(2026, 1, 2)
        assert txns[-1].date == datetime.date(2026, 2, 2)

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
        # page-1 transactions and page-2 transactions carry different
        # page prefixes.
        page1_txns = [t for t in txns if t.date < datetime.date(2026, 1, 27)]
        page2_txns = [t for t in txns if t.date >= datetime.date(2026, 1, 27)]
        assert page1_txns and page2_txns
        for t in page1_txns:
            assert t.source_row // 100_000 == 1
        for t in page2_txns:
            assert t.source_row // 100_000 == 2


class TestCommaAmountsAndDebitCreditSides:
    def setup_method(self):
        self.parser = HabibMetropolitanParser()
        self.filename = "bank_hmb_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_comma_formatted_debit_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        gft = next(t for t in txns if t.description.startswith("GFT Transfer"))
        assert gft.debit == Decimal("250000.00")
        assert gft.credit == Decimal("0")

    def test_comma_formatted_credit_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        remittance = next(t for t in txns if t.description.startswith("Inward Remittance"))
        assert remittance.credit == Decimal("500000.00")
        assert remittance.debit == Decimal("0")

    def test_every_transaction_has_exactly_one_nonzero_side(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert (t.debit == Decimal("0")) != (t.credit == Decimal("0"))

    def test_comma_formatted_balance_parsed_as_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        gft = next(t for t in txns if t.description.startswith("GFT Transfer"))
        assert gft.balance == Decimal("2379421.77")
        assert isinstance(gft.balance, Decimal)

    def test_page2_clean_shape_debit_and_credit_correct(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        outward = next(t for t in txns if t.description.startswith("Outward Chq"))
        assert outward.credit == Decimal("450000.00")
        assert outward.debit == Decimal("0")
        assert outward.balance == Decimal("3717954.84")

        utility = next(t for t in txns if t.description.startswith("Utility Bill"))
        assert utility.debit == Decimal("12345.00")
        assert utility.credit == Decimal("0")


class TestBalanceIntermittency:
    """HMB's export does not print a running balance after every single
    transaction -- some rows have no balance cell at all."""

    def setup_method(self):
        self.parser = HabibMetropolitanParser()
        self.filename = "bank_hmb_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_row_without_balance_yields_none(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        atm = next(t for t in txns if t.description.startswith("ATM Withdrawal"))
        assert atm.balance is None

    def test_row_with_balance_yields_parsed_decimal(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        service_charge = next(t for t in txns if t.description == "Bank Service Charges")
        assert service_charge.balance == Decimal("2868921.77")
        assert isinstance(service_charge.balance, Decimal)

    def test_page2_intermittent_balance_none(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        utility = next(t for t in txns if t.description.startswith("Utility Bill"))
        assert utility.balance is None


class TestMultiLineDescriptionFlattening:
    def setup_method(self):
        self.parser = HabibMetropolitanParser()
        self.filename = "bank_hmb_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_wrapped_description_becomes_single_transaction(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        remittances = [t for t in txns if t.description.startswith("Inward Remittance")]
        assert len(remittances) == 1

    def test_embedded_newline_replaced_with_space(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        txn = next(t for t in txns if t.description.startswith("Inward Remittance"))
        assert "\n" not in txn.description
        assert "FROM: SANA TARIQ" in txn.description
        assert "REF: RM998877" in txn.description
        assert "STAN (445566)" in txn.description
        # No doubled whitespace left behind either.
        assert "  " not in txn.description

    def test_no_stray_newline_in_any_description(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert "\n" not in t.description


class TestJunkRowExclusion:
    def setup_method(self):
        self.parser = HabibMetropolitanParser()
        self.filename = "bank_hmb_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_opening_balance_stray_row_excluded(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        # If the opening-balance stray row (balance=3,000,000.00, no
        # date/particulars/debit/credit) leaked through as a transaction,
        # this balance value would appear somewhere with an empty
        # description, or the count below would be wrong.
        assert all(t.description for t in txns)
        assert not any(t.balance == Decimal("3000000.00") for t in txns)

    def test_header_row_excluded_from_transactions(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        for t in txns:
            assert t.description.lower() != "particulars"
            assert t.debit != t.credit or t.debit == Decimal("0")

    def test_no_header_or_account_info_text_leaks_into_descriptions(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        junk_markers = [
            "Habib Metropolitan Bank Limited",
            "IBAN:",
            "Statement Period",
            "Branch: Test Branch",
        ]
        for t in txns:
            for marker in junk_markers:
                assert marker.lower() not in t.description.lower()

    def test_transaction_count_excludes_garbage_and_header_rows(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        assert len(txns) == 7


class TestFieldMappingAcrossPageShapes:
    """Validates field mapping is correct on both the 9-column (page-1,
    header-detected) shape and the 5-column (page-2+, dynamic
    active-column fallback) shape."""

    def setup_method(self):
        self.parser = HabibMetropolitanParser()
        self.filename = "bank_hmb_sample.pdf"
        self.file_bytes = _load(self.filename)

    def test_9_column_page1_shape_field_mapping(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        txn = next(t for t in txns if t.description.startswith("GFT Transfer"))
        assert txn.date == datetime.date(2026, 1, 2)
        assert txn.debit == Decimal("250000.00")
        assert txn.credit == Decimal("0")
        assert txn.balance == Decimal("2379421.77")

    def test_5_column_page2_shape_field_mapping(self):
        txns = self.parser.parse(self.file_bytes, self.filename)
        txn = next(t for t in txns if t.description.startswith("Inward Fund Transfer"))
        assert txn.date == datetime.date(2026, 2, 2)
        assert txn.debit == Decimal("0")
        assert txn.credit == Decimal("75000.00")
        assert txn.balance == Decimal("3780609.84")


class TestPasswordProtection:
    def setup_method(self):
        self.parser = HabibMetropolitanParser()
        self.filename = "bank_hmb_password_protected.pdf"
        self.file_bytes = _load(self.filename)

    def test_no_password_raises(self):
        with pytest.raises(PasswordProtectedPdfError):
            self.parser.parse(self.file_bytes, self.filename)

    def test_wrong_password_raises(self):
        with pytest.raises(PasswordProtectedPdfError):
            self.parser.parse(self.file_bytes, self.filename, password="0000")

    def test_correct_password_succeeds(self):
        txns = self.parser.parse(self.file_bytes, self.filename, password="1234")
        assert len(txns) == 7
        assert txns[0].date == datetime.date(2026, 1, 2)


class TestCanParse:
    def test_can_parse_true_for_unencrypted_fixture(self):
        parser = HabibMetropolitanParser()
        file_bytes = _load("bank_hmb_sample.pdf")
        assert parser.can_parse(file_bytes, "bank_hmb_sample.pdf") is True

    def test_can_parse_false_for_non_pdf(self):
        parser = HabibMetropolitanParser()
        assert parser.can_parse(b"not a pdf", "statement.csv") is False

    def test_can_parse_false_for_encrypted_without_password(self):
        parser = HabibMetropolitanParser()
        file_bytes = _load("bank_hmb_password_protected.pdf")
        # Best-effort: encrypted PDFs can't be text-extracted without a
        # password at detection time, so can_parse is allowed to say False.
        assert parser.can_parse(file_bytes, "bank_hmb_password_protected.pdf") is False

    def test_can_parse_false_for_unrelated_pdf(self):
        parser = HabibMetropolitanParser()
        # Bank Alfalah's fixture has neither the "mpbl" IBAN marker nor an
        # HMB-style header row.
        file_bytes = _load("bank_alfalah_sample.pdf")
        assert parser.can_parse(file_bytes, "bank_alfalah_sample.pdf") is False
