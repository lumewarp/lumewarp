"""Emergency password authentication and process-local failed-login backoff."""

import hmac
import time
from dataclasses import dataclass, field

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from lumewarp_control_plane.security import hash_secret
from lumewarp_control_plane.settings import Settings

PASSWORD_HASHER = PasswordHasher(type=Type.ID)


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
    attempts: dict[str, tuple[int, float]] = field(default_factory=dict)
    failures: int = 0
    blocked_until: float = 0
    window_until: float = 0

    def reserve(self, ip: str) -> bool:
        now = time.monotonic()
        self.attempts = {ip: row for ip, row in self.attempts.items() if row[1] > now}
        if now >= self.window_until:
            self.failures = 0
            self.window_until = now + 60
        count, until = self.attempts.get(ip, (0, now + 60))
        if now < self.blocked_until or count >= 5:
            return False
        self.attempts[ip] = (count + 1, until)
        self.failures += 1
        if self.failures >= 20:
            self.blocked_until = now + min(60, 2 ** min(self.failures - 20, 6))
        return True

    def success(self, ip: str) -> None:
        self.attempts.pop(ip, None)
        self.failures = max(0, self.failures - 1)
