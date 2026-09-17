"""Client-generated device fingerprints for the login flow.

Every value here is minted on the client; Amazon issues none of them. The
constructions mirror alexa-cookie2's ``lib/proxy.js`` (documented in ref/10,
answered by 02-livingroom on threads/002 Q7), with one deliberate, safe
divergence: alexa-cookie2 fills ``frc`` from ``Math.random()``; we use a CSPRNG,
which Amazon cannot distinguish and which is strictly better.

The ``deviceId`` construction is the load-bearing one. alexapy hex-encodes the
device-type suffix a *second* time, producing a 120-char id whose
``client_id=device:<id>`` carries no device-type marker -- the leading suspect
for why Amazon blocks alexapy's login while accepting alexa-cookie2's. We follow
alexa-cookie2 exactly: 92 chars, the suffix appended already-hex-encoded.

These are stable per registration: mint once, persist, and reuse on every later
login. A fingerprint that churns each login is precisely what anti-abuse looks
for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

# lib/proxy.js:18. Bump only if Amazon starts rejecting logins.
ALEXA_APP_VERSION = "2.2.651540.0"

# Hex of the ASCII "#A2IVLV5VM2W81" -- the device-type marker, appended to the
# device id already hex-encoded. Getting this wrong (e.g. double-encoding, as
# alexapy does) is the suspected cause of the login block.
_DEVICE_TYPE_SUFFIX = "23413249564c5635564d32573831"

_FRC_BYTES = 313
_DEVICE_SERIAL_BYTES = 16
_PKCE_VERIFIER_BYTES = 32


def generate_frc() -> str:
    """A 313-byte device-context blob, base64.

    alexa-cookie2 uses ``Math.random()*255`` (so never emits ``0xFF``); a CSPRNG
    is indistinguishable to Amazon and avoids that quirk.
    """
    return base64.b64encode(secrets.token_bytes(_FRC_BYTES)).decode("ascii")


def generate_map_md() -> str:
    """Base64 of a fixed device-metadata JSON literal (no randomness).

    Emitted with no whitespace so the bytes match alexa-cookie2's literal
    exactly; ``json.dumps`` defaults would insert spaces.
    """
    literal = json.dumps(
        {
            "device_user_dictionary": [],
            "device_registration_data": {"software_version": "1"},
            "app_identifier": {
                "app_version": ALEXA_APP_VERSION,
                "bundle_id": "com.amazon.echo",
            },
        },
        separators=(",", ":"),
    )
    return base64.b64encode(literal.encode("utf-8")).decode("ascii")


def generate_device_serial() -> str:
    """16 random bytes as uppercase hex (32 chars) -- the persisted serial.

    This single value seeds both the registration ``device_serial`` and the
    ``device_id`` derived from it; they must stay linked, because the signin
    URL's ``client_id`` has to equal the one sent to ``/auth/register``. Kept
    uppercase to match the reference implementations.
    """
    return secrets.token_bytes(_DEVICE_SERIAL_BYTES).hex().upper()


def alexa_media_device_serial(instance_id: str, email: str, url: str, index: int) -> str:
    """Rebuild the device serial ``alexa_media_player`` registered with.

    That integration stores no serial. It recomputes one each run from Home
    Assistant's instance id, the account's position among its config entries,
    and a hash of email+url -- so borrowing its refresh token means rebuilding
    the serial the same way, or Amazon refuses the exchange: the token is bound
    to the device that registered it.

    Transcribed from ``alexa_media/helpers.py::calculate_uuid``. The result is
    the low 32 hex digits of the sum, which is why the ``0x`` prefix never
    survives the slice.
    """
    combined = (
        int(instance_id, 16)
        + index
        + int(hashlib.sha256((email.lower() + url.lower()).encode()).hexdigest(), 16)
    )
    return hex(combined)[-32:]


def derive_device_id(device_serial: str) -> str:
    """The 92-char device id for ``openid.oa2.client_id=device:<deviceId>``.

    Hex-encode the serial's ASCII (32 -> 64), then append the already-hex
    device-type suffix (28). The suffix is appended *once* hex-encoded; alexapy
    hex-encodes the whole concatenation, double-encoding the suffix into a
    120-char id with no device-type marker -- the suspected login block.
    """
    return device_serial.encode("ascii").hex() + _DEVICE_TYPE_SUFFIX


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 PKCE exchange.

    Standard RFC 7636: the challenge is the base64url SHA-256 of the verifier's
    ASCII, both unpadded.
    """
    verifier = _b64url(secrets.token_bytes(_PKCE_VERIFIER_BYTES))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def new_fingerprint() -> dict[str, str]:
    """Mint a complete fingerprint set to persist at first registration.

    ``device_id`` is derived from ``device_serial`` so the two never drift.
    """
    serial = generate_device_serial()
    return {
        "frc": generate_frc(),
        "map_md": generate_map_md(),
        "device_serial": serial,
        "device_id": derive_device_id(serial),
    }


def _b64url(data: bytes) -> str:
    """URL-safe base64 without padding, as OAuth PKCE expects."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
