"""Durable ingestion processing.

The outbox rows live in SQLite (``guardian.outbox``).  This module executes
their stages in order and records each stage independently:

    telemetry job: monitoring -> rules -> investigations -> decisions -> supplement
    APK batch job: adapt (mirror eligible live samples into telemetry jobs)

A stage that completed is never executed again.  A failed stage keeps its
error and attempt count; ``recover()`` requeues interrupted/failed jobs on the
next startup.  Downstream writes are already idempotent:

* events: at most one active row per (device, kind)
* investigations: UNIQUE(event_id)
* evidence: UNIQUE(event_id, source_ref)
* decisions: UNIQUE(dedup_key)
* notifications: UNIQUE(event_id)

Rule counters are process memory.  ``DetectionEngine.process_rules`` snapshots
and restores them on failure, so retrying the same stage does not double-count
the sample.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from . import outbox
from .adapters import readings_from_apk, to_legacy_telemetry
from .apk_models import Measurement
from .clock import utc
from .models import Telemetry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .clock import Clock
    from .decision import DecisionEngine
    from .detection import DetectionEngine
    from .investigation import InvestigationService
    from .storage import Storage

# A job payload records the process that accepted it.  ``monotonic`` values
# are only comparable within that process; after a restart the processor uses
# a fresh tick so recovered backlog cannot manufacture a dwell window.
BOOT_ID = uuid4().hex


def _parse(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _merge_transitions(existing: list[dict],
                       new: list[dict]) -> list[dict]:
    """Merge rule transitions by (event_id, action), preserving order."""
    merged = list(existing)
    seen = {(item.get("event_id"), item.get("action")) for item in merged}
    for item in new:
        key = (item.get("event_id"), item.get("action"))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


class IngestionProcessor:
    def __init__(self, storage: "Storage", engine: "DetectionEngine",
                 investigations: "InvestigationService | None" = None,
                 decisions: "DecisionEngine | None" = None, *,
                 clock: "Clock | None" = None,
                 ingest_lock: threading.RLock | None = None,
                 max_attempts: int = outbox.DEFAULT_MAX_ATTEMPTS):
        self.storage = storage
        self.engine = engine
        self.investigations = investigations
        self.decisions = decisions
        self._clock = clock
        self.ingest_lock = ingest_lock or threading.RLock()
        self.max_attempts = max(1, int(max_attempts))
        # One processor instance serializes stage execution in-process.  The
        # production public entry is a proxy, so there is exactly one writer.
        self.lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------
    def recover(self, limit: int = 50) -> list[dict]:
        """Resume interrupted and failed jobs.

        Safe to call on every startup: completed stages are skipped, and
        ``reset_stale_jobs`` only changes rows left in ``processing`` by a
        previous process.  A completed job is never re-executed.
        """
        self.storage.reset_stale_jobs()
        results = []
        for job in self.storage.recoverable_jobs(limit):
            results.append(self.process_job(job["id"]))
        return results

    def process_pending(self, limit: int = 20) -> list[dict]:
        return [self.process_job(job["id"])
                for job in self.storage.recoverable_jobs(limit)]

    def recover_duplicate(self, device_id: str, timestamp: datetime):
        """Recover a failed job when the client retries a duplicate sample."""
        job = self.storage.job_for_timestamp(device_id, timestamp)
        if job is None or job["status"] == outbox.JOB_COMPLETED:
            return None
        return self.process_job(job["id"])

    # -- job execution -----------------------------------------------------
    def process_job(self, job_id: int):
        with self.ingest_lock, self.lock:
            job = self.storage.job(job_id)
            if job is None:
                return None
            if job["status"] == outbox.JOB_COMPLETED:
                return self._outcome(job)
            if job["attempts"] >= job["max_attempts"]:
                return self._outcome(job)

            job = self.storage.start_job(job_id)
            if job is None:
                return None
            try:
                if job["kind"] == "telemetry":
                    self._run_telemetry(job)
                elif job["kind"] == "apk_batch":
                    self._run_apk_batch(job)
                else:
                    raise ValueError(f"unknown ingestion job kind: {job['kind']}")
                if self.storage.job_stages_final(job_id):
                    self.storage.complete_job(job_id)
            except Exception as error:  # stage error already recorded
                self.storage.fail_job(
                    job_id, f"{type(error).__name__}: {error}")
            return self._outcome(self.storage.job(job_id))

    def _outcome(self, job: dict) -> dict:
        job = self.storage.job(job["id"]) or job
        stages = self.storage.job_stages(job["id"])
        rules = next((row for row in stages if row["stage"] == "rules"), None)
        result = (rules or {}).get("result") or {}
        public_stages = [{
            "stage": row["stage"], "status": row["status"],
            "attempts": row["attempts"], "reason": row.get("reason"),
            "error": row.get("last_error"), "result": row.get("result") or {},
        } for row in stages]
        return {
            "job_id": job["id"],
            "kind": job["kind"],
            "device_id": job["device_id"],
            "batch_id": job.get("batch_id"),
            "status": job["status"],
            "attempts": job["attempts"],
            "max_attempts": job["max_attempts"],
            "error": job.get("last_error"),
            "transitions": job.get("transitions") or [],
            "evaluation": result.get("evaluation"),
            "signals": result.get("signals") or [],
            "rule_evaluated": bool(result.get("rule_evaluated", False)),
            "stages": public_stages,
            "processing": {
                "job_id": job["id"],
                "kind": job["kind"],
                "status": job["status"],
                "attempts": job["attempts"],
                "max_attempts": job["max_attempts"],
                "error": job.get("last_error"),
                "stages": public_stages,
            },
        }

    # -- telemetry stages --------------------------------------------------
    def _run_telemetry(self, job: dict) -> None:
        payload = job["payload"]
        telemetry = Telemetry.model_validate(payload["telemetry"])
        now = _parse(payload["received_at"])
        monotonic = self._monotonic(payload)
        delayed = self._is_delayed(telemetry, now)

        for stage in outbox.TELEMETRY_STAGES:
            if self._stage_final(job["id"], stage):
                continue
            try:
                self._run_telemetry_stage(
                    job, telemetry, stage, now=now, monotonic=monotonic,
                    delayed=delayed)
            except Exception as error:
                self.storage.fail_stage(
                    job["id"], stage, f"{type(error).__name__}: {error}")
                raise
            if self.storage.job_stages_final(job["id"]):
                break

    def _run_telemetry_stage(self, job: dict, telemetry: Telemetry,
                             stage: str, *, now: datetime, monotonic: float,
                             delayed: bool) -> None:
        job_id = job["id"]
        device_id = job["device_id"]
        if stage == "monitoring":
            if delayed:
                self.storage.skip_stage(
                    job_id, stage, "delayed_sample",
                    result={"sample_age_seconds": round(
                        (now - telemetry.timestamp).total_seconds(), 3)})
                return
            result = self.engine.process_monitoring(
                telemetry, now=now, delayed=False)
            self.storage.complete_stage(job_id, stage, result=result)
            return

        if stage == "rules":
            if delayed:
                if (telemetry.source in ("stm32", "ring", "esp32")
                        and not self.engine.allow_real_rules):
                    self.storage.skip_stage(
                        job_id, stage, "real_data_rules_disabled",
                        result={"evaluation": "real_data_rules_disabled",
                                "rule_evaluated": False})
                else:
                    self.storage.skip_stage(
                        job_id, stage, "delayed_sample",
                        result={"evaluation": "delayed_sample_ignored",
                                "rule_evaluated": False})
                return
            def record_transition(item):
                # Persist immediately: a later rule failure must not lose an
                # event that already opened.
                self.storage.append_job_transition(job_id, item)

            result = self.engine.process_rules(
                telemetry, now=now, received_at=now, monotonic=monotonic,
                delayed=False, time_status="accepted", continuity={},
                on_transition=record_transition)
            transitions = result.get("transitions") or []
            existing = (self.storage.job(job_id) or {}).get("transitions") or []
            merged = _merge_transitions(existing, transitions)
            summary = {
                "evaluation": result.get("evaluation"),
                "signals": result.get("signals") or [],
                "rule_evaluated": bool(result.get("rule_evaluated", False)),
                "transition_count": len(merged),
            }
            if result.get("evaluation") == "real_data_rules_disabled":
                self.storage.skip_stage(
                    job_id, stage, "real_data_rules_disabled",
                    result=summary)
            else:
                self.storage.complete_stage(
                    job_id, stage, result=summary, transitions=merged)
            return

        if delayed:
            self.storage.skip_stage(job_id, stage, "delayed_sample")
            return

        current = self.storage.job(job_id) or job
        transitions = current.get("transitions") or []
        if stage == "investigations":
            if not transitions:
                self.storage.skip_stage(job_id, stage, "no_transitions",
                                        result={"transition_count": 0})
                return
            if self.investigations is None:
                self.storage.skip_stage(job_id, stage, "no_investigation_service")
                return
            self.investigations.on_transitions(device_id, transitions)
            self.storage.complete_stage(
                job_id, stage, result={"transition_count": len(transitions)})
            return

        if stage == "decisions":
            if self.decisions is None or not self.decisions.enabled:
                self.storage.skip_stage(job_id, stage, "decision_engine_disabled")
                return
            records = self.decisions.evaluate(device_id, transitions, now=now)
            self.storage.complete_stage(
                job_id, stage, result={"decision_count": len(records)})
            return

        if stage == "supplement":
            if self.investigations is None:
                self.storage.skip_stage(job_id, stage, "no_investigation_service")
                return
            self.investigations.supplement_active(device_id)
            self.storage.complete_stage(job_id, stage, result={"status": "ok"})
            return

        raise ValueError(f"unknown telemetry stage: {stage}")

    # -- APK batch stage ---------------------------------------------------
    def apk_job_payload(self, device_id: str, batch_id: str,
                        measurements: list[Measurement], *,
                        received_at: datetime,
                        monotonic: float | None = None) -> dict:
        return {
            "device_id": device_id,
            "batch_id": batch_id,
            "measurements": [item.model_dump(mode="json")
                             for item in measurements],
            "received_at": utc(received_at).isoformat(),
            "monotonic": (float(monotonic) if monotonic is not None
                          else float(self._clock.monotonic()
                                     if self._clock is not None else 0.0)),
            "boot_id": BOOT_ID,
        }

    def _run_apk_batch(self, job: dict) -> None:
        if not self._stage_final(job["id"], "adapt"):
            try:
                self._adapt(job)
            except Exception as error:
                self.storage.fail_stage(
                    job["id"], "adapt", f"{type(error).__name__}: {error}")
                raise
            if not self._stage_final(job["id"], "adapt"):
                raise RuntimeError("adapt stage did not complete")
        # A batch job has only one stage; process_job marks it complete.

    def _adapt(self, job: dict) -> None:
        payload = job["payload"]
        device_id = payload.get("device_id") or job["device_id"]
        batch_id = payload.get("batch_id") or job.get("batch_id")
        now = _parse(payload["received_at"])
        monotonic = self._monotonic(payload)
        measurements = [Measurement.model_validate(item)
                        for item in payload.get("measurements") or []]
        feedable = [
            item for item in measurements
            if item.source == "live_device"
            and item.quality != "invalid"
            and item.observed_at <= now + timedelta(seconds=30)
        ]
        packets = [to_legacy_telemetry(reading)
                   for reading in readings_from_apk(device_id, feedable)]
        packets = sorted((packet for packet in packets if packet is not None),
                         key=lambda packet: packet.timestamp)

        progress = set(job.get("progress") or [])
        stats = {"packets": len(packets), "accepted": 0, "duplicates": 0,
                 "rejected": 0, "already_adapted": 0,
                 "pending_children": 0}
        for packet in packets:
            key = packet.timestamp.isoformat()
            if key in progress:
                stats["already_adapted"] += 1
                continue
            result = self._ingest_packet(
                packet, now=now, monotonic=monotonic, batch_id=batch_id)
            self.storage.append_job_progress(job["id"], key)
            progress.add(key)
            if result.get("accepted", True):
                stats["accepted"] += 1
            elif result.get("time_status") == "duplicate":
                stats["duplicates"] += 1
            else:
                stats["rejected"] += 1
            reference = result.get("persisted")
            if reference and reference.get("job_id"):
                child = self.process_job(reference["job_id"])
                if child and child.get("status") != outbox.JOB_COMPLETED:
                    stats["pending_children"] += 1
        self.storage.complete_stage(job["id"], "adapt", result=stats)

    def _ingest_packet(self, packet: Telemetry, *, now: datetime,
                       monotonic: float, batch_id: str) -> dict:
        def persist(sample: Telemetry):
            return self.storage.put_telemetry_with_job(
                sample, received_at=now, monotonic=monotonic,
                batch_id=batch_id, boot_id=BOOT_ID,
                max_attempts=self.max_attempts)

        def audit(sample: Telemetry, reason: str, continuity: dict):
            self.storage.record_telemetry_rejection(
                sample, received_at=now, reason=reason,
                continuity=continuity)

        return self.engine.analyze(
            packet, now=now, received_at=now, monotonic=monotonic,
            hydrate=True, persist=persist, audit=audit,
            defer_processing=True)

    # -- helpers -----------------------------------------------------------
    def _stage_final(self, job_id: int, stage: str) -> bool:
        rows = self.storage.job_stages(job_id)
        return all(row["status"] in outbox.FINAL_STAGE_STATES
                   for row in rows if row["stage"] == stage)

    def _is_delayed(self, telemetry: Telemetry, now: datetime) -> bool:
        data_mode = telemetry.data_mode or (
            "replay" if telemetry.source == "simulator" else "live")
        if data_mode == "replay":
            return False
        age = (now - telemetry.timestamp).total_seconds()
        return age > self.engine.max_sample_lag_seconds

    def _monotonic(self, payload: dict) -> float:
        stored = payload.get("monotonic")
        if stored is not None and payload.get("boot_id") == BOOT_ID:
            return float(stored)
        # A new process cannot compare its monotonic clock with the old one.
        # A fresh tick keeps recovered backlog from satisfying a dwell window.
        return float(self._clock.monotonic() if self._clock is not None else 0.0)
