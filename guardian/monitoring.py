"""Device monitoring state derived from the latest telemetry sample.

States are intentionally separate from rule-event status:

* ``NORMAL``  -- fresh, valid, good-quality telemetry.
* ``UNKNOWN`` -- data arrived but quality/validity/sampling status is unknown.
* ``STALE``   -- no recent data, or the device reports stalled/disconnected.
* ``NO_DATA`` -- the service has never seen telemetry for this device.

A rule event is never resolved because of ``UNKNOWN``/``STALE``/``NO_DATA``.
Event resolution is handled by the rule engine's close dwell, only after a
continuous run of good samples.  This module only records the monitoring
dimension and the last valid sample time.  Future-dated samples are rejected
input and do not move ``last_seen_at`` or ``last_valid_at``.
"""
from __future__ import annotations

from datetime import datetime, timezone

NORMAL = "NORMAL"
UNKNOWN = "UNKNOWN"
STALE = "STALE"
NO_DATA = "NO_DATA"

STATES = (NORMAL, UNKNOWN, STALE, NO_DATA)
DEFAULT_STALE_SECONDS = 30.0
DEFAULT_FUTURE_SECONDS = 5.0
DEFAULT_MAX_SAMPLE_LAG_SECONDS = 5.0


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reading_dict(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return None


def classify(reading, now: datetime | None = None,
             stale_seconds: float = DEFAULT_STALE_SECONDS,
             max_future_seconds: float = DEFAULT_FUTURE_SECONDS
             ) -> tuple[str, str | None, datetime | None]:
    """Return ``(state, reason, sample_timestamp)`` for one latest reading."""
    moment = now or datetime.now(timezone.utc)
    data = _reading_dict(reading)
    if data is None:
        return NO_DATA, "no_telemetry", None
    timestamp = _parse(data.get("timestamp"))
    if timestamp is None:
        return UNKNOWN, "invalid_timestamp", None
    age = (moment - timestamp).total_seconds()
    if age < -max_future_seconds:
        return UNKNOWN, "clock_skew", timestamp
    if age > stale_seconds:
        return STALE, "stale_input", timestamp

    status = (data.get("device_status") or {}).get("sampling_status")
    if status in ("stalled", "disconnected"):
        return STALE, "device_not_sampling", timestamp
    if status == "pending":
        return UNKNOWN, "sampling_pending", timestamp

    quality = data.get("signal_quality", "unknown")
    if quality == "poor":
        return UNKNOWN, "signal_poor", timestamp
    if quality == "invalid":
        return UNKNOWN, "signal_invalid", timestamp
    if quality == "unknown":
        return UNKNOWN, "signal_unknown", timestamp
    if data.get("reading_valid") is not True:
        return UNKNOWN, "reading_invalid", timestamp
    return NORMAL, None, timestamp


def record(storage, device_id: str, reading, now: datetime | None = None,
           stale_seconds: float = DEFAULT_STALE_SECONDS,
           max_future_seconds: float = DEFAULT_FUTURE_SECONDS,
           max_sample_lag_seconds: float | None = None) -> dict:
    """Persist the current monitoring state and last valid sample time.

    ``max_sample_lag_seconds`` is the live-ingestion lag bound.  A sample that
    was already older than that bound when the server received it is backlog,
    not live evidence: it must not refresh ``last_seen_at``/``last_valid_at``
    or flip a derived STALE state back to NORMAL.  The row itself remains in
    telemetry history.
    """
    moment = now or datetime.now(timezone.utc)
    state, reason, timestamp = classify(
        reading, moment, stale_seconds, max_future_seconds)
    if reason == "clock_skew":
        # A future timestamp is rejected input, not a device state update.
        # Keep the last accepted monitoring state and last_valid_at intact.
        return storage.monitoring(
            device_id, now=moment, stale_seconds=stale_seconds)
    if max_sample_lag_seconds is not None and timestamp is not None:
        age = (moment - timestamp).total_seconds()
        if age > max_sample_lag_seconds:
            # Delayed/backlog input: keep the previous live state.
            return storage.monitoring(
                device_id, now=moment, stale_seconds=stale_seconds)
    valid_at = timestamp if state == NORMAL else None
    return storage.update_monitoring(
        device_id, state=state, reason=reason,
        last_seen_at=timestamp, last_valid_at=valid_at)
