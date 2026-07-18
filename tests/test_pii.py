"""Unit tests for app/ui/pii.mask_pii — the only unit-testable piece of the
UI layer (CLAUDE.md: "UI is not unit-tested", but this helper is pure logic).
"""

from app.ui.pii import mask_pii


def test_cnic_no_dashes_masked_to_last_4():
    assert mask_pii("1234567890123") == "*********0123"


def test_cnic_with_dashes_masked_to_last_4():
    # 42101-1234567-1 -> digits "4210112345671" (13 digits)
    result = mask_pii("42101-1234567-1")
    assert result.endswith("5671")
    assert result.count("*") == 9
    assert "4210112345671" not in result


def test_account_number_in_description_masked():
    text = "Transfer to a/c 01234567890123456 - John Doe"
    result = mask_pii(text)
    assert "01234567890123456" not in result
    assert result.endswith("3456 - John Doe")
    assert "Transfer to a/c" in result
    assert "John Doe" in result


def test_account_number_exactly_8_digits_masked():
    assert mask_pii("12345678") == "****5678"


def test_short_numbers_pass_through_unchanged():
    assert mask_pii("Cheque 4521") == "Cheque 4521"
    assert mask_pii("Invoice 1002") == "Invoice 1002"
    assert mask_pii("1234567") == "1234567"  # 7 digits, below threshold


def test_no_pii_text_unchanged():
    assert mask_pii("Salary payment - August") == "Salary payment - August"
    assert mask_pii("") == ""
    assert mask_pii(None) is None


def test_dates_are_not_masked():
    assert mask_pii("15-07-2026") == "15-07-2026"
    assert mask_pii("2026-07-15") == "2026-07-15"
    assert mask_pii("15/07/2026") == "15/07/2026"
    assert mask_pii("Paid on 15-07-2026 via cheque") == "Paid on 15-07-2026 via cheque"


def test_iban_shaped_string_digit_portion_masked():
    # IBANs are letters (country code + bank code) followed by a long digit
    # run, e.g. Bank Alfalah's "PK39ALFH0240001002662746". The regex only
    # matches contiguous digit characters, so it should mask the trailing
    # digit run to its last 4 digits while leaving the leading letters
    # untouched.
    text = "IBAN PK39ALFH0240001002662746 for transfer"
    result = mask_pii(text)
    assert "0240001002662746" not in result
    assert result.startswith("IBAN PK39ALFH")
    assert result.endswith("2746 for transfer")
    assert "*" in result


def test_multiple_pii_runs_in_one_string_both_masked():
    text = "From 1234567890123456 to 9876543210987654"
    result = mask_pii(text)
    assert "1234567890123456" not in result
    assert "9876543210987654" not in result
    assert result.endswith("7654")
    assert "3456" in result
