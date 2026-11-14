"""Identifier helpers."""

import os
import re
import time
import uuid

# Hello.gateway_id and ReportRequest.gateway_id: one process id, the same rule on both
# gateway services (proto comments).
GATEWAY_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")


def valid_gateway_id(value: str) -> bool:
    """Whether `value` is a usable gateway id (`proto` 1..100 of `[A-Za-z0-9._-]`)."""
    return bool(GATEWAY_ID.fullmatch(value))


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
