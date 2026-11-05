"""Device-neutral telemetry adapters.

The APK contract and the legacy STM32/simulator store keep their own wire
formats.  This module is the single translation point: both formats become one
`UnifiedReading`, and only representable readings are converted back to the
legacy `Telemetry` row the Agent and rule engine understand.

Nothing here persists data or changes either external contract.
"""
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from .apk_models import Measurement
from .models import Telemetry


QUALITY_ORDER = {"good": 0, "poor": 1, "unknown": 2, "invalid": 3}

# Wire metric name -> canonical name. Extend via register_metric() for new devices.
METRIC_NAMES = {
    "hr": "heart_rate", "spo2": "spo2", "bp_sys": "bp_sys", "bp_dia": "bp_dia",
    "bp_hr": "bp_hr", "temp": "temp", "glu": "glu", "stress": "stress",
    "hrv": "hrv", "pi": "pi",
}

# Transport source -> device family. Extend via register_device_type().
DEVICE_TYPES = {
    "simulator": "simulator_v1",
    "stm32": "stm32_f103",
    "ring": "ble_ring_mr11",
}

LEGACY_SOURCES = ("simulator", "stm32", "ring")


def register_metric(wire_name: str, canonical: str) -> None:
    METRIC_NAMES[wire_name] = canonical


def register_device_type(source: str, device_type: str) -> None:
    DEVICE_TYPES[source] = device_type


def worst_quality(values: list[str]) -> str:
    return max(values, key=lambda value: QUALITY_ORDER.get(value, QUALITY_ORDER["unknown"]),
               default="unknown")


@dataclass(frozen=True)
class MetricSample:
    value: float
    unit: str
    quality: str
    source: str
    raw_metric: str


@dataclass(frozen=True)
class UnifiedReading:
    """One observation time for one device, independent of transport."""

    device_id: str
    device_type: str
    source: str
    timestamp: datetime
    metrics: dict[str, MetricSample]
    quality: str
    raw: dict


def readings_from_apk(device_id: str, measurements: list[Measurement]) -> list[UnifiedReading]:
    """Merge per-metric APK rows that share an observation time.

    The APK uploads each metric as its own row; merging by timestamp keeps
    concurrent signals (e.g. hr + spo2) visible to the rule engine without
    changing the APK contract.
    """
    groups: dict[tuple[datetime, str], list[Measurement]] = {}
    for measurement in measurements:
        key = (measurement.observed_at, measurement.source)
        groups.setdefault(key, []).append(measurement)

    readings = []
    for (observed_at, data_source), group in groups.items():
        metrics = {}
        for measurement in group:
            canonical = METRIC_NAMES.get(measurement.metric, measurement.metric)
            candidate = MetricSample(
                value=float(measurement.value), unit=measurement.unit,
                quality=measurement.quality, source=data_source,
                raw_metric=measurement.metric,
            )
            previous = metrics.get(canonical)
            if previous is None or (QUALITY_ORDER.get(candidate.quality, 2)
                                    > QUALITY_ORDER.get(previous.quality, 2)):
                metrics[canonical] = candidate
        readings.append(UnifiedReading(
            device_id=device_id, device_type=DEVICE_TYPES.get("ring", "ring"),
            source="ring", timestamp=observed_at, metrics=metrics,
            quality=worst_quality([sample.quality for sample in metrics.values()]),
            raw={"measurements": [item.model_dump(mode="json") for item in group]},
        ))
    return sorted(readings, key=lambda reading: reading.timestamp)


def reading_from_legacy(telemetry: Telemetry) -> UnifiedReading:
    """Describe an existing STM32/simulator packet in the neutral model."""
    quality = telemetry.signal_quality if telemetry.reading_valid else "invalid"
    metrics = {}
    for field, canonical in (("heart_rate", "heart_rate"), ("spo2", "spo2")):
        value = getattr(telemetry, field)
        if value is not None:
            metrics[canonical] = MetricSample(
                value=float(value), unit="bpm" if canonical == "heart_rate" else "%",
                quality=quality, source=telemetry.source, raw_metric=field,
            )
    return UnifiedReading(
        device_id=telemetry.device_id,
        device_type=DEVICE_TYPES.get(telemetry.source, "unknown"),
        source=telemetry.source, timestamp=telemetry.timestamp,
        metrics=metrics, quality=quality,
        raw=telemetry.model_dump(mode="json"),
    )


def to_legacy_telemetry(reading: UnifiedReading) -> Telemetry | None:
    """Convert a reading only when the legacy row can represent it faithfully.

    Unknown transports, invalid quality and values outside the legacy limits are
    dropped here; the original APK rows remain in their own tables.
    """
    if reading.source not in LEGACY_SOURCES:
        return None
    if reading.quality == "invalid":
        return None
    heart_rate = reading.metrics.get("heart_rate")
    spo2 = reading.metrics.get("spo2")
    if heart_rate is None and spo2 is None:
        return None
    quality = reading.quality if reading.quality in ("good", "poor", "unknown") else "unknown"
    try:
        return Telemetry(
            device_id=reading.device_id, timestamp=reading.timestamp,
            source=reading.source,
            heart_rate=heart_rate.value if heart_rate else None,
            spo2=spo2.value if spo2 else None,
            signal_quality=quality,
            reading_valid=quality in ("good", "poor"),
        )
    except ValidationError:
        return None
