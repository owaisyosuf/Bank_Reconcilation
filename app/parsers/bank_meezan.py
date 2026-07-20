"""Meezan Bank (Pakistan) PDF statement adapter.

Meezan's online-banking "Account Statement (Online)" export (a browser
"Print to PDF") has real ruling lines, unlike Bank Alfalah's whitespace-
aligned layout — so `page.extract_table()` (default settings) returns clean
rows directly; no `extract_words()` + x-clustering fallback is needed here.

Table shape: each row extracted by pdfplumber is a 10-element list:

    [date, value_date, <spacer>, doc_no, particulars, <spacer>,
     debit, credit, balance, txn_detail_marker]

Layout quirks (see M-task spec for the full structural breakdown, derived
from inspecting a real (never-committed) customer statement):

- Page 1 starts with a "garbage" row: page metadata (branch/account/title/
  address/IBAN/opening-balance line) merged into a single huge cell that
  pdfplumber cannot cleanly separate from the first few transactions' worth
  of text. We never try to parse this row for data — we scan forward for
  the real column-header row ("Date(DD/MM)" in column 0) and treat
  everything at/before it as junk to be discarded, which naturally also
  discards the "<= O P E N I N G - B A L A N C E =>" line embedded in it.
- A transaction "block" starts at any row whose date-column cell is
  non-empty and parses as a date (format `DD/MM/YY`, already handled by
  `parse_date`'s `%d/%m/%y` pattern) and continues through every following
  row until the next such date row (or the closing-balance row, or the end
  of the table). Debit/credit are read only from the block's first row;
  the particulars column is concatenated across every row in the block
  (multi-line narration); the balance is whichever row in the block (if
  any) has a non-empty balance cell — Meezan's export does NOT print a
  balance after every single transaction, so many blocks legitimately have
  no balance at all (`balance=None`), by design, not a bug.
- A "<= C L O S I N G - B A L A N C E =>" row (letter-spaced; we normalize
  by stripping everything but letters/digits and matching the substring
  "closingbalance", case-insensitively) has no date but a non-empty
  particulars cell — it
  must not be swallowed as a continuation line of the previous transaction,
  so it's detected and dropped explicitly before the generic
  continuation-row handling runs.
- Multiple pages are handled defensively: each page's table is
  extracted independently and the header-row search (rule 1) naturally
  copes with a header line repeating on every page. If a later page has no
  header line of its own (e.g. a table that continues without repeating
  it), every row on that page is treated as data.

Also handles password-protected PDFs the same way bank_alfalah.py does.
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

# Column indices within each 10-element row returned by extract_table().
_COL_DATE = 0
_COL_DOC_NO = 3
_COL_PARTICULARS = 4
_COL_DEBIT = 6
_COL_CREDIT = 7
_COL_BALANCE = 8

_HEADER_DATE_CELL = "date(dd/mm)"

# Markers used by can_parse() for best-effort bank identification.
_IDENTIFYING_MARKERS = ["meezan bank", "meezan bank limited"]

# Words/messages that indicate pdfplumber/pdfminer couldn't open an
# encrypted PDF because no/wrong password was supplied.
_PASSWORD_ERROR_HINTS = ("password", "encrypt")

_CLOSING_BALANCE_RE = re.compile(r"closingbalance")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _cell(row: list, idx: int) -> str:
    """Safely read a cell from an extract_table() row: out-of-range or None
    cells (pdfplumber emits None for cells absorbed into a merged/spanned
    region) normalize to "".
    """
    if idx >= len(row):
        return ""
    value = row[idx]
    if value is None:
        return ""
    return str(value).strip()


def _is_header_row(row: list) -> bool:
    return _cell(row, _COL_DATE).lower() == _HEADER_DATE_CELL


def _is_closing_balance_row(row: list) -> bool:
    # Strip everything but letters/digits so both the letter-spaced
    # "<= C L O S I N G - B A L A N C E =>" and a plain "Closing Balance"
    # normalize to the same "closingbalance" needle.
    normalized = _NON_ALNUM_RE.sub("", _cell(row, _COL_PARTICULARS).lower())
    return bool(_CLOSING_BALANCE_RE.search(normalized))


def _extract_all_text(pdf: "pdfplumber.PDF") -> str:
    chunks = []
    for page in pdf.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


class _OpenBlock:
    """Accumulator for a transaction block spanning one or more raw rows."""

    __slots__ = ("date", "debit", "credit", "balance", "desc_parts", "source_row", "raw")

    def __init__(self, date_val, debit, credit, balance, desc_part, source_row, raw_first_row):
        self.date = date_val
        self.debit = debit
        self.credit = credit
        self.balance = balance
        self.desc_parts = [desc_part] if desc_part else []
        self.source_row = source_row
        self.raw = {
            "date": raw_first_row[0],
            "doc_no": raw_first_row[1],
            "debit": raw_first_row[2],
            "credit": raw_first_row[3],
            "continuation_lines": [],
        }

    def add_continuation(self, particulars: str, balance_text: str) -> None:
        if particulars:
            self.desc_parts.append(particulars)
            self.raw["continuation_lines"].append(particulars)
        if balance_text and self.balance is None:
            self.balance = parse_amount(balance_text)

    def finalize(self) -> StandardTransaction:
        return StandardTransaction(
            date=self.date,
            description=" ".join(self.desc_parts).strip(),
            debit=self.debit,
            credit=self.credit,
            balance=self.balance,
            source_row=self.source_row,
            raw=self.raw,
        )


class MeezanBankParser(BaseParser):
    """Adapter for Meezan Bank's online-banking "Account Statement (Online)"
    PDF export (a browser print with real ruling lines).
    """

    bank_name = "Meezan Bank"

    def can_parse(self, file_bytes: bytes, filename: str) -> bool:
        if not filename.lower().endswith(".pdf"):
            return False
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = _extract_all_text(pdf).lower()
        except Exception:
            # Encrypted / unreadable without a password — best-effort only;
            # bank selection is primarily driven by the UI dropdown anyway.
            return False
        return any(marker in text for marker in _IDENTIFYING_MARKERS)

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
            current: _OpenBlock | None = None
            header_found_anywhere = False

            for page_index, page in enumerate(pdf.pages, start=1):
                table = page.extract_table()
                if not table:
                    continue

                header_idx = next(
                    (i for i, row in enumerate(table) if _is_header_row(row)), None
                )
                if header_idx is not None:
                    header_found_anywhere = True
                    data_rows = table[header_idx + 1 :]
                    row_offset = header_idx + 2  # 1-based row number after header
                else:
                    # No header line on this page: defensively assume the
                    # table continues from a previous page and treat every
                    # row as data.
                    data_rows = table
                    row_offset = 1

                for offset, row in enumerate(data_rows):
                    row_index_in_table = row_offset + offset
                    source_row = page_index * 100_000 + row_index_in_table

                    if _is_closing_balance_row(row):
                        # Not a transaction; must not be absorbed as a
                        # continuation line of the previous block.
                        if current is not None:
                            transactions.append(current.finalize())
                            current = None
                        continue

                    date_text = _cell(row, _COL_DATE)
                    date_val = None
                    if date_text:
                        try:
                            date_val = parse_date(date_text)
                        except Exception:
                            date_val = None

                    if date_val is not None:
                        # New transaction block begins here.
                        if current is not None:
                            transactions.append(current.finalize())

                        debit = parse_amount(_cell(row, _COL_DEBIT))
                        credit = parse_amount(_cell(row, _COL_CREDIT))
                        balance_text = _cell(row, _COL_BALANCE)
                        balance_val = parse_amount(balance_text) if balance_text else None
                        particulars = _cell(row, _COL_PARTICULARS)

                        current = _OpenBlock(
                            date_val=date_val,
                            debit=debit,
                            credit=credit,
                            balance=balance_val,
                            desc_part=particulars,
                            source_row=source_row,
                            raw_first_row=[
                                date_text,
                                _cell(row, _COL_DOC_NO),
                                _cell(row, _COL_DEBIT),
                                _cell(row, _COL_CREDIT),
                            ],
                        )
                    else:
                        # Continuation row (multi-line particulars and/or an
                        # intermittent balance cell) for the open block.
                        if current is None:
                            continue
                        particulars = _cell(row, _COL_PARTICULARS)
                        balance_text = _cell(row, _COL_BALANCE)
                        current.add_continuation(particulars, balance_text)

            if current is not None:
                transactions.append(current.finalize())

        if not header_found_anywhere:
            # The column-header line ("Date(DD/MM) ... Particulars ...
            # Debit ... Credit ... Balance") was never found on any page:
            # almost always means the wrong bank was selected for this
            # file, not that the statement genuinely has zero transactions.
            raise ValueError(
                "Could not find Meezan Bank's column-header row "
                '("Date(DD/MM)") in this PDF. Check that you selected the '
                "correct bank for this statement."
            )

        transactions.sort(key=lambda t: (t.date, t.source_row))
        return transactions
