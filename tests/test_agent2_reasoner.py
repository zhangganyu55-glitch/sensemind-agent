"""Agent 2.0: structured reasoning contract, grounding and safe fallback."""
from datetime import datetime, timedelta, timezone

import pytest

from guardian.evidence import EvidenceEngine
from guardian.models import Telemetry
from guardian.reasoner import (StructuredAnalysis, StructuredObservation,
                               deterministic_structured, map_legacy_inferences,
                               validate_structured)
from guardian.storage import Storage


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def make_bundle(tmp_path, *, hr=130, quality="good", with_telemetry=True):
    storage = Storage(str(tmp_path / "reasoner.db"))
    transition = storage.event_open("guardian_001", "high_heart_rate",
                                    NOW - timedelta(seconds=5),
                                    "DEMO ONLY: unverified rule; not diagnosis")
    event = storage.get_event(transition["event_id"])
    if with_telemetry:
        storage.put_telemetry(Telemetry(
            device_id="guardian_001", timestamp=NOW - timedelta(seconds=10),
            source="simulator", heart_rate=hr, spo2=98,
            acc_x=0.0, acc_y=0.0, acc_z=1.0,
            signal_quality=quality, reading_valid=quality == "good"))
    bundle = EvidenceEngine(storage).collect_for_event(event)
    return storage, event, bundle


def evidence_id(bundle, source_type):
    return next(item.evidence_id for item in bundle.evidence
                if item.source_type == source_type)


def test_valid_structured_analysis_is_accepted(tmp_path):
    _, event, bundle = make_bundle(tmp_path)
    ref = evidence_id(bundle, "heart_rate_estimate")
    analysis = StructuredAnalysis(
        event_id=event["id"], investigation_status="completed",
        observations=[StructuredObservation(text="心率样本为 130 bpm。",
                                            evidence_refs=[ref])],
        evidence_refs=[ref], conclusion="仅描述已记录的样本。",
        recommended_action=["人工复核设备与数据质量。"])
    result = validate_structured(analysis, event=event, bundle=bundle)
    assert result.investigation_status == "completed"
    assert result.conclusion


def test_unknown_evidence_reference_is_rejected(tmp_path):
    _, event, bundle = make_bundle(tmp_path)
    ref = evidence_id(bundle, "heart_rate_estimate")
    analysis = StructuredAnalysis(
        event_id=event["id"], investigation_status="completed",
        observations=[StructuredObservation(text="心率 130 bpm。",
                                            evidence_refs=["ev_deadbeef"])],
        evidence_refs=[ref], conclusion="测试。")
    with pytest.raises(ValueError, match="invalid_evidence_ref"):
        validate_structured(analysis, event=event, bundle=bundle)


def test_fabricated_measurement_is_rejected(tmp_path):
    _, event, bundle = make_bundle(tmp_path)
    ref = evidence_id(bundle, "heart_rate_estimate")
    analysis = StructuredAnalysis(
        event_id=event["id"], investigation_status="completed",
        observations=[StructuredObservation(text="心率 999 bpm。",
                                            evidence_refs=[ref])],
        evidence_refs=[ref], conclusion="测试。")
    with pytest.raises(ValueError, match="fabricated_measurement"):
        validate_structured(analysis, event=event, bundle=bundle)


def test_completed_with_insufficient_evidence_is_downgraded(tmp_path):
    storage = Storage(str(tmp_path / "insufficient.db"))
    prior = storage.event_open("guardian_001", "high_heart_rate",
                               NOW - timedelta(hours=2), "prior")
    storage.event_close("guardian_001", "high_heart_rate", NOW - timedelta(hours=1))
    transition = storage.event_open("guardian_001", "high_heart_rate",
                                    NOW - timedelta(seconds=5), "current")
    event = storage.get_event(transition["event_id"])
    bundle = EvidenceEngine(storage).collect_for_event(event)
    historical = evidence_id(bundle, "historical_event")
    analysis = StructuredAnalysis(
        event_id=event["id"], investigation_status="completed",
        observations=[StructuredObservation(text="过去存在同类事件。",
                                            evidence_refs=[historical])],
        evidence_refs=[historical], conclusion="测试。")
    result = validate_structured(analysis, event=event, bundle=bundle)
    assert result.investigation_status == "inconclusive"
    assert any("inconclusive" in item or "证据不足" in item
               for item in result.uncertainties)


def test_high_risk_action_is_rejected(tmp_path):
    _, event, bundle = make_bundle(tmp_path)
    ref = evidence_id(bundle, "heart_rate_estimate")
    analysis = StructuredAnalysis(
        event_id=event["id"], investigation_status="completed",
        observations=[StructuredObservation(text="心率 130 bpm。",
                                            evidence_refs=[ref])],
        evidence_refs=[ref], conclusion="测试。",
        recommended_action=["发送报警并拨打急救。"])
    with pytest.raises(ValueError, match="unsafe_recommended_action"):
        validate_structured(analysis, event=event, bundle=bundle)


def test_deterministic_fallback_is_explicit_and_safe(tmp_path):
    _, event, bundle = make_bundle(tmp_path, with_telemetry=False)
    analysis = deterministic_structured(event, bundle, status="inconclusive",
                                        reason="timeout")
    assert analysis.investigation_status == "inconclusive"
    assert analysis.missing_data
    assert any("radar_status" in item for item in analysis.missing_data)
    assert analysis.recommended_action
    assert not any("报警" in action for action in analysis.recommended_action)


def test_legacy_narrative_is_mapped_to_structured_contract(tmp_path):
    _, event, bundle = make_bundle(tmp_path)
    mapped = map_legacy_inferences(
        ["窗口内存在偏高的心率样本。"], ["模拟数据，不能诊断。"],
        ["人工核对设备和数据质量。"], "基于工具结果的限定性摘要。",
        event_id=event["id"], bundle=bundle)
    assert mapped.event_id == event["id"]
    assert mapped.observations
    assert mapped.observations[0].evidence_refs
    assert set(mapped.evidence_refs) <= set(bundle.by_id())
    assert any("旧版" in item for item in mapped.uncertainties)
