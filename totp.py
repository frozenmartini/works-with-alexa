"""Time-based one-time passwords for Amazon's two-step verification.

Amazon's "authenticator app" 2SV is plain RFC 6238 TOTP: HMAC-SHA1, six digits,
a 30-second step, with the shared secret shown to the user once as a
52-character base32 key in groups of four. ``alexa_media_player`` keeps that key
and generates the code from it with ``pyotp`` at sign-in time, so the user
never has to open the authenticator app. This does the same with the standard
library rather than taking on a dependency for fifteen lines of HMAC. The
defaults are pyotp's exactly (cross-checked against pyotp 2.10.0).

No I/O and no Home Assistant imports, so it is unit-tested with the other pure
modules.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time

DIGITS = 6
PERIOD = 30


class InvalidSecret(ValueError):
    """The authenticator key is not base32, so no code can be made from it."""


def normalize_secret(raw: str) -> str:
    """Canonicalise a key as the user typed it, or raise :class:`InvalidSecret`.

    Amazon displays the key in groups of four separated by spaces, and users
    paste it that way; alexa_media_player strips the spaces before handing it
    to pyotp. Lowercase is accepted too, since base32 is case-insensitive and
    some keyboards capitalise unpredictably. The result is what should be kept.
    """
    secret = "".join(raw.split()).upper()
    if not secret:
        raise InvalidSecret("the key is empty")
    try:
        _decode(secret)
    except (binascii.Error, ValueError) as err:
        raise InvalidSecret("the key is not valid base32") from err
    return secret


def totp_code(secret: str, at: float | None = None) -> str:
    """The six-digit code for ``secret`` at time ``at`` (default: now).

    ``secret`` is a normalised key. The construction is RFC 4226 §5.3 with the
    RFC 6238 time counter, identical to ``pyotp.TOTP(secret).now()``.
    """
    counter = int((time.time() if at is None else at) // PERIOD)
    digest = hmac.new(_decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 10**DIGITS:0{DIGITS}d}"


def seconds_remaining(at: float | None = None) -> int:
    """Seconds until the current code stops being the current code."""
    now = time.time() if at is None else at
    return PERIOD - int(now % PERIOD)


def _decode(secret: str) -> bytes:
    # Keys are issued without padding; b32decode insists on it.
    padded = secret + "=" * (-len(secret) % 8)
    return base64.b32decode(padded, casefold=True)
