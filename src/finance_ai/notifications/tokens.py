"""Secrets for the email channel: verification codes and signed unsubscribe links (EML-02).

Both are derived from the deployment's session secret with a purpose label, so a value made for
one purpose can never be replayed as the other. Rotating that secret invalidates open codes
(they live minutes) and the unsubscribe links of emails already sent (the person can still turn
notifications off inside the app); the runbook says so.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable

from finance_ai.core.config import Settings

_CODE_DIGITS = 6
_UNSUBSCRIBE_VERSION = "v1"
_TOKEN_PATTERN = re.compile(r"^v1\.([1-9][0-9]{0,17})\.([A-Za-z0-9_-]{43})$")


def _key(settings: Settings, purpose: str) -> bytes:
    return hmac.new(
        settings.session_limiter_secret.encode(),
        f"finance-ai/notifications/{purpose}".encode(),
        hashlib.sha256,
    ).digest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


# --- verification codes ---------------------------------------------------------------------


def new_verification_code() -> str:
    """A uniformly random six-digit code, with leading zeros kept."""
    return f"{secrets.randbelow(10**_CODE_DIGITS):0{_CODE_DIGITS}d}"


def is_well_formed_code(value: str) -> bool:
    return len(value) == _CODE_DIGITS and value.isascii() and value.isdigit()


def hash_verification_code(settings: Settings, *, user_id: int, code: str) -> str:
    """Keyed hash bound to the person, so a code is only ever valid for the account it was
    issued to and a copy of the table reveals nothing usable."""
    digest = hmac.new(
        _key(settings, "verification-code"), f"{user_id}:{code}".encode(), hashlib.sha256
    )
    return digest.hexdigest()


def codes_match(expected_hash: str, presented_hash: str) -> bool:
    return hmac.compare_digest(expected_hash.encode(), presented_hash.encode())


# --- unsubscribe links ----------------------------------------------------------------------


def _unsubscribe_signature(settings: Settings, user_id: int) -> str:
    return _b64(
        hmac.new(
            _key(settings, "unsubscribe"),
            f"{_UNSUBSCRIBE_VERSION}:{user_id}".encode(),
            hashlib.sha256,
        ).digest()
    )


def make_unsubscribe_token(settings: Settings, user_id: int) -> str:
    """Stable per person: opening it again is harmless because unsubscribing is idempotent."""
    return f"{_UNSUBSCRIBE_VERSION}.{user_id}.{_unsubscribe_signature(settings, user_id)}"


def read_unsubscribe_token(settings: Settings, token: str) -> int | None:
    """The person a genuine token was issued for, or ``None`` for anything else."""
    match = _TOKEN_PATTERN.fullmatch(token)
    if match is None:
        return None
    user_id = int(match.group(1))
    expected = _unsubscribe_signature(settings, user_id)
    if not hmac.compare_digest(expected.encode(), match.group(2).encode()):
        return None
    return user_id


def unsubscribe_url_factory(settings: Settings) -> Callable[[int], str | None]:
    """Builds the link placed in every email, or nothing when no public origin is configured."""
    base = settings.public_base_url

    def build(user_id: int) -> str | None:
        if base is None:
            return None
        return f"{base}/public/unsubscribe?token={make_unsubscribe_token(settings, user_id)}"

    return build
