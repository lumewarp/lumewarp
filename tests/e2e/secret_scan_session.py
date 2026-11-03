"""Fail-closed run context for the plaintext-credential scan.

The e2e and load scripts export `E2E_RUN_DIR`, `E2E_RUN_ID`, `E2E_SECRET_LOG` and
`E2E_SCAN_REPORT`. A direct `pytest` run has none of them; this module creates a run
directory (with a run id, the manifest files and the recorded-secret log) so `Stack`
teardown still scans, and finalizes the scan at the end of the session.

The only opt-out is `E2E_SKIP_SECRET_SCAN=1`; the end of the run then prints a warning.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

OWN_RUN_DIR_ENV = "LUMEWARP_E2E_SCAN_OWN_RUN_DIR"


def ensure_run_context() -> None:
    """Create or complete the run context before the first Stack is built."""
    from secret_scan import (
        RUN_ID_ENV,
        RUN_ID_FILE,
        SCAN_REPORT_ENV,
        SECRET_LOG_ENV,
        scan_disabled,
    )

    if scan_disabled():
        return
    run_dir = os.environ.get("E2E_RUN_DIR")
    if not run_dir:
        run_dir = tempfile.mkdtemp(prefix="lumewarp-e2e-scan-")
        os.environ["E2E_RUN_DIR"] = run_dir
        os.environ[OWN_RUN_DIR_ENV] = "1"
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = os.environ.get(RUN_ID_ENV)
    run_id_path = directory / RUN_ID_FILE
    if not run_id:
        run_id = (
            run_id_path.read_text().strip()
            if run_id_path.exists()
            else uuid.uuid4().hex[:8]
        )
        os.environ[RUN_ID_ENV] = run_id
    if not run_id_path.exists() or run_id_path.read_text().strip() != run_id:
        run_id_path.write_text(run_id + "\n")
    for name in ("pids", "dbs"):
        (directory / name).touch()
    # Logs are set to a file even when the run issues no credential: a scan with a missing
    # log is a hard failure, so its absence must not be the normal case.
    os.environ.setdefault(SECRET_LOG_ENV, str(directory / "secrets.jsonl"))
    os.environ.setdefault(SCAN_REPORT_ENV, str(directory / "secret-scans.jsonl"))
    Path(os.environ[SECRET_LOG_ENV]).touch()
    Path(os.environ[SCAN_REPORT_ENV]).touch()


def finish_run_context(exitstatus: int) -> int:
    """Scan and verify the run's databases; return the pytest exit status to use."""
    from secret_scan import finalize, scan_disabled
    from stack import PG_DSN

    if scan_disabled():
        print(
            "WARNING: E2E_SKIP_SECRET_SCAN=1 disabled the plaintext-credential scan;"
            " no database was searched for credentials",
            file=sys.stderr,
        )
        return exitstatus
    run_dir = os.environ.get("E2E_RUN_DIR")
    if not run_dir:
        print("secret scan: there is no run directory; nothing could be scanned", file=sys.stderr)
        return 1
    pg_url = os.environ.get("E2E_PG_URL", PG_DSN)
    try:
        code = finalize(run_dir, pg_url)
    except Exception as err:  # noqa: BLE001 - any scanner failure fails the run
        print(f"secret scan failed: {type(err).__name__}: {err}", file=sys.stderr)
        code = 1
    finally:
        if os.environ.get(OWN_RUN_DIR_ENV) == "1":
            shutil.rmtree(run_dir, ignore_errors=True)
    return 1 if code else exitstatus
