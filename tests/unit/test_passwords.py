from pathlib import Path

import pytest

from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.core.errors import RegistrationValidationError, ServiceUnavailableError


def test_common_password_matching_is_case_insensitive(tmp_path: Path) -> None:
    corpus = tmp_path / "passwords.txt"
    corpus.write_text("password\n12345678\n", encoding="utf-8")
    provider = UnsafePasswordProvider(corpus)

    with pytest.raises(RegistrationValidationError) as captured:
        provider.reject_if_unsafe("PassWord")

    assert captured.value.field_errors == {
        "password": ["Choose a password that is not commonly compromised."]
    }


def test_unavailable_or_empty_corpus_fails_closed(tmp_path: Path) -> None:
    missing = UnsafePasswordProvider(tmp_path / "missing.txt")
    empty_path = tmp_path / "empty.txt"
    empty_path.write_text("\n", encoding="utf-8")

    with pytest.raises(ServiceUnavailableError):
        missing.ensure_ready()
    with pytest.raises(ServiceUnavailableError):
        UnsafePasswordProvider(empty_path).ensure_ready()


def test_argon2id_hashes_and_verifies_without_exposing_plaintext() -> None:
    hasher = PasswordHasher(memory_kib=19_456, time_cost=2, parallelism=1)
    password = "a unique password for this test"

    password_hash = hasher.hash(password)

    assert password_hash.startswith("$argon2id$")
    assert password not in password_hash
    assert hasher.verify(password_hash, password)
    assert not hasher.verify(password_hash, "wrong password")
    assert not hasher.needs_rehash(password_hash)
