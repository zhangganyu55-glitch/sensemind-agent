"""Autonomous, read-only investigation of rule-engine events.

The rule engine decides *whether* an event exists.  This module gathers real
records, records every tool call, and asks the model only for interpretation.
Sensor facts are built deterministically from tool results; model text is kept
in a separate `ai_*` section and never promoted to fact.
"""
import json
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from math import sqrt
from statistics import median

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .agent import AgentCoordinator
from .evidence import EvidenceBundle, EvidenceEngine
from .llm import DeepSeekClient, LLMError
from .reasoner import (STRUCTURED_PROMPT, StructuredAnalysis,
                       deterministic_structured, map_legacy_inferences,
                       parse_structured, validate_structured)
from .storage import Storage


ALERT_TEXT = {
    "high_heart_rate": "实验规则触发：检测到心率偏高（非诊断）。请人工核对设备与数据质量。",
    "low_spo2": "实验规则触发：检测到血氧偏低（非诊断）。请人工核对设备与数据质量。",
    "concurrent_signals": "实验规则触发：多路信号同时异常（非诊断）。请人工核对。",
    "unusual_motion": "实验规则触发：检测到明显运动（非诊断）。请结合现场情况核对。",
    "data_quality": "实验规则触发：数据质量不足，不能视为正常。请检查设备连接与佩戴。",
    "sustained_hr_change": "Agent 3.0 实验性持续变化检测（非诊断）。内部调查，不推送照护者。",
}

LIMITATIONS = [
    "实验性安全辅助原型，不是医疗器械，不具备经临床验证的癫痫发作识别能力。",
    "规则阈值为演示用软件数字，不是医学阈值；事件只表示规则条件满足。",
    "传感器未做临床标定；心率、血氧等数值只能作为原型数据看待。",
    "AI 分析只基于本次工具读取到的有限样本，不能替代人工判断或紧急救援。",
]


class WindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    before_minutes: int = Field(default=5, ge=1, le=60)
    after_minutes: int = Field(default=1, ge=0, le=60)


class QualityArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    before_minutes: int = Field(default=5, ge=1, le=60)


class SimilarArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hours: int = Field(default=24, ge=1, le=168)


class HistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=20, ge=1, le=100)


TOOL_ARGUMENTS = {
    "get_event": None,
    "get_latest": None,
    "get_window": WindowArgs,
    "get_quality": QualityArgs,
    "get_sensor_intelligence": None,
    "find_similar_events": SimilarArgs,
    "get_history": HistoryArgs,
    "summarize_status": None,
}

TOOL_SPECS = [
    {"name": "get_event", "description": "Read the rule event bound to this investigation.",
     "properties": {}},
    {"name": "get_latest", "description": "Read the latest telemetry packet for the bound device.",
     "properties": {}},
    {"name": "get_window",
     "description": "Read telemetry around the event time. Only the bound device is accessible.",
     "properties": {
         "before_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
         "after_minutes": {"type": "integer", "minimum": 0, "maximum": 60}}},
    {"name": "get_quality",
     "description": "Summarize signal quality, missing values and staleness before the event.",
     "properties": {"before_minutes": {"type": "integer", "minimum": 1, "maximum": 60}}},
    {"name": "get_sensor_intelligence",
     "description": "Read the latest experimental ML evidence for the bound device "
                    "(activity, PPG reliability, anomaly score, model version).",
     "properties": {}},
    {"name": "find_similar_events",
     "description": "Find earlier events of the same kind on the same device.",
     "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 168}}},
    {"name": "get_history", "description": "Read the most recent N telemetry packets.",
     "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
    {"name": "summarize_status",
     "description": "Read stale/clock-skew status and active events for the bound device.",
     "properties": {}},
]

BASE_CALLS = [
    ("get_event", {}),
    ("get_latest", {}),
    ("get_window", {"before_minutes": 5, "after_minutes": 1}),
    ("get_quality", {"before_minutes": 5}),
    ("get_sensor_intelligence", {}),
    ("find_similar_events", {"hours": 24}),
]

SYSTEM_PROMPT = """你是守护天使的自动调查 Agent（实验性安全辅助原型，不是医疗器械）。
你只能读取系统提供的只读工具；设备身份由服务端绑定，工具没有 device_id 参数。
必须明确区分：可验证的传感器事实、规则引擎判断、你的推测。
不能编造数值、时间、趋势或事件；不能声称确认、取消、修改事件或发出报警。
不能给出诊断、用药建议或急救承诺。
系统已提供事件、最新数据、前后窗口、质量与相似事件。你可以按需调用工具补充证据，
但最多再调用 {remaining} 次。工具结果为空或质量不足时，必须明确标注不确定性。
最后只输出一个 JSON 对象，不要输出 Markdown：
{{"inferences":["..."],"uncertainties":["..."],"recommended_actions":["..."],"narrative":"..."}}
inferences 只能基于工具结果；不确定的内容放进 uncertainties；
recommended_actions 只能是核对设备、人工查看、联系专业人员等保守建议。
"""


class AIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inferences: list[str] = Field(default_factory=list, max_length=8)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    recommended_actions: list[str] = Field(default_factory=list, max_length=8)
    narrative: str = Field(default="", max_length=2000)

    @field_validator("inferences", "uncertainties", "recommended_actions")
    @classmethod
    def short_items(cls, values):
        cleaned = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 400:
                raise ValueError("Analysis items must be 1-400 characters")
            cleaned.append(value.strip())
        return cleaned


def parse_analysis(text: str) -> AIAnalysis:
    if not isinstance(text, str) or not text.strip():
        raise LLMError("invalid_response")
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("invalid_response")
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        raise LLMError("invalid_response") from None
    try:
        return AIAnalysis.model_validate(data)
    except ValidationError:
        raise LLMError("invalid_response") from None


def _structured_error_code(error: ValueError) -> str:
    text = str(error)
    if text in ("invalid_evidence_ref", "event_id_mismatch",
                "no_observations", "no_evidence_references",
                "missing_conclusion"):
        return "invalid_evidence_refs"
    if text.startswith("fabricated_measurement"):
        return "fabricated_measurement"
    if text.startswith("unsafe_recommended_action"):
        return "unsafe_recommended_action"
    return "invalid_response"


def _stats(samples: list[dict], key: str) -> dict:
    values = [float(sample[key]) for sample in samples if sample.get(key) is not None]
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": median(values) if values else None,
        "max": max(values) if values else None,
    }


def describe_trend(samples: list[dict]) -> dict:
    """Descriptive half-window medians; never a medical trend claim."""
    usable = sorted(samples, key=lambda sample: sample["timestamp"])
    if len(usable) < 4:
        return {"status": "insufficient", "basis": "需要至少 4 条样本才能做半窗描述"}
    middle = len(usable) // 2
    first, second = usable[:middle], usable[middle:]

    def direction(key, threshold):
        before = _stats(first, key)["median"]
        after = _stats(second, key)["median"]
        if before is None or after is None:
            return {"first_median": before, "second_median": after,
                    "direction": "unknown"}
        if after - before > threshold:
            change = "up"
        elif before - after > threshold:
            change = "down"
        else:
            change = "stable"
        return {"first_median": before, "second_median": after, "direction": change}

    return {
        "status": "descriptive",
        "heart_rate": direction("heart_rate", max(2.0, abs(
            (_stats(first, "heart_rate")["median"] or 0)) * 0.05)),
        "spo2": direction("spo2", 1.0),
        "basis": "窗口前后半段中位数的描述性比较，不是医学趋势判断",
    }


def summarize_samples(samples: list[dict]) -> dict:
    quality_counts = {}
    for sample in samples:
        quality = sample.get("signal_quality", "unknown")
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
    both = sum(1 for sample in samples
               if sample.get("heart_rate") is not None and sample.get("spo2") is not None)
    magnitudes = []
    with_physiology = 0
    for sample in samples:
        if all(sample.get(key) is not None for key in ("acc_x", "acc_y", "acc_z")):
            magnitudes.append(sqrt(sample["acc_x"] ** 2 + sample["acc_y"] ** 2
                                   + sample["acc_z"] ** 2))
            if sample.get("heart_rate") is not None or sample.get("spo2") is not None:
                with_physiology += 1
    return {
        "heart_rate": _stats(samples, "heart_rate"),
        "spo2": _stats(samples, "spo2"),
        "acceleration": {"count": len(magnitudes),
                         "max_magnitude": max(magnitudes) if magnitudes else None,
                         "with_physiology": with_physiology},
        "quality_counts": quality_counts,
        "sources": sorted({sample.get("source", "unknown") for sample in samples}),
        "both_hr_spo2_samples": both,
        "first": samples[0]["timestamp"] if samples else None,
        "last": samples[-1]["timestamp"] if samples else None,
    }


class InvestigationTools:
    """Read-only tools bound to one event's device. No argument can change it."""

    def __init__(self, storage: Storage, agent: AgentCoordinator, event: dict):
        self.storage = storage
        self.agent = agent
        self.event = event
        self.device_id = event["device_id"]
        self.event_time = datetime.fromisoformat(event["opened_at"])

    def definitions(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": spec["name"], "description": spec["description"],
            "parameters": {"type": "object", "properties": spec["properties"],
                           "additionalProperties": False},
        }} for spec in TOOL_SPECS]

    def validate(self, name: str, raw_arguments: str) -> dict:
        if name not in TOOL_ARGUMENTS:
            raise LLMError("tool_call_rejected")
        if not isinstance(raw_arguments, str) or len(raw_arguments) > 4000:
            raise LLMError("tool_call_rejected")
        try:
            data = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError:
            raise LLMError("tool_call_rejected") from None
        if not isinstance(data, dict):
            raise LLMError("tool_call_rejected")
        model = TOOL_ARGUMENTS[name]
        if model is None:
            if data:
                raise LLMError("tool_call_rejected")
            return {}
        try:
            return model.model_validate(data).model_dump()
        except ValidationError:
            raise LLMError("tool_call_rejected") from None

    def call(self, name: str, arguments: dict) -> tuple[dict, dict]:
        if name == "get_event":
            result = dict(self.event)
            summary = {key: result.get(key) for key in
                       ("id", "device_id", "kind", "status", "opened_at")}
            return result, summary
        if name == "get_latest":
            result = self.storage.latest_valid(
                self.device_id, now=datetime.now(timezone.utc),
                max_future_seconds=5.0, max_lag_seconds=5.0)
            summary = None if result is None else {
                key: result.get(key) for key in
                ("timestamp", "source", "heart_rate", "spo2",
                 "signal_quality", "reading_valid")}
            return result, summary
        if name == "get_window":
            start = self.event_time - timedelta(minutes=arguments["before_minutes"])
            end = self.event_time + timedelta(minutes=arguments["after_minutes"])
            samples = self.storage.telemetry_between(self.device_id, start, end)
            summary = {"count": len(samples), **summarize_samples(samples)}
            result = {"before_minutes": arguments["before_minutes"],
                      "after_minutes": arguments["after_minutes"],
                      "from": start.isoformat(), "to": end.isoformat(),
                      "count": len(samples), **summarize_samples(samples)}
            # Keep the model context bounded; nearest samples are chosen for the report.
            if samples:
                before = [s for s in samples
                          if datetime.fromisoformat(s["timestamp"]) <= self.event_time][-20:]
                after = [s for s in samples
                         if datetime.fromisoformat(s["timestamp"]) > self.event_time][:10]
                result["samples"] = before + after
            else:
                result["samples"] = []
            return result, summary
        if name == "get_quality":
            start = self.event_time - timedelta(minutes=arguments["before_minutes"])
            # Include the sample at the exact event moment (end is exclusive).
            samples = self.storage.telemetry_between(
                self.device_id, start, self.event_time + timedelta(microseconds=1))
            latest = self.storage.latest_valid(
                self.device_id, now=datetime.now(timezone.utc),
                max_future_seconds=5.0, max_lag_seconds=5.0)
            status = self.agent.status(self.device_id)
            quality_counts = {}
            for sample in samples:
                quality = sample.get("signal_quality", "unknown")
                quality_counts[quality] = quality_counts.get(quality, 0) + 1
            result = {
                "window_minutes": arguments["before_minutes"],
                "sample_count": len(samples),
                "quality_counts": quality_counts,
                "reading_valid_count": sum(bool(sample.get("reading_valid")) for sample in samples),
                "missing_heart_rate": sum(sample.get("heart_rate") is None for sample in samples),
                "missing_spo2": sum(sample.get("spo2") is None for sample in samples),
                "sources": sorted({sample.get("source", "unknown") for sample in samples}),
                "latest_timestamp": latest.get("timestamp") if latest else None,
                "stale": status["stale"], "age_seconds": status["age_seconds"],
                "clock_skew": status["clock_skew"],
            }
            return result, result
        if name == "get_sensor_intelligence":
            row = self.storage.latest_ml_inference(self.device_id)
            if row is None:
                result = {"available": False, "reason": "no_model_inference"}
                return result, result
            result = dict(row["result"])
            result["inference_id"] = row["id"]
            result["stored_at"] = row["created_at"]
            summary = {key: result.get(key) for key in
                       ("available", "activity_label", "activity_confidence",
                        "signal_reliability", "anomaly_score", "model_version",
                        "window_start", "window_end", "dataset", "participant_id")}
            return result, summary
        if name == "find_similar_events":
            start = self.event_time - timedelta(hours=arguments["hours"])
            events = self.storage.similar_events(
                self.device_id, self.event["kind"], start, self.event_time, self.event["id"])
            result = {"hours": arguments["hours"], "count": len(events),
                      "events": [{key: item.get(key) for key in
                                  ("id", "opened_at", "status", "acknowledged")}
                                 for item in events[:10]]}
            return result, result
        if name == "get_history":
            samples = self.storage.history(self.device_id, arguments["limit"])
            result = {"count": len(samples), "samples": samples}
            summary = {"count": len(samples),
                       "first": samples[0]["timestamp"] if samples else None,
                       "last": samples[-1]["timestamp"] if samples else None}
            return result, summary
        if name == "summarize_status":
            result = self.agent.status(self.device_id)
            summary = {"stale": result["stale"], "age_seconds": result["age_seconds"],
                       "clock_skew": result["clock_skew"],
                       "active_event_count": len(result["active_events"])}
            return result, summary
        raise LLMError("tool_call_rejected")


TRANSIENT_LLM_ERRORS = {"timeout", "network_error", "rate_limited",
                        "provider_unavailable"}
ALLOWED_TRANSITIONS = {
    "queued": {"running", "failed"},
    "running": {"completed", "failed", "retrying"},
    "retrying": {"running", "failed"},
    "completed": set(),
    "failed": {"queued"},
}
TERMINAL_STATUSES = {"completed", "failed"}


class InvestigationService:
    def __init__(self, storage: Storage, agent: AgentCoordinator,
                 llm: DeepSeekClient | None = None, *,
                 max_tool_calls: int = 8, deadline_seconds: int = 20,
                 notification_ttl_seconds: int = 60,
                 notification_cooldown_seconds: int = 300,
                 max_attempts: int = 2,
                 retry_backoff_seconds: float = 0.2,
                 max_workers: int = 1,
                 max_llm_tokens: int = 8000,
                 total_deadline_seconds: int = 30,
                 duplicate_window_seconds: int = 300,
                 supplement_interval_seconds: float = 1.0,
                 evidence_engine: EvidenceEngine | None = None):
        if not 5 <= max_tool_calls <= 12:
            raise ValueError("max_tool_calls must be 5-12")
        if not 1 <= deadline_seconds <= 60:
            raise ValueError("deadline_seconds must be 1-60")
        if not 1 <= max_attempts <= 4:
            raise ValueError("max_attempts must be 1-4")
        if not 1 <= max_workers <= 4:
            raise ValueError("max_workers must be 1-4")
        if not 1 <= total_deadline_seconds <= 120:
            raise ValueError("total_deadline_seconds must be 1-120")
        if max_llm_tokens < 100:
            raise ValueError("max_llm_tokens must be >= 100")
        self.storage = storage
        self.agent = agent
        self.llm = llm
        self.max_tool_calls = max_tool_calls
        self.deadline_seconds = deadline_seconds
        self.notification_ttl_seconds = notification_ttl_seconds
        self.notification_cooldown_seconds = notification_cooldown_seconds
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.max_workers = max_workers
        self.max_llm_tokens = max_llm_tokens
        self.total_deadline_seconds = total_deadline_seconds
        self.duplicate_window_seconds = duplicate_window_seconds
        self.supplement_interval_seconds = supplement_interval_seconds
        self.evidence = evidence_engine or EvidenceEngine(storage)
        self._queue = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._worker_lock = threading.Lock()
        self._last_supplement = {}
        self._recover_incomplete()

    # -- trigger -----------------------------------------------------------
    def on_transitions(self, device_id: str, transitions: list[dict], *,
                       notify: bool = True) -> None:
        """Start investigations for opened events.

        ``notify=False`` is a real branch used only for decision-engine-created
        events (currently ``sustained_hr_change``): the event and investigation
        still exist, but no caregiver notification is created.  Rule-engine
        callers keep the default ``notify=True`` and are unchanged.
        """
        for transition in transitions:
            if transition.get("action") != "opened":
                continue
            event = self.storage.get_event(transition["event_id"])
            if not event:
                continue
            if notify:
                self.storage.create_notification(
                    event, ALERT_TEXT.get(event["kind"], "实验规则触发异常（非诊断）。"),
                    cooldown_seconds=self.notification_cooldown_seconds)
            duplicate_of = None
            opened = datetime.fromisoformat(event["opened_at"])
            prior = self.storage.similar_events(
                device_id, event["kind"],
                opened - timedelta(seconds=self.duplicate_window_seconds),
                opened, event["id"], limit=1)
            if prior:
                prior_investigation = self.storage.investigation_for_event(prior[0]["id"])
                if prior_investigation:
                    duplicate_of = prior[0]["id"]
            investigation, created = self.storage.create_investigation(
                event["id"], device_id, event["kind"])
            if created:
                if duplicate_of:
                    self.storage.update_investigation(
                        investigation["id"], duplicate_of=duplicate_of,
                        max_attempts=self.max_attempts)
                self._enqueue(investigation["id"])

    # -- workers -----------------------------------------------------------
    def _ensure_workers(self):
        with self._worker_lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            while len(self._workers) < self.max_workers:
                worker = threading.Thread(target=self._worker_loop,
                                          name=f"guardian-investigator-{len(self._workers)}",
                                          daemon=True)
                worker.start()
                self._workers.append(worker)

    def _enqueue(self, investigation_id: str):
        self._ensure_workers()
        self._queue.put(investigation_id)

    def _transition(self, investigation_id: str, from_status: str | None,
                    to_status: str, reason: str | None = None) -> None:
        if from_status == to_status:
            self.storage.update_investigation(investigation_id, status=to_status)
            return
        allowed = ALLOWED_TRANSITIONS.get(from_status or "", set())
        if to_status not in allowed:
            raise ValueError(f"invalid investigation transition {from_status}->{to_status}")
        self.storage.add_investigation_transition(
            investigation_id, from_status, to_status, reason)
        self.storage.update_investigation(investigation_id, status=to_status)

    def _recover_incomplete(self):
        """Requeue unfinished work; completed investigations are never rerun."""
        try:
            incomplete = self.storage.incomplete_investigations()
        except Exception:
            return
        for investigation in incomplete:
            attempts = investigation.get("attempts") or 0
            status = investigation.get("status")
            if attempts >= self.max_attempts:
                try:
                    self.storage.add_investigation_transition(
                        investigation["id"], status, "failed",
                        "recovered_attempts_exhausted")
                    self.storage.update_investigation(
                        investigation["id"], status="failed",
                        ai_status="recovered_abandoned",
                        error="recovered_after_restart", completed=True)
                except Exception:
                    pass
                continue
            if status == "running":
                try:
                    self.storage.add_investigation_transition(
                        investigation["id"], "running", "retrying",
                        "recovered_after_restart")
                    self.storage.update_investigation(
                        investigation["id"], status="retrying")
                except Exception:
                    pass
            self._enqueue(investigation["id"])

    def _worker_loop(self):
        while True:
            try:
                investigation_id = self._queue.get(timeout=1)
            except queue.Empty:
                try:
                    self.storage.expire_pending_notifications(self.notification_ttl_seconds)
                except Exception:
                    pass  # the test database may already be gone
                continue
            try:
                self._run(investigation_id)
            except Exception as error:  # never let one investigation kill the worker
                current = self.storage.investigation(investigation_id) or {}
                try:
                    self._transition(investigation_id, current.get("status"),
                                     "failed", f"investigation_failed:{type(error).__name__}")
                except Exception:
                    pass
                self.storage.update_investigation(
                    investigation_id, ai_status="failed",
                    error=f"investigation_failed:{type(error).__name__}", completed=True)
            finally:
                self._queue.task_done()
                try:
                    self.storage.expire_pending_notifications(self.notification_ttl_seconds)
                except Exception:
                    pass

    # -- follow-up evidence ------------------------------------------------
    def supplement(self, event_id: str, *, since: datetime | None = None):
        investigation = self.storage.investigation_for_event(event_id)
        if not investigation or not investigation.get("completed_at"):
            return None
        event = self.storage.get_event(event_id)
        if not event:
            return None
        cutoff = since or self.storage.latest_evidence_time(event_id)
        bundle = self.evidence.collect_for_event(event, since=cutoff)
        all_evidence = self.storage.evidence_for_event(event_id)
        report = investigation.get("report") or {}
        report["evidence"] = all_evidence
        report["evidence_availability"] = [item.model_dump()
                                           for item in bundle.availability]
        report["evidence_alignment"] = bundle.alignment
        report["follow_up_evidence_count"] = len(all_evidence)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.storage.update_investigation(
            investigation["id"], report=report, evidence_count=len(all_evidence))
        return {"investigation_id": investigation["id"],
                "evidence_count": len(all_evidence),
                "new_evidence": len(bundle.evidence)}

    def supplement_active(self, device_id: str) -> None:
        now = time.monotonic()
        if now - self._last_supplement.get(device_id, 0) < self.supplement_interval_seconds:
            return
        self._last_supplement[device_id] = now
        for event in self.storage.events(device_id, active_only=True):
            investigation = self.storage.investigation_for_event(event["id"])
            if investigation and investigation.get("completed_at"):
                try:
                    self.supplement(event["id"])
                except Exception:
                    pass

    def retry(self, investigation_id: str):
        investigation = self.storage.investigation(investigation_id)
        if not investigation:
            return None
        if investigation["status"] not in ("failed", "completed"):
            raise ValueError("not_retryable")
        retryable = ("timeout", "network_error", "rate_limited",
                     "provider_unavailable", "failed", "recovered_abandoned",
                     "cost_limit")
        if (investigation["status"] == "completed"
                and investigation.get("ai_status") not in retryable):
            raise ValueError("already_completed")
        self.storage.requeue_investigation(investigation_id)
        self.storage.add_investigation_transition(
            investigation_id, investigation["status"], "queued", "manual_retry")
        self._enqueue(investigation_id)
        return self.storage.investigation(investigation_id)

    # -- investigation -----------------------------------------------------
    def _run(self, investigation_id: str):
        investigation = self.storage.investigation(investigation_id)
        if not investigation or investigation.get("completed_at"):
            return
        self._transition(investigation_id, investigation.get("status"),
                         "running", "worker_started")
        attempts = (investigation.get("attempts") or 0) + 1
        self.storage.update_investigation(
            investigation_id, started=True, attempts=attempts,
            max_attempts=self.max_attempts)
        event = self.storage.get_event(investigation["event_id"])
        if not event:
            self._transition(investigation_id, "running", "failed", "event_not_found")
            self.storage.update_investigation(
                investigation_id, ai_status="failed",
                error="event_not_found", completed=True)
            return

        deadline = time.monotonic() + self.total_deadline_seconds
        evidence = self.evidence.collect_for_event(event)
        duplicate_of = investigation.get("duplicate_of_event_id")
        ai_status, ai_error, model, usage = "not_configured", None, None, {}
        analysis = None
        structured: StructuredAnalysis | None = None
        trace: list[dict] = []
        base: dict = {}
        tools = InvestigationTools(self.storage, self.agent, event)
        for name, arguments in BASE_CALLS:
            started = time.monotonic()
            result, summary = tools.call(name, arguments)
            trace.append(self._step(name, arguments, "system", result, summary,
                                    time.monotonic() - started))
            base[name] = result
        self._extra_context(tools, base, trace)

        if duplicate_of:
            # Duplicate events are merged into the earlier investigation's
            # evidence chain; do not spend a second model call on them.
            analysis = self._fallback_analysis(event, base)
            structured = deterministic_structured(
                event, evidence, status="completed",
                reason="重复事件已合并到之前的事件，未重复调用大模型。")
            ai_status = "duplicate_merged"
        elif self.llm:
            model = self.llm.settings.model
            while True:
                try:
                    analysis, structured, usage = self._llm_analysis(
                        event, tools, base, trace, evidence, deadline)
                    ai_status = "completed"
                    break
                except LLMError as failure:
                    code = str(failure)
                    usage = getattr(failure, "usage", {}) or usage
                    if code in TRANSIENT_LLM_ERRORS and attempts < self.max_attempts:
                        self._transition(investigation_id, "running", "retrying", code)
                        time.sleep(self.retry_backoff_seconds * attempts)
                        self._transition(investigation_id, "retrying", "running", "retry")
                        attempts += 1
                        self.storage.update_investigation(
                            investigation_id, attempts=attempts)
                        trace = [step for step in trace
                                 if step.get("actor") == "system"]
                        continue
                    ai_status, ai_error = code, (getattr(failure, "detail", None) or code)
                    break
                except Exception:
                    ai_status, ai_error = "failed", "analysis_failed"
                    break
        if analysis is None:
            analysis = self._fallback_analysis(event, base)
        if structured is None:
            structured = deterministic_structured(
                event, evidence,
                status="completed" if ai_status == "completed" else "inconclusive",
                reason=None if ai_status == "completed" else ai_error)

        event = self.storage.get_event(event["id"]) or event
        report = self._build_report(event, base, trace, analysis, ai_status,
                                    ai_error, model, usage, evidence, structured,
                                    attempts, duplicate_of)
        current = self.storage.investigation(investigation_id) or {}
        self._transition(investigation_id, current.get("status", "running"),
                         "completed", "worker_finished")
        self.storage.update_investigation(
            investigation_id, ai_status=ai_status, model=model, report=report,
            error=ai_error, completed=True,
            structured=structured.model_dump(mode="json"), usage=usage,
            evidence_count=len(evidence.evidence))

    def _extra_context(self, tools, base, trace):
        """Adaptive deterministic follow-ups when the initial evidence is weak."""
        quality = base["get_quality"]
        window = base["get_window"]

        def run(name, arguments):
            if len(trace) >= self.max_tool_calls:
                return None
            started = time.monotonic()
            result, summary = tools.call(name, arguments)
            trace.append(self._step(name, arguments, "system", result, summary,
                                    time.monotonic() - started))
            return result

        if window["count"] == 0:
            wider = run("get_window", {"before_minutes": 15, "after_minutes": 5})
            if wider is not None:
                base["get_window"] = wider
        elif quality["stale"] or any(
                quality["quality_counts"].get(key, 0)
                for key in ("poor", "unknown", "invalid")):
            history = run("get_history", {"limit": 50})
            if history is not None:
                base["history"] = history
        if base["find_similar_events"]["count"]:
            wider = run("find_similar_events", {"hours": 72})
            if wider is not None:
                base["find_similar_events"] = wider

    @staticmethod
    def _step(name, arguments, actor, result, summary, duration):
        return {
            "tool": name, "arguments": arguments, "actor": actor,
            "at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round(duration * 1000, 1),
            "result": summary,
        }

    def _llm_analysis(self, event, tools, base, trace, evidence, total_deadline):
        remaining = self.max_tool_calls - len(trace)
        messages = [
            {"role": "system",
             "content": STRUCTURED_PROMPT + f"\n最多再调用 {max(0, remaining)} 次只读工具。"},
            {"role": "user", "content": json.dumps({
                "event": base["get_event"],
                "latest": base["get_latest"],
                "window": base["get_window"],
                "quality": base["get_quality"],
                "similar_events": base["find_similar_events"],
                "evidence": evidence.as_context(),
                "evidence_availability": [item.model_dump()
                                          for item in evidence.availability],
                "alignment": evidence.alignment,
            }, ensure_ascii=False)},
        ]
        deadline = min(time.monotonic() + self.deadline_seconds, total_deadline)
        seen = set()
        usage = {}
        for _ in range(4):
            left = deadline - time.monotonic()
            if left <= 0:
                raise LLMError("timeout")
            choice = "auto" if remaining > 0 else "none"
            message, tokens = self.llm.complete(messages, tools.definitions(), choice, left,
                                                max_tokens=2000)
            for key, value in tokens.items():
                usage[key] = usage.get(key, 0) + value
            total_tokens = usage.get("total_tokens") or (
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
            if total_tokens > self.max_llm_tokens:
                raise LLMError("cost_limit", usage)
            calls = message.get("tool_calls") or []
            if not calls:
                content = message.get("content")
                try:
                    structured = validate_structured(
                        parse_structured(content), event=event, bundle=evidence,
                        extra_context=base)
                except ValueError as error:
                    code = _structured_error_code(error)
                    try:
                        legacy = parse_analysis(content)
                    except LLMError:
                        raise LLMError(code, usage, detail=str(error)) from None
                    structured = map_legacy_inferences(
                        legacy.inferences, legacy.uncertainties,
                        legacy.recommended_actions, legacy.narrative,
                        event_id=event["id"], bundle=evidence)
                    return legacy, structured, usage
                analysis = AIAnalysis(
                    inferences=[item.text for item in structured.observations][:8],
                    uncertainties=structured.uncertainties[:8],
                    recommended_actions=structured.recommended_action[:8],
                    narrative=structured.conclusion,
                )
                return analysis, structured, usage
            if remaining <= 0:
                raise LLMError("tool_limit_exceeded")
            prepared = []
            for call in calls:
                try:
                    call_id = call["id"]
                    function = call["function"]
                    name = function["name"]
                    raw = function["arguments"]
                    if (call.get("type") != "function" or not isinstance(call_id, str)
                            or not call_id or call_id in seen or not isinstance(raw, str)):
                        raise ValueError()
                    arguments = tools.validate(name, raw)
                except (ValueError, KeyError, TypeError, LLMError):
                    raise LLMError("tool_call_rejected") from None
                prepared.append((call_id, name, arguments))
            message_out = {"role": "assistant", "content": message.get("content"),
                           "tool_calls": calls}
            if message.get("reasoning_content"):
                message_out["reasoning_content"] = message["reasoning_content"]
            messages.append(message_out)
            for call_id, name, arguments in prepared:
                if remaining <= 0:
                    raise LLMError("tool_limit_exceeded")
                if call_id in seen:
                    raise LLMError("tool_call_rejected")
                seen.add(call_id)
                started = time.monotonic()
                result, summary = tools.call(name, arguments)
                trace.append(self._step(name, arguments, "agent", result, summary,
                                        time.monotonic() - started))
                remaining -= 1
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": json.dumps(result, ensure_ascii=False)})
        raise LLMError("tool_limit_exceeded")

    def _fallback_analysis(self, event, base) -> AIAnalysis:
        quality = base["get_quality"]
        window = base["get_window"]
        similar = base["find_similar_events"]
        latest = base["get_latest"]
        inferences, uncertainties, actions = [], [], []
        if latest is None:
            inferences.append("事件触发时没有可用的遥测记录，无法评估传感器趋势。")
        if quality["stale"]:
            inferences.append("最新数据已超过新鲜度阈值，设备可能离线或上传中断。")
        poor = sum(quality["quality_counts"].get(key, 0)
                   for key in ("poor", "unknown", "invalid"))
        if poor:
            inferences.append("窗口内存在低质量或无效样本，趋势和阈值判断的可靠性下降。")
        if window["count"] == 0:
            inferences.append("事件前后窗口没有遥测记录，无法计算变化趋势。")
        elif window["heart_rate"]["count"] or window["spo2"]["count"]:
            inferences.append(
                f"窗口内共 {window['count']} 条遥测；心率中位数 "
                f"{window['heart_rate']['median']}，血氧中位数 {window['spo2']['median']}。")
        trend = describe_trend(window.get("samples", []))
        if trend["status"] == "descriptive":
            for key, label in (("heart_rate", "心率"), ("spo2", "血氧")):
                direction = trend[key]["direction"]
                if direction in ("up", "down"):
                    inferences.append(
                        f"窗口后半段{label}中位数较前半段"
                        f"{'上升' if direction == 'up' else '下降'}"
                        "（描述性统计，不是医学趋势判断）。")
        if similar["count"]:
            inferences.append(f"过去 {similar['hours']} 小时有 {similar['count']} 次同类历史事件。")
        if window["both_hr_spo2_samples"]:
            inferences.append(
                f"有 {window['both_hr_spo2_samples']} 条样本同时包含心率与血氧；"
                "这只表示时间相关，不能证明因果关系。")
        if window["acceleration"]["with_physiology"]:
            inferences.append(
                f"有 {window['acceleration']['with_physiology']} 条样本同时包含加速度与生理指标；"
                "这只表示时间相关，不能证明因果关系。")
        model = base.get("get_sensor_intelligence") or {}
        if model.get("available"):
            parts = []
            if model.get("activity_label"):
                parts.append(f"活动={model['activity_label']} "
                             f"(置信度 {model.get('activity_confidence')})")
            if model.get("signal_reliability") is not None:
                parts.append(f"信号可靠性={model['signal_reliability']}")
            if model.get("anomaly_score") is not None:
                parts.append(f"异常评分={model['anomaly_score']}")
            if model.get("model_version"):
                parts.append(f"模型={model['model_version']}")
            if parts:
                inferences.append("模型辅助证据：" + "，".join(parts)
                                  + "。这是实验性辅助证据，不覆盖规则告警。")
        elif model.get("reason"):
            uncertainties.append(f"模型辅助证据不可用：{model['reason']}。")
        uncertainties.extend(LIMITATIONS)
        if poor or quality["stale"] or latest is None:
            uncertainties.append("数据质量或新鲜度不足，不能把当前状态解释为正常或安全。")
        if not similar["count"]:
            uncertainties.append("没有足够的同类历史事件，无法判断是否属于个人基线变化。")
        actions.extend([
            "核对设备佩戴、电量和连接状态，确认遥测是否有效。",
            "由照护者在控制台查看证据链并完成人工确认。",
            "若同类事件反复出现或数据持续异常，请结合现场情况联系专业人员；本系统不用于急救。",
        ])
        return AIAnalysis(inferences=inferences[:8], uncertainties=uncertainties[:8],
                          recommended_actions=actions[:8],
                          narrative="模型不可用，已生成本地确定性调查摘要。")

    def _build_report(self, event, base, trace, analysis, ai_status,
                      ai_error, model, usage, evidence, structured,
                      attempts, duplicate_of) -> dict:
        latest = base["get_latest"]
        window = base["get_window"]
        quality = base["get_quality"]
        similar = base["find_similar_events"]
        event_time = datetime.fromisoformat(event["opened_at"])
        if event.get("closed_at"):
            duration_seconds = max(0.0, (
                datetime.fromisoformat(event["closed_at"]) - event_time).total_seconds())
            duration_state = "resolved"
        else:
            duration_seconds = max(0.0, (
                datetime.now(timezone.utc) - event_time).total_seconds())
            duration_state = "active"
        trend = describe_trend(window.get("samples", []))
        facts = [{
            "text": (f"规则引擎于 {event['opened_at']} 为设备 {event['device_id']} "
                     f"创建 {event['kind']} 事件；当前状态 {event['status']}。"),
            "evidence": {"event_id": event["id"], "source": "rule_engine"},
        }]
        if latest:
            sampling = (latest.get("device_status") or {}).get("sampling_status")
            facts.append({
                "text": (f"最新遥测时间 {latest['timestamp']}，来源 {latest.get('source')}，"
                         f"心率 {latest.get('heart_rate')}，血氧 {latest.get('spo2')}，"
                         f"信号质量 {latest.get('signal_quality')}，"
                         f"reading_valid={latest.get('reading_valid')}，"
                         f"sampling_status={sampling}。"),
                "evidence": {"tool": "get_latest"},
            })
        else:
            facts.append({"text": "调查时没有可用遥测记录。",
                          "evidence": {"tool": "get_latest"}})
        motion = window["acceleration"]
        motion_text = (
            f"含三轴加速度的样本 {motion['count']} 条"
            f"（最大合加速度 {motion['max_magnitude']} g，"
            f"其中 {motion['with_physiology']} 条同时含生理指标）"
            if motion["count"] else "没有三轴加速度数据")
        facts.append({
            "text": (f"事件前 {window['before_minutes']} 分钟、"
                     f"事件后 {window['after_minutes']} 分钟的窗口内共 {window['count']} 条遥测；"
                     f"心率样本 {window['heart_rate']['count']} 条"
                     f"（最小 {window['heart_rate']['min']}，中位 {window['heart_rate']['median']}，"
                     f"最大 {window['heart_rate']['max']}），"
                     f"血氧样本 {window['spo2']['count']} 条"
                     f"（最小 {window['spo2']['min']}，中位 {window['spo2']['median']}，"
                     f"最大 {window['spo2']['max']}）；"
                     f"同时含两项的样本 {window['both_hr_spo2_samples']} 条；{motion_text}。"),
            "evidence": {"tool": "get_window", "from": window.get("from"),
                         "to": window.get("to")},
        })
        if motion["with_physiology"]:
            facts.append({
                "text": (f"窗口内有 {motion['with_physiology']} 条样本同时包含加速度与"
                         "心率/血氧；同时出现只表示时间相关，不能证明因果关系。"),
                "evidence": {"tool": "get_window", "analysis": "correlation_only"},
            })
        facts.append({
            "text": (f"质量统计 good={quality['quality_counts'].get('good', 0)}，"
                     f"poor={quality['quality_counts'].get('poor', 0)}，"
                     f"unknown={quality['quality_counts'].get('unknown', 0)}，"
                     f"invalid={quality['quality_counts'].get('invalid', 0)}；"
                     f"最新数据年龄 {quality['age_seconds']} 秒，stale={quality['stale']}，"
                     f"clock_skew={quality['clock_skew']}。"),
            "evidence": {"tool": "get_quality"},
        })
        facts.append({
            "text": f"过去 {similar['hours']} 小时同一设备同类事件 {similar['count']} 次。",
            "evidence": {"tool": "find_similar_events"},
        })
        model_evidence = base.get("get_sensor_intelligence") or {
            "available": False, "reason": "not_requested"}
        if model_evidence.get("available"):
            facts.append({
                "text": (f"模型辅助证据：活动 {model_evidence.get('activity_label')}"
                         f"（置信度 {model_evidence.get('activity_confidence')}），"
                         f"信号可靠性 {model_evidence.get('signal_reliability')}，"
                         f"异常评分 {model_evidence.get('anomaly_score')}，"
                         f"模型版本 {model_evidence.get('model_version')}；"
                         f"数据来源 {model_evidence.get('dataset')}/"
                         f"{model_evidence.get('participant_id')}，"
                         f"窗口 {model_evidence.get('window_start')} 至 "
                         f"{model_evidence.get('window_end')}。"
                         "模型只提供辅助证据，不覆盖规则引擎告警。"),
                "evidence": {"tool": "get_sensor_intelligence",
                             "experimental": True},
            })
        facts.append({
            "text": (f"事件{'已持续' if duration_state == 'active' else '持续'} "
                     f"{round(duration_seconds)} 秒"
                     f"（调查时状态：{'活动' if duration_state == 'active' else '已由规则解除'}）。"),
            "evidence": {"event_id": event["id"]},
        })
        if trend["status"] == "descriptive":
            direction_text = {"up": "上升", "down": "下降", "stable": "基本持平",
                              "unknown": "无法比较"}
            facts.append({
                "text": (f"窗口样本前半段→后半段心率中位数 "
                         f"{trend['heart_rate']['first_median']} → "
                         f"{trend['heart_rate']['second_median']}"
                         f"（{direction_text[trend['heart_rate']['direction']]}），"
                         f"血氧中位数 {trend['spo2']['first_median']} → "
                         f"{trend['spo2']['second_median']}"
                         f"（{direction_text[trend['spo2']['direction']]}）。"
                         "这是描述性统计，不是医学趋势判断。"),
                "evidence": {"tool": "get_window", "basis": trend["basis"]},
            })
        key_history = []
        if window.get("samples"):
            nearest = sorted(
                window["samples"],
                key=lambda sample: abs((datetime.fromisoformat(sample["timestamp"])
                                        - event_time).total_seconds()))[:12]
            key_history = [{key: sample.get(key) for key in
                            ("timestamp", "source", "heart_rate", "spo2",
                             "signal_quality", "reading_valid")}
                           for sample in sorted(nearest, key=lambda item: item["timestamp"])]
        return {
            "event": event,
            "device_id": event["device_id"],
            "event_time": event["opened_at"],
            "event_kind": event["kind"],
            "event_status": event["status"],
            "event_duration_seconds": round(duration_seconds, 1),
            "event_duration_state": duration_state,
            "trigger_rule": "experimental_threshold_rules",
            "trend": trend,
            "sources": quality["sources"],
            "data_quality": quality,
            "facts": facts,
            "rule_findings": [{
                "text": event["note"],
                "evidence": {"rule": "experimental_threshold_rules",
                             "not_medical": True},
            }],
            "ai_status": ai_status,
            "ai_error": ai_error,
            "analysis_source": ("ai" if ai_status == "completed"
                                else "duplicate_merged" if ai_status == "duplicate_merged"
                                else "local_deterministic"),
            "model": model,
            "ai_usage": usage,
            "ai_inferences": analysis.inferences,
            "uncertainties": analysis.uncertainties,
            "recommended_actions": analysis.recommended_actions,
            "ai_narrative": analysis.narrative,
            "model_evidence": model_evidence,
            "key_history": key_history,
            "tool_calls": trace,
            "evidence": evidence.as_context(limit=120),
            "evidence_availability": [item.model_dump()
                                      for item in evidence.availability],
            "evidence_alignment": evidence.alignment,
            "structured_analysis": structured.model_dump(mode="json"),
            "investigation_state": {
                "attempts": attempts,
                "max_attempts": self.max_attempts,
                "duplicate_of_event_id": duplicate_of,
            },
            "follow_up_evidence_count": len(evidence.evidence),
            "duplicate_of_event_id": duplicate_of,
            "limitations": LIMITATIONS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
