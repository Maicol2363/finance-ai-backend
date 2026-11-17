"""Verification codes and signed unsubscribe links (EML-02)."""

from __future__ import annotations

import pytest

from finance_ai.core.config import Settings
from finance_ai.notifications import tokens
from finance_ai.notifications.tokens import (
    codes_match,
    hash_verification_code,
    is_well_formed_code,
    make_unsubscribe_token,
    new_verification_code,
    read_unsubscribe_token,
    unsubscribe_url_factory,
)

SETTINGS = Settings(_env_file=None, session_limiter_secret="unit-test-secret-for-tokens-1")
LOOKALIKE_DIGITS = "".join(chr(0xFF11 + offset) for offset in range(6))  # fullwidth, not 0-9
ARABIC_INDIC_DIGITS = "".join(chr(0x0661 + offset) for offset in range(6))
OTHER = Settings(_env_file=None, session_limiter_secret="unit-test-secret-for-tokens-2")


def test_codes_are_six_digits_and_keep_leading_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens.secrets, "randbelow", lambda _limit: 42)

    assert new_verification_code() == "000042"


def test_real_codes_are_always_well_formed() -> None:
    assert all(is_well_formed_code(new_verification_code()) for _ in range(200))


@pytest.mark.parametrize(
    "value",
    [
        "",
        "12345",
        "1234567",
        "12345a",
        LOOKALIKE_DIGITS,
        ARABIC_INDIC_DIGITS,
        " 12345",
        "123456" + chr(10),
    ],
)
def test_malformed_codes_are_recognised(value: str) -> None:
    assert is_well_formed_code(value) is False


def test_a_code_hash_is_bound_to_the_person_and_the_deployment() -> None:
    base = hash_verification_code(SETTINGS, user_id=1, code="123456")

    assert base == hash_verification_code(SETTINGS, user_id=1, code="123456")
    assert base != hash_verification_code(SETTINGS, user_id=2, code="123456")
    assert base != hash_verification_code(SETTINGS, user_id=1, code="123457")
    assert base != hash_verification_code(OTHER, user_id=1, code="123456")
    assert len(base) == 64 and "123456" not in base


def test_codes_are_compared_for_equality_only() -> None:
    first = hash_verification_code(SETTINGS, user_id=1, code="123456")

    assert codes_match(first, first)
    assert not codes_match(first, hash_verification_code(SETTINGS, user_id=1, code="654321"))


def test_an_unsubscribe_token_round_trips_to_the_same_person() -> None:
    token = make_unsubscribe_token(SETTINGS, 4711)

    assert read_unsubscribe_token(SETTINGS, token) == 4711
    assert token == make_unsubscribe_token(SETTINGS, 4711)  # stable: safe to reopen


def test_an_unsubscribe_token_is_not_valid_for_another_person_or_secret() -> None:
    token = make_unsubscribe_token(SETTINGS, 4711)

    assert read_unsubscribe_token(OTHER, token) is None
    assert read_unsubscribe_token(SETTINGS, token.replace(".4711.", ".4712.")) is None
    assert read_unsubscribe_token(SETTINGS, token[:-1] + ("A" if token[-1] != "A" else "B")) is None


@pytest.mark.parametrize(
    "value",
    ["", "v1", "v1.", "v1..x", "v1.1.short", "v1.01." + "A" * 43, "v1.1." + "A" * 44, "x" * 300],
)
def test_garbage_is_never_read_as_a_token(value: str) -> None:
    assert read_unsubscribe_token(SETTINGS, value) is None


def test_the_token_cannot_double_as_a_verification_hash() -> None:
    token = make_unsubscribe_token(SETTINGS, 1)

    assert token.split(".")[-1] != hash_verification_code(SETTINGS, user_id=1, code="123456")


def test_the_link_is_built_only_when_a_public_origin_is_configured() -> None:
    without = unsubscribe_url_factory(SETTINGS)
    with_origin = unsubscribe_url_factory(
        Settings(
            _env_file=None,
            session_limiter_secret="unit-test-secret-for-tokens-1",
            public_base_url="https://api.finance.example.com",
        )
    )

    assert without(1) is None
    url = with_origin(1)
    assert url is not None
    assert url.startswith("https://api.finance.example.com/public/unsubscribe?token=v1.1.")
