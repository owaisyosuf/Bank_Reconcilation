"""Confidence scoring for the reconciliation matcher.

Implements exactly what's specified in
`skills/reconciliation-matcher/SKILL.md`:
- `MatchConfig` — tunable tolerances/windows/thresholds.
- `effective_tolerance` — max(abs tolerance, pct tolerance * bank amount).
- `score_pair` — the 0.55/0.25/0.20 weighted composite score used to rank
  candidate bank/ledger pairs within a pass.
- `MatchRecord` — the output contract the UI/export layer depends on.

Pure and deterministic: no I/O, no Streamlit, no network calls. The optional
LLM description re-ranker is a pre-processing hook that lives OUTSIDE this
layer — if an orchestrator has already stashed a score in
`StandardTransaction.raw["llm_desc_score"]`, this module will consider it,
but it never computes or fetches that value itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional

from rapidfuzz import fuzz

from app.parsers.base import StandardTransaction


@dataclass
class MatchConfig:
    """Tunable knobs for the matching pipeline.

    Defaults exactly per CLAUDE.md / SKILL.md.
    """

    amount_tolerance_abs: Decimal = Decimal("2")  # Rs 2
    amount_tolerance_pct: Decimal = Decimal("0.005")  # 0.5%
    date_window_days: int = 3
    review_date_window_days: int = 10
    description_review_threshold: int = 85  # rapidfuzz ratio


@dataclass
class MatchRecord:
    """One row of the reconciliation output. Never remove a field — the UI
    and Excel export depend on every one of them.
    """

    tier: Literal["EXACT", "TOLERANCE", "REVIEW", "UNMATCHED"]
    bank_txn: Optional[StandardTransaction]
    ledger_txn: Optional[StandardTransaction]
    amount_diff: Decimal
    date_diff_days: int
    description_score: int
    reason: str


@dataclass
class PairScore:
    """Intermediate scoring result for a single candidate bank/ledger pair."""

    score: Decimal
    amount_diff: Decimal
    date_diff_days: int
    desc_score: int
    effective_tolerance: Decimal


def net_amount(txn: StandardTransaction) -> Decimal:
    """Signed net amount for a transaction: credit - debit.

    Working on signed amounts (rather than absolute value) is what makes
    direction agreement automatic: a bank debit (negative net amount) can
    never equal/near a ledger credit (positive net amount) of the same
    magnitude, because their signed difference is ~2x the magnitude, not 0.
    """
    return txn.credit - txn.debit


def effective_tolerance(bank_amount: Decimal, config: MatchConfig) -> Decimal:
    """max(abs_tolerance, pct_tolerance * |bank_amount|)."""
    pct_tolerance = config.amount_tolerance_pct * abs(bank_amount)
    return max(config.amount_tolerance_abs, pct_tolerance)


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal("0")
    if value > 1:
        return Decimal("1")
    return value


def description_score(bank_txn: StandardTransaction, ledger_txn: StandardTransaction) -> int:
    """rapidfuzz token_sort_ratio (0-100 int), taking the max with an
    optional pre-computed LLM score stashed in `bank_txn.raw['llm_desc_score']`
    by an orchestrator outside this layer. Never makes network calls.
    """
    fuzzy = fuzz.token_sort_ratio(bank_txn.description or "", ledger_txn.description or "")
    raw = bank_txn.raw or {}
    llm_score = raw.get("llm_desc_score")
    if llm_score is not None:
        return int(round(max(fuzzy, float(llm_score))))
    return int(round(fuzzy))


def score_pair(
    bank_txn: StandardTransaction,
    ledger_txn: StandardTransaction,
    config: MatchConfig,
    window_days: int,
) -> PairScore:
    """Composite candidate-ranking score for a bank/ledger pair.

    score = 0.55 * amount_score + 0.25 * date_score + 0.20 * desc_score
      amount_score = 1 - (abs(amount_diff) / effective_tolerance), clamped 0..1;
                      1.0 when amount_diff == 0 (exact).
      date_score   = 1 - (abs(date_diff_days) / window_days), clamped 0..1.
      desc_score   = rapidfuzz.fuzz.token_sort_ratio(...) / 100 (or the LLM
                      override, see `description_score`).
    """
    bank_amount = net_amount(bank_txn)
    ledger_amount = net_amount(ledger_txn)
    amount_diff = bank_amount - ledger_amount
    tolerance = effective_tolerance(bank_amount, config)

    if amount_diff == 0:
        amount_score = Decimal("1")
    elif tolerance == 0:
        amount_score = Decimal("0")
    else:
        amount_score = _clamp01(Decimal("1") - (abs(amount_diff) / tolerance))

    date_diff_days = (bank_txn.date - ledger_txn.date).days
    if window_days > 0:
        date_score = _clamp01(
            Decimal("1") - (Decimal(abs(date_diff_days)) / Decimal(window_days))
        )
    else:
        date_score = Decimal("1") if date_diff_days == 0 else Decimal("0")

    desc = description_score(bank_txn, ledger_txn)
    desc_score_fraction = Decimal(desc) / Decimal("100")

    score = (
        Decimal("0.55") * amount_score
        + Decimal("0.25") * date_score
        + Decimal("0.20") * desc_score_fraction
    )

    return PairScore(
        score=score,
        amount_diff=amount_diff,
        date_diff_days=date_diff_days,
        desc_score=desc,
        effective_tolerance=tolerance,
    )
