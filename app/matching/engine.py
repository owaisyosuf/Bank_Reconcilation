"""The reconciliation matching pipeline.

Implements the fixed 3-pass pipeline from
`skills/reconciliation-matcher/SKILL.md`:

    Pass 1 - EXACT       (amount equal to the paisa, date within window)
    Pass 2 - TOLERANCE   (amount within effective tolerance, date within window)
    Pass 3 - REVIEW      (exact amount but date outside window up to the wider
                          review window, OR tolerance amount + strong
                          description match)
    Remainder - UNMATCHED (split into bank_only / ledger_only)

Once a transaction is matched in a pass it is removed from all later passes
(one-to-one matching). Assignment within a pass is greedy by descending
composite score, tie-broken by smallest date difference then smallest
source-row distance.

Pure and deterministic: no I/O, no Streamlit imports, no LLM/network calls.
All money comparisons use Decimal.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.matching.scoring import (
    MatchConfig,
    MatchRecord,
    PairScore,
    effective_tolerance,
    net_amount,
    score_pair,
)
from app.parsers.base import StandardTransaction

# Candidates carried between the candidate-building and assignment steps:
# (bank_index, ledger_index, PairScore, reason)
_Candidate = tuple[int, int, PairScore, str]


@dataclass
class ReconciliationResult:
    """All matching output, grouped by tier.

    `bank_only` / `ledger_only` hold UNMATCHED records where the opposite
    side is None (a bank txn with no ledger counterpart, and vice versa).
    """

    exact: list[MatchRecord] = field(default_factory=list)
    tolerance: list[MatchRecord] = field(default_factory=list)
    review: list[MatchRecord] = field(default_factory=list)
    bank_only: list[MatchRecord] = field(default_factory=list)
    ledger_only: list[MatchRecord] = field(default_factory=list)

    @property
    def all_records(self) -> list[MatchRecord]:
        return (
            self.exact
            + self.tolerance
            + self.review
            + self.bank_only
            + self.ledger_only
        )

    @property
    def matched_count(self) -> int:
        return len(self.exact) + len(self.tolerance) + len(self.review)

    @property
    def unmatched_count(self) -> int:
        return len(self.bank_only) + len(self.ledger_only)


def _row_distance(bank_txn: StandardTransaction, ledger_txn: StandardTransaction) -> int:
    return abs(bank_txn.source_row - ledger_txn.source_row)


def _greedy_assign(
    candidates: list[_Candidate],
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    free_bank: set[int],
    free_ledger: set[int],
) -> list[_Candidate]:
    """Sort candidates by score desc (tie-break: date_diff asc, row-distance
    asc), then greedily assign, skipping any side that's already taken.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (
            -c[2].score,
            abs(c[2].date_diff_days),
            _row_distance(bank_txns[c[0]], ledger_txns[c[1]]),
        ),
    )
    assigned: list[_Candidate] = []
    for bi, li, ps, reason in ordered:
        if bi not in free_bank or li not in free_ledger:
            continue
        free_bank.discard(bi)
        free_ledger.discard(li)
        assigned.append((bi, li, ps, reason))
    return assigned


def _to_records(tier: str, assigned: list[_Candidate], bank_txns, ledger_txns) -> list[MatchRecord]:
    return [
        MatchRecord(
            tier=tier,
            bank_txn=bank_txns[bi],
            ledger_txn=ledger_txns[li],
            amount_diff=ps.amount_diff,
            date_diff_days=ps.date_diff_days,
            description_score=ps.desc_score,
            reason=reason,
        )
        for bi, li, ps, reason in assigned
    ]


# ---------------------------------------------------------------------------
# Pass 1 — EXACT (with duplicate-amount ambiguity handling)
# ---------------------------------------------------------------------------


def _build_exact_candidates(
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    config: MatchConfig,
    free_bank: set[int],
    free_ledger: set[int],
) -> list[tuple[int, int, PairScore]]:
    candidates = []
    for bi in free_bank:
        b = bank_txns[bi]
        b_amt = net_amount(b)
        for li in free_ledger:
            l = ledger_txns[li]
            l_amt = net_amount(l)
            if b_amt != l_amt:
                continue
            date_diff = (b.date - l.date).days
            if abs(date_diff) > config.date_window_days:
                continue
            ps = score_pair(b, l, config, config.date_window_days)
            candidates.append((bi, li, ps))
    return candidates


def _detect_ambiguous_duplicate_pairs(
    candidates: list[tuple[int, int, PairScore]],
    bank_txns: list[StandardTransaction],
) -> set[tuple[int, int]]:
    """The duplicate-amounts problem: same amount, same day, multiple rows.

    Group same-day candidates by (amount, date). Within a group, if any
    bank txn's top-2 ledger candidates (by description score) are within 5
    points of each other -- or vice versa for any ledger txn's top-2 bank
    candidates -- the whole group is ambiguous: every pair in it gets
    demoted to REVIEW with reason "ambiguous_duplicates" rather than risk a
    wrong confident match.
    """
    groups: dict[tuple, dict] = defaultdict(lambda: {"bank": set(), "ledger": set(), "pairs": {}})
    for bi, li, ps in candidates:
        if ps.date_diff_days != 0:
            continue  # not a same-day duplicate candidate
        key = (net_amount(bank_txns[bi]), bank_txns[bi].date)
        grp = groups[key]
        grp["bank"].add(bi)
        grp["ledger"].add(li)
        grp["pairs"][(bi, li)] = ps

    ambiguous_pairs: set[tuple[int, int]] = set()

    for grp in groups.values():
        bank_ids, ledger_ids, pairs = grp["bank"], grp["ledger"], grp["pairs"]
        if len(bank_ids) < 2 and len(ledger_ids) < 2:
            continue  # no actual duplication in this group

        is_ambiguous = False
        for bi in bank_ids:
            scores = sorted(
                (pairs[(bi, li)].desc_score for li in ledger_ids if (bi, li) in pairs),
                reverse=True,
            )
            if len(scores) >= 2 and (scores[0] - scores[1]) < 5:
                is_ambiguous = True
                break
        if not is_ambiguous:
            for li in ledger_ids:
                scores = sorted(
                    (pairs[(bi, li)].desc_score for bi in bank_ids if (bi, li) in pairs),
                    reverse=True,
                )
                if len(scores) >= 2 and (scores[0] - scores[1]) < 5:
                    is_ambiguous = True
                    break

        if is_ambiguous:
            ambiguous_pairs.update(pairs.keys())

    return ambiguous_pairs


def _run_pass1_exact(
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    config: MatchConfig,
    free_bank: set[int],
    free_ledger: set[int],
) -> tuple[list[MatchRecord], list[MatchRecord]]:
    candidates = _build_exact_candidates(bank_txns, ledger_txns, config, free_bank, free_ledger)
    ambiguous_pairs = _detect_ambiguous_duplicate_pairs(candidates, bank_txns)

    normal = [(bi, li, ps, "exact") for bi, li, ps in candidates if (bi, li) not in ambiguous_pairs]
    ambiguous = [(bi, li, ps, "ambiguous_duplicates") for bi, li, ps in candidates if (bi, li) in ambiguous_pairs]

    # Resolve unambiguous, high-confidence pairs first; only fall back to a
    # best-effort (still one-to-one) REVIEW pairing for the ambiguous group
    # with whatever remains free.
    exact_assigned = _greedy_assign(normal, bank_txns, ledger_txns, free_bank, free_ledger)
    review_assigned = _greedy_assign(ambiguous, bank_txns, ledger_txns, free_bank, free_ledger)

    exact_records = _to_records("EXACT", exact_assigned, bank_txns, ledger_txns)
    review_records = _to_records("REVIEW", review_assigned, bank_txns, ledger_txns)
    return exact_records, review_records


# ---------------------------------------------------------------------------
# Pass 2 — TOLERANCE
# ---------------------------------------------------------------------------


def _run_pass2_tolerance(
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    config: MatchConfig,
    free_bank: set[int],
    free_ledger: set[int],
) -> list[MatchRecord]:
    candidates: list[_Candidate] = []
    for bi in free_bank:
        b = bank_txns[bi]
        b_amt = net_amount(b)
        tol = effective_tolerance(b_amt, config)
        for li in free_ledger:
            l = ledger_txns[li]
            l_amt = net_amount(l)
            amount_diff = b_amt - l_amt
            if amount_diff == 0:
                continue
            if abs(amount_diff) > tol:
                continue
            date_diff = (b.date - l.date).days
            if abs(date_diff) > config.date_window_days:
                continue
            ps = score_pair(b, l, config, config.date_window_days)
            candidates.append((bi, li, ps, "within_tolerance"))

    assigned = _greedy_assign(candidates, bank_txns, ledger_txns, free_bank, free_ledger)
    return _to_records("TOLERANCE", assigned, bank_txns, ledger_txns)


# ---------------------------------------------------------------------------
# Pass 3 — REVIEW
# ---------------------------------------------------------------------------


def _run_pass3_review(
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    config: MatchConfig,
    free_bank: set[int],
    free_ledger: set[int],
) -> list[MatchRecord]:
    candidates: list[_Candidate] = []
    for bi in free_bank:
        b = bank_txns[bi]
        b_amt = net_amount(b)
        tol = effective_tolerance(b_amt, config)
        for li in free_ledger:
            l = ledger_txns[li]
            l_amt = net_amount(l)
            amount_diff = b_amt - l_amt
            date_diff = (b.date - l.date).days

            if amount_diff == 0:
                # (a) amount exact, date outside the normal window but
                # within the wider review window.
                if not (config.date_window_days < abs(date_diff) <= config.review_date_window_days):
                    continue
                ps = score_pair(b, l, config, config.review_date_window_days)
                reason = f"date_offset_{abs(date_diff)}d"
                candidates.append((bi, li, ps, reason))
            else:
                # (b) amount within tolerance AND a strong description match,
                # within the normal date window.
                if abs(amount_diff) > tol:
                    continue
                if abs(date_diff) > config.date_window_days:
                    continue
                ps = score_pair(b, l, config, config.date_window_days)
                if ps.desc_score < config.description_review_threshold:
                    continue
                candidates.append((bi, li, ps, "tolerance_strong_desc"))

    assigned = _greedy_assign(candidates, bank_txns, ledger_txns, free_bank, free_ledger)
    return _to_records("REVIEW", assigned, bank_txns, ledger_txns)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reconcile(
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    config: MatchConfig | None = None,
) -> ReconciliationResult:
    """Run the full 3-pass reconciliation pipeline and return the result.

    Pure function: no I/O, no side effects beyond the returned result.
    """
    config = config or MatchConfig()
    result = ReconciliationResult()

    free_bank: set[int] = set(range(len(bank_txns)))
    free_ledger: set[int] = set(range(len(ledger_txns)))

    exact_records, ambiguous_review_records = _run_pass1_exact(
        bank_txns, ledger_txns, config, free_bank, free_ledger
    )
    result.exact.extend(exact_records)

    tolerance_records = _run_pass2_tolerance(bank_txns, ledger_txns, config, free_bank, free_ledger)
    result.tolerance.extend(tolerance_records)

    review_records = _run_pass3_review(bank_txns, ledger_txns, config, free_bank, free_ledger)
    # Ambiguous-duplicate REVIEW records from pass 1 are reported alongside
    # pass-3 REVIEW records (same tier, different reason).
    result.review.extend(ambiguous_review_records)
    result.review.extend(review_records)

    for bi in sorted(free_bank):
        b = bank_txns[bi]
        result.bank_only.append(
            MatchRecord(
                tier="UNMATCHED",
                bank_txn=b,
                ledger_txn=None,
                amount_diff=net_amount(b),
                date_diff_days=0,
                description_score=0,
                reason="no_match_bank_only",
            )
        )

    for li in sorted(free_ledger):
        l = ledger_txns[li]
        result.ledger_only.append(
            MatchRecord(
                tier="UNMATCHED",
                bank_txn=None,
                ledger_txn=l,
                amount_diff=net_amount(l),
                date_diff_days=0,
                description_score=0,
                reason="no_match_ledger_only",
            )
        )

    return result
