"""Tests for the M6 LLM description-matching hook (app/llm/description_matcher.py).

No real network calls: every test injects a fake `gemini_call` so the
Gemini API is never actually hit.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from app.llm.description_matcher import (
    LlmUnavailableError,
    get_llm_description_scores,
    is_llm_configured,
)
from app.matching.scoring import MatchConfig
from app.parsers.base import StandardTransaction


def _txn(date, description, credit="0", debit="0", source_row=1):
    return StandardTransaction(
        date=date,
        description=description,
        debit=Decimal(debit),
        credit=Decimal(credit),
        balance=None,
        source_row=source_row,
        raw={},
    )


def test_is_llm_configured_reflects_env_var(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert is_llm_configured() is False
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    assert is_llm_configured() is True


def test_raises_when_no_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    bank = [_txn(datetime.date(2026, 1, 1), "ABC TRADERS", credit="1000", source_row=1)]
    ledger = [_txn(datetime.date(2026, 1, 1), "A.B.C Traders", credit="1000", source_row=1)]
    with pytest.raises(LlmUnavailableError):
        get_llm_description_scores(bank, ledger)


def test_shortlist_only_includes_amount_and_date_matches():
    # Bank credit (receipt) pairs with a ledger DEBIT of the same amount --
    # opposite sides, per real bookkeeping (bank passbook vs. ledger asset
    # convention). See app/matching/scoring.py's net_amount(is_ledger=...).
    bank = [
        _txn(datetime.date(2026, 1, 10), "ABC TRADERS", credit="1000", source_row=1),
    ]
    ledger = [
        _txn(datetime.date(2026, 1, 10), "A.B.C Traders", debit="1000", source_row=1),  # in window
        _txn(datetime.date(2026, 1, 10), "XYZ CORP", debit="9999", source_row=2),  # amount too far
        _txn(datetime.date(2026, 3, 1), "A.B.C Traders", debit="1000", source_row=3),  # date too far
    ]

    calls = []

    def fake_call(bank_description, candidate_descriptions):
        calls.append((bank_description, candidate_descriptions))
        return [90] * len(candidate_descriptions)

    scores = get_llm_description_scores(
        bank, ledger, api_key="fake-key", gemini_call=fake_call
    )

    assert scores == {(1, 1): 90}
    assert len(calls) == 1
    assert calls[0][1] == ["A.B.C Traders"]  # only the in-tolerance/in-window candidate


def test_scores_clamped_to_0_100():
    bank = [_txn(datetime.date(2026, 1, 1), "ABC TRADERS", credit="1000", source_row=1)]
    ledger = [_txn(datetime.date(2026, 1, 1), "A.B.C Traders", debit="1000", source_row=1)]

    def fake_call(bank_description, candidate_descriptions):
        return [150]  # out of range, should clamp to 100

    scores = get_llm_description_scores(
        bank, ledger, api_key="fake-key", gemini_call=fake_call
    )
    assert scores == {(1, 1): 100}


def test_gemini_failure_degrades_gracefully_for_that_bank_txn():
    bank = [
        _txn(datetime.date(2026, 1, 1), "GOOD ONE", credit="1000", source_row=1),
        _txn(datetime.date(2026, 1, 1), "BAD ONE", credit="2000", source_row=2),
    ]
    ledger = [
        _txn(datetime.date(2026, 1, 1), "GOOD ONE", debit="1000", source_row=1),
        _txn(datetime.date(2026, 1, 1), "BAD ONE", debit="2000", source_row=2),
    ]

    def flaky_call(bank_description, candidate_descriptions):
        if bank_description == "BAD ONE":
            raise RuntimeError("simulated API failure")
        return [95] * len(candidate_descriptions)

    scores = get_llm_description_scores(
        bank, ledger, api_key="fake-key", gemini_call=flaky_call
    )

    # Only the bank txn whose call succeeded contributes a score; the
    # failing one is simply absent (engine falls back to fuzzy for it).
    assert scores == {(1, 1): 95}


def test_mismatched_response_length_is_ignored():
    bank = [_txn(datetime.date(2026, 1, 1), "A", credit="1000", source_row=1)]
    ledger = [
        _txn(datetime.date(2026, 1, 1), "A1", credit="1000", source_row=1),
        _txn(datetime.date(2026, 1, 1), "A2", credit="1000", source_row=2),
    ]

    def bad_call(bank_description, candidate_descriptions):
        return [50]  # wrong length -- should be silently dropped

    scores = get_llm_description_scores(
        bank, ledger, api_key="fake-key", gemini_call=bad_call
    )
    assert scores == {}


def test_no_shortlisted_candidates_makes_no_calls():
    bank = [_txn(datetime.date(2026, 1, 1), "LONE", credit="5000", source_row=1)]
    ledger = [_txn(datetime.date(2026, 6, 1), "UNRELATED", credit="1", source_row=1)]

    calls = []

    def fake_call(bank_description, candidate_descriptions):
        calls.append(1)
        return []

    scores = get_llm_description_scores(
        bank, ledger, api_key="fake-key", gemini_call=fake_call
    )
    assert scores == {}
    assert calls == []


def test_end_to_end_with_matching_engine_reconcile():
    """The dict this module returns must plug directly into
    reconcile(llm_scores=...) and correctly disambiguate a duplicate-amount
    scenario that plain fuzzy matching alone could not resolve."""
    from app.matching.engine import reconcile

    bank = [
        _txn(datetime.date(2026, 1, 15), "NEFT REF 001", credit="50000", source_row=1),
        _txn(datetime.date(2026, 1, 15), "NEFT REF 002", credit="50000", source_row=2),
    ]
    ledger = [
        _txn(datetime.date(2026, 1, 15), "Payment A", debit="50000", source_row=1),
        _txn(datetime.date(2026, 1, 15), "Payment B", debit="50000", source_row=2),
    ]

    def fake_call(bank_description, candidate_descriptions):
        # Bank txn 1 -> ledger 1 strongly; bank txn 2 -> ledger 2 strongly.
        if bank_description == "NEFT REF 001":
            return [95, 20]
        return [20, 95]

    llm_scores = get_llm_description_scores(
        bank, ledger, api_key="fake-key", gemini_call=fake_call
    )
    result = reconcile(bank, ledger, llm_scores=llm_scores)

    exact_pairs = {(r.bank_txn.source_row, r.ledger_txn.source_row) for r in result.exact}
    assert exact_pairs == {(1, 1), (2, 2)}
