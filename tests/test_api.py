from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from guardian.clock import ManualClock
from guardian.main import create_app
from guardian.llm import LLMSettings
from tests.support import ClockedClient


def make_reading(index, **changes):
    data = {
        "device_id": "guardian_001",
        "timestamp": (datetime(2026, 10, 28, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat(),
        "source": "simulator", "heart_rate": 75, "spo2": 98,
        "acc_x": 0, "acc_y": 0, "acc_z": 1,
        "signal_quality": "good", "reading_valid": True,
    }
    data.update(changes)
    return data


def client(tmp_path, real=False):
    clock = ManualClock(datetime(2026, 10, 28, tzinfo=timezone.utc))
    return ClockedClient(
        create_app(str(tmp_path / "testing.db"), allow_real_rules=real,
                   llm_settings=LLMSettings(), clock=clock),
        clock)


def test_health_and_history(tmp_path):
    c = client(tmp_path)
    assert c.get("/health").json()["ok"]
    assert c.post("/v1/telemetry", json=make_reading(0)).status_code == 200
    assert len(c.get("/v1/history").json()) == 1
    assert c.get("/v1/live/status").json()["latest"]["heart_rate"] == 75
    assert c.get("/v1/tools").status_code == 200


def test_trigger_ack_and_recover(tmp_path):
    c = client(tmp_path)
    for i in range(3):
        result = c.post("/v1/telemetry", json=make_reading(i, heart_rate=130, spo2=91)).json()
    assert {x["kind"] for x in result["transitions"]} == {"high_heart_rate", "low_spo2", "concurrent_signals"}
    for i in range(3, 6):
        assert c.post("/v1/telemetry", json=make_reading(i, heart_rate=130, spo2=91)).json()["transitions"] == []
    events = c.get("/v1/events").json()
    assert len(events) == 3
    event_id = events[0]["id"]
    assert c.post(f"/v1/events/{event_id}/ack").json()["acknowledged"]
    # Close dwell is 10 s: recovery needs a continuous good run, not 3 samples.
    for i in range(6, 17):
        c.post("/v1/telemetry", json=make_reading(i))
    assert all(x["status"] == "resolved" for x in c.get("/v1/events").json())
    assert c.get("/v1/live/status").json()["active_events"] == []


def test_poor_quality_not_interpreted_as_physiological_event(tmp_path):
    c = client(tmp_path)
    for i in range(3):
        c.post("/v1/telemetry", json=make_reading(i, heart_rate=130, spo2=90, signal_quality="poor", reading_valid=False))
    assert {e["kind"] for e in c.get("/v1/events").json()} == {"data_quality"}


def test_real_device_passive_default(tmp_path):
    c = client(tmp_path)
    for i in range(3):
        response = c.post("/v1/telemetry", json=make_reading(i, source="stm32", heart_rate=130, spo2=90))
        assert response.json()["evaluation"] == "real_data_rules_disabled"
    assert c.get("/v1/events").json() == []


def test_validation_and_out_of_order(tmp_path):
    c = client(tmp_path)
    assert c.post("/v1/telemetry", json=make_reading(0, spo2=150)).status_code == 422
    assert c.post("/v1/telemetry", json=make_reading(0)).status_code == 200
    assert c.post("/v1/telemetry", json=make_reading(0)).status_code == 409
    assert c.post("/v1/tools/call", json={"device_id": "guardian_001", "tool": "delete_database"}).status_code == 422
    assert c.post("/v1/assistant", json={"device_id": "guardian_001", "question": "最近有异常事件吗？"}).json()["tool_used"] == "list_events"


def test_subsecond_timestamp_is_accepted(tmp_path):
    c = client(tmp_path)
    assert c.post("/v1/telemetry", json=make_reading(0)).status_code == 200
    assert c.post("/v1/telemetry", json=make_reading(0.1)).status_code == 200
    assert c.post("/v1/telemetry", json=make_reading(0.1)).status_code == 409
    assert c.post("/v1/telemetry", json=make_reading(0.05)).status_code == 409


def test_future_timestamp_cannot_block_current_readings(tmp_path):
    c = client(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert c.post("/v1/telemetry", json=make_reading(0, timestamp=future)).status_code == 422
    assert c.post("/v1/telemetry", json=make_reading(0)).status_code == 200


def test_missing_values_do_not_resolve_existing_events(tmp_path):
    c = client(tmp_path)
    for i in range(3):
        c.post("/v1/telemetry", json=make_reading(i, heart_rate=130, spo2=90))
    for i in range(3,6):
        c.post("/v1/telemetry", json=make_reading(i, heart_rate=None, spo2=None,
                                                acc_x=None, acc_y=None, acc_z=None))
    events = {e["kind"]: e["status"] for e in c.get("/v1/events").json()}
    assert events == {"high_heart_rate": "active", "low_spo2": "active",
                      "concurrent_signals": "active", "data_quality": "active"}


def test_bad_signal_does_not_resolve_existing_events(tmp_path):
    c = client(tmp_path)
    for i in range(3):
        c.post("/v1/telemetry", json=make_reading(i, heart_rate=130))
    for i in range(3,6):
        c.post("/v1/telemetry", json=make_reading(i, signal_quality="poor", reading_valid=False))
    assert {e["kind"]: e["status"] for e in c.get("/v1/events").json()} == {
        "high_heart_rate": "active", "data_quality": "active"}


def test_live_socket_reports_missing_data_as_stale(tmp_path):
    c = client(tmp_path)
    with c.websocket_connect("/ws/live?device_id=guardian_001") as ws:
        state = ws.receive_json()
        assert state["device_id"] == "guardian_001"
        assert state["latest"] is None and state["stale"] is True
