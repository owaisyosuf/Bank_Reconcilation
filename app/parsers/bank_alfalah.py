"""Bank Alfalah (Pakistan) PDF statement adapter.

Bank Alfalah's "Monthly Statement of Account" PDFs have no visible grid
(`page.extract_table()` returns None/garbage) — they're whitespace-aligned
text. Per skills/bank-statement-parser/SKILL.md's "PDF Extraction" section,
this adapter uses `page.extract_words()` + x-coordinate clustering instead.

Layout (see M5 task spec for the full structural breakdown):
    - Repeating junk on every page: "MONTHLY", "Page X of Y Statement of
      Account", "From Date:...To Date:...".
    - Page-1-only metadata block (branch, account #, title, address,
      currency, IBAN, etc.) appears BEFORE the column-header line and is
      never transaction data.
    - Column-header line "Date Description Cheq/Inst# Debit Credit Balance"
      repeats at the top of every page; its words' x0 positions are used
      (dynamically, per file/page) to build column boundaries — never
      hardcoded pixel constants.
    - "Opening Balance <amount>" / "Closing Balance <amount>" lines have no
      date and no debit/credit — dropped by the shared "date failed to
      parse AND both amounts zero" junk-row rule (no special-casing).
    - A transaction row's Date-column bucket holds a `DD MMM YYYY` string.
      Debit/Credit show a literal "-" when not applicable (parse_amount
      already returns Decimal("0") for that).
    - Continuation lines (no word in the Date-column x-range) are
      multi-line narration wraps — their text is appended to the
      *previous* transaction's description, space-joined.
    - Footer disclaimer text at the very end is stripped via marker match.

Also handles password-protected PDFs (see `PasswordProtectedPdfError`).
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

# Column-header words, left to right, exactly as they appear on every page.
HEADER_WORDS = ["Date", "Description", "Cheq/Inst#", "Debit", "Credit", "Balance"]
COLUMN_ORDER = ["date", "description", "cheq", "debit", "credit", "balance"]

# Lines that repeat on every page and carry no transaction data.
_PAGE_FOOTER_RE = re.compile(r"Page\s+\d+\s+of\s+\d+\s+Statement of Account", re.IGNORECASE)
_DATE_RANGE_RE = re.compile(r"From Date\s*:.*To Date\s*:.*", re.IGNORECASE)

# Disclaimer/footer markers at the very end of the statement (case-insensitive
# substring match).
_FOOTER_MARKERS = [
    "please notify your branch",
    "for information & queries",
    "bankalfalah.com",
]

# Markers used by can_parse() for best-effort bank identification.
_IDENTIFYING_MARKERS = ["bank alfalah", "bankalfalah.com", "alfh"]

# Words/messages that indicate pdfplumber/pdfminer couldn't open an
# encrypted PDF because no/wrong password was supplied.
_PASSWORD_ERROR_HINTS = ("password", "encrypt")

_LINE_TOP_TOLERANCE = 2.5


def _group_words_into_lines(words: list[dict]) -> list[list[dict]]:
    """Cluster words extracted from a page into physical text lines, using
    each word's vertical ('top') position with a small tolerance to absorb
    sub-pixel jitter, then order words left-to-right within each line.
    """
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = []
    current_top: float | None = None

    for word in ordered:
        if current and current_top is not None and abs(word["top"] - current_top) > _LINE_TOP_TOLERANCE:
            lines.append(current)
            current = []
            current_top = None
        current.append(word)
        current_top = word["top"] if current_top is None else current_top

    if current:
        lines.append(current)

    for line in lines:
        line.sort(key=lambda w: w["x0"])

    return lines


def _is_header_line(line: list[dict]) -> bool:
    return [w["text"] for w in line] == HEADER_WORDS


def _compute_midpoints(header_line: list[dict]) -> list[float]:
    x0s = [w["x0"] for w in header_line]
    return [(x0s[i] + x0s[i + 1]) / 2 for i in range(len(x0s) - 1)]


def _bucket_index(x0: float, midpoints: list[float]) -> int:
    idx = 0
    for midpoint in midpoints:
        if x0 >= midpoint:
            idx += 1
    return min(idx, len(COLUMN_ORDER) - 1)


def _classify_line(line: list[dict], midpoints: list[float]) -> dict[str, str]:
    buckets: dict[str, list[dict]] = {col: [] for col in COLUMN_ORDER}
    for word in line:
        col = COLUMN_ORDER[_bucket_index(word["x0"], midpoints)]
        buckets[col].append(word)
    return {
        col: " ".join(w["text"] for w in sorted(words, key=lambda w: w["x0"]))
        for col, words in buckets.items()
    }


def _is_footer_junk(stripped_line: str) -> bool:
    if stripped_line.upper() == "MONTHLY":
        return True
    if _PAGE_FOOTER_RE.search(stripped_line):
        return True
    if _DATE_RANGE_RE.search(stripped_line):
        return True
    lower = stripped_line.lower()
    return any(marker in lower for marker in _FOOTER_MARKERS)


def _extract_all_text(pdf: "pdfplumber.PDF") -> str:
    chunks = []
    for page in pdf.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


class BankAlfalahParser(BaseParser):
    """Adapter for Bank Alfalah's whitespace-aligned "Monthly Statement of
    Account" PDF exports.
    """

    bank_name = "Bank Alfalah"

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
            current: StandardTransaction | None = None
            midpoints: list[float] | None = None

            for page_index, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                lines = _group_words_into_lines(words)
                started = False  # have we passed this page's column-header line?

                for line_index, line in enumerate(lines, start=1):
                    stripped = " ".join(w["text"] for w in line).strip()
                    if not stripped:
                        continue

                    if _is_header_line(line):
                        midpoints = _compute_midpoints(line)
                        started = True
                        continue

                    if not started:
                        # Repeating banner lines ("MONTHLY", "Page X of Y...",
                        # "From Date:...To Date:...") and, on page 1 only,
                        # the account metadata block — none of this is
                        # transaction data.
                        continue

                    if midpoints is None:
                        continue  # safety net; should not happen once started

                    if _is_footer_junk(stripped):
                        continue

                    cell_text = _classify_line(line, midpoints)
                    date_text = cell_text["date"].strip()

                    if date_text:
                        # Any line with a word in the Date-column bucket is a
                        # candidate new transaction row (per spec) — this
                        # also naturally covers "Opening Balance"/"Closing
                        # Balance" rows, which fail date parsing and have
                        # zero debit/credit, so they're dropped below by the
                        # shared junk-row rule rather than special-cased.
                        try:
                            date_val = parse_date(date_text)
                        except Exception:
                            date_val = None

                        debit = parse_amount(cell_text["debit"])
                        credit = parse_amount(cell_text["credit"])

                        if date_val is None:
                            # Drop junk rows: unparseable date AND both
                            # amounts zero (shared rule).
                            continue

                        balance_text = cell_text["balance"].strip()
                        balance_val = parse_amount(balance_text) if balance_text else None
                        description = cell_text["description"].strip()

                        source_row = page_index * 100_000 + line_index

                        current = StandardTransaction(
                            date=date_val,
                            description=description,
                            debit=debit,
                            credit=credit,
                            balance=balance_val,
                            source_row=source_row,
                            raw={
                                "page": page_index,
                                "line": line_index,
                                "date": date_text,
                                "description": description,
                                "cheq_inst": cell_text["cheq"].strip(),
                                "debit": cell_text["debit"].strip(),
                                "credit": cell_text["credit"].strip(),
                                "balance": balance_text,
                                "continuation_lines": [],
                            },
                        )
                        transactions.append(current)
                    else:
                        # No word in the Date-column bucket -> continuation
                        # of the previous transaction's multi-line narration.
                        if current is None:
                            continue
                        extra_parts = [
                            cell_text[col].strip()
                            for col in ("description", "cheq", "debit", "credit", "balance")
                            if cell_text[col].strip()
                        ]
                        if not extra_parts:
                            continue
                        addition = " ".join(extra_parts)
                        current.description = (current.description + " " + addition).strip()
                        current.raw.setdefault("continuation_lines", []).append(addition)

        transactions.sort(key=lambda t: (t.date, t.source_row))
        return transactions
