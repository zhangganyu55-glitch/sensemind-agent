"""Tests for the device-neutral telemetry adapter layer."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from guardian.adapters import (DEVICE_TYPES, METRIC_NAMES, reading_from_legacy,
                               readings_from_apk, register_device_type,
                               register_metric, to_legacy_telemetry)
from guardian.apk_models import Measurement
from guardian.models import Telemetry


TIME = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)


def legacy(**changes):
    data = {
        "device_id": "stm32_f103_001", "timestamp": TIME, "source": "stm32",
        "heart_rate": 72.0, "spo2": 98.0, "signal_quality": "good",
        "reading_valid": True,
    }
    data.update(changes)
    return Telemetry(**data)


def apk_sample(**changes):
    data = {
        "sample_id": uuid4(), "metric": "hr", "value": 72.0, "unit": "bpm",
        "observed_at": TIME, "phone_received_at": TIME, "time_basis": "device",
        "quality": "good", "source": "live_device",
    }
    data.update(changes)
    return Measurement(**data)


def test_legacy_reading_round_trip_preserves_identity_time_and_quality():
    original = legacy()
    reading = reading_from_legacy(original)

    assert reading.device_id == "stm32_f103_001"
    assert reading.device_type == "stm32_f103"
    assert reading.source == "stm32"
    assert reading.timestamp == TIME
    assert reading.quality == "good"
    assert reading.metrics["heart_rate"].value == 72.0
    assert reading.metrics["heart_rate"].unit == "bpm"
    assert reading.metrics["spo2"].unit == "%"

    restored = to_legacy_telemetry(reading)
    assert restored is not None
    assert restored.model_dump() == original.model_dump()


def test_invalid_legacy_reading_is_not_passed_back():
    reading = reading_from_legacy(legacy(reading_valid=False, signal_quality="poor"))
    assert reading.quality == "invalid"
    assert to_legacy_telemetry(reading) is None


def test_apk_rows_at_one_timestamp_merge_and_keep_the_worst_quality():
    readings = readings_from_apk("ring-device", [
        apk_sample(),
        apk_sample(metric="spo2", value=97.0, unit="%", quality="poor"),
    ])
    assert len(readings) == 1
    reading = readings[0]
    assert set(reading.metrics) == {"heart_rate", "spo2"}
    assert reading.quality == "poor"
    assert reading.metrics["spo2"].source == "live_device"
    assert len(reading.raw["measurements"]) == 2

    packet = to_legacy_telemetry(reading)
    assert packet is not None
    assert packet.heart_rate == 72.0 and packet.spo2 == 97.0
    assert packet.signal_quality == "poor" and packet.reading_valid is True


def test_apk_rows_with_different_timestamps_stay_separate_and_sorted():
    later = apk_sample(observed_at=TIME + timedelta(seconds=5), value=75.0)
    earlier = apk_sample()
    readings = readings_from_apk("ring-device", [later, earlier])
    assert [reading.timestamp for reading in readings] == [TIME, TIME + timedelta(seconds=5)]


def test_values_the_legacy_store_cannot_represent_are_dropped():
    # The APK protocol allows hr up to 255; the legacy Telemetry contract is 250.
    high = readings_from_apk("ring-device", [apk_sample(value=252.0)])[0]
    assert to_legacy_telemetry(high) is None

    future = readings_from_apk("ring-device", [
        apk_sample(observed_at=datetime.now(timezone.utc) + timedelta(minutes=5))])[0]
    assert to_legacy_telemetry(future) is None

    unknown_transport = reading_from_legacy(legacy(source="simulator"))
    object.__setattr__(unknown_transport, "source", "ble")  # future device transport
    assert to_legacy_telemetry(unknown_transport) is None


def test_metric_and_device_type_registries_are_extensible():
    original_metric = METRIC_NAMES["hr"]
    original_type = DEVICE_TYPES["ring"]
    try:
        register_metric("hr", "heart_rate")
        register_device_type("ring", "ble_ring_mr11")
        assert METRIC_NAMES["hr"] == "heart_rate"
        assert DEVICE_TYPES["ring"] == "ble_ring_mr11"
    finally:
        METRIC_NAMES["hr"] = original_metric
        DEVICE_TYPES["ring"] = original_type


def test_history_metric_keeps_its_source_marker():
    readings = readings_from_apk("ring-device", [
        apk_sample(source="device_history", time_basis="device")])
    assert readings[0].metrics["heart_rate"].source == "device_history"
