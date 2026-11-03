"""Credential-safe negative assertions.

pytest's assertion rewriting expands the operands of `assert secret not in text` into its
failure report, so a real credential can land in a CI log. This helper computes the
result itself and fails with the context and the position only, never the value.
"""

from __future__ import annotations


def assert_no_secret(haystack, secrets, what: str) -> None:
    """Fail if any value in `secrets` appears in `haystack` (str or bytes).

    `what` names the kind of place searched (an audit row, an exported span, ...). The
    failure message carries the index and position, never the secret or its surroundings.
    """
    import pytest

    if isinstance(haystack, str):
        needles = [
            (index, secret if isinstance(secret, str) else secret.decode())
            for index, secret in enumerate(secrets)
        ]
    else:
        needles = [
            (index, secret if isinstance(secret, bytes) else secret.encode())
            for index, secret in enumerate(secrets)
        ]
    for index, needle in needles:
        at = haystack.find(needle)
        if at >= 0:
            pytest.fail(
                f"{what}: secret #{index} appears at position {at} ({len(needle)} long)",
                pytrace=False,
            )
