"""Emergency password authentication and process-local failed-login backoff."""

import hmac
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from ipaddress import ip_address

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request

from lumewarp_control_plane.security import hash_secret
from lumewarp_control_plane.settings import Settings

PASSWORD_HASHER = PasswordHasher(type=Type.ID)
MAX_IPS = 4096
WINDOW_SECONDS = 60
MAX_GLOBAL_DELAY_SECONDS = 1.0


def validate_credentials(settings: Settings) -> None:
    if settings.break_glass_password and not settings.dev_mode:
        raise ValueError("plaintext break-glass password requires LUMEWARP_DEV_MODE=true")
    if settings.break_glass_password_hash:
        encoded = settings.break_glass_password_hash.get_secret_value()
        try:
            parameters = extract_parameters(encoded)
            if parameters.type is not Type.ID:
                raise InvalidHashError()
            # Also ask the Argon2 decoder to validate the salt, digest and costs.
            PASSWORD_HASHER.verify(encoded, "lumewarp-config-validation")
        except VerificationError as exc:
            from argon2.exceptions import VerifyMismatchError

            if not isinstance(exc, VerifyMismatchError):
                raise ValueError("break-glass password hash must be valid argon2id") from None
        except (InvalidHashError, ValueError):
            raise ValueError("break-glass password hash must be valid argon2id") from None


def fingerprint(settings: Settings) -> bytes | None:
    credential = settings.break_glass_password_hash or settings.break_glass_password
    if not credential:
        return None
    kind = "hash:" if settings.break_glass_password_hash else "plaintext:"
    return hmac.digest(
        b"lumewarp-break-glass-credential-v1",
        (kind + credential.get_secret_value()).encode(),
        "sha256",
    )


def client_ip(request: Request, settings: Settings) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        address = ip_address(peer)
        if not any(address in network for network in settings.trusted_proxies):
            return str(address)
        # Walk from the socket peer toward the origin, never past an untrusted hop.
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        for hop in reversed(forwarded.split(",")):
            if not any(address in network for network in settings.trusted_proxies):
                break
            address = ip_address(hop.strip())
        return str(address)
    except ValueError:
        # Malformed forwarding data cannot select an attacker-controlled identity.
        return peer


def client_allowed(ip: str, settings: Settings) -> bool:
    if not settings.break_glass_allowed_cidrs:
        return True
    try:
        return any(ip_address(ip) in network for network in settings.break_glass_allowed_cidrs)
    except ValueError:
        return False


def enabled(settings: Settings) -> bool:
    return bool(settings.break_glass_password_hash or settings.break_glass_password)


def verify(password: str, settings: Settings) -> bool:
    if settings.break_glass_password_hash:
        encoded = settings.break_glass_password_hash.get_secret_value()
        if not encoded.startswith("$argon2id$"):
            return False
        try:
            return PASSWORD_HASHER.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False
    if settings.break_glass_password:
        return hmac.compare_digest(
            hash_secret(password), hash_secret(settings.break_glass_password.get_secret_value())
        )
    return False


@dataclass
class LoginLimiter:
    # Reservations count in-flight verifications too, so concurrent attempts cannot
    # bypass the limit while argon2 runs outside the event loop.
    attempts: OrderedDict[str, tuple[int, float]] = field(default_factory=OrderedDict)
    failures: int = 0
    window_until: float = 0
    audit_until: float = 0

    def reserve(self, ip: str) -> bool:
        now = time.monotonic()
        self.attempts = OrderedDict((ip, row) for ip, row in self.attempts.items() if row[1] > now)
        if now >= self.window_until:
            self.failures = 0
            self.window_until = now + WINDOW_SECONDS
        count, until = self.attempts.get(ip, (0, now + WINDOW_SECONDS))
        if count >= 5:
            return False
        if ip not in self.attempts and len(self.attempts) >= MAX_IPS:
            self.attempts.popitem(last=False)
        self.attempts[ip] = (count + 1, until)
        self.failures += 1
        return True

    def delay(self) -> float:
        return min(MAX_GLOBAL_DELAY_SECONDS, max(0, self.failures - 19) * 0.05)

    def summarize_rejection(self) -> bool:
        now = time.monotonic()
        if now < self.audit_until:
            return False
        self.audit_until = now + WINDOW_SECONDS
        return True

    def success(self, ip: str) -> None:
        self.attempts.pop(ip, None)
        self.failures = max(0, self.failures - 1)
