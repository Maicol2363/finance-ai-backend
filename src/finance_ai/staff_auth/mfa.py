"""SEC-01 TOTP primitives: protected secrets, replay-aware verification and QR enrollment."""

from __future__ import annotations

import base64
import hmac
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import quote

import pyotp
import qrcode
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from qrcode.image.svg import SvgPathImage

from finance_ai.core.config import Settings

# ASCII digits only: Unicode digits would otherwise reach compare_digest as non-ASCII text.
_CODE_PATTERN: Final = re.compile(r"^[0-9]{6}$")
_NONCE_BYTES: Final = 12


class TotpSecretCipher:
    """AES-256-GCM protection for TOTP secrets, bound to the owning account."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("The TOTP encryption key must be 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def from_encoded(cls, encoded: str) -> TotpSecretCipher:
        padded = encoded.strip() + "=" * (-len(encoded.strip()) % 4)
        return cls(base64.urlsafe_b64decode(padded))

    def encrypt(self, secret: str, *, user_id: int) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, secret.encode("ascii"), _aad(user_id))

    def decrypt(self, payload: bytes, *, user_id: int) -> str:
        if len(payload) <= _NONCE_BYTES:
            raise ValueError("Protected TOTP secret is malformed")
        nonce, ciphertext = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, _aad(user_id)).decode("ascii")
        except InvalidTag as exc:
            raise ValueError("Protected TOTP secret cannot be authenticated") from exc


class TotpVerifier:
    """RFC 6238 SHA-1, six digits, 30-second steps with one step of accepted skew."""

    STEP_SECONDS: Final = 30
    ALLOWED_SKEW_STEPS: Final = 1

    @staticmethod
    def new_secret() -> str:
        return pyotp.random_base32()

    @staticmethod
    def is_well_formed(code: str) -> bool:
        return _CODE_PATTERN.fullmatch(code) is not None

    def matching_step(self, secret: str, code: str, *, now: datetime) -> int | None:
        """Return the accepted time step for ``code`` or ``None`` when it does not match."""
        if not self.is_well_formed(code):
            return None
        totp = pyotp.TOTP(secret, interval=self.STEP_SECONDS)
        current_step = int(now.timestamp()) // self.STEP_SECONDS
        matched: int | None = None
        # Compare every candidate to keep timing independent of which step matched.
        for offset in range(-self.ALLOWED_SKEW_STEPS, self.ALLOWED_SKEW_STEPS + 1):
            step = current_step + offset
            if step >= 0 and hmac.compare_digest(totp.generate_otp(step), code):
                matched = step
        return matched


@dataclass(frozen=True)
class StaffMfaPolicy:
    cipher: TotpSecretCipher
    verifier: TotpVerifier
    issuer: str
    challenge_minutes: int
    max_attempts: int

    @classmethod
    def from_settings(cls, settings: Settings) -> StaffMfaPolicy | None:
        if settings.staff_mfa_encryption_key is None:
            return None
        return cls(
            cipher=TotpSecretCipher.from_encoded(
                settings.staff_mfa_encryption_key.get_secret_value()
            ),
            verifier=TotpVerifier(),
            issuer=settings.staff_mfa_issuer,
            challenge_minutes=settings.staff_mfa_challenge_minutes,
            max_attempts=settings.staff_mfa_max_attempts,
        )


def provisioning_uri(*, secret: str, account_name: str, issuer: str) -> str:
    return pyotp.TOTP(secret, interval=TotpVerifier.STEP_SECONDS).provisioning_uri(
        name=account_name, issuer_name=issuer
    )


def qr_svg_data_url(uri: str) -> str:
    """Render the enrollment URI as an inline SVG data URL; no external QR service is used."""
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
        image_factory=SvgPathImage,
    )
    code.add_data(uri)
    code.make(fit=True)
    buffer = io.BytesIO()
    code.make_image().save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _aad(user_id: int) -> bytes:
    return quote(f"finance-ai:staff-totp:{user_id}").encode("ascii")
