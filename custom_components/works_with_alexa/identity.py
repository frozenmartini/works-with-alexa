"""The stored identity of an Amazon account.

An entry has to know which account it belongs to -- to refuse a second setup
of the same account, and to check that reauthentication signs in to the same
one -- but it has no need to know the account's email. Amazon's account lookup
returns the email; only this one-way key of it is stored, as the entry's
unique id and ``directed_id``.
"""

from __future__ import annotations

import hashlib

_PREFIX = "sha256:"


def account_id(email: str) -> str:
    """The stored key for an account email: SHA-256 of the trimmed, lowercased form."""
    normalized = email.strip().lower()
    return _PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_email(value: object) -> bool:
    """Whether a stored identity is a readable email that predates the key."""
    return isinstance(value, str) and "@" in value and not value.startswith(_PREFIX)
