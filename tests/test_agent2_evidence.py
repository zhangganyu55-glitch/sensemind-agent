"""Agent 2.0: evidence model, availability, provenance and alignment."""
from datetime import datetime, timedelta, timezone

from guardian.evidence import Evidence, EvidenceEngine
from guardian.models import Telemetry
from guardian.storage import Storage


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def make_event(storage, *, device="guardian_001", kind="high_heart_rate", when=None):
    transition = storage.event_open(
        device, kind, when or (NOW - timedelta(seconds=5)),
        "DEMO ONLY: unverified rule; not diagnosis")
    return storage.get_event(transition["event_id"])


def put_reading(storage, when, *, device="guardian_001", hr=75, quality="good",
                source="simulator", valid=True, dataset=None):
    storage.put_telemetry(Telemetry(
        device_id=device, timestamp=when, source=source, heart_rate=hr,
        spo2=98, acc_x=0.0, acc_y=0.0, acc_z=1.0,
        signal_quality=quality, reading_valid=valid, dataset=dataset))


def test_evidence_has_required_fields_and_is_idempotent(tmp_path):
    storage = Storage(str(tmp_path / "evidence.db"))
    event = make_event(storage)
    put_reading(storage, NOW - timedelta(seconds=10))
    storage.put_ml_inference({
        "device_id": "guardian_001", "window_start": iso(NOW - timedelta(seconds=20)),
        "window_end": iso(NOW - timedelta(seconds=10)), "dataset": "ppg-dalia",
        "participant_id": "S13", "sampling_hz": 64.0,
        "activity_label": "walking", "activity_confidence": 0.9,
        "signal_reliability": 0.82, "anomaly_score": 0.18,
        "model_version": "ppg-dalia-rf-test", "available": True,
        "activity": {"available": True}, "signal": {"available": True,
                                                    "estimated_hr": 71.0,
                                                    "reliability": 0.82},
    })
    engine = EvidenceEngine(storage)
    first = engine.collect_for_event(event)
    second = engine.collect_for_event(event)

    assert first.evidence
    ids = [item.evidence_id for item in first.evidence]
    assert ids == [item.evidence_id for item in second.evidence]
    for item in first.evidence:
        assert item.evidence_id.startswith("ev_")
        assert item.event_id == event["id"]
        assert item.device_id == "guardian_001"
        assert item.source_type
        assert item.quality_status in ("good", "poor", "unknown", "invalid", "unavailable")
        assert item.data_mode in ("live", "replay", "unknown")
        assert item.source_ref
    assert storage.evidence_for_event(event["id"])
    assert any(item.source_type == "heart_rate_estimate" for item in first.evidence)
    assert any(item.source_type == "activity" and item.value == "walking"
               for item in first.evidence)


def test_missing_sensors_are_explicitly_unavailable(tmp_path):
    storage = Storage(str(tmp_path / "missing.db"))
    event = make_event(storage)
    put_reading(storage, NOW - timedelta(seconds=5))
    bundle = EvidenceEngine(storage).collect_for_event(event)
    availability = {item.source_type: item for item in bundle.availability}

    assert availability["heart_rate_estimate"].available is True
    assert availability["imu_raw"].available is True
    assert availability["radar_status"].available is False
    assert availability["radar_status"].reason == "sensor_not_connected"
    assert availability["breathing_rate"].available is False
    assert availability["ppg"].available is False
    assert availability["ppg"].reason == "raw_ppg_not_ingested"
    assert all(item.reason for item in bundle.availability if not item.available)


def test_replay_data_is_never_labelled_live(tmp_path):
    storage = Storage(str(tmp_path / "mode.db"))
    event = make_event(storage)
    put_reading(storage, NOW - timedelta(seconds=5), source="simulator")
    bundle = EvidenceEngine(storage).collect_for_event(event)
    assert bundle.data_modes == ["replay"]
    assert all(item.data_mode == "replay" for item in bundle.evidence)

    live_storage = Storage(str(tmp_path / "live.db"))
    live_event = make_event(live_storage, device="guardian_002")
    put_reading(live_storage, NOW - timedelta(seconds=5),
                device="guardian_002", source="stm32")
    live = EvidenceEngine(live_storage).collect_for_event(live_event)
    assert live.data_modes == ["live"]


def test_cross_device_alignment_respects_clock_uncertainty(tmp_path):
    storage = Storage(str(tmp_path / "align.db"))
    event = make_event(storage)
    put_reading(storage, NOW - timedelta(seconds=5))
    radar = Evidence(
        evidence_id="ev_radar", event_id=event["id"], device_id="radar_001",
        source_type="radar_status", timestamp=NOW - timedelta(seconds=5),
        value={"target": "present"}, unit=None, quality_status="good",
        data_mode="replay", time_sync_status="synced",
        clock_uncertainty_ms=50, synthetic=True, source_ref="radar:1")
    bundle = EvidenceEngine(storage).collect_for_event(event, extra=[radar])
    assert bundle.alignment["fusion_allowed"] is True

    unsynced = radar.model_copy(update={"evidence_id": "ev_radar_2",
                                        "time_sync_status": "unsynced",
                                        "clock_uncertainty_ms": 9000,
                                        "source_ref": "radar:2"})
    blocked = EvidenceEngine(storage).collect_for_event(event, extra=[unsynced])
    assert blocked.alignment["fusion_allowed"] is False
    assert "unsynced" in blocked.alignment["reason"] or "uncertainty" in blocked.alignment["reason"]


def test_evidence_is_event_and_device_scoped(tmp_path):
    storage = Storage(str(tmp_path / "scope.db"))
    event = make_event(storage)
    put_reading(storage, NOW - timedelta(seconds=10))
    put_reading(storage, NOW - timedelta(seconds=9), device="other_device")
    bundle = EvidenceEngine(storage).collect_for_event(event)
    assert all(item.event_id == event["id"] for item in bundle.evidence)
    assert all(item.device_id == "guardian_001" for item in bundle.evidence)
    assert "other_device" not in str([item.value for item in bundle.evidence])


def test_follow_up_collection_can_be_bounded_by_since(tmp_path):
    storage = Storage(str(tmp_path / "since.db"))
    event = make_event(storage)
    put_reading(storage, NOW - timedelta(seconds=20), hr=70)
    engine = EvidenceEngine(storage)
    first = engine.collect_for_event(event)
    cutoff = NOW - timedelta(seconds=12)
    put_reading(storage, NOW - timedelta(seconds=8), hr=110)
    later = engine.collect_for_event(event, since=cutoff)
    old_refs = {item.source_ref for item in first.evidence}
    new_refs = {item.source_ref for item in later.evidence}
    assert new_refs - old_refs
    assert all(":8" in ref or "hr" in ref or "quality" in ref or "imu" in ref
               for ref in new_refs - old_refs)


def test_synthetic_physical_evidence_must_be_marked_synthetic(tmp_path):
    storage = Storage(str(tmp_path / "synthetic.db"))
    event = make_event(storage)
    fake_live = Evidence(
        evidence_id="ev_fake", event_id=event["id"], device_id="guardian_001",
        source_type="radar_status", timestamp=NOW, value="target_present",
        unit=None, quality_status="good", data_mode="live",
        time_sync_status="synced", clock_uncertainty_ms=10,
        synthetic=False, source_ref="radar:fake")
    try:
        EvidenceEngine(storage).collect_for_event(event, extra=[fake_live])
    except ValueError as error:
        assert "hardware source" in str(error)
    else:
        raise AssertionError("live physical evidence without hardware must be rejected")
