"""Window validation and descriptive summaries for the APK contract."""
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_window(start: datetime, end: datetime, timezone_name: str) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Time window must include offsets")
    if not start < end or end - start > timedelta(days=31):
        raise ValueError("Select a positive time window of at most 31 days")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Unknown IANA timezone") from error


def summarize(samples: list[dict], start: datetime, end: datetime, timezone_name: str) -> dict:
    """Describe what was received. Never asserts safety or continuity."""
    validate_window(start, end, timezone_name)
    result = {
        "from": start.isoformat(), "to": end.isoformat(), "timezone": timezone_name,
        "status": "data_available" if samples else "no_data", "metrics": {},
        "continuous_coverage": None, "baseline_status": "not_established",
        "limitations": [
            "Sparse samples do not establish continuous monitoring or absence of events.",
            "Ring signal quality is not clinically validated; values are not diagnoses.",
        ],
    }
    for metric in sorted({row["metric"] for row in samples}):
        records = sorted([row for row in samples if row["metric"] == metric],
                         key=lambda row: row["observed_at"])
        usable = [row for row in records
                  if row["quality"] != "invalid"
                  and row["source"] in ("live_device", "device_history")]
        values = [float(row["value"]) for row in usable]
        times = [datetime.fromisoformat(row["observed_at"]) for row in usable]
        intervals = [(later - earlier).total_seconds() for earlier, later in zip(times, times[1:])]
        first = datetime.fromisoformat(records[0]["observed_at"])
        last = datetime.fromisoformat(records[-1]["observed_at"])
        result["metrics"][metric] = {
            "count": len(records), "usable_numeric_count": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "median": median(values) if values else None,
            "unit": records[0]["unit"],
            "first": records[0]["observed_at"], "last": records[-1]["observed_at"],
            "maximum_interval_seconds": max(intervals) if intervals else None,
            "leading_empty_seconds": (first - start).total_seconds(),
            "trailing_empty_seconds": (end - last).total_seconds(),
            "unknown_quality_count": sum(row["quality"] == "unknown" for row in records),
            "phone_time_count": sum(row["time_basis"] == "phone_receive" for row in records),
            "sources": sorted({row["source"] for row in records}),
        }
    return result
