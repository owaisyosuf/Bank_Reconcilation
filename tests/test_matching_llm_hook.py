"""Tests for the per-pair LLM description-score hook (M6 prep).

Per `skills/reconciliation-matcher/SKILL.md`'s "LLM Hook" section, an
orchestrator may pre-compute an alternative desc_score using an LLM for
shortlisted candidate pairs. This must be keyed per (bank, ledger) PAIR --
not applied as a single flat value across every candidate involving a given
bank transaction -- because a single bank txn is often scored against
MULTIPLE ledger candidates in the duplicate-amounts scenario, and a flat
override would defeat the entire purpose of using descriptions to
disambiguate them.

Covers:
1. Duplicate-amount-same-day scenario (3 candidates) where fuzzy matching
   alone is genuinely ambiguous (all candidates score similarly, mirroring
   an OCR'd/coded bank narration that doesn't textually resemble the ledger
   description), but a per-pair `llm_scores` dict correctly disambiguates
   which bank txn matches which ledger txn.
2. `max(fuzzy, llm)` semantics on a single pair: llm < fuzzy -> fuzzy wins;
   llm > fuzzy -> llm wins.
3. `reconcile(..., llm_scores=None)` (or omitted) is identical to the
   pre-M6 behavior.
"""

import datetime
from decimal import Decimal

from app.matching.engine import reconcile
from app.matching.scoring import MatchConfig, description_score, score_pair
from app.parsers.base import StandardTransaction

BASE_DATE = datetime.date(2026, 1, 15)


def make_txn(
    date: datetime.date,
    description: str,
    amount,
    source_row: int,
    raw: dict | None = None,
) -> StandardTransaction:
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
# 1. Duplicate amounts, same day: per-pair llm_scores disambiguates correctly
# ---------------------------------------------------------------------------


def test_duplicate_amounts_per_pair_llm_scores_disambiguate_correctly():
    amount = "50000.00"
    # Ledger has human-readable narrations.
    ledger = [
        make_txn(BASE_DATE, "Salary Payment - John Doe", amount, 1),
        make_txn(BASE_DATE, "Office Rent - Head Office", amount, 2),
        make_txn(BASE_DATE, "Utility Bill - K-Electric", amount, 3),
    ]
    # Bank narrations are opaque NEFT reference codes -- textually they
    # don't resemble any ledger description, and (crucially) all three bank
    # rows are equally (un)similar to all three ledger rows via rapidfuzz,
    # so plain fuzzy matching is genuinely ambiguous (fuzzy alone would
    # demote this whole group to REVIEW/ambiguous_duplicates).
    bank = [
        make_txn(BASE_DATE, "NEFT REF 00181920", amount, 10),
        make_txn(BASE_DATE, "NEFT REF 00182044", amount, 11),
        make_txn(BASE_DATE, "NEFT REF 00182171", amount, 12),
    ]

    # Sanity: confirm fuzzy is indeed ambiguous without the LLM hook --
    # every bank row's top-2 ledger candidates are within 5 points of each
    # other (in fact identical), which is exactly the SKILL.md ambiguity
    # trigger.
    baseline = reconcile(bank, ledger, MatchConfig())
    assert baseline.exact == []
    assert len(baseline.review) == 3
    assert all(r.reason == "ambiguous_duplicates" for r in baseline.review)

    # The orchestrator has pre-computed (e.g. via an LLM) that these NEFT
    # codes correspond to specific ledger entries, and stashes ONLY the
    # correct pairing at a high score -- keyed per (bank_row, ledger_row).
    llm_scores = {
        (10, 1): 95,  # NEFT ...920  <-> Salary Payment - John Doe
        (11, 2): 95,  # NEFT ...044  <-> Office Rent - Head Office
        (12, 3): 95,  # NEFT ...171  <-> Utility Bill - K-Electric
    }

    result = reconcile(bank, ledger, MatchConfig(), llm_scores=llm_scores)

    assert result.review == []
    assert result.bank_only == []
    assert result.ledger_only == []
    assert len(result.exact) == 3

    matched_pairs = {
        (r.bank_txn.source_row, r.ledger_txn.source_row) for r in result.exact
    }
    assert matched_pairs == {(10, 1), (11, 2), (12, 3)}

    # Every matched record used the boosted (LLM) description score, not
    # the near-zero fuzzy one.
    for r in result.exact:
        assert r.description_score == 95


def test_duplicate_amounts_flat_per_bank_llm_score_would_have_stayed_ambiguous():
    """Contrast case: confirms the OLD flat-per-bank-txn design (a single
    override value applied identically to every candidate for a bank txn)
    could not have disambiguated the scenario above -- it would apply the
    same score to all 3 ledger candidates for a given bank txn, leaving the
    group just as ambiguous as plain fuzzy. This justifies the per-pair
    interface change.
    """
    amount = "50000.00"
    ledger = [
        make_txn(BASE_DATE, "Salary Payment - John Doe", amount, 1),
        make_txn(BASE_DATE, "Office Rent - Head Office", amount, 2),
        make_txn(BASE_DATE, "Utility Bill - K-Electric", amount, 3),
    ]
    # Flat override stashed on the bank txn itself (old M2 mechanism):
    # applies identically to every ledger candidate it's compared against.
    bank = [
        make_txn(BASE_DATE, "NEFT REF 00181920", amount, 10, raw={"llm_desc_score": 95}),
        make_txn(BASE_DATE, "NEFT REF 00182044", amount, 11, raw={"llm_desc_score": 95}),
        make_txn(BASE_DATE, "NEFT REF 00182171", amount, 12, raw={"llm_desc_score": 95}),
    ]

    result = reconcile(bank, ledger, MatchConfig())

    # Every candidate pair for a given bank txn now scores 95 (the flat
    # override applies to all of them equally) -> still ambiguous.
    assert result.exact == []
    assert len(result.review) == 3
    assert all(r.reason == "ambiguous_duplicates" for r in result.review)


# ---------------------------------------------------------------------------
# 2. max(fuzzy, llm) semantics for a single pair
# ---------------------------------------------------------------------------


def test_llm_score_lower_than_fuzzy_fuzzy_wins():
    bank = make_txn(BASE_DATE, "Payment to Alpha Traders", "1000.00", 10)
    ledger = make_txn(BASE_DATE, "Payment to Alpha Traders", "1000.00", 1)

    fuzzy_only = description_score(bank, ledger)
    assert fuzzy_only == 100

    llm_scores = {(10, 1): 50}  # deliberately lower than fuzzy
    combined = description_score(bank, ledger, llm_scores)
    assert combined == 100  # fuzzy wins: max(100, 50) == 100


def test_llm_score_higher_than_fuzzy_llm_wins():
    bank = make_txn(BASE_DATE, "Payment to Alpha Traders", "1000.00", 10)
    ledger = make_txn(BASE_DATE, "Completely Different Text Zulu", "1000.00", 1)

    fuzzy_only = description_score(bank, ledger)
    assert fuzzy_only < 50  # genuinely dissimilar text

    llm_scores = {(10, 1): 90}  # LLM says these are semantically the same
    combined = description_score(bank, ledger, llm_scores)
    assert combined == 90  # llm wins: max(fuzzy, 90) == 90


def test_score_pair_threads_llm_scores_into_desc_score():
    bank = make_txn(BASE_DATE, "Payment to Alpha Traders", "1000.00", 10)
    ledger = make_txn(BASE_DATE, "Completely Different Text Zulu", "1000.00", 1)
    config = MatchConfig()

    without = score_pair(bank, ledger, config, config.date_window_days)
    with_llm = score_pair(
        bank, ledger, config, config.date_window_days, llm_scores={(10, 1): 90}
    )

    assert with_llm.desc_score == 90
    assert with_llm.desc_score > without.desc_score
    # Only the description component differs; amount/date components are
    # untouched by the LLM hook.
    assert with_llm.amount_diff == without.amount_diff
    assert with_llm.date_diff_days == without.date_diff_days


def test_llm_scores_only_applies_to_the_exact_keyed_pair():
    """A llm_scores dict entry for one pair must not leak onto a different
    pair involving the same bank txn but a different ledger txn."""
    bank = make_txn(BASE_DATE, "Payment to Alpha Traders", "1000.00", 10)
    ledger_a = make_txn(BASE_DATE, "Payment to Alpha Traders", "1000.00", 1)
    ledger_b = make_txn(BASE_DATE, "Completely Different Text Zulu", "1000.00", 2)

    llm_scores = {(10, 1): 40}  # only keyed for (bank=10, ledger=1)

    # (10, 1): fuzzy is already 100, llm (40) is lower -> fuzzy wins, still 100.
    assert description_score(bank, ledger_a, llm_scores) == 100
    # (10, 2): no entry for this pair -> plain fuzzy only, unaffected by the
    # (10, 1) entry.
    plain_b = description_score(bank, ledger_b)
    assert description_score(bank, ledger_b, llm_scores) == plain_b


# ---------------------------------------------------------------------------
# 3. reconcile(..., llm_scores=None) / omitted is unchanged from before
# ---------------------------------------------------------------------------


def test_reconcile_with_llm_scores_none_matches_omitted_default():
    ledger = [
        make_txn(BASE_DATE, "Vendor Payment Alpha", "5000.00", 1),
        make_txn(BASE_DATE, "Vendor Payment Beta", "7000.00", 2),
    ]
    bank = [
        make_txn(BASE_DATE, "Vendor Payment Alpha", "5000.00", 1),
        make_txn(BASE_DATE + datetime.timedelta(days=5), "Vendor Payment Beta", "7000.00", 2),
    ]

    result_omitted = reconcile(bank, ledger, MatchConfig())
    result_explicit_none = reconcile(bank, ledger, MatchConfig(), llm_scores=None)

    def summarize(result):
        return [
            (r.tier, r.bank_txn.source_row if r.bank_txn else None,
             r.ledger_txn.source_row if r.ledger_txn else None,
             r.amount_diff, r.date_diff_days, r.description_score, r.reason)
            for r in result.all_records
        ]

    assert summarize(result_omitted) == summarize(result_explicit_none)
    assert len(result_omitted.exact) == 1
    assert len(result_omitted.review) == 1


def test_reconcile_llm_scores_none_reproduces_ambiguous_duplicates_case():
    """Regression: the existing ambiguous-duplicates test (SKILL.md case 5)
    must behave identically whether llm_scores is omitted or explicitly
    None."""
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

    result = reconcile(bank, ledger, MatchConfig(), llm_scores=None)

    assert result.exact == []
    assert result.tolerance == []
    assert result.bank_only == []
    assert result.ledger_only == []
    assert len(result.review) == 2
    assert all(r.reason == "ambiguous_duplicates" for r in result.review)
