"""Adapter registry mapping the UI's bank dropdown to parser classes.

Only "Other/Auto-detect" (bank_generic.GenericParser) is registered for M1.
Bank-specific adapters (HBL, UBL, Meezan, ...) are added in later milestones
as their own `bank_<name>.py` files — never by editing existing adapters.
"""

from __future__ import annotations

from app.parsers.base import BaseParser, ScannedPdfError, StandardTransaction
from app.parsers.bank_generic import GenericParser

BANK_ADAPTERS: dict[str, type[BaseParser]] = {
    "Other/Auto-detect": GenericParser,
}


def get_parser(bank_name: str) -> BaseParser:
    """Instantiate the registered adapter for the given bank dropdown value."""
    try:
        adapter_cls = BANK_ADAPTERS[bank_name]
    except KeyError as exc:
        raise KeyError(
            f"No parser adapter registered for bank {bank_name!r}. "
            f"Available: {list(BANK_ADAPTERS)}"
        ) from exc
    return adapter_cls()


__all__ = [
    "BaseParser",
    "StandardTransaction",
    "ScannedPdfError",
    "BANK_ADAPTERS",
    "get_parser",
    "GenericParser",
]
