"""Agent 3.0 Decision Engine V0.1.

A small, deterministic layer that decides whether the system should observe,
verify data quality, start an existing Agent 2.0 investigation, or record a
non-urgent insight.  It deliberately does **not** call a language model, does
not use model-reported confidence, and cannot send notifications by itself.

State is derived from persisted telemetry, events and the decision log.  The
only new persistence is the decision audit table; there is no general-purpose
State Engine and no baseline state that can drift independently of the data.

Safety boundaries
-----------------
* ``DetectionEngine`` remains the only source of rule-engine events.  This
  module never enables real-device physiological rules.
* Real-device sustained-change detection requires both the engine flag and a
  second explicit ``allow_real_sustained`` flag.
* A decision failure must never block telemetry ingestion, rule alerts or an
  existing investigation; callers wrap :meth:`evaluate` accordingly.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Callable

from . import monitoring

DECISION_TYPES = ("OBSERVE", "VERIFY", "INVESTIGATE", "INSIGHT")
POLICY_VERSION = "agent3-decision-v0.1"
SUSTAINED_KIND = "sustained_hr_change"
REAL_SOURCES = {"stm32", "ring", "esp32"}
PHYSIOLOGICAL_KINDS = {
    "high_heart_rate", "low_spo2", "unusual_motion", "concurrent_signals",
}
RULE_INVESTIGATE_KINDS = PHYSIOLOGICAL_KINDS
SUSTAINED_NOTE = (
    "Agent 3.0 experimental sustained-change rule (non-diagnostic). "
    "Internal investigation only; not a caregiver alert."
)


@dataclass(frozen=True)
class DecisionConfig:
    """Demo-only thresholds.  These are software rules, not medical cutoffs."""

    observe_heartbeat_seconds: int = 60
    verify_bucket_seconds: int = 300
    insight_bucket_seconds: int = 600
    stale_seconds: int = 30
    history_window_seconds: int = 900
    baseline_window_seconds: int = 300
    baseline_min_samples: int = 20
    baseline_min_span_seconds: int = 60
    sustained_min_samples: int = 10
    sustained_min_duration_seconds: int = 30
    sustained_max_gap_seconds: float = 3.0
    sustained_deviation_bpm: float = 15.0
    sustained_deviation_ratio: float = 0.15
    sustained_cooldown_seconds: int = 600


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


class DecisionEngine:
    def __init__(self, storage, investigations, *,
                 enabled: bool = False,
                 allow_real_sustained: bool = False,
                 config: DecisionConfig | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.storage = storage
        self.investigations = investigations
        self.enabled = enabled
        self.allow_real_sustained = allow_real_sustained
        self.config = config or DecisionConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweep_lock = threading.Lock()
        self.last_sweep_at: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None

    # -- public ------------------------------------------------------------
    def evaluate(self, device_id: str, transitions: list[dict] | None = None,
                 now: datetime | None = None) -> list[dict]:
        """Evaluate one ingestion tick or sweep tick.

        Returns every decision written or touched.  Rule transitions are
        handled one by one; a new state decision is added in the same call.
        Nothing is ever dropped in favour of a single "primary" decision.
        """
        if not self.enabled:
            return []
        moment = _utc(now) or _utc(self._clock())
        # Live validity is shared with the rule engine: 5 s future bound.
        latest = self.storage.latest_valid(
            device_id, now=moment, max_future_seconds=5.0,
            max_lag_seconds=monitoring.DEFAULT_MAX_SAMPLE_LAG_SECONDS)
        results: list[dict] = []
        for transition in transitions or []:
            record = self._handle_transition(device_id, transition, latest, moment)
            if record:
                results.append(record)
        state = self._state_decision(device_id, latest, moment)
        if state:
            results.append(state)
        return results

    def sweep(self, now: datetime | None = None) -> list[dict]:
        """Periodic stale/no-data pass.  Runs are serialized in-process."""
        if not self.enabled:
            return []
        if not self._sweep_lock.acquire(blocking=False):
            return []
        try:
            moment = _utc(now) or _utc(self._clock())
            results: list[dict] = []
            errors: list[str] = []
            try:
                device_ids = self.storage.all_device_ids()
            except Exception as error:
                self.last_sweep_at = moment.isoformat()
                self.last_error = f"list_devices:{type(error).__name__}"[:300]
                self.last_error_at = moment.isoformat()
                return []
            for device_id in device_ids:
                try:
                    latest = self.storage.latest_valid(
                        device_id, now=moment, max_future_seconds=5.0,
                        max_lag_seconds=monitoring.DEFAULT_MAX_SAMPLE_LAG_SECONDS)
                    monitoring.record(
                        self.storage, device_id, latest, now=moment,
                        max_sample_lag_seconds=monitoring.DEFAULT_MAX_SAMPLE_LAG_SECONDS)
                    results.extend(self.evaluate(device_id, None, moment))
                except Exception as error:
                    # One bad device must not stop the sweep.
                    errors.append(f"{device_id}:{type(error).__name__}")
                    continue
            self.last_sweep_at = moment.isoformat()
            self.last_error = "; ".join(errors)[:300] if errors else None
            if errors:
                self.last_error_at = moment.isoformat()
            return results
        finally:
            self._sweep_lock.release()

    def start_background(self, interval_seconds: float = 10.0) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval_seconds):
                try:
                    self.sweep()
                except Exception as error:
                    self.last_error = f"sweep_thread:{type(error).__name__}"[:300]
                    self.last_error_at = datetime.now(timezone.utc).isoformat()

        self._thread = threading.Thread(
            target=loop, name="guardian-decision-sweep", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "allow_real_sustained": self.allow_real_sustained,
            "policy_version": POLICY_VERSION,
            "decision_types": list(DECISION_TYPES),
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "last_sweep_at": self.last_sweep_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "config": asdict(self.config),
        }

    # -- rule transitions --------------------------------------------------
    def _handle_transition(self, device_id: str, transition: dict,
                           latest: dict | None, now: datetime) -> dict | None:
        event_id = transition.get("event_id")
        event = self.storage.get_event(event_id) if event_id else None
        if not event:
            return None
        action = transition.get("action")
        kind = event["kind"]
        quality, quality_basis = self._quality(latest, now)
        data_mode = self._data_mode(latest)

        if action == "opened":
            decision_type = "VERIFY" if kind == "data_quality" else "INVESTIGATE"
            investigation = self.storage.investigation_for_event(event_id)
            status = "processed" if investigation else "recorded"
            summary = (
                "规则事件触发数据质量核实；数据不可用不等于正常。"
                if decision_type == "VERIFY"
                else "规则事件触发已有 Agent 调查；结果为实验性证据，不是诊断。"
            )
            return self._emit(
                device_id=device_id, decision_type=decision_type,
                source="rule_engine", reason_code=f"rule_{kind}_opened",
                summary=summary,
                basis={"transition": action, "kind": kind, "event_id": event_id,
                       "event_note": event.get("note"),
                       "investigation_id": investigation["id"] if investigation else None,
                       "quality": quality_basis},
                data_quality=quality, data_mode=data_mode, now=now,
                event_id=event_id,
                investigation_id=investigation["id"] if investigation else None,
                status=status,
                dedup_key=f"event:{event_id}:{decision_type}",
            )

        if action == "resolved":
            return self._emit(
                device_id=device_id, decision_type="INSIGHT",
                source="rule_engine", reason_code="rule_resolved",
                summary="规则条件已解除；这不代表身体恢复或安全。",
                basis={"transition": action, "kind": kind, "event_id": event_id,
                       "quality": quality_basis},
                data_quality=quality, data_mode=data_mode, now=now,
                event_id=event_id, status="recorded",
                dedup_key=f"event:{event_id}:INSIGHT:resolved",
            )
        return None

    # -- state decision ----------------------------------------------------
    def _state_decision(self, device_id: str, latest: dict | None,
                        now: datetime) -> dict | None:
        if latest is None:
            return self._verify(
                device_id, "no_data", "没有任何遥测记录，不能视为正常。",
                {"reason": "no_telemetry"}, "no_data", "unknown", now,
                bucket=self.config.verify_bucket_seconds)

        quality, quality_basis = self._quality(latest, now)
        data_mode = self._data_mode(latest)
        if quality != "good":
            return self._verify(
                device_id, f"quality_{quality}",
                "数据质量或新鲜度不足，需先核实；不得解释为正常。",
                {"quality": quality_basis}, quality, data_mode, now,
                bucket=self.config.verify_bucket_seconds)

        active = self._active_sustained(device_id)
        if active:
            return self._active_sustained_decision(
                device_id, active, latest, quality_basis, data_mode, now)

        allowed, gate = self._sustained_allowed(latest)
        detection = self._detect_sustained(device_id, now) if allowed else None
        if detection:
            return self._create_sustained(
                device_id, detection, quality_basis, data_mode, now)
        if not allowed:
            gate = {**gate, "sustained_detection_skipped": "real_device_protection"}

        recovered = self._quality_recovery(device_id, now)
        if recovered:
            return self._emit(
                device_id=device_id, decision_type="INSIGHT",
                source="quality_gate", reason_code="quality_recovered",
                summary="数据质量恢复；这只说明采集恢复，不代表健康状态恢复。",
                basis={"quality": quality_basis,
                       "previous_decision_id": recovered["decision_id"]},
                data_quality=quality, data_mode=data_mode, now=now,
                status="recorded",
                dedup_key=f"recovery:{recovered['decision_id']}",
            )

        baseline = self._baseline(
            self._samples(device_id, now), now, now)
        return self._emit(
            device_id=device_id, decision_type="OBSERVE",
            source="state_engine", reason_code="normal_observe",
            summary="持续观察：当前数据质量良好，未触发规则或持续变化。",
            basis={"quality": quality_basis,
                   "baseline_ready": baseline is not None,
                   "baseline": baseline,
                   "sustained_gate": gate,
                   "active_events": len(self.storage.events(
                       device_id, active_only=True))},
            data_quality=quality, data_mode=data_mode, now=now,
            status="recorded",
            dedup_key=(f"observe:{device_id}:"
                       f"{self._bucket(now, self.config.observe_heartbeat_seconds)}"),
        )

    def _verify(self, device_id: str, reason: str, summary: str, basis: dict,
                quality: str, data_mode: str, now: datetime,
                bucket: int) -> dict:
        return self._emit(
            device_id=device_id, decision_type="VERIFY",
            source="quality_gate", reason_code=reason, summary=summary,
            basis=basis, data_quality=quality, data_mode=data_mode, now=now,
            status="recorded",
            dedup_key=f"verify:{device_id}:{reason}:{self._bucket(now, bucket)}",
        )

    # -- sustained change --------------------------------------------------
    def _sustained_allowed(self, latest: dict) -> tuple[bool, dict]:
        source = latest.get("source")
        gate = {"source": source, "real_device": source in REAL_SOURCES,
                "allow_real_sustained": self.allow_real_sustained}
        if source in REAL_SOURCES and not self.allow_real_sustained:
            return False, gate
        return True, gate

    def _detect_sustained(self, device_id: str, now: datetime) -> dict | None:
        """Two-pass detector: find a recent run, then rebuild the baseline
        strictly before the run so candidate data cannot contaminate it."""
        samples = self._samples(device_id, now)
        good = [sample for sample in samples if self._good(sample)]
        if len(good) < (self.config.baseline_min_samples
                        + self.config.sustained_min_samples):
            return None

        window_start = now - timedelta(seconds=self.config.baseline_window_seconds)
        pass_one = [float(sample["heart_rate"]) for sample in good
                    if sample["_ts"] >= window_start]
        if len(pass_one) < self.config.baseline_min_samples:
            return None
        reference = median(pass_one)
        threshold = self._threshold(reference)

        run: list[dict] = []
        previous = None
        for sample in reversed(samples):
            if sample["_ts"] < window_start:
                break
            if not self._good(sample):
                break
            if previous is not None and (
                    previous - sample["_ts"]).total_seconds() > \
                    self.config.sustained_max_gap_seconds:
                break
            if abs(float(sample["heart_rate"]) - reference) < threshold:
                break
            run.append(sample)
            previous = sample["_ts"]
        run.reverse()

        if len(run) < self.config.sustained_min_samples:
            return None
        span = (run[-1]["_ts"] - run[0]["_ts"]).total_seconds()
        if span < self.config.sustained_min_duration_seconds:
            return None
        if any((later["_ts"] - earlier["_ts"]).total_seconds() >
               self.config.sustained_max_gap_seconds
               for earlier, later in zip(run, run[1:])):
            return None

        baseline = self._baseline(samples, run[0]["_ts"], now)
        if baseline is None:
            return None
        reference = baseline["median"]
        threshold = self._threshold(reference)
        signs = set()
        values = []
        for sample in run:
            value = float(sample["heart_rate"])
            if abs(value - reference) < threshold:
                return None
            signs.add(value > reference)
            values.append(value)
        if len(signs) != 1:
            return None

        return {
            "run_start": run[0]["_ts"].isoformat(),
            "run_end": run[-1]["_ts"].isoformat(),
            "run_samples": len(run), "run_span_seconds": round(span, 1),
            "baseline": baseline, "threshold_bpm": round(threshold, 1),
            "median_hr": median(values),
            "delta_bpm": round(median(values) - reference, 1),
            "direction": "up" if values and values[-1] > reference else "down",
        }

    def _active_sustained(self, device_id: str) -> dict | None:
        for event in self.storage.events(device_id, active_only=True):
            if event["kind"] == SUSTAINED_KIND:
                return event
        return None

    def _active_sustained_decision(self, device_id: str, event: dict,
                                   latest: dict, quality_basis: dict,
                                   data_mode: str, now: datetime) -> dict | None:
        # The event's lifecycle opened_at is server UTC.  The original sample
        # timeline is kept separately so it can exclude the candidate period
        # from the physiological baseline.
        opened = _parse(event.get("source_started_at")) or _parse(event.get("opened_at"))
        samples = self._samples(device_id, now)
        baseline = self._baseline(samples, opened, now) if opened else None
        if baseline is None:
            return self._verify(
                device_id, "baseline_insufficient_active",
                "持续变化事件仍在，但基线不足，无法确认是否解除。",
                {"event_id": event["id"], "quality": quality_basis},
                "good", data_mode, now,
                bucket=self.config.verify_bucket_seconds)

        threshold = self._threshold(baseline["median"])
        resolved_run: list[dict] = []
        previous = None
        for sample in reversed(samples):
            if sample["_ts"] < opened:
                break
            if not self._good(sample):
                break
            if previous is not None and (
                    previous - sample["_ts"]).total_seconds() > \
                    self.config.sustained_max_gap_seconds:
                break
            if abs(float(sample["heart_rate"]) - baseline["median"]) >= threshold:
                break
            resolved_run.append(sample)
            previous = sample["_ts"]
        resolved_run.reverse()
        if len(resolved_run) >= self.config.sustained_min_samples:
            span = (resolved_run[-1]["_ts"] - resolved_run[0]["_ts"]).total_seconds()
        else:
            span = 0.0
        if span >= self.config.sustained_min_duration_seconds:
            transition = self.storage.event_close(device_id, SUSTAINED_KIND, now)
            if transition:
                self.investigations.on_transitions(
                    device_id, [transition], notify=False)
            return self._emit(
                device_id=device_id, decision_type="INSIGHT",
                source="state_engine", reason_code="sustained_resolved",
                summary="持续变化条件解除；这只表示规则条件结束，不是健康恢复。",
                basis={"event_id": event["id"], "baseline": baseline,
                       "threshold_bpm": round(threshold, 1),
                       "normal_samples": len(resolved_run),
                       "normal_span_seconds": round(span, 1),
                       "quality": quality_basis},
                data_quality="good", data_mode=data_mode, now=now,
                event_id=event["id"], status="processed",
                dedup_key=f"event:{event['id']}:INSIGHT:sustained_resolved",
            )

        return self._emit(
            device_id=device_id, decision_type="OBSERVE",
            source="state_engine", reason_code="sustained_active",
            summary="持续变化事件仍在观察中；内部调查，不推送照护者。",
            basis={"event_id": event["id"], "baseline": baseline,
                   "threshold_bpm": round(threshold, 1),
                   "quality": quality_basis},
            data_quality="good", data_mode=data_mode, now=now,
            event_id=event["id"], status="recorded",
            dedup_key=(f"observe:{device_id}:"
                       f"{self._bucket(now, self.config.observe_heartbeat_seconds)}"),
        )

    def _create_sustained(self, device_id: str, detection: dict,
                          quality_basis: dict, data_mode: str,
                          now: datetime) -> dict:
        run_start = _parse(detection["run_start"])
        if run_start is None:
            raise ValueError("invalid sustained run_start")
        opened = self.storage.events_by_activity(
            device_id, SUSTAINED_KIND,
            now - timedelta(seconds=self.config.sustained_cooldown_seconds),
            now + timedelta(seconds=1), limit=1)
        if opened:
            return self._emit(
                device_id=device_id, decision_type="INVESTIGATE",
                source="baseline_tracker", reason_code="sustained_cooldown",
                summary="持续变化仍在冷却期内，未重复创建调查。",
                basis={"detection": detection, "quality": quality_basis,
                       "cooldown_event_id": opened[0]["id"]},
                data_quality="good", data_mode=data_mode, now=now,
                event_id=opened[0]["id"], status="suppressed",
                dedup_key=(f"sustained_cooldown:{device_id}:"
                           f"{self._bucket(now, self.config.sustained_cooldown_seconds)}"),
            )

        active = [event for event in self.storage.events(device_id, active_only=True)
                  if event["kind"] in PHYSIOLOGICAL_KINDS | {SUSTAINED_KIND}]
        if active:
            return self._emit(
                device_id=device_id, decision_type="INVESTIGATE",
                source="baseline_tracker", reason_code="sustained_existing_event",
                summary="已有生理事件正在调查，持续变化不重复建案。",
                basis={"detection": detection, "quality": quality_basis,
                       "active_event_id": active[0]["id"]},
                data_quality="good", data_mode=data_mode, now=now,
                event_id=active[0]["id"], status="suppressed",
                dedup_key=f"event:{active[0]['id']}:INVESTIGATE:suppressed",
            )

        claim_key = (f"sustained:{device_id}:{run_start.isoformat()}:"
                     f"{self._bucket(now, self.config.sustained_cooldown_seconds)}")
        decision_id, created = self.storage.put_decision({
            "device_id": device_id, "event_id": None, "investigation_id": None,
            "decision_type": "INVESTIGATE", "source": "baseline_tracker",
            "basis": {"detection": detection, "quality": quality_basis},
            "data_quality": "good", "data_mode": data_mode,
            "decided_at": now, "status": "recorded",
            "reason_code": "sustained_change",
            "summary": "检测到持续心率偏离基线，启动内部自主调查；非诊断、不推送照护者。",
            "policy_version": POLICY_VERSION, "dedup_key": claim_key,
        })
        current = self.storage.decision(decision_id)
        if not created and current and current.get("event_id"):
            return current

        # The event starts at the first deviating sample so the baseline used
        # when later judging resolution cannot include the candidate period.
        transition = self.storage.event_open(
            device_id, SUSTAINED_KIND, now, SUSTAINED_NOTE,
            source_started_at=run_start)
        if transition is None:
            # Another path may have created it, or a previous process crashed
            # after claiming the decision but before updating it.  Link the
            # existing event instead of creating a duplicate.
            existing = self._active_sustained(device_id)
            investigation = (self.storage.investigation_for_event(existing["id"])
                             if existing else None)
            self.storage.update_decision(
                decision_id,
                status="processed" if existing else "suppressed",
                event_id=existing["id"] if existing else None,
                investigation_id=investigation["id"] if investigation else None,
                summary="事件已由并发路径创建，未重复建案。")
            return self.storage.decision(decision_id)

        self.investigations.on_transitions(device_id, [transition], notify=False)
        investigation = self.storage.investigation_for_event(transition["event_id"])
        self.storage.update_decision(
            decision_id, status="processed", event_id=transition["event_id"],
            investigation_id=investigation["id"] if investigation else None)
        return self.storage.decision(decision_id)

    # -- helpers -----------------------------------------------------------
    def _emit(self, *, device_id: str, decision_type: str, source: str,
              reason_code: str, summary: str, basis: dict, data_quality: str,
              data_mode: str, now: datetime, status: str, dedup_key: str,
              event_id: str | None = None,
              investigation_id: str | None = None) -> dict:
        decision_id, _created = self.storage.put_decision({
            "device_id": device_id, "event_id": event_id,
            "investigation_id": investigation_id,
            "decision_type": decision_type, "source": source,
            "basis": basis, "data_quality": data_quality,
            "data_mode": data_mode, "decided_at": now, "status": status,
            "reason_code": reason_code, "summary": summary,
            "policy_version": POLICY_VERSION, "dedup_key": dedup_key,
        })
        return self.storage.decision(decision_id)

    def _quality(self, latest: dict | None, now: datetime) -> tuple[str, dict]:
        if latest is None:
            return "no_data", {"reason": "no_telemetry"}
        timestamp = _parse(latest.get("timestamp"))
        age = ((now - timestamp).total_seconds()
               if timestamp is not None else None)
        status = (latest.get("device_status") or {}).get("sampling_status")
        quality = latest.get("signal_quality", "unknown")
        basis = {
            "signal_quality": quality,
            "reading_valid": latest.get("reading_valid"),
            "sampling_status": status,
            "age_seconds": round(age, 1) if age is not None else None,
            "heart_rate": latest.get("heart_rate"),
            "spo2": latest.get("spo2"),
            "source": latest.get("source"),
            "data_mode": self._data_mode(latest),
        }
        if timestamp is None or age is None:
            return "unknown", {**basis, "reason": "invalid_timestamp"}
        if age > self.config.stale_seconds:
            return "stale", {**basis, "reason": "stale"}
        if age < -30:
            return "unknown", {**basis, "reason": "clock_skew"}
        if status in ("stalled", "disconnected"):
            return "stale", {**basis, "reason": "device_not_sampling"}
        if quality == "invalid":
            return "invalid", basis
        if quality == "poor" or latest.get("reading_valid") is not True:
            return "poor", basis
        if quality == "unknown" or status == "pending":
            return "unknown", basis
        if latest.get("heart_rate") is None:
            return "unknown", {**basis, "reason": "no_heart_rate"}
        return "good", basis

    @staticmethod
    def _data_mode(latest: dict | None) -> str:
        if not latest:
            return "unknown"
        value = latest.get("data_mode")
        if value in ("live", "replay"):
            return value
        if latest.get("source") == "simulator" or latest.get("dataset"):
            return "replay"
        if latest.get("source") in REAL_SOURCES:
            return "live"
        return "unknown"

    def _samples(self, device_id: str, now: datetime) -> list[dict]:
        start = now - timedelta(seconds=self.config.history_window_seconds)
        raw = self.storage.telemetry_between(
            device_id, start, now + timedelta(seconds=1), limit=4000,
            with_receipt=True)
        samples, seen = [], set()
        for item in raw:
            timestamp = _parse(item.get("timestamp"))
            if timestamp is None or timestamp > now + timedelta(seconds=1):
                continue
            received = _parse(item.get("_received_at"))
            if (received is not None
                    and (received - timestamp).total_seconds()
                    > monitoring.DEFAULT_MAX_SAMPLE_LAG_SECONDS):
                # Backlog uploads remain in history but are not live
                # physiology for baseline comparison.
                continue
            key = timestamp.isoformat()
            if key in seen:
                continue
            seen.add(key)
            samples.append({**item, "_ts": timestamp})
        samples.sort(key=lambda sample: sample["_ts"])
        return samples

    @staticmethod
    def _good(sample: dict) -> bool:
        status = (sample.get("device_status") or {}).get("sampling_status")
        return (sample.get("heart_rate") is not None
                and sample.get("reading_valid") is True
                and sample.get("signal_quality") == "good"
                and status not in ("pending", "stalled", "disconnected"))

    def _baseline(self, samples: list[dict], before: datetime,
                  now: datetime) -> dict | None:
        window_start = now - timedelta(seconds=self.config.baseline_window_seconds)
        usable = [sample for sample in samples
                  if sample["_ts"] < before and sample["_ts"] >= window_start
                  and self._good(sample)]
        if len(usable) < self.config.baseline_min_samples:
            return None
        span = (usable[-1]["_ts"] - usable[0]["_ts"]).total_seconds()
        if span < self.config.baseline_min_span_seconds:
            return None
        values = [float(sample["heart_rate"]) for sample in usable]
        return {"median": median(values), "count": len(usable),
                "span_seconds": round(span, 1),
                "first": usable[0]["_ts"].isoformat(),
                "last": usable[-1]["_ts"].isoformat()}

    def _threshold(self, baseline_median: float) -> float:
        return max(self.config.sustained_deviation_bpm,
                   abs(baseline_median) * self.config.sustained_deviation_ratio)

    def _quality_recovery(self, device_id: str, now: datetime) -> dict | None:
        recent = self.storage.decisions(device_id, limit=20)
        verify = next((item for item in recent
                       if item["decision_type"] == "VERIFY"
                       and item["reason_code"].startswith("quality_")), None)
        if verify is None:
            return None
        decided = _parse(verify.get("decided_at"))
        if decided is None or (now - decided).total_seconds() > 1800:
            return None
        recovery = next((item for item in recent
                         if item["decision_type"] == "INSIGHT"
                         and item["reason_code"] == "quality_recovered"), None)
        if recovery and _parse(recovery.get("decided_at")) >= decided:
            return None
        return verify

    @staticmethod
    def _bucket(now: datetime, seconds: int) -> int:
        return int(now.timestamp() // max(1, seconds))
