#!/usr/bin/env python3
"""Sample cumulative CPU% and memory (PSS) of a process tree from /proc.

Used by bench/scripts/run.sh to record the resource cost of whichever server is
under load. Sums the whole tree because LiteLLM (`--num_workers`) forks many
worker processes and `uvx` is a Python wrapper around the real server.

Memory is proportional set size from `/proc/<pid>/smaps_rollup` (PSS, so pages
shared between LiteLLM workers are not double-counted). If smaps_rollup cannot
be read, RSS from `/proc/<pid>/statm` is used instead.

Stdlib only; run as `python3 -I proc_sampler.py --pid N --out file.csv`.
Writes CSV rows `elapsed_s,cpu_pct,pss_kb,nprocs` once per interval until
SIGTERM/SIGINT, then exits 0. Exits 1 if the root process does not exist at
startup.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

_stop = False


def _handle_signal(_signum: int, _frame: object) -> None:
    global _stop
    _stop = True


def read_stat(pid: int) -> tuple[int, int] | None:
    """Return (ppid, utime+stime in clock ticks) for `pid`, or None if gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # comm may contain spaces/parens; split on the *last* ')'.
    rparen = data.rfind(")")
    if rparen < 0:
        return None
    fields = data[rparen + 2 :].split()
    # fields[0]=state, fields[1]=ppid, fields[11]=utime, fields[12]=stime
    try:
        return int(fields[1]), int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def read_rss_kb(pid: int) -> int:
    """RSS from statm, used only when smaps_rollup is unreadable."""
    try:
        with open(f"/proc/{pid}/statm", "rb") as fh:
            fields = fh.read().split()
        # /proc/pid/statm: size resident shared text lib data dt (in pages)
        return int(fields[1]) * PAGE_SIZE // 1024
    except (OSError, IndexError, ValueError):
        return 0


def read_pss_kb(pid: int) -> int:
    """PSS from smaps_rollup (falls back to RSS if unreadable)."""
    try:
        with open(f"/proc/{pid}/smaps_rollup", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return read_rss_kb(pid)


def snapshot() -> dict[int, tuple[int, int]]:
    stats: dict[int, tuple[int, int]] = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            stat = read_stat(int(name))
            if stat is not None:
                stats[int(name)] = stat
    return stats


def tree_pids(root: int, stats: dict[int, tuple[int, int]]) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _ticks) in stats.items():
        children.setdefault(ppid, []).append(pid)
    out: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid not in stats:
            continue
        out.append(pid)
        stack.extend(children.get(pid, ()))
    return out


def tree_usage(root: int, stats: dict[int, tuple[int, int]]) -> tuple[int, int, int]:
    """Return (ticks, pss_kb, nprocs) summed over the process tree."""
    pids = tree_pids(root, stats)
    ticks = sum(stats[pid][1] for pid in pids)
    pss_kb = sum(read_pss_kb(pid) for pid in pids)
    return ticks, pss_kb, len(pids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="root of the tree to sample")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    parser.add_argument("--out", required=True, help="CSV output path")
    args = parser.parse_args()

    if not os.path.isdir(f"/proc/{args.pid}"):
        print(f"proc_sampler: pid {args.pid} does not exist", file=sys.stderr)
        return 1

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    start = time.monotonic()
    prev_time = start
    prev_ticks: int | None = None

    with open(args.out, "w", encoding="utf-8") as out:
        out.write("elapsed_s,cpu_pct,pss_kb,nprocs\n")
        out.flush()
        while not _stop:
            stats = snapshot()
            if args.pid not in stats and prev_ticks is not None:
                # Root exited; report what is left of the tree once and stop.
                break
            ticks, pss_kb, nprocs = tree_usage(args.pid, stats)
            now = time.monotonic()
            if prev_ticks is None:
                cpu_pct = 0.0
            else:
                dt = now - prev_time
                cpu_pct = ((ticks - prev_ticks) / CLK_TCK / dt * 100.0) if dt > 0 else 0.0
                # A worker may exit between samples; never report negative CPU.
                cpu_pct = max(0.0, cpu_pct)
            out.write(f"{now - start:.3f},{cpu_pct:.2f},{pss_kb},{nprocs}\n")
            out.flush()
            prev_ticks = ticks
            prev_time = now
            # Sleep in small slices so SIGTERM is handled promptly.
            deadline = now + args.interval
            while not _stop and time.monotonic() < deadline:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
