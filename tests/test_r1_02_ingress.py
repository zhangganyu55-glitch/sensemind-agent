"""R1-02 Phase A: production ingress timebase integration tests.

These tests go through the real `/v1/telemetry` route.  They deliberately do
not call `DetectionEngine.analyze()` directly, because the regression being
fixed lived at the HTTP boundary (pre-filtering before the engine).
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from guardian.clock import ManualClock
from guardian.llm import LLMSettings
from guardian.main import create_app


BASE = datetime(2026, 10, 28, tzinfo=timezone.utc)
DEVICE = "r102_device"


def make_client(tmp_path):
    clock = ManualClock(BASE)
    database = tmp_path / "guardian.db"
    app = create_app(
        str(database), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=clock)
    return TestClient(app), clock, database


def reading(seconds, *, counter=None, source="ring", **changes):
    data = {
        "device_id": DEVICE,
        "timestamp": (BASE + timedelta(seconds=seconds)).isoformat(),
        "source": source,
        "heart_rate": 75,
        "spo2": 98,
        "acc_x": 0, "acc_y": 0, "acc_z": 1,
        "signal_quality": "good",
        "reading_valid": True,
    }
    if counter is not None:
        data["device_status"] = {
            "transport": "stlink",
            "sampling_status": "live",
            "sample_counter": counter,
        }
    data.update(changes)
    return data


def post(client, clock, payload, advance=1.0):
    clock.advance(advance)
    return client.post("/v1/telemetry", json=payload)


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_http_clock_rollback_reanchors_with_counter(tmp_path):
    client, clock, _ = make_client(tmp_path)

    first = post(client, clock, reading(0, counter=1))
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] is True

    # 6 s device-time jump, received 1 s later: a clock event, not a gap.
    jump = post(client, clock, reading(6, counter=2))
    assert jump.status_code == 200, jump.text
    assert jump.json()["continuity"]["clock_jump"] is True
    assert jump.json()["time_status"] == "clock_jump"

    # The correction is >5 s behind the jump and carries a later counter.
    # This must reach DetectionEngine, not be rejected by the HTTP layer.
    rollback = post(client, clock, reading(0.9, counter=3))
    assert rollback.status_code == 200, rollback.text
    body = rollback.json()
    assert body["accepted"] is True
    assert body["time_status"] == "clock_rollback"
    assert body["continuity"]["clock_rollback"] is True

    normal = post(client, clock, reading(1.0, counter=4))
    assert normal.status_code == 200, normal.text
    assert normal.json()["accepted"] is True

    history = client.get("/v1/history", params={"device_id": DEVICE}).json()
    assert [parse_ts(item["timestamp"]) for item in history] == [
        BASE + timedelta(seconds=value) for value in (0, 6, 0.9, 1.0)
    ]


def test_future_sample_does_not_poison_anchor_or_monitoring(tmp_path):
    client, clock, database = make_client(tmp_path)

    first = post(client, clock, reading(0))
    assert first.status_code == 200, first.text

    future = post(client, clock, reading(20))
    assert future.status_code == 200, future.text
    body = future.json()
    assert body["accepted"] is False
    assert body["evaluation"] == "clock_skew_ignored"
    assert body["time_status"] == "future_skew"

    normal = post(client, clock, reading(1))
    assert normal.status_code == 200, normal.text
    assert normal.json()["accepted"] is True

    history = client.get("/v1/history", params={"device_id": DEVICE}).json()
    assert len(history) == 2
    assert parse_ts(history[-1]["timestamp"]) == BASE + timedelta(seconds=1)

    status = client.get("/v1/live/status", params={"device_id": DEVICE}).json()
    assert parse_ts(status["latest"]["timestamp"]) == BASE + timedelta(seconds=1)

    monitoring = client.get(f"/v1/monitoring/{DEVICE}").json()
    assert monitoring["state"] == "NORMAL"
    assert parse_ts(monitoring["last_valid_at"]) == BASE + timedelta(seconds=1)

    with sqlite3.connect(database) as con:
        reasons = [row[0] for row in con.execute(
            "SELECT reason FROM telemetry_rejections WHERE device_id=?",
            (DEVICE,)).fetchall()]
    assert reasons == ["future_skew"]


def test_restart_hydrates_anchor_without_false_clock_jump(tmp_path):
    client, clock, database = make_client(tmp_path)
    assert post(client, clock, reading(0)).status_code == 200

    # New process, same database and a fresh clock.
    restarted_clock = ManualClock(BASE + timedelta(seconds=2))
    app = create_app(
        str(database), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=restarted_clock)
    restarted = TestClient(app)

    restarted_clock.advance(1)
    duplicate = restarted.post("/v1/telemetry", json=reading(0))
    assert duplicate.status_code == 409

    restarted_clock.advance(1)
    gap = restarted.post("/v1/telemetry", json=reading(6))
    assert gap.status_code == 200, gap.text
    body = gap.json()
    assert body["accepted"] is True
    assert body["continuity"]["gap_detected"] is True
    assert body["continuity"]["clock_jump"] is False


def test_duplicate_and_plain_out_of_order_still_rejected(tmp_path):
    client, clock, _ = make_client(tmp_path)
    assert post(client, clock, reading(0)).status_code == 200

    duplicate = post(client, clock, reading(0))
    assert duplicate.status_code == 409
    older = post(client, clock, reading(-0.1))
    assert older.status_code == 409

    # A jump followed by a zero-progress packet must not look like a rollback.
    jump = post(client, clock, reading(6, counter=2))
    assert jump.status_code == 200, jump.text
    assert jump.json()["continuity"]["clock_jump"] is True

    zero = post(client, clock, reading(6, counter=2))
    assert zero.status_code == 409
