"""Habib Metropolitan Bank (HMB) (Pakistan) PDF statement adapter.

HMB's statement export has real ruling lines (like Meezan, unlike Bank
Alfalah's whitespace-aligned text), so `page.extract_table()` (default
settings) returns clean rows directly -- no `extract_words()` + x-clustering
fallback is needed here.

Column shape varies by page -- this is the main engineering challenge (see
M-task spec for the full structural breakdown, derived from inspecting a
real (never-committed) customer statement):

- **Page 1 only**: the table has 9 columns because the account-info box at
  the top of page 1 (branch/A-C-type/IBAN/currency/date-range boilerplate)
  bleeds extra blank "filler" columns into pdfplumber's grid detection.
  Real fields land at specific indices with `None`-filled filler columns
  interspersed between them, e.g. the header row extracts as:
      ['Date', 'Particulars', 'Debit', None, 'Credit', None, None, 'Balance', None]
  i.e. date=0, particulars=1, debit=2, credit=4, balance=7; indices
  3, 5, 6, 8 are always `None` for every row in this table (true filler,
  never populated).
- **Pages 2+**: the table is a clean 5 columns, no header row repeats:
  `[date, particulars, debit, credit, balance]` directly.

We never hardcode "page 1 vs page 2+". Instead, for each page's table
independently:
  (a) Scan for a row whose `str(row[0]).strip().lower() == "date"` -- if
      found, that's the header row; build the field -> column-index map by
      matching each header cell's stripped text case-insensitively against
      {"date", "particulars", "debit", "credit", "balance"} (also accepting
      "description" as a synonym for particulars), skip that row itself,
      and use this map for the rest of that page's table (rows before the
      header -- the account-info box -- are discarded, same convention as
      bank_meezan.py).
  (b) If no such header row exists in this page's table (the normal case
      for continuation pages), fall back to a dynamic active-column rule:
      column 0 = date, column 1 = particulars, then scan columns from
      index 2 onward and collect every column index where at least one row
      in this page's table has a non-`None` value (an empty string ''
      counts as "non-None"/active; only truly-always-`None` columns are
      filler) -- take these active indices in left-to-right order as
      [debit, credit, balance]. This correctly derives [2, 4, 7] for the
      9-col page-1 shape and [2, 3, 4] for the clean 5-col shape without
      ever hardcoding either.

No continuation-row grouping is needed (simpler than both Alfalah and
Meezan): every transaction is exactly one row in the extracted table --
pdfplumber's extract_table() already merges wrapped/multi-line narration
into a single cell as one string containing embedded "\n" characters. We
just replace embedded "\n" with a space (and collapse repeated whitespace)
when building `description`.

Balance is intermittent (same quirk as Meezan): not every row has a
populated balance cell -- when empty, `balance=None`.

Junk rows: the header row is explicitly skipped (never becomes a
transaction). The "Opening Balance" stray row on page 1 (only the balance
column populated, date/particulars/debit/credit all empty/`None`) is
naturally dropped because its date fails to parse. "Closing Balance" text
(if present) renders outside the table's grid bounding box and never
reaches the row-processing loop at all.

Also handles password-protected PDFs the same way bank_alfalah.py/
bank_meezan.py does.
"""

from __future__ import annotations

import io
import re

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

try:
    from pdfminer.pdfdocument import PDFEncryptionError
except ImportError:  # pragma: no cover - defensive, pdfminer ships with pdfplumber
    PDFEncryptionError = Exception  # type: ignore[assignment, misc]

from app.parsers.base import (
    BaseParser,
    PasswordProtectedPdfError,
    ScannedPdfError,
    StandardTransaction,
    parse_amount,
    parse_date,
)

# Header-cell synonyms (case-insensitive exact match, not fuzzy -- these are
# static column labels, not free-text descriptions). "description" is
# accepted as a synonym for "particulars" per the shared synonym table.
_HEADER_FIELD_SYNONYMS = {
    "date": "date",
    "particulars": "particulars",
    "description": "particulars",
    "debit": "debit",
    "credit": "credit",
    "balance": "balance",
}

_REQUIRED_FIELDS = {"date", "particulars", "debit", "credit", "balance"}

# Markers used by can_parse() for best-effort bank identification. The
# bank's name/logo is an image (not extractable text), so we rely on the
# IBAN institution code substring "mpbl" (e.g. "PK66MPBL...") combined with
# a structural/text check for extra confidence, to keep false positives low.
_IBAN_MARKER = "mpbl"

# Words/messages that indicate pdfplumber/pdfminer couldn't open an
# encrypted PDF because no/wrong password was supplied.
_PASSWORD_ERROR_HINTS = ("password", "encrypt")

_WHITESPACE_RE = re.compile(r"\s+")


def _cell(row: list, idx: int) -> str:
    """Safely read a cell from an extract_table() row as a stripped string:
    out-of-range or `None` cells (pdfplumber emits `None` for cells that
    have no detected cell boundary in this row -- e.g. the page-1 "filler"
    columns) normalize to "".
    """
    if idx >= len(row):
        return ""
    value = row[idx]
    if value is None:
        return ""
    return str(value).strip()


def _raw_cell(row: list, idx: int):
    """Return the raw (possibly None) cell value at idx, or None if the row
    is too short to have that index at all.
    """
    if idx >= len(row):
        return None
    return row[idx]


def _is_header_row(row: list) -> bool:
    return _cell(row, 0).lower() == "date"


def _build_header_field_map(row: list) -> dict[str, int] | None:
    """If `row` is a column-header row, build a field -> column-index map by
    matching each cell's stripped text case-insensitively against the
    synonym table. Returns None if the required fields aren't all present.
    """
    field_idx: dict[str, int] = {}
    for idx, _ in enumerate(row):
        key = _cell(row, idx).lower()
        field = _HEADER_FIELD_SYNONYMS.get(key)
        if field and field not in field_idx:
            field_idx[field] = idx
    if not _REQUIRED_FIELDS.issubset(field_idx):
        return None
    return field_idx


def _build_dynamic_field_map(table_rows: list[list]) -> dict[str, int]:
    """Fallback for pages with no header row: column 0 = date, column 1 =
    particulars, then the first three columns (from index 2 onward) that
    have at least one non-`None` value anywhere in this page's table are
    taken, in left-to-right order, as [debit, credit, balance].
    """
    max_len = max((len(r) for r in table_rows), default=0)
    active_indices = [
        idx
        for idx in range(2, max_len)
        if any(_raw_cell(r, idx) is not None for r in table_rows)
    ]
    field_idx: dict[str, int] = {"date": 0, "particulars": 1}
    for field, idx in zip(("debit", "credit", "balance"), active_indices):
        field_idx[field] = idx
    return field_idx


def _extract_all_text(pdf: "pdfplumber.PDF") -> str:
    chunks = []
    for page in pdf.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


def _flatten_description(raw_text: str) -> str:
    """Collapse embedded newlines (from pdfplumber merging a wrapped,
    multi-line narration cell into one string) and repeated whitespace into
    single spaces.
    """
    return _WHITESPACE_RE.sub(" ", raw_text).strip()


class HabibMetropolitanParser(BaseParser):
    """Adapter for Habib Metropolitan Bank's (HMB) ruled-table PDF statement
    export.
    """

    bank_name = "Habib Metropolitan Bank"

    def can_parse(self, file_bytes: bytes, filename: str) -> bool:
        if not filename.lower().endswith(".pdf"):
            return False
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = _extract_all_text(pdf).lower()
                has_header_row = False
                for page in pdf.pages:
                    table = page.extract_table()
                    if not table:
                        continue
                    if any(_is_header_row(row) for row in table):
                        has_header_row = True
                        break
        except Exception:
            # Encrypted / unreadable without a password — best-effort only;
            # bank selection is primarily driven by the UI dropdown anyway.
            return False

        if _IBAN_MARKER not in text:
            return False
        # Don't rely on "mpbl" alone (short/generic-looking token): also
        # require either the structural column-header row, or the plain
        # text phrase "particulars" alongside it.
        return has_header_row or "particulars" in text

    def parse(
        self,
        file_bytes: bytes,
        filename: str,
        password: str | None = None,
    ) -> list[StandardTransaction]:
        try:
            pdf = pdfplumber.open(io.BytesIO(file_bytes), password=password or "")
        except PDFEncryptionError as exc:
            raise PasswordProtectedPdfError() from exc
        except PdfminerException as exc:
            # pdfplumber wraps any PDFDocument-construction failure (e.g. a
            # missing/incorrect password on an encrypted PDF) into this
            # generic wrapper, with the original pdfminer exception as its
            # sole arg.
            original = exc.args[0] if exc.args else None
            if isinstance(original, PDFEncryptionError) or any(
                hint in str(exc).lower() for hint in _PASSWORD_ERROR_HINTS
            ):
                raise PasswordProtectedPdfError() from exc
            raise
        except Exception as exc:
            message = str(exc).lower()
            if isinstance(exc, PDFEncryptionError) or any(
                hint in message for hint in _PASSWORD_ERROR_HINTS
            ):
                raise PasswordProtectedPdfError() from exc
            raise

        with pdf:
            page_texts = [page.extract_text() or "" for page in pdf.pages]
            if not any(text.strip() for text in page_texts):
                raise ScannedPdfError()

            transactions: list[StandardTransaction] = []
            header_found_anywhere = False

            for page_index, page in enumerate(pdf.pages, start=1):
                table = page.extract_table()
                if not table:
                    continue

                header_idx = next(
                    (i for i, row in enumerate(table) if _is_header_row(row)), None
                )
                field_idx: dict[str, int] | None = None
                if header_idx is not None:
                    field_idx = _build_header_field_map(table[header_idx])

                if field_idx is not None:
                    header_found_anywhere = True
                    data_rows = table[header_idx + 1 :]
                    row_offset = header_idx + 2  # 1-based row number after header
                else:
                    # No (valid) header row on this page: assume the table
                    # is a continuation page and derive columns dynamically.
                    data_rows = table
                    row_offset = 1
                    field_idx = _build_dynamic_field_map(table)

                for offset, row in enumerate(data_rows):
                    row_index_in_table = row_offset + offset
                    source_row = page_index * 100_000 + row_index_in_table

                    date_text = _cell(row, field_idx["date"])
                    date_val = None
                    if date_text:
                        try:
                            date_val = parse_date(date_text)
                        except Exception:
                            date_val = None

                    if date_val is None:
                        # Junk row: the "Opening Balance" stray row (only
                        # the balance cell populated) and any stray/leaked
                        # header-like row both fail date parsing here and
                        # are dropped, per the shared junk-row rule.
                        continue

                    debit = parse_amount(_cell(row, field_idx["debit"]))
                    credit = parse_amount(_cell(row, field_idx["credit"]))
                    balance_text = _cell(row, field_idx["balance"])
                    balance_val = parse_amount(balance_text) if balance_text else None
                    particulars_raw = _cell(row, field_idx["particulars"])
                    description = _flatten_description(particulars_raw)

                    transactions.append(
                        StandardTransaction(
                            date=date_val,
                            description=description,
                            debit=debit,
                            credit=credit,
                            balance=balance_val,
                            source_row=source_row,
                            raw={
                                "page": page_index,
                                "date": date_text,
                                "particulars": particulars_raw,
                                "debit": _cell(row, field_idx["debit"]),
                                "credit": _cell(row, field_idx["credit"]),
                                "balance": balance_text,
                            },
                        )
                    )

        if not header_found_anywhere:
            # The column-header row ("Date"/"Particulars"/"Debit"/"Credit"/
            # "Balance") was never found on any page: almost always means
            # the wrong bank was selected for this file, not that the
            # statement genuinely has zero transactions.
            raise ValueError(
                "Could not find Habib Metropolitan Bank's column-header row "
                '("Date", "Particulars", "Debit", "Credit", "Balance") in '
                "this PDF. Check that you selected the correct bank for "
                "this statement."
            )

        transactions.sort(key=lambda t: (t.date, t.source_row))
        return transactions
