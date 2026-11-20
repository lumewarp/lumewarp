"""Identifier helpers."""

import os
import re
import secrets
import time
import uuid

# ReportRequest.gateway_id and the control-plane-assigned id: 1..100 of `[A-Za-z0-9._-]`.
GATEWAY_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
_NOT_IN_GATEWAY_HOST = re.compile(r"[^A-Za-z0-9._-]")


def valid_gateway_id(value: str) -> bool:
    """Whether `value` is a usable gateway id (`proto` 1..100 of `[A-Za-z0-9._-]`)."""
    return bool(GATEWAY_ID.fullmatch(value))


def gateway_hostname(hostname: str) -> str:
    """The display host of an assigned gateway id: `[A-Za-z0-9._-]`, at most 60 chars."""
    return _NOT_IN_GATEWAY_HOST.sub("", hostname)[:60] or "local"


def new_gateway_id(hostname: str) -> str:
    """A fresh id for one stream (`<host>-<8 lowercase hex>`); the control plane assigns it."""
    return f"{gateway_hostname(hostname)}-{secrets.token_hex(4)}"


def uuid7() -> uuid.UUID:
    """Time-ordered UUIDv7 (RFC 9562): 48-bit ms timestamp, then random bits.

    Used for primary keys so inserts stay index-local.
    """
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10))
    value = (ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)
