"""v0.5 tests: autonomous investigation, evidence chain and alert loop."""
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from guardian.clock import ManualClock
from guardian.investigation import TOOL_SPECS
from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.storage import Storage
from tests.support import ClockedClient


BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def make_client(tmp_path, *, rules=True, llm=None, transport=None,
                ttl=60, cooldown=300, deadline=2):
    database = str(tmp_path / "v05.db")
    clock = ManualClock(BASE)
    return ClockedClient(create_app(
        database, allow_real_rules=rules,
        llm_settings=llm or LLMSettings(mode="off"),
        llm_transport=transport,
        notification_ttl_seconds=ttl,
        notification_cooldown_seconds=cooldown,
        investigation_deadline_seconds=deadline,
        clock=clock,
    ), clock)


def reading(when, *, hr=75, spo2=98, quality="good", valid=True,
            source="simulator", device="guardian_001"):
    return {
        "device_id": device, "timestamp": when.isoformat(), "source": source,
        "heart_rate": hr, "spo2": spo2,
        "acc_x": 0, "acc_y": 0, "acc_z": 1,
        "signal_quality": quality, "reading_valid": valid,
    }


def post(client, when, **changes):
    return client.post("/v1/telemetry", json=reading(when, **changes))


def trigger_high_hr(client, start=BASE, device="guardian_001", source="simulator"):
    response = None
    for index in range(3):
        response = post(client, start + timedelta(seconds=index),
                        hr=130, device=device, source=source)
    return response


def events_of(client, kind, device="guardian_001"):
    return [event for event in
            client.get("/v1/events", params={"device_id": device}).json()
            if event["kind"] == kind]


def wait_investigation(client, event_id, timeout=15):
    deadline = time.monotonic() + timeout
    detail = None
    while time.monotonic() < deadline:
        response = client.get(f"/v1/console/events/{event_id}")
        assert response.status_code == 200
        detail = response.json()
        investigation = detail.get("investigation")
        if investigation and investigation["status"] in ("completed", "failed"):
            return detail
        time.sleep(0.05)
    raise AssertionError(f"investigation did not finish: {detail}")


def completion(*, calls=None, text=None):
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": {
        "role": "assistant", "content": text, "tool_calls": calls or [],
    }}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def tool_call(name, arguments=None, call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {})}}


def test_simulated_anomaly_starts_automatic_investigation(tmp_path):
    client = make_client(tmp_path)
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    detail = wait_investigation(client, event["id"])
    investigation = detail["investigation"]
    report = investigation["report"]

    assert investigation["status"] == "completed"
    assert report["ai_status"] == "not_configured"
    assert report["analysis_source"] == "local_deterministic"
    assert [call["tool"] for call in report["tool_calls"][:6]] == [
        "get_event", "get_latest", "get_window", "get_quality",
        "get_sensor_intelligence", "find_similar_events"]
    assert report["facts"] and report["uncertainties"]
    assert report["recommended_actions"]
    assert report["ai_inferences"]
    assert report["key_history"]
    assert detail["event"]["status"] == "active"
    api_report = client.get(f"/v1/events/{event['id']}/investigation").json()
    assert api_report["status"] == "completed"
    assert api_report["report"]["facts"] == report["facts"]
    assert report["event_duration_seconds"] >= 0
    assert report["trend"]["status"] in ("descriptive", "insufficient")
    # The fixture uses data two minutes old, so staleness must be explicit.
    assert report["data_quality"]["stale"] is True
    assert any("过期" in text or "离线" in text
               for text in report["ai_inferences"] + report["uncertainties"])
    assert detail["event"]["acknowledged"] is False
    assert detail["notifications"]
    assert detail["timeline"][0]["type"] == "rule_event_opened"


def test_stm32_packets_enter_the_same_pipeline(tmp_path):
    # Simulated STM32 packets (source=stm32), not physical hardware.
    client = make_client(tmp_path)
    trigger_high_hr(client, source="stm32")
    event = events_of(client, "high_heart_rate")[0]
    detail = wait_investigation(client, event["id"])
    report = detail["investigation"]["report"]
    assert "stm32" in json.dumps(report["facts"], ensure_ascii=False)
    assert report["data_quality"]["sources"] == ["stm32"]
    assert report["data_quality"]["sample_count"] == 3
    assert any("加速度" in fact["text"] for fact in report["facts"])


def test_llm_investigation_selects_extra_tools_and_keeps_facts_separate(tmp_path):
    requests = []
    replies = iter([
        completion(calls=[tool_call("get_window",
                                    {"before_minutes": 10, "after_minutes": 0})]),
        completion(text=json.dumps({
            "inferences": ["窗口内存在持续偏高的心率样本。"],
            "uncertainties": ["这是模拟数据，不能用于诊断。"],
            "recommended_actions": ["核对设备与数据质量，并由照护者确认。"],
            "narrative": "基于工具结果，心率样本偏高；结论仍需人工核对。",
        }, ensure_ascii=False)),
    ])

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    client = make_client(
        tmp_path, llm=LLMSettings(mode="deepseek", api_key="test-secret"),
        transport=httpx.MockTransport(handler))
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    detail = wait_investigation(client, event["id"])
    report = detail["investigation"]["report"]

    assert report["ai_status"] == "completed"
    assert report["analysis_source"] == "ai"
    assert report["model"] == "deepseek-flash"
    assert report["ai_inferences"] == ["窗口内存在持续偏高的心率样本。"]
    assert any(call["actor"] == "agent" and call["tool"] == "get_window"
               and call["arguments"]["before_minutes"] == 10
               for call in report["tool_calls"])
    # Deterministic facts stay independent of model prose.
    assert any("规则引擎" in fact["text"] for fact in report["facts"])
    assert "窗口内存在持续偏高的心率样本。" not in json.dumps(
        report["facts"], ensure_ascii=False)
    assert requests[0]["tool_choice"] == "auto"
    assert any(tool["function"]["name"] == "get_quality"
               for tool in requests[0]["tools"])


def test_model_timeout_keeps_base_alert_and_local_report(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("private timeout detail", request=request)

    client = make_client(
        tmp_path, llm=LLMSettings(mode="deepseek", api_key="test-secret"),
        transport=httpx.MockTransport(handler))
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    detail = wait_investigation(client, event["id"])
    investigation = detail["investigation"]
    assert investigation["status"] == "completed"
    assert investigation["ai_status"] == "timeout"
    assert investigation["report"]["facts"]
    assert detail["event"]["status"] == "active"
    assert detail["notifications"]


def test_ai_write_tool_is_rejected_and_cannot_acknowledge(tmp_path):
    names = {spec["name"] for spec in TOOL_SPECS}
    assert names == {"get_event", "get_latest", "get_window", "get_quality",
                     "get_sensor_intelligence", "find_similar_events",
                     "get_history", "summarize_status"}
    assert not names & {"acknowledge", "resolve", "send_alert", "delete_event"}
    for spec in TOOL_SPECS:
        assert "device_id" not in spec["properties"]

    def handler(request):
        return httpx.Response(200, json=completion(
            calls=[tool_call("acknowledge_event", {"event_id": "x"})]))

    client = make_client(
        tmp_path, llm=LLMSettings(mode="deepseek", api_key="test-secret"),
        transport=httpx.MockTransport(handler))
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    detail = wait_investigation(client, event["id"])
    assert detail["investigation"]["ai_status"] == "tool_call_rejected"
    assert detail["event"]["status"] == "active"
    assert detail["event"]["acknowledged"] is False


def test_poor_quality_is_marked_as_uncertain(tmp_path):
    client = make_client(tmp_path)
    for index in range(3):
        post(client, BASE + timedelta(seconds=index), hr=130,
             quality="poor", valid=False)
    event = events_of(client, "data_quality")[0]
    detail = wait_investigation(client, event["id"])
    report = detail["investigation"]["report"]
    assert report["data_quality"]["quality_counts"].get("poor", 0) == 3
    assert any("质量" in fact["text"] for fact in report["facts"])
    assert any("质量" in text for text in report["uncertainties"])
    assert event["status"] == "active"


def test_repeated_events_do_not_create_repeated_notifications(tmp_path):
    client = make_client(tmp_path, cooldown=300)
    trigger_high_hr(client)
    first = events_of(client, "high_heart_rate")[0]
    first_detail = wait_investigation(client, first["id"])
    assert first_detail["notifications"][0]["status"] in ("pending", "sent", "failed")

    # The 10 s close dwell must elapse before the next event is independent.
    for index in range(13):
        post(client, BASE + timedelta(seconds=10 + index), hr=75)
    trigger_high_hr(client, start=BASE + timedelta(seconds=30))
    events = events_of(client, "high_heart_rate")
    assert len(events) >= 2
    second = events[0]
    second_detail = wait_investigation(client, second["id"])
    statuses = [item["status"] for item in second_detail["notifications"]]
    assert "suppressed_duplicate" in statuses


def test_human_ack_is_separate_from_rule_recovery(tmp_path):
    client = make_client(tmp_path)
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    ack = client.post(f"/v1/events/{event['id']}/ack",
                      json={"confirmed_by": "护士A"})
    assert ack.status_code == 200
    assert ack.json()["acknowledged_by"] == "护士A"

    detail = wait_investigation(client, event["id"])
    assert detail["event"]["status"] == "active"
    assert detail["event"]["acknowledged"] is True
    assert any(item["type"] == "human_acknowledged" for item in detail["timeline"])

    for index in range(11):
        post(client, BASE + timedelta(seconds=10 + index), hr=75)
    resolved = events_of(client, "high_heart_rate")[0]
    assert resolved["status"] == "resolved"
    assert resolved["acknowledged"] is True
    final = client.get(f"/v1/console/events/{resolved['id']}").json()
    types = {item["type"] for item in final["timeline"]}
    assert {"human_acknowledged", "rule_event_resolved"} <= types


def test_console_token_is_required_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_CONSOLE_TOKEN", "console-secret")
    client = make_client(tmp_path)
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    denied = client.post(f"/v1/events/{event['id']}/ack",
                         json={"confirmed_by": "护士A"})
    assert denied.status_code == 401
    allowed = client.post(f"/v1/events/{event['id']}/ack",
                          json={"confirmed_by": "护士A"},
                          headers={"X-Console-Token": "console-secret"})
    assert allowed.status_code == 200


def test_undelivered_notification_is_marked_failed(tmp_path):
    client = make_client(tmp_path, ttl=1)
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    deadline = time.monotonic() + 6
    status = None
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/console/events/{event['id']}").json()
        notifications = detail.get("notifications") or []
        if notifications:
            status = notifications[-1]["status"]
            if status == "failed":
                assert "TTL" in notifications[-1]["error"] or notifications[-1]["error"]
                return
        time.sleep(0.1)
    raise AssertionError(f"notification did not fail: {status}")


def test_websocket_marks_notification_sent(tmp_path):
    client = make_client(tmp_path, ttl=60)
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    with client.websocket_connect("/ws/live?device_id=guardian_001") as socket:
        payload = socket.receive_json()
        assert payload["device_id"] == "guardian_001"
        assert payload["events"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/console/events/{event['id']}").json()
        if any(item["status"] == "sent" for item in detail["notifications"]):
            return
        time.sleep(0.05)
    raise AssertionError("notification was not marked sent")


def test_investigation_window_is_device_scoped(tmp_path):
    client = make_client(tmp_path)
    # Another device writes in the same wall-clock window; it must not leak in.
    post(client, BASE + timedelta(seconds=1), device="device_b", hr=75)
    trigger_high_hr(client, start=BASE + timedelta(seconds=2))
    event = events_of(client, "high_heart_rate")[0]
    detail = wait_investigation(client, event["id"])
    report = detail["investigation"]["report"]
    assert report["device_id"] == "guardian_001"
    assert report["data_quality"]["sample_count"] == 3
    assert "device_b" not in json.dumps(report, ensure_ascii=False)


def test_investigation_includes_experimental_model_evidence(tmp_path):
    client = make_client(tmp_path)
    moment = datetime.now(timezone.utc).replace(microsecond=0)
    Storage(str(tmp_path / "v05.db")).put_ml_inference({
        "device_id": "guardian_001",
        "window_start": iso(moment - timedelta(seconds=10)),
        "window_end": iso(moment),
        "dataset": "pamap2", "participant_id": "101", "sampling_hz": 100.0,
        "activity_label": "running", "activity_confidence": 0.91,
        "signal_reliability": 0.88, "anomaly_score": 0.12,
        "model_version": "pamap2-rf-test", "available": True,
    })
    trigger_high_hr(client)
    event = events_of(client, "high_heart_rate")[0]
    detail = wait_investigation(client, event["id"])
    report = detail["investigation"]["report"]

    assert report["model_evidence"]["available"] is True
    assert report["model_evidence"]["activity_label"] == "running"
    assert any("模型辅助证据" in fact["text"] for fact in report["facts"])
    assert any(call["tool"] == "get_sensor_intelligence"
               for call in report["tool_calls"])
    # Model evidence never replaces or clears the rule event.
    assert detail["event"]["status"] == "active"
    assert detail["event"]["acknowledged"] is False


def test_public_app_does_not_expose_console_or_ack(tmp_path):
    app = create_app(str(tmp_path / "public.db"), public_only=True,
                     llm_settings=LLMSettings(mode="off"))
    public = TestClient(app)
    for path in ("/console", "/v1/console/snapshot", "/v1/console/events/x"):
        assert public.get(path).status_code == 404
    assert public.post("/v1/events/x/ack").status_code == 404
