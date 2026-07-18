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
layer. An orchestrator may pass a per-pair `llm_scores` dict (keyed by
`(bank_source_row, ledger_source_row)`) into `score_pair`/`description_score`;
this module will take `max(fuzzy_score, llm_score)` for pairs present in
that dict. For backward compatibility it also still honors a flat
`StandardTransaction.raw["llm_desc_score"]` fallback when no per-pair entry
is found. Either way, this module never computes or fetches that value
itself.
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


def net_amount(txn: StandardTransaction, is_ledger: bool = False) -> Decimal:
    """Signed net amount for a transaction, honoring which side of the
    double-entry convention it comes from.

    A bank STATEMENT is written from the customer's cash perspective (a
    passbook): credit = money in, debit = money out. A company's own
    LEDGER records the bank account as an ASSET, so the same real-world
    transaction is recorded on the OPPOSITE side: a receipt (bank credit)
    is a ledger DEBIT (asset increase); a payment (bank debit) is a
    ledger CREDIT (asset decrease). See
    `skills/reconciliation-matcher/SKILL.md` for the worked examples.

    - `is_ledger=False` (default, bank side): `credit - debit`.
    - `is_ledger=True` (ledger side): `debit - credit` -- the sign is
      flipped relative to the bank-side formula so that a bank credit and
      the correctly-entered ledger debit for the same transaction both
      net out to the same positive signed amount (and therefore compare
      equal), while a same-side bank-debit/ledger-debit collision (a
      different, coincidental transaction) does not.

    Working on signed amounts (rather than absolute value) is what makes
    direction agreement automatic: given the correct per-side convention
    above, a bank debit can never equal/near a ledger DEBIT of the same
    magnitude (that would be a same-side false positive), because their
    signed difference is ~2x the magnitude, not 0.
    """
    if is_ledger:
        return txn.debit - txn.credit
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


def description_score(
    bank_txn: StandardTransaction,
    ledger_txn: StandardTransaction,
    llm_scores: dict[tuple[int, int], int] | None = None,
) -> int:
    """rapidfuzz token_sort_ratio (0-100 int), taking the max with an
    optional pre-computed LLM score for this specific (bank, ledger) pair.

    Per-pair override (preferred): `llm_scores` is a dict keyed by
    `(bank_txn.source_row, ledger_txn.source_row)`, pre-computed by an
    orchestrator outside this layer (e.g. for shortlisted candidates in a
    duplicate-amounts scenario, where a single bank txn is scored against
    several ledger txns and a flat per-bank-txn override would defeat the
    purpose of using descriptions to disambiguate). If present for this
    exact pair, use `max(fuzzy_score, llm_score)`.

    Flat fallback (backward compatible with M2): if no per-pair entry is
    found, fall back to `bank_txn.raw['llm_desc_score']` if present -- a
    single score applied to any pair involving that bank transaction.

    Never makes network calls; only ever consumes pre-computed scores.
    """
    fuzzy = fuzz.token_sort_ratio(bank_txn.description or "", ledger_txn.description or "")

    llm_score = None
    if llm_scores is not None:
        llm_score = llm_scores.get((bank_txn.source_row, ledger_txn.source_row))
    if llm_score is None:
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
    llm_scores: dict[tuple[int, int], int] | None = None,
) -> PairScore:
    """Composite candidate-ranking score for a bank/ledger pair.

    score = 0.55 * amount_score + 0.25 * date_score + 0.20 * desc_score
      amount_score = 1 - (abs(amount_diff) / effective_tolerance), clamped 0..1;
                      1.0 when amount_diff == 0 (exact).
      date_score   = 1 - (abs(date_diff_days) / window_days), clamped 0..1.
      desc_score   = rapidfuzz.fuzz.token_sort_ratio(...) / 100 (or the LLM
                      override, see `description_score`).

    `llm_scores`, if given, is a dict keyed by
    `(bank_txn.source_row, ledger_txn.source_row)` holding a pre-computed
    LLM description score for that specific pair; see `description_score`.
    """
    bank_amount = net_amount(bank_txn)
    ledger_amount = net_amount(ledger_txn, is_ledger=True)
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

    desc = description_score(bank_txn, ledger_txn, llm_scores)
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
