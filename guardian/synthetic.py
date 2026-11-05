"""Explicitly synthetic multimodal scenarios for Agent 2.0 tests.

Every scenario is marked synthetic/replay.  These fixtures validate software
logic only; they do not demonstrate disease detection on real people.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .evidence import Evidence
from .models import Telemetry


@dataclass
class Scenario:
    name: str
    description: str
    event_kind: str
    telemetry: list[Telemetry] = field(default_factory=list)
    extra: list[Evidence] = field(default_factory=list)
    expected: dict = field(default_factory=dict)


def _when(event: dict, offset_seconds: float = -20) -> datetime:
    opened = datetime.fromisoformat(event["opened_at"])
    return opened + timedelta(seconds=offset_seconds)


def _reading(event: dict, offset: float, *, hr=75, quality="good", valid=True,
             acc=(0.0, 0.0, 1.0), device="guardian_001") -> Telemetry:
    return Telemetry(
        device_id=device, timestamp=_when(event, offset), source="simulator",
        heart_rate=hr, spo2=98, acc_x=acc[0], acc_y=acc[1], acc_z=acc[2],
        signal_quality=quality, reading_valid=valid)


def _ev(event: dict, source_type: str, value, *, unit=None, quality="good",
        offset=0.0, device="guardian_001", sync="synced",
        uncertainty=50, synthetic=True, ref=None) -> Evidence:
    return Evidence(
        evidence_id="ev_" + uuid4().hex, event_id=event["id"],
        device_id=device, source_type=source_type,
        timestamp=_when(event, offset), value=value, unit=unit,
        quality_status=quality, data_mode="replay",
        time_sync_status=sync, clock_uncertainty_ms=uncertainty,
        synthetic=synthetic, source_ref=ref or f"synthetic:{source_type}:{uuid4().hex}")


def hr_with_motion(event: dict) -> Scenario:
    telemetry = [_reading(event, offset, hr=135, acc=(2.5, 1.0, 1.5))
                 for offset in (-20, -19, -18, -17)]
    return Scenario(
        name="hr_with_motion", description="心率升高同时存在明显运动",
        event_kind="high_heart_rate", telemetry=telemetry,
        expected={"sources": {"heart_rate_estimate", "imu_raw"},
                  "fusion_allowed": True,
                  "quality": {"heart_rate_estimate": "good"}})


def hr_poor_ppg(event: dict) -> Scenario:
    telemetry = [_reading(event, offset, hr=135, quality="poor", valid=False)
                 for offset in (-20, -19, -18)]
    return Scenario(
        name="hr_poor_ppg", description="心率升高但 PPG 质量差",
        event_kind="data_quality", telemetry=telemetry,
        expected={"sources": {"signal_quality"},
                  "quality": {"signal_quality": "poor"},
                  "uncertainty_marker": "低质量"})


def radar_target_loss(event: dict) -> Scenario:
    telemetry = [_reading(event, -20, hr=75)]
    radar = _ev(event, "radar_status", {"target": "lost"}, offset=-5,
                device="radar_001", ref="synthetic:radar:target_lost")
    return Scenario(
        name="radar_target_loss", description="雷达目标丢失",
        event_kind="data_quality", telemetry=telemetry, extra=[radar],
        expected={"radar_value": {"target": "lost"},
                  "fusion_allowed": True})


def radar_breathing_interruption(event: dict) -> Scenario:
    telemetry = [_reading(event, -20, hr=75)]
    breathing = _ev(event, "breathing_rate", None, unit="breaths/min",
                    quality="unavailable", offset=-5, device="radar_001",
                    ref="synthetic:radar:breathing_interrupted")
    return Scenario(
        name="radar_breathing_interruption", description="雷达呼吸数据中断",
        event_kind="data_quality", telemetry=telemetry, extra=[breathing],
        expected={"missing": {"breathing_rate"}})


def wearable_offline(event: dict) -> Scenario:
    stale = [_reading(event, -240, hr=75)]
    return Scenario(
        name="wearable_offline", description="手环离线，最近数据过期",
        event_kind="data_quality", telemetry=stale,
        expected={"device_online_value": False,
                  "missing": {"radar_status", "breathing_rate"}})


def timestamp_mismatch(event: dict) -> Scenario:
    telemetry = [_reading(event, -20, hr=75)]
    radar = _ev(event, "radar_status", {"target": "present"}, offset=25,
                device="radar_001", sync="gateway_estimated",
                uncertainty=8000, ref="synthetic:radar:clock_mismatch")
    return Scenario(
        name="timestamp_mismatch", description="多传感器时间戳不一致",
        event_kind="data_quality", telemetry=telemetry, extra=[radar],
        expected={"fusion_allowed": False,
                  "alignment_reason": "uncertainty"})


def contradictory(event: dict) -> Scenario:
    telemetry = [_reading(event, offset, hr=135) for offset in (-20, -19, -18)]
    radar = _ev(event, "radar_status", {"target": "lost"}, offset=-5,
                device="radar_001", ref="synthetic:radar:contradiction")
    return Scenario(
        name="contradictory", description="心率偏高但雷达目标丢失",
        event_kind="high_heart_rate", telemetry=telemetry, extra=[radar],
        expected={"conflict": True, "sources": {"heart_rate_estimate",
                                                "radar_status"}})


def normal_turning(event: dict) -> Scenario:
    telemetry = [_reading(event, offset, hr=75, acc=(0.5, 0.2, 2.2))
                 for offset in (-20, -19, -18)]
    return Scenario(
        name="normal_turning", description="正常翻身，心率正常",
        event_kind="unusual_motion", telemetry=telemetry,
        expected={"sources": {"imu_raw"},
                  "quality": {"imu_raw": "good"}})


def duplicate_events(event: dict) -> Scenario:
    return Scenario(
        name="duplicate_events", description="短时间重复事件，应合并调查",
        event_kind="high_heart_rate",
        expected={"duplicate_merge": True})


def agent_timeout(event: dict) -> Scenario:
    telemetry = [_reading(event, -20, hr=135)]
    return Scenario(
        name="agent_timeout", description="Agent API 超时后安全降级",
        event_kind="high_heart_rate", telemetry=telemetry,
        expected={"ai_status": "timeout", "safe_fallback": True})


SCENARIOS = {
    "hr_with_motion": hr_with_motion,
    "hr_poor_ppg": hr_poor_ppg,
    "radar_target_loss": radar_target_loss,
    "radar_breathing_interruption": radar_breathing_interruption,
    "wearable_offline": wearable_offline,
    "timestamp_mismatch": timestamp_mismatch,
    "contradictory": contradictory,
    "normal_turning": normal_turning,
    "duplicate_events": duplicate_events,
    "agent_timeout": agent_timeout,
}


def build(name: str, event: dict) -> Scenario:
    if name not in SCENARIOS:
        raise KeyError(name)
    return SCENARIOS[name](event)
