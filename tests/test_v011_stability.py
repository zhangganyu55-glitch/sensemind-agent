"""V0.1.1 stability, monitoring interruption and sweep reliability tests."""
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from guardian import monitoring
from guardian.agent import AgentCoordinator
from guardian.clock import ManualClock
from guardian.decision import DecisionEngine, SUSTAINED_KIND
from guardian.detection import DetectionEngine
from guardian.investigation import InvestigationService
from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.models import Telemetry
from guardian.storage import Storage
from tests.support import ClockedClient

DEVICE = "guardian_001"
NOW = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)


def make_engine(tmp_path, *, name="rules.db", open_dwell=2.0, close_dwell=10.0,
                gap=5.0):
    storage = Storage(str(tmp_path / name))
    engine = DetectionEngine(
        storage, allow_real_rules=True,
        open_dwell_seconds=open_dwell,
        close_dwell_seconds=close_dwell,
        max_sample_gap_seconds=gap)
    return storage, engine


def make_decision_engine(tmp_path, *, name="decision.db"):
    storage = Storage(str(tmp_path / name))
    agent = AgentCoordinator(storage, None)
    investigations = InvestigationService(
        storage, agent, None, max_attempts=1, max_workers=1,
        duplicate_window_seconds=0, retry_backoff_seconds=0,
        notification_cooldown_seconds=0)
    engine = DecisionEngine(
        storage, investigations, enabled=True, allow_real_sustained=True,
        clock=lambda: NOW)
    return storage, investigations, engine


class Feed:
    def __init__(self, storage, engine, *, device=DEVICE, when=NOW):
        self.storage = storage
        self.engine = engine
        self.device = device
        self.clock = ManualClock(when)

    @property
    def when(self):
        return self.clock.now()

    @when.setter
    def when(self, value):
        delta = (value - self.clock.now()).total_seconds()
        if delta > 0:
            self.clock.advance(delta)
        else:
            self.clock.set_now(value)

    def step(self, *, hr=70, spo2=98, quality="good", valid=True,
             source="simulator", advance=1.0):
        reading = Telemetry(
            device_id=self.device, timestamp=self.clock.now(), source=source,
            heart_rate=hr, spo2=spo2, acc_x=0, acc_y=0, acc_z=1,
            signal_quality=quality, reading_valid=valid)
        self.storage.put_telemetry(reading, received_at=self.clock.now())
        result = self.engine.analyze(
            reading, now=self.clock.now(), received_at=self.clock.now(),
            monotonic=self.clock.monotonic())
        self.clock.advance(advance)
        return result


def transitions(result, action):
    return [item for item in result["transitions"] if item["action"] == action]


def sustained_events(storage, device=DEVICE):
    return [event for event in storage.events(device, limit=100)
            if event["kind"] == SUSTAINED_KIND]


def decision_detection(start):
    return {
        "run_start": start.isoformat(), "run_end": NOW.isoformat(),
        "run_samples": 31, "run_span_seconds": 30, "baseline": {},
        "threshold_bpm": 15, "median_hr": 105, "delta_bpm": 30,
        "direction": "up",
    }


# 1. rule event does not flap on short recovery ----------------------------
def test_rule_event_does_not_flap_on_short_recovery(tmp_path):
    storage, engine = make_engine(tmp_path)
    feed = Feed(storage, engine)
    opens = closes = 0
    for _ in range(10):
        for _ in range(3):
            opens += len(transitions(feed.step(hr=130), "opened"))
        for _ in range(3):
            closes += len(transitions(feed.step(hr=70), "resolved"))
    assert opens == 1
    assert closes == 0
    active = storage.events(DEVICE, active_only=True)
    assert len(active) == 1 and active[0]["kind"] == "high_heart_rate"


def test_high_rate_samples_need_real_time_not_sample_count(tmp_path):
    storage, engine = make_engine(tmp_path)
    feed = Feed(storage, engine)
    result = None
    for _ in range(3):
        result = feed.step(hr=130, advance=0.1)
    assert transitions(result, "opened") == []
    feed.when = NOW + timedelta(seconds=1.0)
    feed.step(hr=130, advance=1.0)
    result = feed.step(hr=130, advance=1.0)
    assert len(transitions(result, "opened")) == 1


# 2. minimum recovery duration --------------------------------------------
def test_rule_event_requires_full_recovery_window(tmp_path):
    storage, engine = make_engine(tmp_path)
    feed = Feed(storage, engine)
    for _ in range(3):
        feed.step(hr=130)
    assert storage.events(DEVICE, active_only=True)

    result = None
    for _ in range(10):  # 9 s of good samples: below the 10 s close dwell
        result = feed.step(hr=70)
    assert transitions(result, "resolved") == []
    assert storage.events(DEVICE, active_only=True)

    result = feed.step(hr=70)  # 10 s span
    assert len(transitions(result, "resolved")) == 1
    assert not storage.events(DEVICE, active_only=True)


def test_short_recovery_does_not_create_second_event_or_investigation(tmp_path):
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=60)
    clock = ManualClock(base)
    client = ClockedClient(create_app(
        str(tmp_path / "short.db"), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=clock), clock)

    def post(index, hr):
        return client.post("/v1/telemetry", json={
            "device_id": DEVICE, "timestamp": (base + timedelta(seconds=index)).isoformat(),
            "source": "simulator", "heart_rate": hr, "spo2": 98,
            "acc_x": 0, "acc_y": 0, "acc_z": 1,
            "signal_quality": "good", "reading_valid": True})

    for index in range(3):
        post(index, 130)
    for index in range(3, 6):
        post(index, 70)
    for index in range(6, 9):
        post(index, 130)

    storage = Storage(str(tmp_path / "short.db"))
    events = [event for event in storage.events(DEVICE, limit=100)
              if event["kind"] == "high_heart_rate"]
    assert len(events) == 1
    assert storage.investigation_for_event(events[0]["id"])
    assert len(storage.recent_investigations(limit=100, device_id=DEVICE)) == 1


# 3. active event + data interruption --------------------------------------
def test_data_gap_keeps_event_active_and_monitoring_stale(tmp_path):
    storage, engine = make_engine(tmp_path)
    feed = Feed(storage, engine)
    for _ in range(3):
        feed.step(hr=130)
    last = feed.when - timedelta(seconds=1)
    state = storage.monitoring(DEVICE, now=last + timedelta(seconds=31))
    assert state["state"] == "STALE"
    assert storage.events(DEVICE, active_only=True)

    # Data resumes after a gap: the event is re-evaluated, not auto-resolved.
    feed.when = last + timedelta(seconds=40)
    for _ in range(3):
        result = feed.step(hr=70)
    assert transitions(result, "resolved") == []
    assert storage.events(DEVICE, active_only=True)


def test_recovery_after_gap_revalidates_and_updates_last_valid(tmp_path):
    storage, engine = make_engine(tmp_path)
    feed = Feed(storage, engine)
    for _ in range(3):
        feed.step(hr=130)
    last = feed.when - timedelta(seconds=1)
    feed.when = last + timedelta(seconds=40)
    result = None
    for _ in range(10):
        result = feed.step(hr=70)
    assert transitions(result, "resolved") == []
    result = feed.step(hr=70)
    assert len(transitions(result, "resolved")) == 1
    state = storage.monitoring(DEVICE, now=feed.when - timedelta(seconds=1))
    assert state["state"] == "NORMAL"
    assert state["last_valid_at"] is not None


# 4. monitoring states -----------------------------------------------------
def test_monitoring_states_and_last_valid_at(tmp_path):
    storage = Storage(str(tmp_path / "monitor.db"))
    assert storage.monitoring("d1")["state"] == "NO_DATA"

    good = {"device_id": "d1", "timestamp": NOW.isoformat(),
            "signal_quality": "good", "reading_valid": True,
            "device_status": None}
    state = monitoring.record(storage, "d1", good, now=NOW)
    assert state["state"] == "NORMAL"
    valid_at = state["last_valid_at"]
    assert valid_at is not None

    poor = {**good, "signal_quality": "poor", "reading_valid": False}
    state = monitoring.record(storage, "d1", poor, now=NOW + timedelta(seconds=1))
    assert state["state"] == "UNKNOWN"
    assert state["last_valid_at"] == valid_at

    stale = storage.monitoring("d1", now=NOW + timedelta(seconds=40))
    assert stale["state"] == "STALE"
    assert stale["last_valid_at"] == valid_at

    good_later = {**good, "timestamp": (NOW + timedelta(seconds=50)).isoformat()}
    state = monitoring.record(storage, "d1", good_later, now=NOW + timedelta(seconds=50))
    assert state["state"] == "NORMAL"
    assert state["last_valid_at"] != valid_at


def test_monitoring_endpoint(tmp_path):
    clock = ManualClock(NOW)
    client = ClockedClient(create_app(
        str(tmp_path / "endpoint.db"), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=clock), clock)
    assert client.get("/v1/monitoring/unknown").json()["state"] == "NO_DATA"
    assert client.post("/v1/telemetry", json={
        "device_id": DEVICE,
        "timestamp": clock.now().isoformat(),
        "source": "simulator", "heart_rate": 70, "spo2": 98,
        "acc_x": 0, "acc_y": 0, "acc_z": 1,
        "signal_quality": "good", "reading_valid": True}).status_code == 200
    state = client.get(f"/v1/monitoring/{DEVICE}").json()
    assert state["state"] == "NORMAL"
    assert state["last_valid_at"] is not None


# 5. long sustained event cooldown ----------------------------------------
def test_long_event_cooldown_uses_closed_at(tmp_path):
    storage, _investigations, engine = make_decision_engine(tmp_path)
    storage.event_open(DEVICE, SUSTAINED_KIND, NOW - timedelta(seconds=700), "old")
    storage.event_close(DEVICE, SUSTAINED_KIND, NOW - timedelta(seconds=40))
    decision = engine._create_sustained(
        DEVICE, decision_detection(NOW - timedelta(seconds=30)), {}, "replay", NOW)
    assert decision["reason_code"] == "sustained_cooldown"
    assert decision["status"] == "suppressed"
    assert len(sustained_events(storage)) == 1


def test_cooldown_expires_after_close_window(tmp_path):
    storage, _investigations, engine = make_decision_engine(
        tmp_path, name="expired.db")
    old = storage.event_open(
        DEVICE, SUSTAINED_KIND, NOW - timedelta(seconds=1300), "old")
    storage.event_close(DEVICE, SUSTAINED_KIND, NOW - timedelta(seconds=700))
    decision = engine._create_sustained(
        DEVICE, decision_detection(NOW - timedelta(seconds=30)), {}, "replay", NOW)
    assert decision["reason_code"] == "sustained_change"
    assert decision["status"] == "processed"
    assert decision["event_id"] != old["event_id"]
    assert len(sustained_events(storage)) == 2


# 6. sweep error recovery --------------------------------------------------
def test_sweep_records_error_and_recovers(tmp_path, monkeypatch):
    storage, _investigations, engine = make_decision_engine(tmp_path)
    for device in ("bad", "good"):
        storage.put_telemetry(Telemetry(
            device_id=device, timestamp=NOW, source="simulator",
            heart_rate=70, spo2=98, acc_x=0, acc_y=0, acc_z=1,
            signal_quality="good", reading_valid=True))

    original = engine.evaluate

    def flaky(device_id, transitions=None, now=None):
        if device_id == "bad":
            raise RuntimeError("boom")
        return original(device_id, transitions, now)

    monkeypatch.setattr(engine, "evaluate", flaky)
    results = engine.sweep(now=NOW)
    assert results
    assert engine.last_sweep_at == NOW.isoformat()
    assert "bad:RuntimeError" in engine.last_error

    monkeypatch.setattr(engine, "evaluate", original)
    engine.sweep(now=NOW)
    assert engine.last_error is None


def test_sweep_thread_starts_and_stops(tmp_path):
    _storage, _investigations, engine = make_decision_engine(tmp_path)
    engine.start_background(interval_seconds=0.05)
    time.sleep(0.15)
    assert engine.status()["thread_alive"] is True
    engine.stop_background()
    assert engine.status()["thread_alive"] is False
    assert engine.last_sweep_at is not None


# 7. local app only starts sweep ------------------------------------------
def test_only_local_app_starts_sweep(tmp_path):
    local = create_app(
        str(tmp_path / "local.db"), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"),
        decision_engine_enabled=True, decision_background=True)
    public = create_app(
        str(tmp_path / "public.db"), public_only=True,
        llm_settings=LLMSettings(mode="off"),
        decision_engine_enabled=True, decision_background=True)
    assert local.state.decision_worker_allowed is True
    assert public.state.decision_worker_allowed is False

    with TestClient(local):
        assert local.state.decision_engine.status()["thread_alive"] is True
    assert local.state.decision_engine.status()["thread_alive"] is False

    with TestClient(public):
        assert public.state.decision_engine.status()["thread_alive"] is False


# 8. original notification path unchanged ---------------------------------
def test_rule_notification_still_created_without_decision_engine(tmp_path):
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
    clock = ManualClock(base)
    client = ClockedClient(create_app(
        str(tmp_path / "notify.db"), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=clock), clock)
    for index in range(3):
        client.post("/v1/telemetry", json={
            "device_id": DEVICE,
            "timestamp": (base + timedelta(seconds=index)).isoformat(),
            "source": "simulator", "heart_rate": 130, "spo2": 98,
            "acc_x": 0, "acc_y": 0, "acc_z": 1,
            "signal_quality": "good", "reading_valid": True})
    storage = Storage(str(tmp_path / "notify.db"))
    event = [item for item in storage.events(DEVICE, limit=100)
             if item["kind"] == "high_heart_rate"][0]
    notifications = storage.notifications_for(event["id"])
    assert notifications
    assert notifications[0]["status"] in ("pending", "sent", "failed")
