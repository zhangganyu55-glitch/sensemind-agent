"""Agent 3.0 Decision Engine V0.1 tests.

All scenarios use synthetic telemetry and an explicit clock.  No hardware,
no external model call and no real personal data are involved.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from guardian.agent import AgentCoordinator
from guardian.clock import ManualClock
from guardian.decision import (DecisionEngine, POLICY_VERSION, SUSTAINED_KIND)
from guardian.investigation import InvestigationService
from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.models import Telemetry
from guardian.storage import Storage
from tests.support import ClockedClient

DEVICE = "guardian_001"
# Align to a minute boundary so OBSERVE heartbeat bucket assertions are
# deterministic regardless of when the suite runs.
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def make_engine(tmp_path, *, enabled=True, real_sustained=False, name="decision.db"):
    storage = Storage(str(tmp_path / name))
    agent = AgentCoordinator(storage, None)
    investigations = InvestigationService(
        storage, agent, None, max_attempts=1, max_workers=1,
        duplicate_window_seconds=0, retry_backoff_seconds=0,
        notification_cooldown_seconds=0)
    engine = DecisionEngine(
        storage, investigations, enabled=enabled,
        allow_real_sustained=real_sustained, clock=lambda: NOW)
    return storage, investigations, engine


def seed(storage, series, *, device=DEVICE):
    for timestamp, hr, changes in series:
        changes = changes or {}
        storage.put_telemetry(Telemetry(
            device_id=device, timestamp=timestamp, source=changes.get("source", "simulator"),
            heart_rate=hr, spo2=changes.get("spo2", 98),
            acc_x=0, acc_y=0, acc_z=1,
            signal_quality=changes.get("quality", "good"),
            reading_valid=changes.get("valid", True)))


def normal_series(end=NOW, seconds=70, hr=70):
    return [(end - timedelta(seconds=seconds - index), hr, None)
            for index in range(seconds + 1)]


def sustained_series(end=NOW, baseline_hr=70, candidate_hr=105,
                     baseline_seconds=65, candidate_seconds=36, **changes):
    start = end - timedelta(seconds=baseline_seconds + candidate_seconds - 1)
    out = [(start + timedelta(seconds=index), baseline_hr, changes)
           for index in range(baseline_seconds)]
    out += [(start + timedelta(seconds=index), candidate_hr, changes)
            for index in range(baseline_seconds,
                               baseline_seconds + candidate_seconds)]
    return out


def events_of(storage, kind=None, device=DEVICE):
    events = storage.events(device, limit=100)
    return [event for event in events if kind is None or event["kind"] == kind]


def decisions_of(storage, decision_type=None, device=DEVICE):
    return storage.decisions(device, limit=100, decision_type=decision_type)


def wait_completed(client, event_id, timeout=15):
    deadline = time.monotonic() + timeout
    detail = None
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/console/events/{event_id}").json()
        investigation = detail.get("investigation")
        # Status is written just before the report in the existing Agent 2.0
        # state machine; wait for the report to avoid a transient read.
        if (investigation and investigation["status"] in ("completed", "failed")
                and investigation.get("report") is not None):
            return detail
        time.sleep(0.05)
    raise AssertionError(f"investigation did not finish: {detail}")


def app_client(tmp_path, *, decision=True, real_sustained=False, rules=True,
               llm=None, transport=None, name="app.db", public=False):
    clock = ManualClock(NOW)
    return ClockedClient(create_app(
        str(tmp_path / name), public_only=public,
        allow_real_rules=rules, llm_settings=llm or LLMSettings(mode="off"),
        llm_transport=transport,
        investigation_max_attempts=1, investigation_retry_backoff_seconds=0,
        decision_engine_enabled=decision,
        decision_real_sustained=real_sustained,
        decision_background=False,
        clock=clock), clock)


def reading(when, *, hr=70, spo2=98, acc_z=1, quality="good", valid=True,
            source="simulator", device=DEVICE):
    return {"device_id": device, "timestamp": iso(when), "source": source,
            "heart_rate": hr, "spo2": spo2,
            "acc_x": 0, "acc_y": 0, "acc_z": acc_z,
            "signal_quality": quality, "reading_valid": valid}


# 1. normal state does not start an investigation ---------------------------
def test_normal_state_records_observe_without_investigation(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, normal_series())
    records = engine.evaluate(DEVICE, [], now=NOW)

    assert records
    assert any(item["decision_type"] == "OBSERVE" for item in records)
    assert not events_of(storage)
    assert not decisions_of(storage, "INVESTIGATE")
    observe = decisions_of(storage, "OBSERVE")[0]
    assert observe["reason_code"] == "normal_observe"
    assert observe["basis"]["baseline_ready"] is True


# 2. unreliable data is verified first, never treated as normal -------------
def test_unreliable_data_records_verify_and_never_observe(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, [(NOW, 70, {"quality": "poor"})])
    records = engine.evaluate(DEVICE, [], now=NOW)

    assert [item["decision_type"] for item in records] == ["VERIFY"]
    assert records[0]["reason_code"] == "quality_poor"
    assert records[0]["data_quality"] == "poor"
    assert not events_of(storage)
    assert not decisions_of(storage, "OBSERVE")


def test_missing_telemetry_is_verify_not_observe(tmp_path):
    _storage, _investigations, engine = make_engine(tmp_path)
    records = engine.evaluate("ghost_device", [], now=NOW)
    assert [item["decision_type"] for item in records] == ["VERIFY"]
    assert records[0]["reason_code"] == "no_data"


# 3. sustained change creates one internal investigation -------------------
def test_sustained_change_creates_one_investigation_without_notification(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, sustained_series())
    records = engine.evaluate(DEVICE, [], now=NOW)

    investigations = [item for item in records
                      if item["decision_type"] == "INVESTIGATE"]
    assert len(investigations) == 1
    decision = investigations[0]
    assert decision["reason_code"] == "sustained_change"
    assert decision["status"] == "processed"
    assert decision["event_id"]
    assert decision["investigation_id"]

    events = events_of(storage, SUSTAINED_KIND)
    assert len(events) == 1
    assert events[0]["status"] == "active"
    # Lifecycle time is server UTC; the first deviating sample is preserved
    # separately for baseline exclusion.
    assert events[0]["opened_at"] == iso(NOW)
    assert events[0]["source_started_at"] < iso(NOW)
    assert storage.investigation_for_event(events[0]["id"])
    assert storage.notifications_for(events[0]["id"]) == []
    assert decision["basis"]["detection"]["baseline"]["median"] == 70
    assert decision["basis"]["detection"]["run_span_seconds"] >= 30


# 4. repeated/cooldown evaluation does not duplicate an investigation ------
def test_repeated_sustained_detection_does_not_duplicate(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, sustained_series())
    engine.evaluate(DEVICE, [], now=NOW)
    engine.evaluate(DEVICE, [], now=NOW)

    assert len(events_of(storage, SUSTAINED_KIND)) == 1
    assert len(decisions_of(storage, "INVESTIGATE")) == 1

    # Close the event, then re-detect within the cooldown window.
    storage.event_close(DEVICE, SUSTAINED_KIND, NOW)
    engine.evaluate(DEVICE, [], now=NOW)
    assert len(events_of(storage, SUSTAINED_KIND)) == 1
    cooled = [item for item in decisions_of(storage, "INVESTIGATE")
              if item["reason_code"] == "sustained_cooldown"]
    assert cooled and cooled[0]["status"] == "suppressed"


def test_active_physiological_event_suppresses_sustained_creation(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, sustained_series())
    storage.event_open(DEVICE, "high_heart_rate", NOW - timedelta(seconds=10),
                       "existing rule event")
    engine.evaluate(DEVICE, [], now=NOW)

    assert not events_of(storage, SUSTAINED_KIND)
    suppressed = [item for item in decisions_of(storage, "INVESTIGATE")
                  if item["reason_code"] == "sustained_existing_event"]
    assert suppressed and suppressed[0]["status"] == "suppressed"


# 5. DeepSeek failure does not lose the event or decision ------------------
def test_deepseek_failure_keeps_event_and_decision(tmp_path):
    def handler(request):
        raise httpx.ConnectError("provider down", request=request)

    client = app_client(
        tmp_path, llm=LLMSettings(mode="deepseek", api_key="test-key"),
        transport=httpx.MockTransport(handler))
    for index in range(3):
        response = client.post("/v1/telemetry", json=reading(
            NOW + timedelta(seconds=index), hr=130, acc_z=3))
        assert response.status_code == 200

    event = events_of(Storage(str(tmp_path / "app.db")), "high_heart_rate")[0]
    detail = wait_completed(client, event["id"])
    assert detail["investigation"]["report"]["analysis_source"] == "local_deterministic"
    assert detail["notifications"]
    decision = client.get(f"/v1/events/{event['id']}/decisions").json()["decisions"]
    assert decision and decision[0]["decision_type"] == "INVESTIGATE"


# 6. data interruption is never normal ------------------------------------
def test_sweep_marks_stale_data_as_verify(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, [(NOW - timedelta(seconds=120), 70, None)])
    records = engine.sweep(now=NOW)

    assert records
    assert [item["decision_type"] for item in records] == ["VERIFY"]
    assert records[0]["reason_code"] == "quality_stale"
    assert not decisions_of(storage, "OBSERVE")


# 7. access control --------------------------------------------------------
def test_decision_engine_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDIAN_DECISION_ENGINE", raising=False)
    client = TestClient(create_app(
        str(tmp_path / "default.db"), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), decision_background=False))
    assert client.get("/health").json()["decision_engine"]["enabled"] is False
    for index in range(3):
        assert client.post("/v1/telemetry", json=reading(
            NOW + timedelta(seconds=index), hr=130, acc_z=3)).status_code == 200
    assert client.get("/v1/decisions").json()["data"] == []


def test_public_app_does_not_expose_decisions(tmp_path):
    public = app_client(tmp_path, public=True, name="public.db")
    for path in ("/v1/decisions", "/v1/decisions/dec_x",
                 "/v1/events/e1/decisions", "/v1/investigations/i1"):
        assert public.get(path).status_code == 404


def test_local_app_exposes_decision_audit(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, normal_series())
    engine.evaluate(DEVICE, [], now=NOW)
    client = app_client(tmp_path, name="local.db")
    # The local app uses its own database; only route availability is asserted.
    assert client.get("/v1/decisions").status_code == 200
    assert client.get("/v1/decisions/dec_missing").status_code == 404


# 8. decision failure never blocks deterministic rule alerts ---------------
def test_decision_failure_does_not_block_rule_alert(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("decision failure")

    monkeypatch.setattr(DecisionEngine, "evaluate", explode)
    client = app_client(tmp_path, name="failure.db")
    for index in range(3):
        assert client.post("/v1/telemetry", json=reading(
            NOW + timedelta(seconds=index), hr=130, acc_z=3)).status_code == 200

    event = events_of(Storage(str(tmp_path / "failure.db")),
                      "high_heart_rate")[0]
    detail = client.get(f"/v1/console/events/{event['id']}").json()
    assert event["status"] == "active"
    assert detail["notifications"]


# additional: real-device protection --------------------------------------
def test_real_device_sustained_requires_explicit_flag(tmp_path):
    storage_protected, _inv, protected = make_engine(
        tmp_path, real_sustained=False, name="real_protected.db")
    seed(storage_protected, sustained_series(
        baseline_hr=70, candidate_hr=105, source="stm32"))
    protected.evaluate(DEVICE, [], now=NOW)
    assert not events_of(storage_protected, SUSTAINED_KIND)
    assert decisions_of(storage_protected, "OBSERVE")

    storage_allowed, _inv2, allowed = make_engine(
        tmp_path, real_sustained=True, name="real_allowed.db")
    seed(storage_allowed, sustained_series(
        baseline_hr=70, candidate_hr=105, source="stm32"))
    allowed.evaluate(DEVICE, [], now=NOW)
    assert len(events_of(storage_allowed, SUSTAINED_KIND)) == 1


# additional: contiguous time, not just sample count ----------------------
def test_discontinuous_samples_do_not_create_sustained_event(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    start = NOW - timedelta(seconds=100)
    seed(storage, [(start + timedelta(seconds=index), 70, None)
                   for index in range(65)])
    # Eight candidate samples over 35 s: too sparse (5 s gaps).
    seed(storage, [(start + timedelta(seconds=65 + 5 * index), 105, None)
                   for index in range(8)])
    engine.evaluate(DEVICE, [], now=NOW)

    assert not events_of(storage, SUSTAINED_KIND)
    assert not decisions_of(storage, "INVESTIGATE")


# additional: candidate period cannot pollute the baseline ----------------
def test_candidate_period_does_not_pollute_baseline(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, sustained_series())
    engine.evaluate(DEVICE, [], now=NOW)

    decision = [item for item in decisions_of(storage, "INVESTIGATE")
                if item["reason_code"] == "sustained_change"][0]
    baseline = decision["basis"]["detection"]["baseline"]
    assert baseline["median"] == 70
    assert baseline["count"] == 65
    assert decision["basis"]["detection"]["baseline"]["last"] < iso(NOW - timedelta(seconds=34))


# additional: all rule events get decisions in the same bucket ------------
def test_multiple_rule_events_are_not_dropped(tmp_path):
    client = app_client(tmp_path, name="multi.db")
    for index in range(3):
        response = client.post("/v1/telemetry", json=reading(
            NOW + timedelta(seconds=index), hr=130, spo2=90, acc_z=3))
        assert response.status_code == 200

    storage = Storage(str(tmp_path / "multi.db"))
    events = events_of(storage)
    kinds = {event["kind"] for event in events}
    assert {"high_heart_rate", "low_spo2", "unusual_motion",
            "concurrent_signals"} <= kinds
    decisions = decisions_of(storage, "INVESTIGATE")
    assert len({item["event_id"] for item in decisions}) == len(events)
    for event in events:
        assert storage.investigation_for_event(event["id"])
        assert storage.notifications_for(event["id"])


def test_sorted_duplicate_and_out_of_order_samples_do_not_trigger(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    start = NOW - timedelta(seconds=100)
    baseline = [(start + timedelta(seconds=index), 70, None)
                for index in range(65)]
    # Duplicate and out-of-order insertion; the detector must dedupe by time.
    candidate = [(start + timedelta(seconds=100 - index), 105, None)
                 for index in range(10)]
    seed(storage, baseline + candidate + candidate[:3])
    engine.evaluate(DEVICE, [], now=NOW)
    # A 10 s run is below the 30 s minimum.
    assert not events_of(storage, SUSTAINED_KIND)


# additional: audit growth control ----------------------------------------
def test_observe_heartbeat_is_rate_limited(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, normal_series())
    for offset in range(10):
        engine.evaluate(DEVICE, [], now=NOW + timedelta(seconds=offset))
    observes = decisions_of(storage, "OBSERVE")
    assert len(observes) == 1
    assert observes[0]["seen_count"] == 10


def test_same_event_repeat_increments_seen_count(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, normal_series())
    transition = storage.event_open(DEVICE, "high_heart_rate", NOW, "test")
    engine.evaluate(DEVICE, [transition], now=NOW)
    engine.evaluate(DEVICE, [transition], now=NOW)

    decisions = decisions_of(storage, "INVESTIGATE")
    assert len(decisions) == 1
    assert decisions[0]["seen_count"] == 2


def test_insufficient_baseline_never_forces_an_event(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    # Only 10 baseline samples, below the 20-sample minimum.
    seed(storage, [(NOW - timedelta(seconds=45 - index), 70, None)
                   for index in range(10)])
    seed(storage, [(NOW - timedelta(seconds=34 - index), 105, None)
                   for index in range(35)])
    engine.evaluate(DEVICE, [], now=NOW)
    assert not events_of(storage, SUSTAINED_KIND)


# additional: concurrency --------------------------------------------------
def test_concurrent_ingest_and_sweep_create_one_investigation(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, sustained_series())
    barrier = threading.Barrier(2)
    results = []

    def ingest():
        barrier.wait()
        results.append(engine.evaluate(DEVICE, [], now=NOW))

    def sweep():
        barrier.wait()
        results.append(engine.sweep(now=NOW))

    threads = [threading.Thread(target=ingest), threading.Thread(target=sweep)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(events_of(storage, SUSTAINED_KIND)) == 1
    assert len([item for item in decisions_of(storage, "INVESTIGATE")
                if item["reason_code"] == "sustained_change"]) == 1


# additional: restart recovery --------------------------------------------
def test_restart_recovers_active_event_and_closes_on_recovery(tmp_path):
    storage, investigations, engine = make_engine(tmp_path)
    seed(storage, sustained_series())
    engine.evaluate(DEVICE, [], now=NOW)
    event = events_of(storage, SUSTAINED_KIND)[0]
    assert event["status"] == "active"

    later = NOW + timedelta(seconds=120)
    seed(storage, [(NOW + timedelta(seconds=index), 70, None)
                   for index in range(1, 105)])
    restarted = DecisionEngine(
        storage, investigations, enabled=True, allow_real_sustained=False,
        clock=lambda: later)
    records = restarted.evaluate(DEVICE, [], now=later)

    assert events_of(storage, SUSTAINED_KIND)[0]["status"] == "resolved"
    assert any(item["decision_type"] == "INSIGHT"
               and item["reason_code"] == "sustained_resolved"
               for item in records)


def test_restart_preserves_observe_dedup(tmp_path):
    storage, investigations, engine = make_engine(tmp_path)
    seed(storage, normal_series())
    engine.evaluate(DEVICE, [], now=NOW)
    restarted = DecisionEngine(
        storage, investigations, enabled=True, clock=lambda: NOW + timedelta(seconds=10))
    restarted.evaluate(DEVICE, [], now=NOW + timedelta(seconds=10))
    observes = decisions_of(storage, "OBSERVE")
    assert len(observes) == 1
    assert observes[0]["seen_count"] == 2


# additional: audit fields and no model confidence -------------------------
def test_decision_audit_fields_are_complete(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, [(NOW, 70, {"quality": "poor"})])
    engine.evaluate(DEVICE, [], now=NOW)
    decision = decisions_of(storage)[0]
    required = {"decision_id", "device_id", "decision_type", "source",
                "basis", "data_quality", "data_mode", "decided_at", "status",
                "reason_code", "summary", "policy_version", "seen_count"}
    assert required <= set(decision)
    assert decision["policy_version"] == POLICY_VERSION
    assert decision["status"] in {"recorded", "processed", "suppressed", "failed"}
    assert decision["decision_type"] in {"OBSERVE", "VERIFY", "INVESTIGATE", "INSIGHT"}
    assert "confidence" not in json.dumps(decision["basis"], ensure_ascii=False).lower()


def test_quality_recovery_emits_non_urgent_insight(tmp_path):
    storage, _investigations, engine = make_engine(tmp_path)
    seed(storage, [(NOW - timedelta(seconds=10), 70, {"quality": "poor"})])
    engine.evaluate(DEVICE, [], now=NOW - timedelta(seconds=10))
    seed(storage, [(NOW, 70, None)])
    records = engine.evaluate(DEVICE, [], now=NOW)
    assert any(item["decision_type"] == "INSIGHT"
               and item["reason_code"] == "quality_recovered"
               for item in records)
