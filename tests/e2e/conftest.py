import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from stack import GATEWAY_BIN, MOCK_BIN, Stack  # noqa: E402

LIVE_IDP_TESTS = ["test_p2_exit_gate.py", "test_p25_self_service.py"]
collect_ignore = [] if os.environ.get("E2E_P2") == "1" else LIVE_IDP_TESTS


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture(scope="session")
def binaries() -> None:
    for binary in (GATEWAY_BIN, MOCK_BIN):
        if not binary.exists():
            pytest.exit(f"missing {binary}; build it first (scripts/e2e-p1.sh does)", returncode=2)


@pytest.fixture
def stack(request, binaries):
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
        try:
            s.teardown()
        finally:
            # teardown raises on a secret-scan hit; the directory still has to go.
            s.cleanup_dir()
