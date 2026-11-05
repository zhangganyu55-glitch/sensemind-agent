"""Agent 2.0: ten explicitly synthetic multimodal scenarios."""
from datetime import datetime, timedelta, timezone

import pytest

from guardian.evidence import EvidenceEngine
from guardian.reasoner import deterministic_structured
from guardian.storage import Storage
from guardian.synthetic import SCENARIOS, build


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def make_event(storage, kind):
    transition = storage.event_open("guardian_001", kind, NOW - timedelta(seconds=5),
                                    "synthetic scenario; not diagnosis")
    return storage.get_event(transition["event_id"])


def bundle_for(tmp_path, name):
    storage = Storage(str(tmp_path / f"{name}.db"))
    placeholder = {"id": "placeholder", "device_id": "guardian_001",
                   "opened_at": (NOW - timedelta(seconds=5)).isoformat()}
    kind = build(name, placeholder).event_kind
    event = make_event(storage, kind)
    scenario = build(name, event)
    for reading in scenario.telemetry:
        storage.put_telemetry(reading)
    bundle = EvidenceEngine(storage).collect_for_event(event, extra=scenario.extra)
    return storage, event, scenario, bundle


def sources(bundle):
    return {item.source_type for item in bundle.evidence}


def availability(bundle):
    return {item.source_type: item for item in bundle.availability}


@pytest.mark.parametrize("name", [
    "hr_with_motion", "hr_poor_ppg", "radar_target_loss",
    "radar_breathing_interruption", "wearable_offline",
    "timestamp_mismatch", "contradictory", "normal_turning",
])
def test_synthetic_evidence_scenarios(tmp_path, name):
    _, event, scenario, bundle = bundle_for(tmp_path, name)
    expected = scenario.expected

    if "sources" in expected:
        assert expected["sources"] <= sources(bundle)
    if "quality" in expected:
        for source_type, quality in expected["quality"].items():
            assert any(item.source_type == source_type and
                       item.quality_status == quality for item in bundle.evidence)
    if "fusion_allowed" in expected:
        assert bundle.alignment["fusion_allowed"] is expected["fusion_allowed"]
    if "alignment_reason" in expected:
        assert expected["alignment_reason"] in (bundle.alignment.get("reason") or "")
    if "missing" in expected:
        unavailable = {key for key, item in availability(bundle).items()
                       if not item.available}
        assert expected["missing"] <= unavailable
    if "radar_value" in expected:
        radar = next(item for item in bundle.evidence
                     if item.source_type == "radar_status")
        assert radar.value == expected["radar_value"]
    if "device_online_value" in expected:
        online = next(item for item in bundle.evidence
                      if item.source_type == "device_online")
        assert online.value is expected["device_online_value"]

    conflicts = []
    if expected.get("conflict"):
        hr = next(item for item in bundle.evidence
                  if item.source_type == "heart_rate_estimate")
        radar = next(item for item in bundle.evidence
                     if item.source_type == "radar_status")
        conflicts = [f"心率证据 {hr.evidence_id} 与雷达证据 {radar.evidence_id} 相矛盾。"]
    structured = deterministic_structured(event, bundle, conflicts=conflicts)
    assert structured.event_id == event["id"]
    assert structured.investigation_status in ("completed", "inconclusive")
    if expected.get("uncertainty_marker"):
        assert any(expected["uncertainty_marker"] in item
                   for item in structured.uncertainties)
    if expected.get("conflict"):
        assert structured.conflicting_evidence
    if name == "radar_breathing_interruption":
        assert any("breathing_rate" in item for item in structured.missing_data)
    if name == "timestamp_mismatch":
        assert any("时间" in item for item in structured.uncertainties)


def test_duplicate_and_timeout_scenarios_are_marked_for_integration():
    # Their end-to-end behaviour is covered in test_agent2_manager.py using the
    # real InvestigationService; here we assert the fixtures stay synthetic.
    duplicate = build("duplicate_events", {"id": "e1", "device_id": "guardian_001",
                                           "opened_at": NOW.isoformat()})
    timeout = build("agent_timeout", {"id": "e2", "device_id": "guardian_001",
                                      "opened_at": NOW.isoformat()})
    assert duplicate.expected["duplicate_merge"] is True
    assert timeout.expected["ai_status"] == "timeout"
    assert timeout.expected["safe_fallback"] is True
    assert all(item.data_mode == "replay" for item in timeout.telemetry)
    assert all(item.synthetic for item in timeout.extra)
