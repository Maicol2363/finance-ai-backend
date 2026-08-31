import base64
import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class IssuedCredential:
    raw: str
    token_hash: str


class CredentialCodec:
    RAW_BYTES = 32

    def __init__(self, random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> None:
        self._random_bytes = random_bytes

    def issue(self) -> IssuedCredential:
        raw_bytes = self._random_bytes(self.RAW_BYTES)
        if len(raw_bytes) != self.RAW_BYTES:
            raise RuntimeError("Credential generator returned an invalid byte length")
        raw = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")
        return IssuedCredential(raw=raw, token_hash=hashlib.sha256(raw_bytes).hexdigest())

    def hash_presented(self, raw: str) -> str:
        raw_bytes = self.decode(raw)
        return hashlib.sha256(raw_bytes).hexdigest()

    def try_hash_presented(self, raw: str) -> str | None:
        try:
            return self.hash_presented(raw)
        except UnicodeError, ValueError:
            return None

    @staticmethod
    def decode(raw: str) -> bytes:
        if _TOKEN_PATTERN.fullmatch(raw) is None:
            raise ValueError("Credential has an invalid format")
        decoded = base64.b64decode(raw + "=", altchars=b"-_", validate=True)
        if len(decoded) != CredentialCodec.RAW_BYTES:
            raise ValueError("Credential has an invalid decoded length")
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if canonical != raw:
            raise ValueError("Credential is not canonically encoded")
        return decoded
