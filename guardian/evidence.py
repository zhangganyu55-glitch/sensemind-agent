"""Agent 2.0 unified evidence engine.

Evidence records are immutable, source-referenced and explicit about time,
quality and data mode.  Missing sensors become ``unavailable`` entries instead
of being silently omitted.  Replay/synthetic data is never relabelled as live,
and evidence from devices whose clocks cannot be aligned is never force-fused.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .storage import Storage


SourceType = Literal[
    "ppg",
    "heart_rate_estimate",
    "signal_quality",
    "activity",
    "imu_raw",
    "radar_status",
    "breathing_rate",
    "device_online",
    "historical_event",
    "device_status",
]

QualityStatus = Literal["good", "poor", "unknown", "invalid", "unavailable"]
DataMode = Literal["live", "replay", "unknown"]
TimeSyncStatus = Literal["synced", "gateway_estimated", "unsynced", "unknown"]

PHYSICAL_SOURCES = {"ppg", "heart_rate_estimate", "imu_raw",
                    "radar_status", "breathing_rate"}
EXPECTED_SOURCES = (
    "ppg", "heart_rate_estimate", "signal_quality", "activity", "imu_raw",
    "radar_status", "breathing_rate", "device_online", "historical_event",
)


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=80)
    device_id: str = Field(min_length=1, max_length=80)
    source_type: SourceType
    timestamp: datetime | None = None
    value: Any | None = None
    unit: str | None = Field(default=None, max_length=24)
    quality_status: QualityStatus
    data_mode: DataMode
    model_version: str | None = Field(default=None, max_length=100)
    clock_uncertainty_ms: int | None = Field(default=None, ge=0)
    time_sync_status: TimeSyncStatus = "unknown"
    synthetic: bool = False
    source_ref: str = Field(min_length=1, max_length=200)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceAvailability(BaseModel):
    source_type: str
    available: bool
    evidence_count: int
    reason: str | None = None


class EvidenceBundle(BaseModel):
    event_id: str
    device_id: str
    window_start: datetime | None
    window_end: datetime | None
    evidence: list[Evidence]
    availability: list[EvidenceAvailability]
    data_modes: list[str]
    has_synthetic: bool
    alignment: dict

    def by_id(self) -> dict[str, Evidence]:
        return {item.evidence_id: item for item in self.evidence}

    def as_context(self, limit: int = 80) -> list[dict]:
        """Bounded JSON-safe context for the model."""
        chosen = self.evidence[:limit]
        return [{
            "evidence_id": item.evidence_id,
            "source_type": item.source_type,
            "device_id": item.device_id,
            "timestamp": item.timestamp.isoformat() if item.timestamp else None,
            "value": item.value,
            "unit": item.unit,
            "quality_status": item.quality_status,
            "data_mode": item.data_mode,
            "model_version": item.model_version,
            "time_sync_status": item.time_sync_status,
            "clock_uncertainty_ms": item.clock_uncertainty_ms,
            "synthetic": item.synthetic,
        } for item in chosen]


class EvidenceEngine:
    """Build one event's evidence set from local records and optional sources."""

    def __init__(self, storage: Storage, *,
                 max_samples: int = 120,
                 fusion_tolerance_ms: int = 2000,
                 default_time_sync_status: TimeSyncStatus = "gateway_estimated",
                 default_clock_uncertainty_ms: int = 1000,
                 extra_sources: list[Evidence] | Callable[..., list[Evidence]] | None = None,
                 allow_live_extra: bool = False):
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        self.storage = storage
        self.max_samples = max_samples
        self.fusion_tolerance_ms = fusion_tolerance_ms
        self.default_time_sync_status = default_time_sync_status
        self.default_clock_uncertainty_ms = default_clock_uncertainty_ms
        self.extra_sources = extra_sources
        self.allow_live_extra = allow_live_extra

    # -- public ------------------------------------------------------------
    def collect_for_event(self, event: dict, *,
                          window_start: datetime | None = None,
                          window_end: datetime | None = None,
                          since: datetime | None = None,
                          extra: list[Evidence] | None = None) -> EvidenceBundle:
        event_time = utc(datetime.fromisoformat(event["opened_at"]))
        start = utc(window_start) or (event_time - timedelta(minutes=5))
        end = utc(window_end) or (event_time + timedelta(minutes=1))
        device = event["device_id"]
        event_id = event["id"]

        samples = self.storage.telemetry_between(device, start, end, limit=4000)
        if since is not None:
            samples = [s for s in samples
                       if utc(datetime.fromisoformat(s["timestamp"])) >= since]
        samples = self._nearest(samples, event_time)

        evidence: list[Evidence] = []
        for sample in samples:
            evidence.extend(self._from_telemetry(event, sample))

        evidence.extend(self._from_ml(event))
        evidence.extend(self._from_similar_events(event, event_time))

        latest = self.storage.latest_valid(
            device, now=datetime.now(timezone.utc), max_future_seconds=5.0,
            max_lag_seconds=5.0)
        evidence.extend(self._device_online(event, latest))

        supplied = self._extra_evidence(event, extra)
        evidence.extend(supplied)

        # Persist idempotently; first write wins for a given (event, source_ref).
        stored: list[Evidence] = []
        for item in evidence:
            evidence_id, _created = self.storage.put_evidence(item.model_dump(mode="json"))
            item = item.model_copy(update={"evidence_id": evidence_id})
            stored.append(item)

        stored.sort(key=lambda item: (item.timestamp is None, item.timestamp,
                                      item.source_type, item.source_ref))
        availability = self._availability(stored)
        alignment = self._alignment(stored)
        return EvidenceBundle(
            event_id=event_id, device_id=device,
            window_start=start, window_end=end,
            evidence=stored, availability=availability,
            data_modes=sorted({item.data_mode for item in stored}),
            has_synthetic=any(item.synthetic for item in stored),
            alignment=alignment,
        )

    # -- builders ----------------------------------------------------------
    def _nearest(self, samples: list[dict], event_time: datetime) -> list[dict]:
        if len(samples) <= self.max_samples:
            return samples
        ordered = sorted(samples, key=lambda item: abs(
            (utc(datetime.fromisoformat(item["timestamp"])) - event_time).total_seconds()))
        keep = {id(item) for item in ordered[:self.max_samples]}
        return sorted([item for item in samples if id(item) in keep],
                      key=lambda item: item["timestamp"])

    def _mode(self, sample: dict) -> DataMode:
        value = sample.get("data_mode")
        if value in ("live", "replay"):
            return value
        if sample.get("source") == "simulator" or sample.get("dataset"):
            return "replay"
        return "live" if sample.get("source") in ("stm32", "ring", "esp32") else "unknown"

    def _base(self, event: dict, source_type: SourceType, source_ref: str,
              timestamp: datetime | None, value: Any, unit: str | None,
              quality: QualityStatus, data_mode: DataMode,
              *, model_version: str | None = None, synthetic: bool = False,
              time_sync_status: TimeSyncStatus | None = None,
              clock_uncertainty_ms: int | None = None,
              metadata: dict | None = None) -> Evidence:
        return Evidence(
            evidence_id="ev_" + uuid4().hex, event_id=event["id"],
            device_id=event["device_id"], source_type=source_type,
            timestamp=utc(timestamp), value=value, unit=unit,
            quality_status=quality, data_mode=data_mode,
            model_version=model_version, synthetic=synthetic,
            time_sync_status=time_sync_status or self.default_time_sync_status,
            clock_uncertainty_ms=(self.default_clock_uncertainty_ms
                                  if clock_uncertainty_ms is None else clock_uncertainty_ms),
            source_ref=source_ref, metadata=metadata or {},
        )

    def _from_telemetry(self, event: dict, sample: dict) -> list[Evidence]:
        ts = utc(datetime.fromisoformat(sample["timestamp"]))
        mode = self._mode(sample)
        quality = sample.get("signal_quality", "unknown")
        if quality not in ("good", "poor", "unknown", "invalid"):
            quality = "unknown"
        device = event["device_id"]
        suffix = f"{device}:{sample['timestamp']}"
        source = sample.get("source", "unknown")
        meta = {"source": source, "reading_valid": sample.get("reading_valid"),
                "dataset": sample.get("dataset"), "participant_id": sample.get("participant_id")}
        out: list[Evidence] = []
        if sample.get("heart_rate") is not None:
            out.append(self._base(
                event, "heart_rate_estimate", f"telemetry:{suffix}:hr", ts,
                float(sample["heart_rate"]), "bpm", quality, mode, metadata=meta))
        out.append(self._base(
            event, "signal_quality", f"telemetry:{suffix}:quality", ts,
            quality, None, quality, mode, metadata=meta))
        if None not in (sample.get("acc_x"), sample.get("acc_y"), sample.get("acc_z")):
            out.append(self._base(
                event, "imu_raw", f"telemetry:{suffix}:imu", ts,
                [float(sample["acc_x"]), float(sample["acc_y"]), float(sample["acc_z"])],
                "g", quality, mode, metadata=meta))
        if sample.get("device_status"):
            out.append(self._base(
                event, "device_status", f"telemetry:{suffix}:status", ts,
                sample["device_status"], None, quality, mode, metadata=meta))
        return out

    def _from_ml(self, event: dict) -> list[Evidence]:
        out: list[Evidence] = []
        for row in self.storage.ml_inferences(event["device_id"], limit=20):
            result = row.get("result") or {}
            created = utc(datetime.fromisoformat(row["created_at"])) if row.get("created_at") else None
            mode: DataMode = "replay" if result.get("dataset") else "unknown"
            version = row.get("model_version") or result.get("model_version")
            activity = result.get("activity") or {}
            signal = result.get("signal") or {}
            if row.get("activity_label"):
                out.append(self._base(
                    event, "activity", f"ml:{row['id']}:activity", created,
                    row["activity_label"], None,
                    "good" if activity.get("available", True) else "unavailable",
                    mode, model_version=version,
                    metadata={"confidence": row.get("activity_confidence"),
                              "dataset": result.get("dataset")}))
            estimate = signal.get("estimated_hr")
            if estimate is not None:
                reliability = signal.get("reliability")
                if reliability is None:
                    quality = "unknown"
                elif reliability >= 0.7:
                    quality = "good"
                elif reliability >= 0.4:
                    quality = "poor"
                else:
                    quality = "invalid"
                out.append(self._base(
                    event, "heart_rate_estimate", f"ml:{row['id']}:hr", created,
                    float(estimate), "bpm", quality, mode, model_version=version,
                    metadata={"reliability": reliability,
                              "dataset": result.get("dataset")}))
                out.append(self._base(
                    event, "signal_quality", f"ml:{row['id']}:quality", created,
                    reliability, "ratio", quality, mode, model_version=version,
                    metadata={"dataset": result.get("dataset")}))
        return out

    def _from_similar_events(self, event: dict, event_time: datetime) -> list[Evidence]:
        events = self.storage.similar_events(
            event["device_id"], event["kind"], event_time - timedelta(hours=24),
            event_time, event["id"], limit=20)
        return [self._base(
            event, "historical_event", f"event:{item['id']}",
            utc(datetime.fromisoformat(item["opened_at"])),
            {"kind": item["kind"], "status": item["status"]}, None,
            "good", "unknown", metadata={"acknowledged": item.get("acknowledged")})
            for item in events]

    def _device_online(self, event: dict, latest: dict | None) -> list[Evidence]:
        if not latest:
            return []
        ts = utc(datetime.fromisoformat(latest["timestamp"]))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        online = -30 <= age <= 30
        status = (latest.get("device_status") or {}).get("sampling_status")
        return [self._base(
            event, "device_online", "device_online", ts, online, None,
            "good" if online else "poor", self._mode(latest),
            metadata={"age_seconds": round(age, 1), "sampling_status": status})]

    def _extra_evidence(self, event: dict, extra: list[Evidence] | None) -> list[Evidence]:
        supplied = extra
        if supplied is None and self.extra_sources is not None:
            supplied = (self.extra_sources(event) if callable(self.extra_sources)
                        else list(self.extra_sources))
        out: list[Evidence] = []
        for item in supplied or []:
            item = item.model_copy(update={"event_id": event["id"]})
            if item.source_type in PHYSICAL_SOURCES and item.data_mode == "live" \
                    and not item.synthetic and not self.allow_live_extra:
                raise ValueError("live physical evidence requires a hardware source")
            out.append(item)
        return out

    # -- summaries ---------------------------------------------------------
    def _availability(self, evidence: list[Evidence]) -> list[EvidenceAvailability]:
        raw: dict[str, int] = {}
        usable: dict[str, int] = {}
        for item in evidence:
            raw[item.source_type] = raw.get(item.source_type, 0) + 1
            if item.quality_status != "unavailable" and item.value is not None:
                usable[item.source_type] = usable.get(item.source_type, 0) + 1
        out = []
        for source_type in EXPECTED_SOURCES:
            count = usable.get(source_type, 0)
            reason = None
            if count == 0:
                if raw.get(source_type, 0):
                    reason = "no_usable_data"
                elif source_type == "ppg":
                    reason = "raw_ppg_not_ingested"
                elif source_type in ("radar_status", "breathing_rate"):
                    reason = "sensor_not_connected"
                else:
                    reason = "no_data"
            out.append(EvidenceAvailability(source_type=source_type,
                                            available=count > 0,
                                            evidence_count=count,
                                            reason=reason))
        return out

    def _alignment(self, evidence: list[Evidence]) -> dict:
        devices: dict[str, dict] = {}
        for item in evidence:
            entry = devices.setdefault(item.device_id, {
                "device_id": item.device_id, "time_sync_status": "synced",
                "clock_uncertainty_ms": None, "evidence_count": 0})
            entry["evidence_count"] += 1
            statuses = {"unsynced": 0, "unknown": 1,
                        "gateway_estimated": 2, "synced": 3}
            if statuses[item.time_sync_status] < statuses[entry["time_sync_status"]]:
                entry["time_sync_status"] = item.time_sync_status
            if item.clock_uncertainty_ms is not None:
                entry["clock_uncertainty_ms"] = max(
                    entry["clock_uncertainty_ms"] or 0, item.clock_uncertainty_ms)
        reason = None
        fusion_allowed = True
        if len(devices) > 1:
            for entry in devices.values():
                if entry["time_sync_status"] not in ("synced", "gateway_estimated"):
                    fusion_allowed = False
                    reason = (f"device {entry['device_id']} clock is "
                              f"{entry['time_sync_status']}")
                    break
                if entry["clock_uncertainty_ms"] is None or \
                        entry["clock_uncertainty_ms"] > self.fusion_tolerance_ms:
                    fusion_allowed = False
                    reason = (f"device {entry['device_id']} clock uncertainty "
                              f"{entry['clock_uncertainty_ms']} ms exceeds tolerance")
                    break
        return {"fusion_allowed": fusion_allowed,
                "tolerance_ms": self.fusion_tolerance_ms,
                "devices": sorted(devices.values(), key=lambda item: item["device_id"]),
                "reason": reason}
