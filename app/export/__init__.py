"""Export layer: turns a `ReconciliationResult` into downloadable artifacts.

Currently just the color-coded Excel report (see `excel_report.py`). Never
re-implements matching/parsing logic — this layer only formats/serializes
what `app/matching/engine.py` already computed.
"""
