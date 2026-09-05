"""Identifier and UTC timestamp helpers used by the server persistence layer."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Final, Literal


IdPrefix = Literal["doc", "conv", "msg", "chat", "req"]

_CROCKFORD_BASE32: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_VALID_PREFIXES: Final = frozenset({"doc", "conv", "msg", "chat", "req"})


def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime suitable for persistence."""

    return datetime.now(UTC)


def to_iso8601(value: datetime | None = None) -> str:
    """Return an ISO 8601 UTC timestamp using the compact ``Z`` suffix."""

    timestamp = value or utc_now()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)
    return timestamp.isoformat().replace("+00:00", "Z")


def _encode_base32(value: int, length: int) -> str:
    encoded = ["0"] * length
    for index in range(length - 1, -1, -1):
        encoded[index] = _CROCKFORD_BASE32[value & 31]
        value >>= 5
    return "".join(encoded)


def generate_id(prefix: IdPrefix) -> str:
    """Generate a prefixed, URL-safe, time-sortable ULID-style identifier.

    The generated payload contains a 48-bit millisecond timestamp followed by
    80 cryptographically random bits and is encoded as 26 Crockford Base32
    characters. It has the same useful lexical ordering property as a ULID.
    """

    if prefix not in _VALID_PREFIXES:
        allowed = ", ".join(sorted(_VALID_PREFIXES))
        raise ValueError(f"unsupported ID prefix {prefix!r}; expected one of: {allowed}")

    timestamp_ms = int(time.time_ns() // 1_000_000)
    if timestamp_ms >= 1 << 48:
        raise OverflowError("current timestamp does not fit in 48 bits")
    random_bits = int.from_bytes(os.urandom(10), byteorder="big")
    payload = (timestamp_ms << 80) | random_bits
    return f"{prefix}_{_encode_base32(payload, 26)}"


def new_document_id() -> str:
    return generate_id("doc")


def new_conversation_id() -> str:
    return generate_id("conv")


def new_message_id() -> str:
    return generate_id("msg")


def new_chat_id() -> str:
    return generate_id("chat")


def new_request_id() -> str:
    return generate_id("req")


__all__ = [
    "IdPrefix",
    "generate_id",
    "new_chat_id",
    "new_conversation_id",
    "new_document_id",
    "new_message_id",
    "new_request_id",
    "to_iso8601",
    "utc_now",
]
