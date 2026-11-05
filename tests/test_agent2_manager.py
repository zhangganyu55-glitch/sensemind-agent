"""Agent 2.0: investigation state machine, retries, recovery and follow-up."""
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from guardian.agent import AgentCoordinator
from guardian.clock import ManualClock
from guardian.investigation import InvestigationService
from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.storage import Storage
from tests.support import ClockedClient


BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def reading(when, *, hr=75, quality="good", valid=True, source="simulator",
            device="guardian_001"):
    return {
        "device_id": device, "timestamp": when.isoformat(), "source": source,
        "heart_rate": hr, "spo2": 98, "acc_x": 0, "acc_y": 0, "acc_z": 1,
        "signal_quality": quality, "reading_valid": valid,
    }


def make_client(tmp_path, *, transport=None, llm=None, **kwargs):
    clock = ManualClock(BASE)
    return ClockedClient(create_app(
        str(tmp_path / "agent2.db"), allow_real_rules=True,
        llm_settings=llm or LLMSettings(mode="off"),
        llm_transport=transport,
        notification_ttl_seconds=60, notification_cooldown_seconds=300,
        investigation_deadline_seconds=2,
        clock=clock,
        **kwargs), clock)


def trigger_high_hr(client, start=BASE):
    response = None
    for index in range(3):
        response = client.post("/v1/telemetry", json=reading(
            start + timedelta(seconds=index), hr=130))
    return response


def event_of(client):
    events = [event for event in client.get(
        "/v1/events", params={"device_id": "guardian_001"}).json()
        if event["kind"] == "high_heart_rate"]
    return events[0]


def wait_completed(client, event_id, timeout=15):
    deadline = time.monotonic() + timeout
    detail = None
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/console/events/{event_id}").json()
        investigation = detail.get("investigation")
        if investigation and investigation["status"] in ("completed", "failed"):
            return detail
        time.sleep(0.05)
    raise AssertionError(f"investigation did not finish: {detail}")


def structured_content(request, *, usage=None, status="completed"):
    body = json.loads(request.content)
    user = json.loads(body["messages"][1]["content"])
    event = user["event"]
    evidence = user.get("evidence") or []
    refs = [item["evidence_id"] for item in evidence[:2]]
    payload = {
        "event_id": event["id"],
        "investigation_status": status,
        "observations": [{"text": "已读取结构化证据，需人工复核。",
                          "evidence_refs": refs}],
        "evidence_refs": refs,
        "uncertainties": ["研究原型，不能用于诊断。"],
        "conflicting_evidence": [],
        "missing_data": [],
        "conclusion": "基于现有证据的限定性摘要。",
        "recommended_action": ["核对设备与数据质量。"],
    }
    return httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5,
                           "total_tokens": 15},
    })


def test_state_machine_transitions_are_persisted(tmp_path):
    client = make_client(tmp_path)
    trigger_high_hr(client)
    event = event_of(client)
    detail = wait_completed(client, event["id"])
    investigation = detail["investigation"]
    assert investigation["status"] == "completed"
    assert investigation["attempts"] == 1
    detail = client.get(f"/v1/investigations/{investigation['id']}").json()
    edges = [(item["from_status"], item["to_status"])
             for item in detail["transitions"]]
    assert ("queued", "running") in edges
    assert ("running", "completed") in edges
    assert detail["evidence"]
    event_evidence = client.get(f"/v1/events/{event['id']}/evidence").json()
    assert event_evidence["evidence"]
    assert client.get(f"/v1/evidence/{detail['evidence'][0]['evidence_id']}").json()


def test_transient_timeout_is_retried_then_succeeds(tmp_path):
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadTimeout("private timeout", request=request)
        return structured_content(request)

    client = make_client(
        tmp_path, transport=httpx.MockTransport(handler),
        llm=LLMSettings(mode="deepseek", api_key="test-secret"),
        investigation_max_attempts=2,
        investigation_retry_backoff_seconds=0.01)
    trigger_high_hr(client)
    event = event_of(client)
    detail = wait_completed(client, event["id"])
    investigation = detail["investigation"]
    assert calls["count"] == 2
    assert investigation["ai_status"] == "completed"
    assert investigation["attempts"] == 2
    detail = client.get(f"/v1/investigations/{investigation['id']}").json()
    assert any(item["to_status"] == "retrying" for item in detail["transitions"])


def test_cost_limit_stops_the_model_and_falls_back(tmp_path):
    def handler(request):
        return structured_content(request, usage={
            "prompt_tokens": 5000, "completion_tokens": 5000, "total_tokens": 10000})

    client = make_client(
        tmp_path, transport=httpx.MockTransport(handler),
        llm=LLMSettings(mode="deepseek", api_key="test-secret"),
        investigation_max_llm_tokens=100)
    trigger_high_hr(client)
    event = event_of(client)
    detail = wait_completed(client, event["id"])
    investigation = detail["investigation"]
    assert investigation["ai_status"] == "cost_limit"
    assert investigation["status"] == "completed"
    assert investigation["report"]["structured_analysis"]


def test_duplicate_event_is_merged_without_second_model_call(tmp_path):
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return structured_content(request)

    client = make_client(
        tmp_path, transport=httpx.MockTransport(handler),
        llm=LLMSettings(mode="deepseek", api_key="test-secret"))
    trigger_high_hr(client)
    first = event_of(client)
    wait_completed(client, first["id"])
    assert calls["count"] == 1
    # The 10 s close dwell must elapse before the next event is independent.
    for index in range(13):
        client.post("/v1/telemetry", json=reading(
            BASE + timedelta(seconds=10 + index), hr=75))
    trigger_high_hr(client, start=BASE + timedelta(seconds=30))
    events = [event for event in client.get(
        "/v1/events", params={"device_id": "guardian_001"}).json()
        if event["kind"] == "high_heart_rate"]
    second = events[0]
    detail = wait_completed(client, second["id"])
    investigation = detail["investigation"]
    assert investigation["report"]["duplicate_of_event_id"] == first["id"]
    assert investigation["report"]["analysis_source"] == "duplicate_merged"
    assert calls["count"] == 1


def test_restart_recovers_incomplete_but_not_completed(tmp_path):
    database = str(tmp_path / "recover.db")
    storage = Storage(database)
    transition = storage.event_open("guardian_001", "high_heart_rate",
                                    BASE, "recover test")
    event = storage.get_event(transition["event_id"])
    investigation, _ = storage.create_investigation(
        event["id"], "guardian_001", "high_heart_rate")
    storage.update_investigation(investigation["id"], status="running", started=True)
    service = InvestigationService(storage, AgentCoordinator(storage, None), None)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        current = storage.investigation(investigation["id"])
        if current["status"] == "completed":
            break
        time.sleep(0.05)
    current = storage.investigation(investigation["id"])
    assert current["status"] == "completed"
    transitions = storage.investigation_transitions(investigation["id"])
    assert any(item["reason"] == "recovered_after_restart" for item in transitions)
    attempts = current["attempts"]

    second = InvestigationService(storage, AgentCoordinator(storage, None), None)
    time.sleep(0.3)
    again = storage.investigation(investigation["id"])
    assert again["status"] == "completed"
    assert again["attempts"] == attempts
    assert second is not None


def test_follow_up_evidence_supplements_completed_investigation(tmp_path):
    client = make_client(tmp_path)
    trigger_high_hr(client)
    event = event_of(client)
    detail = wait_completed(client, event["id"])
    investigation = detail["investigation"]
    before = len(investigation["report"]["evidence"])
    client.post("/v1/telemetry", json=reading(BASE + timedelta(seconds=30), hr=95))
    storage = Storage(str(tmp_path / "agent2.db"))
    service = InvestigationService(storage, AgentCoordinator(storage, None), None)
    result = service.supplement(event["id"])
    assert result["evidence_count"] >= before
    updated = storage.investigation(investigation["id"])
    assert updated["report"]["follow_up_evidence_count"] == result["evidence_count"]


def test_retry_endpoint_requires_console_token(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    trigger_high_hr(client)
    event = event_of(client)
    detail = wait_completed(client, event["id"])
    investigation_id = detail["investigation"]["id"]
    monkeypatch.setenv("GUARDIAN_CONSOLE_TOKEN", "console-secret")
    denied = client.post(f"/v1/investigations/{investigation_id}/retry")
    assert denied.status_code == 401
    allowed = client.post(f"/v1/investigations/{investigation_id}/retry",
                          headers={"X-Console-Token": "console-secret"})
    assert allowed.status_code in (200, 409)


def test_invalid_evidence_reference_falls_back_safely(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        user = json.loads(body["messages"][1]["content"])
        payload = {
            "event_id": user["event"]["id"],
            "investigation_status": "completed",
            "observations": [{"text": "引用了不存在的证据。",
                              "evidence_refs": ["ev_missing"]}],
            "evidence_refs": ["ev_missing"],
            "uncertainties": [], "conflicting_evidence": [],
            "missing_data": [], "conclusion": "测试。",
            "recommended_action": ["人工复核。"],
        }
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant",
                "content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
        })

    client = make_client(
        tmp_path, transport=httpx.MockTransport(handler),
        llm=LLMSettings(mode="deepseek", api_key="test-secret"))
    trigger_high_hr(client)
    event = event_of(client)
    detail = wait_completed(client, event["id"])
    investigation = detail["investigation"]
    assert investigation["ai_status"] == "invalid_evidence_refs"
    assert investigation["status"] == "completed"
    assert investigation["report"]["structured_analysis"]["investigation_status"] == "inconclusive"
    assert investigation["report"]["evidence"]


def test_two_devices_complete_with_two_workers(tmp_path):
    client = make_client(tmp_path, investigation_max_workers=2)
    for device in ("guardian_a", "guardian_b"):
        for index in range(3):
            client.post("/v1/telemetry", json=reading(
                BASE + timedelta(seconds=index), hr=130, device=device))
    for device in ("guardian_a", "guardian_b"):
        events = [event for event in client.get(
            "/v1/events", params={"device_id": device}).json()
            if event["kind"] == "high_heart_rate"]
        assert events
        wait_completed(client, events[0]["id"])
    health = client.get("/health").json()
    assert health["investigations"]["max_workers"] == 2
