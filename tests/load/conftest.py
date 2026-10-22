import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "e2e"))
sys.path.insert(0, str(HERE))

from stack import GATEWAY_BIN, MOCK_BIN, Proc, Stack  # noqa: E402

from loadlib import RESULTS  # noqa: E402


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture(scope="session", autouse=True)
def binaries() -> None:
    for binary in (GATEWAY_BIN, MOCK_BIN):
        if not binary.exists():
            pytest.exit(
                f"missing {binary}; build it first (scripts/load-e2e.sh does)", returncode=2
            )


@pytest.fixture
def stack(request):
    s = Stack()
    if wrapper := os.environ.get("LOAD_CP_WRAPPER"):
        # Run the control plane through a script (profiler, coverage, a local patch); it
        # receives the same CLI arguments as `python -m lumewarp_control_plane`.
        s.cp = Proc("control-plane", [sys.executable, wrapper, "serve"], s.cp_env, s.dir)
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


def _fmt(d: dict | None) -> str:
    if not d or not d.get("n"):
        return "-"
    return f"{d['p50']} / {d['p90']} / {d['p99']} / {d['max']}"


def pytest_sessionfinish(session, exitstatus) -> None:
    reports = sorted(RESULTS.glob("*.json")) if RESULTS.exists() else []
    if not reports:
        return
    lines = [
        "# Load e2e results",
        "",
        "| scenario | requests | ok RPS | chat p50/p90/p99/max ms"
        " | stream p50/p90/p99/max ms | statuses |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    details = []
    for path in reports:
        data = json.loads(path.read_text())
        load = data.get("load", {})
        lines.append(
            f"| {path.stem} | {load.get('requests')} | {load.get('rps_ok')} | "
            f"{_fmt(load.get('latency_ms'))} | {_fmt(load.get('stream_latency_ms'))} | "
            f"{load.get('statuses')} |"
        )
        extra = {k: v for k, v in data.items() if k != "load"}
        details.append(f"## {path.stem}\n\n```json\n{json.dumps(extra, indent=2)}\n```\n")
    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n\n" + "\n".join(details))
    print("\n" + "\n".join(lines))
    print(f"\nload e2e report: {RESULTS / 'summary.md'}")
