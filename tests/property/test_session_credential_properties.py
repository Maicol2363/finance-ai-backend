import base64
import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finance_ai.auth.credentials import CredentialCodec


@given(st.binary(min_size=32, max_size=32))
def test_exact_32_byte_credentials_round_trip_canonically(raw_bytes: bytes) -> None:
    codec = CredentialCodec(lambda _: raw_bytes)
    issued = codec.issue()

    assert len(issued.raw) == 43
    assert "=" not in issued.raw
    assert CredentialCodec.decode(issued.raw) == raw_bytes
    assert codec.hash_presented(issued.raw) == hashlib.sha256(raw_bytes).hexdigest()


@given(st.binary().filter(lambda value: len(value) != 32))
def test_generator_rejects_every_non_32_byte_result(raw_bytes: bytes) -> None:
    codec = CredentialCodec(lambda _: raw_bytes)
    with pytest.raises(RuntimeError):
        codec.issue()


@given(st.text(max_size=100).filter(lambda value: len(value) != 43))
def test_noncanonical_presented_values_never_hash(raw: str) -> None:
    assert CredentialCodec().try_hash_presented(raw) is None


def test_noncanonical_43_character_encoding_is_rejected() -> None:
    encoded = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode("ascii")
    assert len(encoded) == 43
    assert CredentialCodec.decode(encoded) == b"x" * 32
    with pytest.raises(ValueError):
        CredentialCodec.decode("!" + encoded[1:])
