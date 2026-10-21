import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from stack import GATEWAY_BIN, MOCK_BIN, Stack  # noqa: E402


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture(scope="session", autouse=True)
def binaries() -> None:
    for binary in (GATEWAY_BIN, MOCK_BIN):
        if not binary.exists():
            pytest.exit(f"missing {binary}; build it first (scripts/e2e-p1.sh does)", returncode=2)


@pytest.fixture
def stack(request):
    s = Stack()
    try:
        s.setup()
        yield s
    finally:
        failed = any(
            getattr(request.node, f"rep_{when}", None) is not None
            and getattr(request.node, f"rep_{when}").failed
            for when in ("setup", "call")
        ) or not hasattr(request.node, "rep_call")
        if failed:
            print("\n" + s.logs(), file=sys.stderr)
        s.teardown()
        s.cleanup_dir()
