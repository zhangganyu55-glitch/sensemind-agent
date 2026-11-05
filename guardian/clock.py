"""Injectable clocks for the research prototype.

The service deliberately keeps three time concepts separate:

* ``sample_time``   -- the device/sample timestamp (``Telemetry.timestamp``);
* ``received_at``   -- the server's UTC wall-clock time when a packet is
  accepted (persisted for audit and freshness checks);
* ``monotonic``     -- a process-local elapsed-time clock used for dwell
  windows.  It is not persisted and resets when the process restarts.

Only the monotonic clock is trusted for measuring how long a condition has
been observed.  Server UTC is used for persisted event lifecycle timestamps.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
import time


def utc(value: datetime) -> datetime:
    """Normalise an aware/naive datetime to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current server UTC time."""

    def monotonic(self) -> float:
        """Return a process-monotonic elapsed-time value in seconds."""


class SystemClock:
    """Production clock backed by the operating system."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """Deterministic clock for tests and replay harnesses.

    ``advance`` moves both UTC and monotonic time together.  Tests can also
    move them independently with ``set_now`` / ``set_monotonic`` to model a
    server clock change without a real elapsed-time change.
    """

    def __init__(self, now: datetime, monotonic: float = 0.0):
        self._now = utc(now)
        self._monotonic = float(monotonic)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += float(seconds)

    def set_now(self, now: datetime) -> None:
        self._now = utc(now)

    def set_monotonic(self, value: float) -> None:
        self._monotonic = float(value)
