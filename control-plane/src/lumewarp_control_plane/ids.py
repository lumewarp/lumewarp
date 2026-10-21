"""Identifier helpers."""

import os
import time
import uuid


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
