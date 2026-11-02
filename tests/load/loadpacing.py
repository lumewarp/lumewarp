"""Stdlib-only helpers for the Locust processes, importable without Locust so they can be unit
tested. Both work on CLOCK_MONOTONIC, which a wall clock step does not move and which every
process on the machine shares with the orchestrator.
"""

import json
import logging
import os
import time
from collections.abc import Callable

CPU_WARNING = "CPU usage above 90%"
CPU_WARNING_FILES = "cpu-warnings-*.jsonl"


class Pacer:
    """locust.constant_throughput on the monotonic clock: every call returns what is left of the
    interval after the task that just ran. Locust's own reads time.time(), so a step back of N
    seconds stalls every user for N seconds, on each step. Create it with the user, as Locust
    does (User.__init__ sets _cp_last_run), or the first task's run time is not deducted."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = interval
        self.clock = clock
        self.last_run = clock()
        self.last_wait = 0.0

    def next(self) -> float:
        now = self.clock()
        ran = now - self.last_run - self.last_wait
        self.last_wait = max(0.0, self.interval - ran)
        self.last_run = now
        return self.last_wait


class CpuWarningRecorder(logging.Handler):
    """Writes the monotonic time of each Locust CPU warning to `cpu-warnings-<pid>.jsonl`, when
    it is logged. Locust's log carries wall clock stamps, which cannot be placed in a monotonic
    step once the wall clock moved. Attach it after the process forked and set up logging."""

    def __init__(self, directory: str, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(logging.WARNING)
        self.directory = directory
        self.clock = clock

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if CPU_WARNING not in record.getMessage():
                return
            path = os.path.join(self.directory, f"cpu-warnings-{os.getpid()}.jsonl")
            with open(path, "a") as out:
                out.write(json.dumps({"t": self.clock()}) + "\n")
        except Exception:  # noqa: BLE001 - logging must not break the load
            self.handleError(record)
