"""Tests for the app/parsers/__init__.py adapter registry."""

import pytest

from app.parsers import BANK_ADAPTERS, GenericParser, get_parser


def test_other_auto_detect_registered():
    assert "Other/Auto-detect" in BANK_ADAPTERS
    assert BANK_ADAPTERS["Other/Auto-detect"] is GenericParser


def test_get_parser_returns_instance():
    parser = get_parser("Other/Auto-detect")
    assert isinstance(parser, GenericParser)


def test_get_parser_unknown_bank_raises():
    with pytest.raises(KeyError):
        get_parser("Some Unregistered Bank")
