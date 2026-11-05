"""Experimental *non-medical* rules; not validated for seizures or patient care.

Event lifecycle is intentionally conservative:

* open requires ``required_samples`` consecutive bad samples *and* both a
  monotonic real-time ``open_dwell_seconds`` window and a valid sample-time
  window;
* close requires consecutive good samples and a longer
  ``close_dwell_seconds`` recovery window on both clocks;
* a data gap resets the candidate counters but never closes an active event;
* historical/backlogged live data is ignored for rule evaluation so it cannot
  masquerade as a real-time anomaly;
* future-dated live samples are rejected before they can update monitoring or
  become a timeline anchor;
* a backwards clock correction may re-anchor only after the same device
  produced an accepted forward clock jump and its sample counter proves the
  correction was produced afterwards; without such proof the sample is
  conservatively rejected until the device timeline catches up;
* each event kind has its own counters, so no global cooldown can suppress an
  independent safety event.

Lifecycle timestamps written to the event store are server UTC.  The original
sample time is kept separately as ``source_started_at``.
"""
from collections import defaultdict
from datetime import datetime
from math import sqrt
from threading import RLock
from typing import Callable

from . import monitoring
from .clock import Clock, SystemClock, utc
from .models import Telemetry
from .storage import Storage


class DetectionEngine:
    # Values intentionally demo-only: these are NOT medical advice or clinical cutoffs.
    def __init__(self, storage: Storage, allow_real_rules: bool = False,
                 required_samples: int = 3,
                 open_dwell_seconds: float = 2.0,
                 close_dwell_seconds: float = 10.0,
                 max_sample_gap_seconds: float = 5.0,
                 max_sample_lag_seconds: float = 5.0,
                 max_future_skew_seconds: float = 5.0,
                 monitoring_stale_seconds: float = 30.0,
                 clock: Clock | None = None):
        if required_samples < 1:
            raise ValueError("required_samples must be positive")
        if not 0 <= open_dwell_seconds <= 30:
            raise ValueError("open_dwell_seconds must be 0-30")
        if not 1 <= close_dwell_seconds <= 120:
            raise ValueError("close_dwell_seconds must be 1-120")
        if not 1 <= max_sample_gap_seconds <= 60:
            raise ValueError("max_sample_gap_seconds must be 1-60")
        if not 0 <= max_sample_lag_seconds <= 300:
            raise ValueError("max_sample_lag_seconds must be 0-300")
        if not 0 <= max_future_skew_seconds <= 30:
            raise ValueError("max_future_skew_seconds must be 0-30")
        self.storage = storage
        self.allow_real_rules = allow_real_rules
        self.required_samples = required_samples
        self.open_dwell_seconds = open_dwell_seconds
        self.close_dwell_seconds = close_dwell_seconds
        self.max_sample_gap_seconds = max_sample_gap_seconds
        self.max_sample_lag_seconds = max_sample_lag_seconds
        self.max_future_skew_seconds = max_future_skew_seconds
        self.monitoring_stale_seconds = monitoring_stale_seconds
        self.clock = clock or SystemClock()
        self.counters = defaultdict(self._empty)
        self._last_sample: dict[str, datetime] = {}
        self._last_monotonic: dict[str, float] = {}
        self._last_received: dict[str, datetime] = {}
        # Per-device clock anomaly state.  A rollback may only re-anchor after
        # this device produced an accepted forward clock jump.  The pre-jump
        # anchor prevents a duplicate/backlog packet from being mistaken for a
        # correction.
        self._clock_jump_state: dict[str, dict] = {}
        # A process restart loses the in-memory anchors.  The first sample for
        # a device is hydrated from the last accepted persisted row; a large
        # forward step is treated as a restart gap, not a device clock jump.
        self._restart_gap: set[str] = set()
        self.lock = RLock()

    @staticmethod
    def _empty():
        return {
            "bad": 0, "good": 0,
            "bad_since_mono": None, "bad_since_sample": None,
            "good_since_mono": None, "good_since_sample": None,
        }

    def reset_state(self) -> None:
        """Clear all in-memory dwell candidates and per-device clock state.

        A process restart constructs a fresh engine, so an unfinished dwell
        never survives a restart.  This explicit method is used by tests and
        any future hot-reload path.
        """
        with self.lock:
            self.counters.clear()
            self._last_sample.clear()
            self._last_monotonic.clear()
            self._last_received.clear()
            self._clock_jump_state.clear()
            self._restart_gap.clear()

    def _continuity(self, t: Telemetry, received: datetime, monotonic: float,
                    sample_delta: float | None, arrival_delta: float | None,
                    receipt_delta: float | None, *, gap: bool = False,
                    clock_jump: bool = False, clock_rollback: bool = False,
                    clock_uncertain: bool = False, delayed: bool = False,
                    out_of_order: bool = False, future_skew: bool = False) -> dict:
        return {
            "sample_time": t.timestamp.isoformat(),
            "received_at": received.isoformat(),
            "monotonic": monotonic,
            "sample_delta_seconds": (round(sample_delta, 3)
                                     if sample_delta is not None else None),
            "arrival_delta_seconds": (round(arrival_delta, 3)
                                      if arrival_delta is not None else None),
            "receipt_delta_seconds": (round(receipt_delta, 3)
                                      if receipt_delta is not None else None),
            "sample_age_seconds": round((received - t.timestamp).total_seconds(), 3),
            "gap_detected": gap,
            "clock_jump": clock_jump,
            "clock_rollback": clock_rollback,
            "clock_uncertain": clock_uncertain,
            "future_skew": future_skew,
            "delayed_sample": delayed,
            "out_of_order": out_of_order,
        }

    @staticmethod
    def _sample_counter(t: Telemetry) -> int | None:
        # Telemetry has no generic stream sequence or boot/session id.  The
        # STM32 bridge's DeviceStatus counter is the only authoritative 32-bit
        # sequence currently carried into the rule engine.  H1 raw
        # sequence/boot_id and APK vendor_sequence stay in their own rows and
        # are not promoted to Telemetry, so those paths use the conservative
        # no-counter policy.
        status = t.device_status
        return status.sample_counter if status is not None else None

    @staticmethod
    def _counter_after(counter: int | None, reference: int | None) -> bool:
        """Return True only when a 32-bit counter proves forward progress."""
        if counter is None or reference is None:
            return False
        delta = (counter - reference) & 0xffffffff
        return 0 < delta < 0x80000000

    def _hydrate_anchor(self, device_id: str, received: datetime,
                        monotonic: float) -> None:
        """Restore the newest *time-valid* sample time after a restart.

        ``latest_valid`` skips future-dated rows and keeps scanning backwards,
        so one legacy future row cannot erase an older valid anchor.
        """
        row = self.storage.latest_valid(
            device_id, now=received,
            max_future_seconds=self.max_future_skew_seconds,
            max_lag_seconds=self.max_sample_lag_seconds)
        if not row:
            return
        try:
            stored = datetime.fromisoformat(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            return
        self._last_sample[device_id] = stored
        self._last_monotonic[device_id] = monotonic
        self._last_received[device_id] = received
        self._restart_gap.add(device_id)

    @staticmethod
    def _safe_audit(audit, t: Telemetry, reason: str, continuity: dict) -> None:
        if audit is None:
            return
        try:
            audit(t, reason, continuity)
        except Exception:
            # Audit is best-effort; it must not change the rejection result.
            pass

    def _is_clock_rollback(self, device_id: str, t: Telemetry,
                           sample_time: datetime,
                           sample_delta: float | None,
                           arrival_delta: float | None,
                           receipt_delta: float | None,
                           sample_age: float) -> bool:
        """Recognise a fresh clock correction, not a late/duplicate sample.

        A rollback is only eligible when this device previously produced an
        accepted forward clock jump *and* the device's own sample counter
        proves the candidate was produced after that jump.  A single timestamp
        plus server receipt is not enough: a late pre-jump packet can look
        exactly like a correction.  Server receipt and monotonic time must
        strictly advance; equal ticks are rejected.
        """
        state = self._clock_jump_state.get(device_id)
        if state is None:
            return False
        if not self._counter_after(
                self._sample_counter(t), state.get("jump_counter")):
            return False
        if sample_time <= state["previous_sample"]:
            return False
        if sample_delta is None or sample_delta >= -self.max_sample_gap_seconds:
            return False
        if not -self.max_future_skew_seconds <= sample_age <= self.max_sample_lag_seconds:
            return False
        if arrival_delta is None or arrival_delta <= 0:
            return False
        if receipt_delta is None or receipt_delta <= 0:
            return False
        return True

    @staticmethod
    def _result(evaluation: str, transitions: list[dict], continuity: dict,
                *, rule_evaluated: bool = True, signals: list[str] | None = None,
                accepted: bool = True, time_status: str = "accepted",
                **extra) -> dict:
        return {
            "evaluation": evaluation,
            "signals": signals or [],
            "transitions": transitions,
            "rule_evaluated": rule_evaluated,
            "accepted": accepted,
            "time_status": time_status,
            "continuity": continuity,
            **extra,
        }

    def analyze(self, t: Telemetry, *, now: datetime | None = None,
                received_at: datetime | None = None,
                monotonic: float | None = None,
                persist: Callable[[Telemetry], None] | None = None,
                audit: Callable[[Telemetry, str, dict], None] | None = None,
                hydrate: bool = False, defer_processing: bool = False):
        """Accept or reject one sample and (optionally) process it.

        ``defer_processing=True`` performs only the timebase decision and
        persistence.  The caller must have persisted the sample together with
        a durable outbox job, then run the processing stages.  The default
        remains inline processing so direct engine callers keep the original
        behaviour.
        """
        moment = utc(now) if now is not None else utc(self.clock.now())
        received = utc(received_at) if received_at is not None else moment
        mono = float(monotonic) if monotonic is not None else float(self.clock.monotonic())

        with self.lock:
            if hydrate and t.device_id not in self._last_sample:
                self._hydrate_anchor(t.device_id, received, mono)
            last_sample = self._last_sample.get(t.device_id)
            last_mono = self._last_monotonic.get(t.device_id)
            last_received = self._last_received.get(t.device_id)
            sample_delta = ((t.timestamp - last_sample).total_seconds()
                            if last_sample is not None else None)
            arrival_delta = (mono - last_mono
                             if last_mono is not None else None)
            receipt_delta = ((received - last_received).total_seconds()
                             if last_received is not None else None)

            sample_age = (received - t.timestamp).total_seconds()
            data_mode = t.data_mode or ("replay" if t.source == "simulator" else "live")
            delayed = (sample_age > self.max_sample_lag_seconds
                       and data_mode != "replay")
            # Live data uses the strict 5 s future bound.  Replay data may use
            # a virtual schedule up to the model-level 30 s tolerance; it is
            # still prevented from updating live monitoring below.
            future_limit = (30.0 if data_mode == "replay"
                            else self.max_future_skew_seconds)
            future_skew = sample_age < -future_limit

            # Exact-timestamp duplicates are rejected independently of the
            # live anchor.  This matters for delayed samples: they are stored
            # but deliberately do not become the anchor, so without this probe
            # a retry would be persisted a second time.  A future timestamp is
            # still classified as future-skew rather than duplicate.
            if (not future_skew and persist is not None
                    and self.storage.telemetry_exists(t.device_id, t.timestamp)):
                continuity = self._continuity(
                    t, received, mono, sample_delta, arrival_delta,
                    receipt_delta, delayed=delayed,
                    clock_uncertain=(
                        self._clock_jump_state.get(t.device_id) is not None),
                    out_of_order=True)
                self._safe_audit(audit, t, "duplicate", continuity)
                return self._result(
                    "out_of_order_ignored", [], continuity,
                    rule_evaluated=False, accepted=False,
                    time_status="duplicate")

            # Detect a forward clock jump before the future check so the
            # diagnostic still reports both conditions on the same packet.
            # A delayed backlog packet is not live evidence and must not
            # create or clear jump state.  A hydrated first sample is a
            # restart, not a device clock jump.
            gap_detected = False
            clock_jump = False
            restart_sample = t.device_id in self._restart_gap
            if (not delayed and last_sample is not None
                    and sample_delta is not None and sample_delta > 0):
                if restart_sample:
                    if sample_delta > self.max_sample_gap_seconds:
                        gap_detected = True
                        self._reset_counters(t.device_id)
                else:
                    sample_advanced = sample_delta > self.max_sample_gap_seconds
                    arrival_advanced = (arrival_delta is not None
                                        and arrival_delta > self.max_sample_gap_seconds)
                    receipt_advanced = (receipt_delta is not None
                                        and receipt_delta > self.max_sample_gap_seconds)
                    if sample_advanced and not arrival_advanced and not receipt_advanced:
                        # The device timestamp jumped while the server was receiving
                        # normally.  Treat it as a clock event, not as 5+ seconds of
                        # observed physiology.
                        clock_jump = True
                        self._reset_counters(t.device_id)
                    elif arrival_advanced or receipt_advanced:
                        gap_detected = True
                        self._reset_counters(t.device_id)

            # A rejected future timestamp must not touch monitoring/last_valid
            # or become a new timeline anchor.  This check runs before the
            # real-rule gate so passive real-device monitoring is safe too.
            if future_skew:
                self._reset_counters(t.device_id)
                continuity = self._continuity(
                    t, received, mono, sample_delta, arrival_delta,
                    receipt_delta, gap=gap_detected, clock_jump=clock_jump,
                    delayed=delayed, future_skew=True)
                self._safe_audit(audit, t, "future_skew", continuity)
                return self._result(
                    "clock_skew_ignored", [], continuity,
                    rule_evaluated=False, accepted=False,
                    time_status="future_skew")

            # Once the device timeline passes the temporary jump anchor, the
            # anomaly is over: normal ordering resumes and the pending state
            # is cleared.  A duplicate exactly at the anchor does not clear it.
            prior_state = self._clock_jump_state.get(t.device_id)
            clear_pending = (prior_state is not None
                             and t.timestamp > prior_state["jump_sample"])

            persisted = None
            clock_rollback = False
            if sample_delta is not None and sample_delta <= 0:
                if (not delayed and self._is_clock_rollback(
                        t.device_id, t, t.timestamp, sample_delta,
                        arrival_delta, receipt_delta, sample_age)):
                    # The device returned to its normal timeline after a
                    # forward jump.  Re-anchor instead of rejecting every
                    # normal sample until the temporary anchor is reached.
                    clock_rollback = True
                    persisted = persist(t) if persist is not None else None
                    self._restart_gap.discard(t.device_id)
                    self._clock_jump_state.pop(t.device_id, None)
                    self._reset_counters(t.device_id)
                else:
                    # Late/duplicate/rollback data must not touch monitoring
                    # or the live candidate counters.  If a jump is still
                    # pending, report the uncertainty and wait for counter
                    # proof or the device timeline to catch up.
                    continuity = self._continuity(
                        t, received, mono, sample_delta, arrival_delta,
                        receipt_delta, delayed=delayed,
                        clock_uncertain=(
                            self._clock_jump_state.get(t.device_id)
                            is not None),
                        out_of_order=True)
                    time_status = ("duplicate" if sample_delta == 0
                                   else "out_of_order")
                    self._safe_audit(audit, t, time_status, continuity)
                    return self._result(
                        "out_of_order_ignored", [], continuity,
                        rule_evaluated=False, accepted=False,
                        time_status=time_status)

            if delayed:
                # Persist for history/audit, but do not promote this sample to
                # the live anchor, do not update Monitoring and do not run
                # rules.  The outbox job records all stages as skipped.
                persisted = persist(t) if persist is not None else None
                self._reset_counters(t.device_id)
                continuity = self._continuity(
                    t, received, mono, sample_delta, arrival_delta,
                    receipt_delta, delayed=True)
                # A real-device stream is read-only when experimental rules are
                # disabled.  Preserve the legacy evaluation label, but keep the
                # delayed sample out of Monitoring and out of the live anchor.
                if (t.source in ("stm32", "ring", "esp32")
                        and not self.allow_real_rules):
                    evaluation = "real_data_rules_disabled"
                    rule_skipped = "real_data_rules_disabled"
                else:
                    evaluation = "delayed_sample_ignored"
                    rule_skipped = None
                result = self._result(
                    evaluation, [], continuity,
                    rule_evaluated=False, time_status="delayed",
                    persisted=persisted, delayed=True,
                    **({"rule_skipped": rule_skipped} if rule_skipped else {}))
                if defer_processing:
                    result["processing_pending"] = bool(persisted)
                return result

            if not clock_rollback:
                persisted = persist(t) if persist is not None else None
            self._restart_gap.discard(t.device_id)
            if clear_pending:
                self._clock_jump_state.pop(t.device_id, None)
            if clock_jump and last_sample is not None:
                # Remember the pre-jump anchor and the device counter for
                # this device.  A later rollback is eligible only against
                # this recorded anomaly and only with counter-proven
                # ordering.
                self._clock_jump_state[t.device_id] = {
                    "previous_sample": last_sample,
                    "jump_sample": t.timestamp,
                    "jump_counter": self._sample_counter(t),
                }

            time_status = ("clock_rollback" if clock_rollback
                           else "clock_jump" if clock_jump else "accepted")

            continuity = self._continuity(
                t, received, mono, sample_delta, arrival_delta, receipt_delta,
                gap=gap_detected, clock_jump=clock_jump,
                clock_rollback=clock_rollback, delayed=False)

            self._last_sample[t.device_id] = t.timestamp
            self._last_monotonic[t.device_id] = mono
            self._last_received[t.device_id] = received

            if defer_processing:
                return self._result(
                    "accepted_pending_processing", [], continuity,
                    rule_evaluated=False, time_status=time_status,
                    persisted=persisted, processing_pending=True)

            return self.process_accepted(
                t, now=moment, received_at=received, monotonic=mono,
                delayed=False, time_status=time_status,
                continuity=continuity, persisted=persisted)

    def process_accepted(self, t: Telemetry, *, now: datetime,
                         received_at: datetime, monotonic: float,
                         delayed: bool = False, time_status: str = "accepted",
                         continuity: dict | None = None,
                         persisted=None):
        """Run Monitoring and rules for an already accepted sample.

        This is the inline-compatible path used when callers do not need the
        outbox.  The outbox processor calls ``process_monitoring`` and
        ``process_rules`` separately so each stage can be retried on its own.
        """
        continuity = continuity or {}
        with self.lock:
            if delayed:
                self._reset_counters(t.device_id)
                return self._result(
                    "delayed_sample_ignored", [], continuity,
                    rule_evaluated=False, time_status=time_status,
                    persisted=persisted, delayed=True)
            self.process_monitoring(t, now=now, delayed=False)
            return self.process_rules(
                t, now=now, received_at=received_at, monotonic=monotonic,
                delayed=False, time_status=time_status,
                continuity=continuity, persisted=persisted)

    def process_monitoring(self, t: Telemetry, *, now: datetime,
                           delayed: bool = False) -> dict:
        """Run only the Monitoring stage; safe to retry."""
        with self.lock:
            if delayed:
                return {"status": "skipped", "reason": "delayed_sample"}
            monitoring.record(
                self.storage, t.device_id, t, now=now,
                stale_seconds=self.monitoring_stale_seconds,
                max_future_seconds=self.max_future_skew_seconds,
                max_sample_lag_seconds=self.max_sample_lag_seconds)
            return {"status": "completed"}

    def process_rules(self, t: Telemetry, *, now: datetime,
                      received_at: datetime, monotonic: float,
                      delayed: bool = False,
                      time_status: str = "accepted",
                      continuity: dict | None = None,
                      persisted=None, on_transition=None):
        """Run only the rule/dwell stage; counters roll back on failure.

        Rule counters are process memory.  A failed stage restores the
        per-device snapshot so a retry evaluates the same sample from the same
        counter state instead of double-counting it.
        """
        continuity = continuity or {}
        with self.lock:
            if delayed:
                self._reset_counters(t.device_id)
                return self._result(
                    "delayed_sample_ignored", [], continuity,
                    rule_evaluated=False, time_status=time_status,
                    persisted=persisted, delayed=True)

            # The real-device safety gate takes precedence over stickiness: a
            # disabled real-device rule path must keep reporting the same
            # evaluation.  Replay/test compatibility is intentionally left to
            # T12-4.
            if t.source in ("stm32", "ring", "esp32") and not self.allow_real_rules:
                return self._result(
                    "real_data_rules_disabled", [], continuity,
                    rule_skipped="real_data_rules_disabled",
                    time_status=time_status, persisted=persisted)

            snapshot = self._snapshot_counters(t.device_id)
            transitions: list[dict] = []
            try:
                quality_good = t.reading_valid and t.signal_quality == "good"
                observed = {
                    "high_heart_rate": t.heart_rate is not None,
                    "low_spo2": t.spo2 is not None,
                    "unusual_motion": None not in (t.acc_x, t.acc_y, t.acc_z),
                }
                observed["concurrent_signals"] = all(observed.values())
                observed["data_quality"] = True
                signals = []
                if quality_good:
                    if t.heart_rate is not None and t.heart_rate >= 120:
                        signals.append("high_heart_rate")
                    if t.spo2 is not None and t.spo2 <= 93:
                        signals.append("low_spo2")
                    if None not in (t.acc_x, t.acc_y, t.acc_z):
                        mag = sqrt(t.acc_x ** 2 + t.acc_y ** 2 + t.acc_z ** 2)
                        if mag >= 2.5:
                            signals.append("unusual_motion")
                    if len(signals) >= 2:
                        signals.append("concurrent_signals")
                        observed["concurrent_signals"] = True
                    if not all(observed.values()):
                        signals.append("data_quality")
                else:
                    signals.append("data_quality")

                keys = ("high_heart_rate", "low_spo2", "unusual_motion",
                        "concurrent_signals", "data_quality")
                # Unknown/poor signal may never resolve an existing
                # physiological event.
                for key in keys:
                    if key != "data_quality" and (
                            not quality_good or not observed[key]):
                        self.counters[(t.device_id, key)] = self._empty()
                        continue
                    counts = self.counters[(t.device_id, key)]
                    if key in signals:
                        if counts["bad"] == 0:
                            counts["bad_since_mono"] = monotonic
                            counts["bad_since_sample"] = t.timestamp
                        counts["bad"] += 1
                        counts["good"] = 0
                        counts["good_since_mono"] = None
                        counts["good_since_sample"] = None
                        elapsed_mono = monotonic - counts["bad_since_mono"]
                        elapsed_sample = (
                            t.timestamp - counts["bad_since_sample"]).total_seconds()
                        if (counts["bad"] >= self.required_samples
                                and elapsed_mono >= self.open_dwell_seconds
                                and elapsed_sample >= self.open_dwell_seconds):
                            transition = self.storage.event_open(
                                t.device_id, key, received_at,
                                "DEMO ONLY: unvalidated signal rule; investigate "
                                "source/quality; not diagnosis",
                                source_started_at=counts["bad_since_sample"])
                            if transition:
                                transitions.append(transition)
                                if on_transition is not None:
                                    on_transition(transition)
                            counts["bad"] = 0
                            counts["bad_since_mono"] = None
                            counts["bad_since_sample"] = None
                    else:
                        if counts["good"] == 0:
                            counts["good_since_mono"] = monotonic
                            counts["good_since_sample"] = t.timestamp
                        counts["good"] += 1
                        counts["bad"] = 0
                        counts["bad_since_mono"] = None
                        counts["bad_since_sample"] = None
                        elapsed_mono = monotonic - counts["good_since_mono"]
                        elapsed_sample = (
                            t.timestamp - counts["good_since_sample"]).total_seconds()
                        if (counts["good"] >= self.required_samples
                                and elapsed_mono >= self.close_dwell_seconds
                                and elapsed_sample >= self.close_dwell_seconds):
                            transition = self.storage.event_close(
                                t.device_id, key, received_at)
                            if transition:
                                transitions.append(transition)
                                if on_transition is not None:
                                    on_transition(transition)
                            counts["good"] = 0
                            counts["good_since_mono"] = None
                            counts["good_since_sample"] = None
                return self._result(
                    "demo_experimental_rules", transitions, continuity,
                    signals=signals, time_status=time_status,
                    persisted=persisted)
            except Exception:
                self._restore_counters(t.device_id, snapshot)
                raise

    _RULE_KEYS = ("high_heart_rate", "low_spo2", "unusual_motion",
                  "concurrent_signals", "data_quality")

    def _snapshot_counters(self, device_id: str) -> dict:
        return {key: dict(self.counters[(device_id, key)])
                for key in self._RULE_KEYS}

    def _restore_counters(self, device_id: str, snapshot: dict) -> None:
        for key in self._RULE_KEYS:
            self.counters[(device_id, key)] = dict(snapshot[key])

    def _reset_counters(self, device_id: str) -> None:
        for key in ("high_heart_rate", "low_spo2", "unusual_motion",
                    "concurrent_signals", "data_quality"):
            self.counters[(device_id, key)] = self._empty()
