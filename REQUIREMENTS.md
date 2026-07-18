# REQUIREMENTS.md — Bank Reconciliation App

## 1. Problem

Pakistani SMEs reconcile bank statements against their internal ledgers manually in Excel. This is slow, error-prone, and painful because:
- Every bank exports a different format (HBL, UBL, MCB, Meezan, Allied, Bank Alfalah...)
- Amounts differ by small margins (bank charges, rounding) — Rs 1-2 differences
- Dates differ (transaction date vs clearing date, cheques clear in 1-3 days)
- Descriptions never match exactly ("ABC TRADERS PVT LTD" vs "A.B.C Traders")

## 2. Goal

A browser app where a user uploads two files and gets a reconciliation report in under a minute, with every match explainable and auditable.

## 3. Users

- Primary: small business accountants/owners in Pakistan (e.g., tyre shops, traders)
- Secondary: Nexora as a service — Owais runs reconciliations for clients

## 4. Functional Requirements

### F1 — File Ingestion
- F1.1 Accept bank statement as PDF (text-based), XLSX, or CSV
- F1.2 Accept local ledger as XLSX or CSV
- F1.3 Bank selector dropdown; "Other/Auto-detect" uses generic fuzzy parser
- F1.4 Scanned (image) PDFs: detect and show a clear error message suggesting CSV/Excel export instead. OCR is out of scope for v1.
- F1.5 Column-mapping UI for ledger files (user maps Date/Debit/Credit/Description once per format)

### F2 — Normalization
- F2.1 All parsers output the StandardTransaction schema (see CLAUDE.md)
- F2.2 Handle comma-formatted numbers, DR/CR columns, day-first dates, multi-line PDF headers

### F3 — Matching
- F3.1 Deterministic multi-pass pipeline (Exact → Tolerance → Review → Unmatched)
- F3.2 Configurable amount tolerance (default Rs 2 / 0.5%) and date window (default ±3 days)
- F3.3 One-to-one matching with best-score assignment
- F3.4 Optional LLM-assisted description matching (toggle, off by default) — only re-ranks deterministic candidates

### F4 — Results & Export
- F4.1 Summary metrics: counts and amounts per tier, % reconciled
- F4.2 Tabbed detail tables per tier with color coding
- F4.3 Two unmatched lists: "In bank only" and "In ledger only"
- F4.4 Color-coded Excel export with a summary sheet + all detail sheets
- F4.5 Every row traceable to its original file row number

### F5 — Privacy
- F5.1 Account numbers / CNIC masked everywhere (last 4 digits)
- F5.2 No files stored on disk beyond the session; process in memory

## 5. Non-Functional Requirements

- N1: 5,000 transactions per side reconciled in < 30 seconds
- N2: Works offline except the optional LLM toggle
- N3: All money math in Decimal — zero floating-point drift
- N4: pytest coverage on parsers and matching engine

## 6. Out of Scope (v1)

- OCR for scanned PDFs
- Multi-currency
- User accounts / saved history / database
- Auto-fetching statements from bank APIs

## 7. Success Criteria

- Real HBL/UBL/Meezan sample statements parse without manual edits
- A 500-row test reconciliation reaches ≥ 95% correct matches vs a hand-checked answer key
- Rapid Tyres' actual monthly reconciliation completes end-to-end as the pilot case

## 8. Build Order (suggested milestones)

1. **M1**: StandardTransaction schema + generic CSV/Excel parser + ledger column-mapping
2. **M2**: Matching engine (all passes) + pytest suite with synthetic fixtures
3. **M3**: Streamlit UI with summary + tabbed results
4. **M4**: Excel export (color-coded)
5. **M5**: HBL + UBL + Meezan PDF adapters (pdfplumber)
6. **M6**: Optional LLM description-matching toggle
7. **M7**: Polish — PII masking audit, error messages, edge cases
