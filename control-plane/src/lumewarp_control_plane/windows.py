"""Budget windows. P1 has one window kind: the UTC calendar month."""

from datetime import UTC, datetime


def month_window(ts: datetime) -> tuple[datetime, datetime]:
    """Return `[start, end)` of the UTC calendar month containing `ts`."""
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    ts = ts.astimezone(UTC)
    start = datetime(ts.year, ts.month, 1, tzinfo=UTC)
    if ts.month == 12:
        end = datetime(ts.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(ts.year, ts.month + 1, 1, tzinfo=UTC)
    return start, end
