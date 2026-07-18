"""Tests for app/matching/engine.py + scoring.py.

Covers all 8 required test cases from
skills/reconciliation-matcher/SKILL.md:

1. Exact match, same date
2. Rs 1.50 difference -> TOLERANCE
3. Exact amount: date +2 days -> EXACT; +5 days -> REVIEW; +15 days -> UNMATCHED
4. Three duplicate amounts same day with distinct descriptions -> all matched correctly
5. Duplicate amounts with near-identical descriptions -> all REVIEW (ambiguous)
6. Bank charge present only in bank statement -> bank_only
7. Signed direction: bank debit must not match ledger credit of same magnitude
8. 1,000-row randomized round-trip: >= 99% recovered (EXACT or TOLERANCE)
"""

import datetime
import random
from decimal import Decimal

from app.matching.engine import reconcile
from app.matching.scoring import MatchConfig, net_amount
from app.parsers.base import StandardTransaction

BASE_DATE = datetime.date(2026, 1, 15)


def make_txn(
    date: datetime.date,
    description: str,
    amount,
    source_row: int,
    raw: dict | None = None,
) -> StandardTransaction:
    """Build a StandardTransaction from a signed amount (positive = credit,
    negative = debit)."""
    amount = Decimal(str(amount))
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
# 1. Exact match, same date
# ---------------------------------------------------------------------------


def test_exact_match_same_date():
    bank = [make_txn(BASE_DATE, "Payment ABC Traders", 15000, 1)]
    ledger = [make_txn(BASE_DATE, "Payment ABC Traders", 15000, 1)]

    result = reconcile(bank, ledger, MatchConfig())

    assert len(result.exact) == 1
    assert result.tolerance == []
    assert result.review == []
    assert result.bank_only == []
    assert result.ledger_only == []

    record = result.exact[0]
    assert record.tier == "EXACT"
    assert record.amount_diff == Decimal("0")
    assert record.date_diff_days == 0
    assert record.reason == "exact"
    assert record.bank_txn is bank[0]
    assert record.ledger_txn is ledger[0]


# ---------------------------------------------------------------------------
# 2. Rs 1.50 difference -> TOLERANCE
# ---------------------------------------------------------------------------


def test_rs_1_50_difference_is_tolerance():
    bank = [make_txn(BASE_DATE, "Utility Bill Payment", "1000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Utility Bill Payment", "998.50", 1)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert len(result.tolerance) == 1
    assert result.review == []

    record = result.tolerance[0]
    assert record.tier == "TOLERANCE"
    assert record.amount_diff == Decimal("1.50")
    assert record.reason == "within_tolerance"


def test_amount_diff_beyond_tolerance_is_unmatched_or_review():
    # Rs 10 difference on a small amount (Rs 100) is well beyond both the
    # Rs 2 absolute and 0.5% relative tolerance -> not TOLERANCE.
    bank = [make_txn(BASE_DATE, "Misc Payment", "100.00", 1)]
    ledger = [make_txn(BASE_DATE, "Misc Payment", "110.00", 1)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.tolerance == []
    assert result.exact == []


# ---------------------------------------------------------------------------
# 3. Exact amount: date +2 days -> EXACT; +5 days -> REVIEW; +15 days -> UNMATCHED
# ---------------------------------------------------------------------------


def test_exact_amount_date_offsets():
    ledger = [
        make_txn(BASE_DATE, "Vendor Payment Alpha", "5000.00", 1),
        make_txn(BASE_DATE, "Vendor Payment Beta", "7000.00", 2),
        make_txn(BASE_DATE, "Vendor Payment Gamma", "9000.00", 3),
    ]
    bank = [
        # +2 days, within date_window_days(3) -> EXACT
        make_txn(BASE_DATE + datetime.timedelta(days=2), "Vendor Payment Alpha", "5000.00", 1),
        # +5 days, beyond date window but within review_date_window_days(10) -> REVIEW
        make_txn(BASE_DATE + datetime.timedelta(days=5), "Vendor Payment Beta", "7000.00", 2),
        # +15 days, beyond review window entirely -> UNMATCHED
        make_txn(BASE_DATE + datetime.timedelta(days=15), "Vendor Payment Gamma", "9000.00", 3),
    ]

    result = reconcile(bank, ledger, MatchConfig())

    assert len(result.exact) == 1
    assert result.exact[0].bank_txn.description == "Vendor Payment Alpha"
    assert result.exact[0].date_diff_days == 2

    assert len(result.review) == 1
    assert result.review[0].bank_txn.description == "Vendor Payment Beta"
    assert result.review[0].date_diff_days == 5
    assert result.review[0].reason == "date_offset_5d"

    assert len(result.bank_only) == 1
    assert result.bank_only[0].bank_txn.description == "Vendor Payment Gamma"
    assert len(result.ledger_only) == 1
    assert result.ledger_only[0].ledger_txn.description == "Vendor Payment Gamma"


# ---------------------------------------------------------------------------
# 4. Three duplicate amounts same day, distinct descriptions -> all matched
# ---------------------------------------------------------------------------


def test_duplicate_amounts_distinct_descriptions_all_match_correctly():
    amount = "50000.00"
    ledger = [
        make_txn(BASE_DATE, "Utility Bill - K-Electric", amount, 1),
        make_txn(BASE_DATE, "Office Rent - Head Office", amount, 2),
        make_txn(BASE_DATE, "Salary Payment - John Doe", amount, 3),
    ]
    # Same descriptions, different row order/numbers, to force the engine to
    # rely on description similarity rather than row proximity.
    bank = [
        make_txn(BASE_DATE, "Salary Payment - John Doe", amount, 10),
        make_txn(BASE_DATE, "Utility Bill - K-Electric", amount, 11),
        make_txn(BASE_DATE, "Office Rent - Head Office", amount, 12),
    ]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.review == []
    assert result.bank_only == []
    assert result.ledger_only == []
    assert len(result.exact) == 3

    matched_pairs = {
        (r.bank_txn.description, r.ledger_txn.description) for r in result.exact
    }
    assert matched_pairs == {
        ("Salary Payment - John Doe", "Salary Payment - John Doe"),
        ("Utility Bill - K-Electric", "Utility Bill - K-Electric"),
        ("Office Rent - Head Office", "Office Rent - Head Office"),
    }


# ---------------------------------------------------------------------------
# 5. Duplicate amounts with near-identical descriptions -> all REVIEW
# ---------------------------------------------------------------------------


def test_duplicate_amounts_ambiguous_descriptions_all_review():
    amount = "3000.00"
    description = "Cheque Deposit"
    ledger = [
        make_txn(BASE_DATE, description, amount, 1),
        make_txn(BASE_DATE, description, amount, 2),
    ]
    bank = [
        make_txn(BASE_DATE, description, amount, 1),
        make_txn(BASE_DATE, description, amount, 2),
    ]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert result.tolerance == []
    assert result.bank_only == []
    assert result.ledger_only == []
    assert len(result.review) == 2
    assert all(r.reason == "ambiguous_duplicates" for r in result.review)
    assert all(r.tier == "REVIEW" for r in result.review)


# ---------------------------------------------------------------------------
# 6. Bank charge present only in bank statement -> bank_only
# ---------------------------------------------------------------------------


def test_bank_only_charge_not_in_ledger():
    bank = [
        make_txn(BASE_DATE, "Payment XYZ Corp", "20000.00", 1),
        make_txn(BASE_DATE, "SMS Alert Charges", "50.00", 2),
    ]
    ledger = [
        make_txn(BASE_DATE, "Payment XYZ Corp", "20000.00", 1),
    ]

    result = reconcile(bank, ledger, MatchConfig())

    assert len(result.exact) == 1
    assert len(result.bank_only) == 1
    assert result.ledger_only == []

    unmatched = result.bank_only[0]
    assert unmatched.tier == "UNMATCHED"
    assert unmatched.bank_txn.description == "SMS Alert Charges"
    assert unmatched.ledger_txn is None
    assert unmatched.reason == "no_match_bank_only"


# ---------------------------------------------------------------------------
# 7. Signed direction: bank debit must not match ledger credit of same magnitude
# ---------------------------------------------------------------------------


def test_signed_direction_debit_does_not_match_opposite_credit():
    # Bank shows this as a debit (money out); ledger (wrongly, or for a
    # different, offsetting transaction) has a credit of the same magnitude,
    # same day, same description. They must NOT be matched.
    bank = [make_txn(BASE_DATE, "Transfer XYZ", "-10000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Transfer XYZ", "10000.00", 1)]

    result = reconcile(bank, ledger, MatchConfig())

    assert result.exact == []
    assert result.tolerance == []
    assert result.review == []
    assert len(result.bank_only) == 1
    assert len(result.ledger_only) == 1
    assert result.bank_only[0].amount_diff == Decimal("-10000.00")
    assert result.ledger_only[0].amount_diff == Decimal("10000.00")


def test_signed_direction_same_direction_still_matches():
    # Sanity check: same-direction debits do match normally.
    bank = [make_txn(BASE_DATE, "Transfer XYZ", "-10000.00", 1)]
    ledger = [make_txn(BASE_DATE, "Transfer XYZ", "-10000.00", 1)]

    result = reconcile(bank, ledger, MatchConfig())

    assert len(result.exact) == 1


# ---------------------------------------------------------------------------
# 8. 1,000-row randomized round-trip
# ---------------------------------------------------------------------------


def test_randomized_round_trip_1000_rows_recovers_99_percent():
    rng = random.Random(42)
    descriptions = [
        "Invoice Payment",
        "Salary Transfer",
        "Utility Bill",
        "Vendor Payment",
        "Rent",
        "Office Supplies",
        "Consulting Fee",
        "Loan Repayment",
        "Insurance Premium",
        "Tax Payment",
    ]

    n = 1000
    ledger = []
    for i in range(n):
        cents = rng.randint(10_000, 99_999_999)  # Rs 100.00 .. Rs 999,999.99
        amount = Decimal(cents) / Decimal("100")
        d = BASE_DATE + datetime.timedelta(days=rng.randint(0, 300))
        desc = f"{rng.choice(descriptions)} #{i}"
        ledger.append(make_txn(d, desc, amount, i + 1))

    bank = []
    perturbed_count = 0
    for i, l in enumerate(ledger):
        amount = net_amount(l)
        d = l.date
        desc = l.description
        if rng.random() < 0.2:
            perturbed_count += 1
            delta_cents = rng.randint(-190, 190)  # up to +/- Rs 1.90
            amount = amount + (Decimal(delta_cents) / Decimal("100"))
            d = d + datetime.timedelta(days=rng.randint(-2, 2))
        bank.append(make_txn(d, desc, amount, i + 1))

    result = reconcile(bank, ledger, MatchConfig())

    recovered = len(result.exact) + len(result.tolerance)
    recovery_rate = recovered / n

    assert perturbed_count > 100  # sanity: the perturbation actually happened
    assert recovery_rate >= 0.99, (
        f"only recovered {recovered}/{n} ({recovery_rate:.4f}); "
        f"review={len(result.review)} bank_only={len(result.bank_only)} "
        f"ledger_only={len(result.ledger_only)}"
    )
