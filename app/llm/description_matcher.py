"""Gemini-backed description re-ranking hook (M6).

CLAUDE.md Hard Rule #7: "No LLM in the core matching path. LLM calls (if
enabled) only refine description similarity for candidates already
shortlisted by deterministic amount+date filters." This module is the
orchestrator-level hook the reconciliation-matcher SKILL.md's "LLM Hook"
section refers to:

    The orchestrator may pre-compute an alternative desc_score using an LLM
    for shortlisted pairs and pass it in ... This layer itself never makes
    network calls.

`app.matching` stays pure. This module:
1. Builds a deterministic amount+date shortlist (no LLM involved) using the
   exact same tolerance/window math as the matching engine.
2. Calls the Gemini API once per bank transaction that has >=1 shortlisted
   ledger candidates, asking it to score description similarity only.
3. Returns a `{(bank_source_row, ledger_source_row): score}` dict in the
   shape `app.matching.engine.reconcile(..., llm_scores=...)` expects.

Never used for amount or date decisions. Failures degrade gracefully: a
bank transaction whose Gemini call errors out is simply left out of the
returned dict, so the engine falls back to plain fuzzy matching for it
rather than the whole reconciliation failing.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from app.matching.scoring import MatchConfig, effective_tolerance, net_amount
from app.parsers.base import StandardTransaction

DEFAULT_MODEL = "gemini-2.5-flash"

_PROMPT_TEMPLATE = """You are helping reconcile a Pakistani business's bank \
statement against its internal ledger. Bank narrations and ledger \
descriptions for the SAME real-world transaction often differ due to \
abbreviations, transliteration, word order, or missing punctuation \
(e.g. "ABC TRADERS PVT LTD" and "A.B.C Traders" are the same counterparty).

Bank transaction description:
{bank_description!r}

Candidate ledger descriptions (score EACH one independently):
{candidates_block}

For each candidate, return an integer 0-100: how likely it refers to the \
SAME transaction as the bank description (100 = certainly the same, \
0 = certainly unrelated). Respond with ONLY a JSON array of integers, one \
per candidate, in the same order, e.g. [85, 40, 12]. No other text."""


class LlmUnavailableError(Exception):
    """Raised when the LLM hook is invoked but not configured (no API key)."""


def is_llm_configured() -> bool:
    """Whether a Google API key is available in the environment."""
    return bool(os.environ.get("GOOGLE_API_KEY"))


def _build_shortlist(
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    config: MatchConfig,
) -> dict[int, list[int]]:
    """Deterministic amount+date shortlist only -- no LLM involved here.

    A pair is shortlisted if the amount is within the effective tolerance
    AND the date is within the widest configured window
    (`review_date_window_days`), matching CLAUDE.md's "candidates already
    shortlisted by deterministic amount+date filters" requirement.

    Returns {bank_index: [ledger_index, ...]}, skipping bank transactions
    with no candidates.
    """
    shortlist: dict[int, list[int]] = {}
    for bi, bank in enumerate(bank_txns):
        bank_amount = net_amount(bank)
        tolerance = effective_tolerance(bank_amount, config)
        candidates = []
        for li, ledger in enumerate(ledger_txns):
            amount_diff = abs(bank_amount - net_amount(ledger))
            if amount_diff > tolerance:
                continue
            date_diff = abs((bank.date - ledger.date).days)
            if date_diff > config.review_date_window_days:
                continue
            candidates.append(li)
        if candidates:
            shortlist[bi] = candidates
    return shortlist


def _default_gemini_call(
    api_key: str, model: str, bank_description: str, candidate_descriptions: list[str]
) -> list[int]:
    """Real network call to Gemini. Isolated in its own function so tests
    can substitute a fake without touching the network."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    candidates_block = "\n".join(
        f"{idx}. {desc!r}" for idx, desc in enumerate(candidate_descriptions)
    )
    prompt = _PROMPT_TEMPLATE.format(
        bank_description=bank_description, candidates_block=candidates_block
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[int],
        ),
    )
    return json.loads(response.text)


def _clamp_score(value: object) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def get_llm_description_scores(
    bank_txns: list[StandardTransaction],
    ledger_txns: list[StandardTransaction],
    config: Optional[MatchConfig] = None,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    gemini_call: Optional[Callable[[str, list[str]], list[int]]] = None,
) -> dict[tuple[int, int], int]:
    """Pre-compute per-pair LLM description scores for deterministically
    shortlisted bank/ledger candidates.

    Returns `{(bank_source_row, ledger_source_row): score}`, ready to pass
    as `reconcile(..., llm_scores=...)`. Raises `LlmUnavailableError` if no
    API key is configured. Individual bank transactions whose Gemini call
    fails are silently omitted (graceful degradation to fuzzy-only for
    just that transaction) rather than raising.
    """
    config = config or MatchConfig()
    resolved_key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not resolved_key:
        raise LlmUnavailableError(
            "GOOGLE_API_KEY is not set. AI description matching requires a "
            "Google Gemini API key in the environment."
        )

    call = gemini_call or (
        lambda bank_description, candidate_descriptions: _default_gemini_call(
            resolved_key, model, bank_description, candidate_descriptions
        )
    )

    shortlist = _build_shortlist(bank_txns, ledger_txns, config)
    scores: dict[tuple[int, int], int] = {}

    for bi, ledger_indices in shortlist.items():
        bank = bank_txns[bi]
        candidate_descriptions = [ledger_txns[li].description for li in ledger_indices]
        try:
            raw_scores = call(bank.description, candidate_descriptions)
        except Exception:
            # Graceful degradation: skip this bank txn's LLM scores, the
            # matching engine falls back to plain fuzzy for it.
            continue

        if not isinstance(raw_scores, list) or len(raw_scores) != len(ledger_indices):
            continue

        for li, raw_score in zip(ledger_indices, raw_scores):
            ledger = ledger_txns[li]
            scores[(bank.source_row, ledger.source_row)] = _clamp_score(raw_score)

    return scores
