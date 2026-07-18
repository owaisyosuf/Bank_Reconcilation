# CLAUDE.md — Bank Reconciliation App (Pakistan)

## Project Overview

A browser-based bank reconciliation app for Pakistani SMEs. It compares a **bank statement** (PDF/Excel/CSV from Pakistani banks) against a **local ledger** (company's own Excel records) and produces a matched/unmatched report with confidence scores.

Built by Owais (Nexora agency, Karachi). This will also serve as a portfolio product, so code quality and UX polish matter.

## Tech Stack

- **Language**: Python 3.11+
- **UI**: Streamlit (MVP). Do NOT introduce Next.js/FastAPI unless explicitly asked.
- **Data**: pandas for all tabular operations
- **PDF extraction**: pdfplumber (primary). Do not add OCR (Tesseract) unless explicitly asked.
- **Fuzzy matching**: rapidfuzz for description similarity
- **LLM (optional layer)**: Only for description/narration semantic matching and as a PDF-extraction fallback. NEVER for amount or date matching.

## Architecture (3 layers — keep them separate)

```
app/
├── parsers/          # Layer 1: file ingestion → StandardTransaction schema
│   ├── base.py       # StandardTransaction dataclass + BaseParser interface
│   ├── bank_hbl.py   # one adapter file per bank
│   ├── bank_ubl.py
│   ├── bank_meezan.py
│   ├── bank_generic.py  # fuzzy column-detection fallback for unknown banks
│   └── ledger_parser.py # local ledger with user-configurable column mapping
├── matching/         # Layer 2: reconciliation engine (pure Python, deterministic)
│   ├── engine.py     # matching pipeline
│   └── scoring.py    # confidence scoring
├── ui/               # Layer 3: Streamlit interface
│   └── main.py
└── export/
    └── excel_report.py  # color-coded Excel export
```

## Standard Internal Schema

Every parser MUST output a list of `StandardTransaction`:

```python
@dataclass
class StandardTransaction:
    date: datetime.date
    description: str
    debit: Decimal        # 0 if not applicable
    credit: Decimal       # 0 if not applicable
    balance: Decimal | None
    source_row: int       # original row number for traceability
    raw: dict             # original row data, untouched
```

## Hard Rules (never violate)

1. **Use `Decimal`, never `float`, for money.** Parse comma-formatted strings like `"1,234,567.89"` safely.
2. **Never silently auto-match.** Every match gets a confidence tier: `EXACT`, `TOLERANCE` (amount within Rs 2 OR 0.5%), `REVIEW` (date/description fuzzy), `UNMATCHED`. Tolerance and Review matches must be visually flagged in UI and export.
3. **Date matching window**: ±3 days default, user-configurable in UI sidebar.
4. **Amount tolerance**: Rs 2 absolute OR 0.5% relative (whichever is larger), user-configurable.
5. **PII masking**: mask account numbers and CNIC in all UI displays and exports (show last 4 digits only). Full values stay only in memory.
6. **One-to-one matching**: each bank transaction matches at most one ledger entry. Use greedy best-score assignment; on ties, prefer closer date.
7. **No LLM in the core matching path.** LLM calls (if enabled) only refine description similarity for candidates already shortlisted by deterministic amount+date filters.
8. **Every parser must handle**: comma-formatted numbers, empty rows, multi-line headers, DR/CR indicator columns, and both `DD/MM/YYYY` and `DD-MMM-YYYY` date formats (Pakistani banks use both).
9. **Traceability**: every output row must link back to `source_row` in the original file.

## Pakistani Bank Format Notes

- **Dates are DD/MM/YYYY** (day first) — never parse as US format.
- Common column name variants to fuzzy-match:
  - Date: `Date`, `Txn Date`, `Value Date`, `Transaction Date`, `Posting Date`
  - Debit: `Debit`, `Withdrawal`, `DR`, `Debit Amount`
  - Credit: `Credit`, `Deposit`, `CR`, `Credit Amount`
  - Description: `Description`, `Particulars`, `Narration`, `Details`, `Transaction Details`
- Some banks use a single `Amount` column + `DR/CR` flag column — normalize into debit/credit.
- PDF statements often have page headers/footers repeating mid-table — strip them.

## Matching Pipeline (implement in this order)

1. **Pass 1 — Exact**: same amount (to the paisa) + date within window → highest description similarity wins.
2. **Pass 2 — Tolerance**: amount within tolerance + date within window.
3. **Pass 3 — Review candidates**: amount exact but date outside window (up to ±10 days), OR amount within tolerance with strong description match (rapidfuzz ratio ≥ 85).
4. **Everything else** → UNMATCHED (shown in two lists: "In bank, not in ledger" and "In ledger, not in bank").

## UI Requirements (Streamlit)

- Sidebar: file uploads (bank + ledger), bank selector dropdown, tolerance/date-window sliders, "Enable AI description matching" toggle (off by default).
- First-time ledger upload: show column-mapping UI (dropdowns: which column is Date/Debit/Credit/Description). Persist mapping in `st.session_state`.
- Results: summary metrics row (total matched / tolerance / review / unmatched, matched amount %), then tabbed tables per tier.
- Color coding: green = exact, yellow = tolerance, orange = review, red = unmatched.
- Export button → color-coded Excel via `export/excel_report.py`.

## Testing

- pytest for all parser and matching logic. UI is not unit-tested.
- Every bank adapter needs a fixture file in `tests/fixtures/` (anonymized sample).
- Matching engine tests must cover: exact match, Rs 1-2 difference, date +2 days, duplicate amounts on same day, and completely unmatched rows.
- Run `pytest -q` before declaring any task complete.

## Workflow for Claude Code

- Use the **parser-agent**, **matcher-agent**, and **ui-agent** subagents (defined in `.claude/agents/`) for their respective layers.
- Consult `skills/bank-statement-parser/SKILL.md` before writing or modifying any parser.
- Consult `skills/reconciliation-matcher/SKILL.md` before touching matching logic.
- Small, focused commits. Conventional commit messages (`feat:`, `fix:`, `test:`).
