---
name: reconciliation-matcher
description: The matching algorithm for reconciling bank transactions against ledger entries — pass order, tolerance rules, date windows, confidence scoring, and tie-breaking. Use this skill whenever writing or modifying matching logic, adjusting tolerances, debugging wrong/missing matches, handling duplicate amounts, or explaining why two transactions did or didn't match.
---

# Reconciliation Matcher Skill

## Config

```python
@dataclass
class MatchConfig:
    amount_tolerance_abs: Decimal = Decimal("2")      # Rs 2
    amount_tolerance_pct: Decimal = Decimal("0.005")  # 0.5%
    date_window_days: int = 3
    review_date_window_days: int = 10
    description_review_threshold: int = 85            # rapidfuzz ratio
```

Effective amount tolerance for a pair = `max(abs_tolerance, pct_tolerance * bank_amount)`.

## The Pipeline (order is fixed)

Work on **signed net amount** per transaction: `credit - debit`. Bank debits match ledger payments, etc. — direction must agree.

### Pass 1 — EXACT
- `bank.amount == ledger.amount` (to the paisa)
- `abs(date_diff) <= date_window_days`
- If multiple candidates: highest rapidfuzz description ratio wins; tie → smallest date_diff → smallest row distance.

### Pass 2 — TOLERANCE
On remaining transactions:
- `abs(amount_diff) <= effective_tolerance` and `amount_diff != 0`
- `abs(date_diff) <= date_window_days`
- Same tie-breaking.

### Pass 3 — REVIEW
On remaining transactions, either condition:
- (a) amount exact but `date_window_days < abs(date_diff) <= review_date_window_days`
- (b) amount within tolerance AND description ratio ≥ `description_review_threshold`

### Remainder — UNMATCHED
Split into `bank_only` and `ledger_only` lists.

## Scoring Formula (for candidate ranking within a pass)

```
score = 0.55 * amount_score + 0.25 * date_score + 0.20 * desc_score

amount_score = 1 - (abs(amount_diff) / effective_tolerance)   # clamp 0..1; 1.0 if exact
date_score   = 1 - (abs(date_diff_days) / window)             # clamp 0..1
desc_score   = rapidfuzz.fuzz.token_sort_ratio(a, b) / 100
```

Assignment: sort all candidate pairs by score descending, greedily assign, skip pairs where either side is already taken (this approximates optimal assignment and is fast enough; do NOT bring in scipy Hungarian unless tests show it's needed).

## The Duplicate-Amounts Problem

Same amount, same day, multiple rows (e.g., three payments of Rs 50,000 on the 15th):
- Description similarity decides.
- If top two candidates' desc_scores are within 5 points of each other → do NOT pick. Demote all involved to REVIEW with reason `"ambiguous_duplicates"`. A wrong confident match is worse than a flagged one.

## MatchRecord (output — the UI/export contract)

```python
@dataclass
class MatchRecord:
    tier: Literal["EXACT", "TOLERANCE", "REVIEW", "UNMATCHED"]
    bank_txn: StandardTransaction | None
    ledger_txn: StandardTransaction | None
    amount_diff: Decimal
    date_diff_days: int
    description_score: int
    reason: str          # e.g. "exact", "within_rs2", "date_offset_5d", "ambiguous_duplicates"
```

Never remove fields from this contract — UI and Excel export depend on every one.

## Required Test Cases (pytest)

1. Exact match, same date
2. Rs 1.50 difference → TOLERANCE
3. Exact amount, date +2 days → EXACT; +5 days → REVIEW; +15 days → UNMATCHED
4. Three duplicate amounts same day with distinct descriptions → all matched correctly
5. Duplicate amounts with near-identical descriptions → all REVIEW (ambiguous)
6. Bank charge present only in bank statement → bank_only
7. Signed direction: bank debit must not match ledger credit of same magnitude
8. 1,000-row randomized round-trip: generate ledger, perturb into fake bank statement (±Rs 2, ±2 days on 20% of rows), assert ≥ 99% recovered

## LLM Hook (context only — implemented outside this layer)

The orchestrator may pre-compute an alternative `desc_score` using an LLM for shortlisted pairs and pass it in via `StandardTransaction.raw["llm_desc_score"]`. If present, use `max(fuzz_score, llm_score)`. This layer itself never makes network calls.
