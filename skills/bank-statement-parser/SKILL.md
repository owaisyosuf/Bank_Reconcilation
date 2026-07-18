---
name: bank-statement-parser
description: How to parse Pakistani bank statements (PDF/Excel/CSV) and local ledgers into the app's StandardTransaction schema. Use this skill whenever writing or modifying any parser, adding a new bank adapter, handling column detection, fixing PDF extraction issues, or dealing with Pakistani number/date formats — even if the task only mentions "reading a file" or "fixing amounts".
---

# Bank Statement Parser Skill

## Adapter Pattern

Every bank gets its own adapter file. All adapters subclass `BaseParser`:

```python
class BaseParser(ABC):
    bank_name: str

    @abstractmethod
    def can_parse(self, file_bytes: bytes, filename: str) -> bool: ...

    @abstractmethod
    def parse(self, file_bytes: bytes, filename: str) -> list[StandardTransaction]: ...
```

Registry in `parsers/__init__.py` maps the UI's bank dropdown to adapter classes. `bank_generic.py` is the fallback when the user picks "Other/Auto-detect".

## Shared Helpers (in `parsers/base.py`) — always use these, never inline-parse

### parse_amount(raw) -> Decimal
Must handle ALL of:
- `"1,234,567.89"` → comma thousands (standard in FBR exports and bank Excel files)
- `"(5,000.00)"` → parentheses = negative
- `"5,000.00 DR"` / `"5,000.00 CR"` → strip suffix, return sign info separately if needed
- `""`, `"-"`, `None`, `NaN` → Decimal("0")
- Never `float()` anywhere in the path. `Decimal(str(...))` after cleaning.

### parse_date(raw) -> date
Day-first always. Accept: `31/01/2026`, `31-01-2026`, `31-Jan-2026`, `31 Jan 2026`, `31.01.2026`, and Excel serial numbers. Reject ambiguous US-style interpretation: `03/04/2026` is 3rd April, never March 4th.

## Column Fuzzy-Matching Table

When headers are unknown (generic parser or ledger), match case-insensitively with rapidfuzz (ratio ≥ 80) against these synonym groups:

| Target | Synonyms |
|---|---|
| date | date, txn date, value date, transaction date, posting date, tarikh |
| debit | debit, withdrawal, dr, debit amount, paid out, withdrawals |
| credit | credit, deposit, cr, credit amount, paid in, deposits |
| description | description, particulars, narration, details, transaction details, remarks |
| balance | balance, running balance, closing balance |
| amount (single-col mode) | amount, txn amount, transaction amount |
| drcr flag | dr/cr, type, txn type, indicator |

If a single `amount` + `drcr flag` pair is found instead of debit/credit columns, normalize: DR → debit, CR → credit.

## PDF Extraction (pdfplumber)

1. `page.extract_table()` first; if empty, fall back to `extract_words()` with x-coordinate clustering.
2. Strip repeating page headers/footers: any row identical (fuzzy ≥ 95) to the first page's header row, and rows containing "Page X of Y", "Statement of Account", bank slogans.
3. Multi-line descriptions: a row whose date cell is empty AND amount cells are empty is a continuation → append its text to the previous transaction's description.
4. If `page.extract_text()` returns None/near-empty across pages → raise `ScannedPdfError`.

## Header-Row Detection (Excel/CSV)

Bank Excel files often have 5-15 junk rows (logo, account info) before the real header. Scan the first 25 rows; the header row is the first row where ≥ 3 cells fuzzy-match the synonym table. Everything above it is metadata (extract account number from it for PII masking if present).

## Validation Before Returning

- Drop rows where date failed to parse AND both amounts are zero (junk rows).
- If balance column exists: spot-check that `prev_balance ± amount ≈ balance` on a sample of rows; log a warning (don't fail) if > 10% inconsistent — signals a parsing bug.
- Result must be sorted by date, then source_row.

## Fixture & Test Requirements

Every adapter: `tests/fixtures/<bank>_sample.{csv|xlsx|pdf}` (anonymized — fake names, fake account numbers) + tests covering happy path, comma amounts, DR/CR normalization, and junk-header skipping.
