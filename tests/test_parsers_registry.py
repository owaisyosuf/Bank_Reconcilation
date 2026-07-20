"""Tests for the app/parsers/__init__.py adapter registry."""

import pytest

from app.parsers import (
    BANK_ADAPTERS,
    BankAlfalahParser,
    GenericParser,
    HabibMetropolitanParser,
    MeezanBankParser,
    get_parser,
)


def test_other_auto_detect_registered():
    assert "Other/Auto-detect" in BANK_ADAPTERS
    assert BANK_ADAPTERS["Other/Auto-detect"] is GenericParser


def test_bank_alfalah_registered():
    assert "Bank Alfalah" in BANK_ADAPTERS
    assert BANK_ADAPTERS["Bank Alfalah"] is BankAlfalahParser


def test_meezan_bank_registered():
    assert "Meezan Bank" in BANK_ADAPTERS
    assert BANK_ADAPTERS["Meezan Bank"] is MeezanBankParser


def test_habib_metropolitan_bank_registered():
    assert "Habib Metropolitan Bank" in BANK_ADAPTERS
    assert BANK_ADAPTERS["Habib Metropolitan Bank"] is HabibMetropolitanParser


def test_get_parser_returns_instance():
    parser = get_parser("Other/Auto-detect")
    assert isinstance(parser, GenericParser)


def test_get_parser_returns_alfalah_instance():
    parser = get_parser("Bank Alfalah")
    assert isinstance(parser, BankAlfalahParser)


def test_get_parser_returns_meezan_instance():
    parser = get_parser("Meezan Bank")
    assert isinstance(parser, MeezanBankParser)


def test_get_parser_returns_habib_metropolitan_instance():
    parser = get_parser("Habib Metropolitan Bank")
    assert isinstance(parser, HabibMetropolitanParser)


def test_get_parser_unknown_bank_raises():
    with pytest.raises(KeyError):
        get_parser("Some Unregistered Bank")
