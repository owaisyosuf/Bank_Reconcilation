"""M7 edge-case audit regression tests for app/matching/engine.py + scoring.py.

These supplement the 8 required SKILL.md test cases in
tests/test_matching_engine.py with edge cases that weren't previously
exercised: empty inputs, single-transaction inputs, zero-amount
transactions, negative-vs-negative net amounts, very large amounts,
a larger (5+) group of truly identical duplicates, date_window_days=0,
and review_date_window_days == date_window_days.

Bank-vs-ledger sign convention: a bank STATEMENT is a passbook (credit =
money in, debit = money out). A company's own LEDGER records the bank
account as an ASSET, so the SAME real transaction is entered on the
OPPOSITE side: a receipt is a bank credit but a ledger DEBIT; a payment is
a bank debit but a ledger CREDIT. See
skills/reconciliation-matcher/SKILL.md. `make_txn(..., is_ledger=True)`
builds a transaction using the ledger-side convention so an
intentionally-matching bank/ledger pair can be built from the same signed
"amount" value.
"""

import datetime
from decimal import Decimal

from app.matching.engine import reconcile
from app.matching.scoring import MatchConfig, effective_tolerance, net_amount
from app.parsers.base import StandardTransaction

BASE_DATE = datetime.date(2026, 1, 15)


def make_txn(
    date: datetime.date,
    description: str,
    amount,
    source_row: int,
    raw: dict | None = None,
    is_ledger: bool = False,
) -> StandardTransaction:
    """Build a StandardTransaction from a signed amount (positive = a
    receipt, negative = a payment).

    - `is_ledger=False` (default, bank side): positive -> credit, negative ->
      debit.
    - `is_ledger=True` (ledger side): positive -> debit, negative -> credit
      (the ledger's asset convention -- opposite of the bank side).
    """
    amount = Decimal(str(amount))
    if is_ledger:
        if amount >= 0:
            debit, credit = amount, Decimal("0")
        else:
            debit, credit = Decimal("0"), -amount
    else:
        if amount >= 0:
            credit, debit = amount, Decimal("0")
        else:
            credit, debit = Decimal("0"), -amount
    return StandardTransaction(
        date=date,
        description=description,
        debit=debit,
        credit=credit,
        balance=None,
        source_row=source_row,
        raw=raw or {},
    )


# ---------------------------------------------------------------------------
# 1. Empty inputs
# ---------------------------------------------------------------------------


def test_reconcile_both_empty_returns_empty_result_no_crash():
    result = reconcile([], [], MatchConfig())

    assert result.matched_count == 0
    assert result.unmatched_count == 0
    assert result.exact == []
    assert result.tolerance == []
    assert result.review == []
    assert result.bank_only == []
    assert result.ledger_only == []
    assert result.all_records == []


def test_reconcile_empty_ledger_all_bank_only():
    bank = [
        make_txn(BASE_DATE, "Payment A", "1000.00", 1),
        make_txn(BASE_DATE, "Payment B", "2000.00", 2),
        make_txn(BASE_DATE, "Payment C", "3000.00", 3),
    ]

    result = reconcile(bank, [], MatchConfig())

    assert result.matched_count == 0
    assert len(result.bank_only) == 3
    assert result.ledger_only == []
    assert {r.bank_txn.source_row for r in result.bank_only} == {1, 2, 3}
    assert all(r.tier == "UNMATCHED" for r in result.bank_only)
    assert all(r.reason == "no_match_bank_only" for r in result.bank_only)
    assert all(r.ledger_txn is None for r in result.bank_only)


def test_reconcile_empty_bank_all_ledger_only():
    ledger = [
        make_txn(BASE_DATE, "Payment A", "1000.00", 1),
        make_txn(BASE_DATE, "Payment B", "2000.00", 2),
    ]

    result = reconcile([], ledger, MatchConfig())

    assert result.matched_count == 0
    assert len(result.ledger_only) == 2
    assert result.bank_only == []
    assert {r.ledger_txn.source_row for r in result.ledger_only} == {1, 2}
    assert all(r.tier == "UNMATCHED" for r in result.ledger_only)
    assert all(r.reason == "no_match_ledger_only" for r in result.ledger_only)
    assert all(r.bank_txn is None for r in result.ledger_only)


# ---------------------------------------------------------------------------
# 2. Single-transaction inputs
# ---------------------------------------------------------------------------


def test_single_bank_single_ledger_exact_match_no_crash():
    bank = [make_txn(BASE_DATE, "Only Payment", "500.00", 1)]
    ledger = [make_txn(BASE_DATE, "Only Payment", "500.00", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert len(result.exact) == 1
    assert result.bank_only == []
    assert result.ledger_only == []
    assert result.exact[0].reason == "exact"


# ---------------------------------------------------------------------------
# 3. Zero-amount transactions
# ---------------------------------------------------------------------------


def test_zero_amount_transactions_match_each_other():
    bank = [make_txn(BASE_DATE, "Zero Value Adjustment", "0", 1)]
    ledger = [make_txn(BASE_DATE, "Zero Value Adjustment", "0", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert len(result.exact) == 1
    assert result.exact[0].amount_diff == Decimal("0")
    assert result.bank_only == []
    assert result.ledger_only == []


def test_effective_tolerance_zero_amount_uses_abs_tolerance_only():
    # pct_tolerance = 0.5% * 0 = 0, so effective tolerance must fall back to
    # the Rs 2 absolute tolerance -- not silently become 0 (which would make
    # a Rs 0 vs Rs 1 pair impossible to ever reach TOLERANCE).
    config = MatchConfig()
    tol = effective_tolerance(Decimal("0"), config)
    assert tol == config.amount_tolerance_abs
    assert tol == Decimal("2")


def test_zero_amount_bank_txn_within_rs2_of_zero_ledger_is_tolerance():
    bank = [make_txn(BASE_DATE, "Rounding artifact", "1.50", 1)]
    ledger = [make_txn(BASE_DATE, "Rounding artifact", "0", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert len(result.tolerance) == 1
    assert result.tolerance[0].amount_diff == Decimal("1.50")


# ---------------------------------------------------------------------------
# 4. Negative net amounts on both sides (reversal/refund recorded as debit)
# ---------------------------------------------------------------------------


def test_negative_net_amount_both_sides_exact_match():
    # Bank side records a Rs 5,000 payment as a debit (money out). The
    # correctly-entered ledger counterpart is a CREDIT of the same
    # magnitude (asset decrease) -- opposite sides, same real transaction.
    bank = [make_txn(BASE_DATE, "Reversal - Order 991", "-5000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Reversal - Order 991", "-5000.00", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert len(result.exact) == 1
    assert net_amount(bank[0]) == Decimal("-5000.00")
    assert net_amount(ledger[0], is_ledger=True) == Decimal("-5000.00")
    assert result.exact[0].amount_diff == Decimal("0")


def test_negative_net_amount_both_sides_tolerance_match():
    bank = [make_txn(BASE_DATE, "Reversal - Order 992", "-5000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Reversal - Order 992", "-4999.00", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert len(result.tolerance) == 1
    # bank(-5000) - ledger(-4999) = -1.00
    assert result.tolerance[0].amount_diff == Decimal("-1.00")


def test_negative_net_amount_same_side_debit_debit_not_falsely_matched():
    # Same-side collision: bank records a Rs 3,000 debit (payment out), and
    # the ledger ALSO happens to record a Rs 3,000 debit (e.g. a
    # mis-entered row, or a genuinely different transaction). Under correct
    # double-entry bookkeeping these are NOT the same transaction (a real
    # matching ledger counterpart would be a CREDIT, not a debit) and must
    # NOT be matched, even with identical amount/date/description.
    bank = [make_txn(BASE_DATE, "Reversal - Order 993", "-3000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Reversal - Order 993", "-3000.00", 1)]  # same-side, is_ledger=False

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert result.tolerance == []
    assert result.review == []
    assert len(result.bank_only) == 1
    assert len(result.ledger_only) == 1


def test_negative_net_amount_opposite_side_debit_credit_matches():
    # Opposite-side pairing of the same magnitude: bank debit of Rs 3,000
    # (payment out) correctly pairs with a ledger CREDIT of Rs 3,000 (asset
    # decrease) for the same real transaction -- this MUST match, unlike
    # the same-side collision above.
    bank = [make_txn(BASE_DATE, "Reversal - Order 994", "-3000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Reversal - Order 994", "-3000.00", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact != []
    assert len(result.exact) == 1
    assert result.exact[0].amount_diff == Decimal("0")


# ---------------------------------------------------------------------------
# 5. Very large amounts
# ---------------------------------------------------------------------------


def test_large_amount_effective_tolerance_picks_pct_over_abs():
    config = MatchConfig()
    tol = effective_tolerance(Decimal("50000000.00"), config)
    # 0.5% of 50,000,000 = 250,000, which dwarfs the Rs 2 absolute tolerance.
    assert tol == Decimal("250000.00000")
    assert tol > config.amount_tolerance_abs


def test_large_amount_rs1_diff_is_tolerance_not_exact():
    bank = [make_txn(BASE_DATE, "Large Settlement", "50000000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Large Settlement", "50000001.00", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert len(result.tolerance) == 1
    assert result.tolerance[0].amount_diff == Decimal("-1.00")


def test_large_amount_diff_beyond_pct_tolerance_is_not_tolerance():
    # Rs 500,000 diff on Rs 50,000,000 (1%) exceeds the 0.5% pct tolerance
    # (Rs 250,000) and the Rs 2 absolute tolerance -- should not match.
    bank = [make_txn(BASE_DATE, "Large Settlement 2", "50000000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Large Settlement 2", "49500000.00", 1, is_ledger=True)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert result.tolerance == []


# ---------------------------------------------------------------------------
# 6. All-bank-txns-identical duplicates (5+, same amount/date/description)
# ---------------------------------------------------------------------------


def test_five_true_duplicates_all_become_review_one_to_one():
    amount = "1000.00"
    description = "Cash Deposit"
    bank = [make_txn(BASE_DATE, description, amount, i + 1) for i in range(5)]
    ledger = [make_txn(BASE_DATE, description, amount, i + 1, is_ledger=True) for i in range(5)]

    result = reconcile(bank, ledger, MatchConfig())

    # No crash, no arbitrary confident pick -- every one of the 5 rows is
    # genuinely ambiguous (identical desc_score across all candidates) and
    # must be demoted to REVIEW, not silently paired as EXACT.
    assert result.exact == []
    assert result.tolerance == []
    assert len(result.review) == 5
    assert result.bank_only == []
    assert result.ledger_only == []
    assert all(r.reason == "ambiguous_duplicates" for r in result.review)
    assert all(r.tier == "REVIEW" for r in result.review)

    # Still strictly one-to-one: every bank and every ledger row used exactly
    # once across the 5 REVIEW records.
    bank_rows = [r.bank_txn.source_row for r in result.review]
    ledger_rows = [r.ledger_txn.source_row for r in result.review]
    assert sorted(bank_rows) == [1, 2, 3, 4, 5]
    assert sorted(ledger_rows) == [1, 2, 3, 4, 5]


def test_six_true_duplicates_with_one_extra_bank_row_leaves_one_bank_only():
    # 6 bank rows vs 5 ledger rows, all identical amount/date/description:
    # one bank row can never be paired (no ledger counterpart) and must fall
    # through to bank_only, without crashing the ambiguity/greedy-assign
    # logic on the uneven group sizes.
    amount = "2500.00"
    description = "Cash Deposit"
    bank = [make_txn(BASE_DATE, description, amount, i + 1) for i in range(6)]
    ledger = [make_txn(BASE_DATE, description, amount, i + 1, is_ledger=True) for i in range(5)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert result.tolerance == []
    assert len(result.review) == 5
    assert all(r.reason == "ambiguous_duplicates" for r in result.review)
    assert len(result.bank_only) == 1
    assert result.ledger_only == []


# ---------------------------------------------------------------------------
# 7. date_window_days = 0 (same-day only)
# ---------------------------------------------------------------------------


def test_date_window_zero_same_day_matches_exact_no_division_error():
    config = MatchConfig(date_window_days=0)
    bank = [make_txn(BASE_DATE, "Same Day Payment", "1000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Same Day Payment", "1000.00", 1, is_ledger=True)]

    result = reconcile(bank, ledger, config)

    assert len(result.exact) == 1
    assert result.exact[0].date_diff_days == 0


def test_date_window_zero_next_day_is_not_exact_but_review():
    config = MatchConfig(date_window_days=0)
    bank = [make_txn(BASE_DATE, "Same Day Payment", "1000.00", 1)]
    ledger = [
        make_txn(
            BASE_DATE + datetime.timedelta(days=1),
            "Same Day Payment",
            "1000.00",
            1,
            is_ledger=True,
        )
    ]

    result = reconcile(bank, ledger, config)

    # date_window_days=0 means +1 day is already outside the normal window,
    # but still within the (default) review window -> REVIEW, not a crash
    # and not a false EXACT.
    assert result.exact == []
    assert len(result.review) == 1
    assert result.review[0].reason == "date_offset_1d"


# ---------------------------------------------------------------------------
# 8. review_date_window_days == date_window_days (no separate review window)
# ---------------------------------------------------------------------------


def test_review_window_equals_date_window_no_condition_a_matches():
    # With both windows equal to 5, Pass 3 condition (a)
    # (date_window_days < abs(date_diff) <= review_date_window_days) is
    # 5 < abs(diff) <= 5, an empty range -- so amount-exact-but-later
    # transactions must go straight from EXACT (within 5 days) to UNMATCHED
    # (beyond 5 days), with no REVIEW step in between.
    config = MatchConfig(date_window_days=5, review_date_window_days=5)

    ledger = [
        make_txn(BASE_DATE, "Within window", "1000.00", 1, is_ledger=True),
        make_txn(BASE_DATE, "Beyond window", "2000.00", 2, is_ledger=True),
    ]
    bank = [
        # Exactly at the boundary (5 days) -> still EXACT.
        make_txn(BASE_DATE + datetime.timedelta(days=5), "Within window", "1000.00", 1),
        # One day past the boundary (6 days) -> would have been REVIEW if
        # review_date_window_days were wider, but here must be UNMATCHED.
        make_txn(BASE_DATE + datetime.timedelta(days=6), "Beyond window", "2000.00", 2),
    ]

    result = reconcile(bank, ledger, config)

    assert len(result.exact) == 1
    assert result.exact[0].bank_txn.description == "Within window"
    assert result.exact[0].date_diff_days == 5

    assert result.review == []
    assert len(result.bank_only) == 1
    assert result.bank_only[0].bank_txn.description == "Beyond window"
    assert len(result.ledger_only) == 1
    assert result.ledger_only[0].ledger_txn.description == "Beyond window"
