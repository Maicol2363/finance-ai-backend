from base64 import b64decode
from binascii import Error
from pathlib import PurePosixPath

from finance_ai.core.errors import AppError
from finance_ai.support.schemas import AttachmentInput

MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024

_SIGNATURES = {
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".pdf": b"%PDF-",
}


def decode_attachment(file: AttachmentInput) -> bytes:
    name = file.name
    extension = PurePosixPath(name).suffix.lower()
    try:
        signature = _SIGNATURES[extension]
        content = b64decode(file.content, validate=True)
        if (
            len(content) > MAX_ATTACHMENT_BYTES
            or not content.startswith(signature)
            or any(char in name for char in "/\\")
            or not name.isprintable()
        ):
            raise ValueError
    except KeyError, ValueError, Error:
        raise AppError(
            status=422,
            code="support_attachment_invalid",
            title="Invalid support attachment",
            detail="Attach a PNG, JPEG or PDF file up to 2 MiB.",
        ) from None
    return content
