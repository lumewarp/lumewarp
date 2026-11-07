"""Upstream API keys at rest: AES-256-GCM sealed with a key derived from
LUMEWARP_ENCRYPTION_KEY.

The same key is given to every gateway, which opens the ciphertext carried in the
snapshot (`Upstream.api_key_ciphertext`) while applying it. Format and derivation are
shared with `gateway/crates/lumewarp-gateway/src/sealed.rs`:

    0x01 || 12-byte nonce || ciphertext || 16-byte tag
    key = HKDF-SHA256(ikm=LUMEWARP_ENCRYPTION_KEY, salt=none, info=_KEY_INFO), 32 bytes
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .settings import Settings

VERSION = b"\x01"
_KEY_INFO = b"lumewarp/upstream-api-key/v1"
_NONCE_BYTES = 12
MAX_SECRET_BYTES = 4096


class SealingUnavailable(Exception):
    """LUMEWARP_ENCRYPTION_KEY is not configured."""


class SealedInvalid(Exception):
    """The ciphertext was not sealed with the configured key (or is corrupt)."""


def _key(settings: Settings) -> bytes:
    secret = settings.encryption_key
    if secret is None:
        raise SealingUnavailable("LUMEWARP_ENCRYPTION_KEY is not set")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(
        secret.get_secret_value().encode()
    )


def seal(settings: Settings, plaintext: str) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return VERSION + nonce + AESGCM(_key(settings)).encrypt(nonce, plaintext.encode(), None)


def unseal(settings: Settings, sealed: bytes) -> str:
    if len(sealed) < 1 + _NONCE_BYTES + 16 or sealed[:1] != VERSION:
        raise SealedInvalid("unknown sealed format")
    nonce = sealed[1 : 1 + _NONCE_BYTES]
    try:
        return AESGCM(_key(settings)).decrypt(nonce, sealed[1 + _NONCE_BYTES :], None).decode()
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise SealedInvalid("cannot open with LUMEWARP_ENCRYPTION_KEY") from exc
