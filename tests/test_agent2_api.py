"""Agent 2.0: new evidence/investigation endpoints and access control."""
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from guardian.clock import ManualClock
from guardian.llm import LLMSettings
from guardian.main import create_app
from tests.support import ClockedClient


BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)


def reading(when, hr=130):
    return {"device_id": "guardian_001", "timestamp": when.isoformat(),
            "source": "simulator", "heart_rate": hr, "spo2": 98,
            "acc_x": 0, "acc_y": 0, "acc_z": 1,
            "signal_quality": "good", "reading_valid": True}


def local_client(tmp_path):
    clock = ManualClock(BASE)
    return ClockedClient(create_app(
        str(tmp_path / "api.db"), allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=clock), clock)


def test_public_app_does_not_expose_agent2_endpoints(tmp_path):
    public = TestClient(create_app(str(tmp_path / "public2.db"), public_only=True,
                                   llm_settings=LLMSettings(mode="off")))
    for path in ("/v1/investigations/x", "/v1/events/x/evidence",
                 "/v1/evidence/x", "/v1/console/snapshot"):
        assert public.get(path).status_code == 404
    assert public.post("/v1/investigations/x/retry").status_code == 404


def test_local_evidence_endpoints_are_read_only(tmp_path):
    client = local_client(tmp_path)
    event = None
    for index in range(3):
        response = client.post("/v1/telemetry", json=reading(
            BASE + timedelta(seconds=index)))
        assert response.status_code == 200
    events = [item for item in client.get(
        "/v1/events", params={"device_id": "guardian_001"}).json()
        if item["kind"] == "high_heart_rate"]
    event = events[0]
    deadline = time.monotonic() + 6
    investigation = None
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/console/events/{event['id']}").json()
        investigation = detail.get("investigation")
        if investigation and investigation["status"] == "completed":
            break
        time.sleep(0.05)
    assert investigation
    evidence = client.get(f"/v1/events/{event['id']}/evidence").json()["evidence"]
    assert evidence
    first = evidence[0]
    assert client.get(f"/v1/evidence/{first['evidence_id']}").json()["event_id"] == event["id"]
    assert client.get("/v1/evidence/ev_missing").status_code == 404
    detail = client.get(f"/v1/investigations/{investigation['id']}").json()
    assert detail["transitions"]
    assert detail["evidence"]
