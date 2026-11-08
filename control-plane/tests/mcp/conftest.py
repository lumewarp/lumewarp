"""Fixtures shared with the compiler tests: a cloned, migrated database per test.

Catalog ingestion publishes config versions and its race test needs two sessions, so it
uses the same committed-row factory the compiler tests use.
"""

from tests.compiler.conftest import factory, template_db  # noqa: F401
