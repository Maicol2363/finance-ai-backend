from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from pydantic import SecretStr, ValidationError

from finance_ai.core.config import Settings
from finance_ai.staff_auth.mfa import (
    StaffMfaPolicy,
    TotpSecretCipher,
    TotpVerifier,
    provisioning_uri,
    qr_svg_data_url,
)

NOW = datetime(2026, 11, 14, 15, 0, 10, tzinfo=UTC)


def encoded_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def test_cipher_round_trip_is_bound_to_the_owning_account() -> None:
    cipher = TotpSecretCipher.from_encoded(encoded_key())
    secret = TotpVerifier.new_secret()
    payload = cipher.encrypt(secret, user_id=7)

    assert secret.encode() not in payload
    assert cipher.decrypt(payload, user_id=7) == secret
    with pytest.raises(ValueError, match="cannot be authenticated"):
        cipher.decrypt(payload, user_id=8)
    with pytest.raises(ValueError, match="malformed"):
        cipher.decrypt(b"short", user_id=7)
    other = TotpSecretCipher.from_encoded(encoded_key())
    with pytest.raises(ValueError):
        other.decrypt(payload, user_id=7)


def test_cipher_rejects_keys_that_are_not_256_bits() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        TotpSecretCipher(b"x" * 16)


def test_verifier_accepts_one_step_of_skew_and_reports_the_matched_step() -> None:
    verifier = TotpVerifier()
    secret = verifier.new_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(NOW.timestamp()) // 30

    assert verifier.matching_step(secret, totp.at(NOW), now=NOW) == current_step
    previous = totp.at(NOW - timedelta(seconds=30))
    assert verifier.matching_step(secret, previous, now=NOW) == current_step - 1
    following = totp.at(NOW + timedelta(seconds=30))
    assert verifier.matching_step(secret, following, now=NOW) == current_step + 1
    stale = totp.at(NOW - timedelta(seconds=90))
    assert verifier.matching_step(secret, stale, now=NOW) is None


# Unicode FULLWIDTH DIGIT ONE..SIX: digits for str.isdigit() but not ASCII.
FULLWIDTH_DIGITS = "".join(chr(0xFF11 + index) for index in range(6))


@pytest.mark.parametrize("code", ["", "12345", "1234567", "12345a", " 123456", FULLWIDTH_DIGITS])
def test_verifier_rejects_malformed_codes_without_matching(code: str) -> None:
    verifier = TotpVerifier()
    assert verifier.matching_step(verifier.new_secret(), code, now=NOW) is None
    assert not verifier.is_well_formed(code)


def test_enrollment_uri_and_qr_are_generated_locally() -> None:
    secret = TotpVerifier.new_secret()
    uri = provisioning_uri(secret=secret, account_name="auditor@example.test", issuer="FinanciAI")
    assert uri.startswith("otpauth://totp/FinanciAI:auditor%40example.test?")
    assert f"secret={secret}" in uri
    data_url = qr_svg_data_url(uri)
    assert data_url.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(data_url.split(",", 1)[1])
    assert b"<svg" in svg
    assert b"script" not in svg.lower()


def test_policy_is_disabled_without_a_key_and_settings_reject_bad_keys() -> None:
    assert (
        StaffMfaPolicy.from_settings(
            Settings(_env_file=None, environment="test", staff_mfa_encryption_key=None)
        )
        is None
    )
    settings = Settings(
        _env_file=None, environment="test", staff_mfa_encryption_key=SecretStr(encoded_key())
    )
    policy = StaffMfaPolicy.from_settings(settings)
    assert policy is not None
    assert policy.issuer == "FinanciAI"
    assert policy.max_attempts == 5
    with pytest.raises(ValidationError, match="staff MFA encryption key"):
        Settings(
            _env_file=None, environment="test", staff_mfa_encryption_key=SecretStr("too-short")
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", staff_mfa_issuer="Bad:Issuer")
    blank = Settings(_env_file=None, environment="test", staff_mfa_encryption_key="  ")
    assert blank.staff_mfa_encryption_key is None
