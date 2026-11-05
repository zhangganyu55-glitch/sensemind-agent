import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from guardian.llm import LLMSettings
from guardian.main import create_app


def tool(name, arguments="{}", call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def completion(*, calls=None, text=None):
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": {
        "role": "assistant", "content": text, "tool_calls": calls or [],
    }}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def client(tmp_path, handler):
    return TestClient(create_app(
        str(tmp_path / "llm.db"), allow_real_rules=False,
        llm_settings=LLMSettings(mode="deepseek", api_key="test-secret"),
        llm_transport=httpx.MockTransport(handler),
    ))


def ask(c, question="最近的心率趋势如何？"):
    response = c.post("/v1/assistant", json={"device_id": "my_device", "question": question})
    assert response.status_code == 200
    assert "test-secret" not in response.text
    return response.json()


def test_model_selects_tools_receives_records_and_audits(tmp_path):
    requests = []
    replies = iter([
        completion(calls=[tool("summarize_status")]),
        completion(calls=[tool("get_history", '{"limit":2}', "call_2")]),
        completion(text="最近两条是模拟数据，心率为75和76次/分，不能据此判断健康状况。"),
    ])

    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    c = client(tmp_path, handler)
    base = datetime.now(timezone.utc) - timedelta(seconds=5)
    for i in range(2):
        assert c.post("/v1/telemetry", json={
            "device_id": "my_device", "timestamp": (base + timedelta(seconds=i)).isoformat(),
            "heart_rate": 75 + i, "source": "simulator", "signal_quality": "good", "reading_valid": True,
        }).status_code == 200
    result = ask(c)
    assert result["agent_mode"] == "llm_tool_agent"
    assert [item["tool"] for item in result["tools_used"]] == ["summarize_status", "get_history"]
    assert [row["heart_rate"] for row in result["data"]] == [75, 76]
    assert result["usage"]["total_tokens"] == 45
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["tool_choice"]["function"]["name"] == "summarize_status"
    assert all("device_id" not in spec["function"]["parameters"]["properties"] for spec in requests[0]["tools"])
    history_message = requests[2]["messages"][-1]
    assert history_message["role"] == "tool" and history_message["tool_call_id"] == "call_2"
    assert json.loads(history_message["content"])[-1]["heart_rate"] == 76
    with sqlite3.connect(tmp_path / "llm.db") as db:
        row = db.execute("SELECT id,mode,tools_json,usage_json FROM assistant_runs").fetchone()
        assert row[0] == result["request_id"] and row[1] == "llm_tool_agent"
        assert json.loads(row[2])[-1] == {"tool": "get_history", "arguments": {"limit": 2}}
        assert "test-secret" not in str(row)
    assert c.get("/health").json()["assistant"] == {"enabled": True, "mode": "deepseek", "model": "deepseek-flash"}


@pytest.mark.parametrize("bad_call", [
    tool("delete_database"),
    tool("summarize_status", '{"device_id":"someone_else"}'),
    tool("summarize_status", "not-json"),
    tool("summarize_status", "[]"),
    tool("summarize_status", '{"limit":true}'),
])
def test_rejects_write_tools_device_override_and_invalid_arguments(tmp_path, bad_call):
    c = client(tmp_path, lambda _: httpx.Response(200, json=completion(calls=[bad_call])))
    result = ask(c)
    assert result["agent_mode"] == "deterministic_tool_router_no_llm"
    assert result["llm_error"] == "tool_call_rejected"
    assert result["tools_used"] == []


@pytest.mark.parametrize("status,code", [(401, "authentication_failed"), (402, "insufficient_balance"),
                                         (429, "rate_limited"), (503, "provider_unavailable")])
def test_provider_errors_use_local_fallback_without_leaking_body(tmp_path, status, code):
    c = client(tmp_path, lambda _: httpx.Response(status, text="provider secret: test-secret"))
    result = ask(c)
    assert result["agent_mode"] == "deterministic_tool_router_no_llm"
    assert result["llm_error"] == code
    assert "退回本地查询" in result["response"]


def test_timeout_falls_back(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("private request information", request=request)
    result = ask(client(tmp_path, handler))
    assert result["llm_error"] == "timeout"


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": None}]},
                                   {"choices": [{"message": {"role": "assistant", "content": "invented"}}]}])
def test_malformed_or_ungrounded_response_falls_back(tmp_path, body):
    result = ask(client(tmp_path, lambda _: httpx.Response(200, json=body)))
    assert result["llm_error"] == "invalid_response"


def test_repeated_calls_stop_at_budget(tmp_path):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=completion(calls=[tool("summarize_status", call_id=f"call_{count}")]))
    result = ask(client(tmp_path, handler))
    assert count == 4
    assert result["llm_error"] == "tool_limit_exceeded"
    assert len(result["tools_used"]) == 3


def test_limits_and_cross_device_arguments_rejected_after_status(tmp_path):
    for bad in ('{"limit":201}', '{"limit":0}', '{"limit":true}', '{"device_id":"other"}'):
        replies = iter([completion(calls=[tool("summarize_status")]),
                        completion(calls=[tool("get_history", bad, "call_2")])])
        c = client(tmp_path, lambda _: httpx.Response(200, json=next(replies)))
        result = ask(c)
        assert result["llm_error"] == "tool_call_rejected"
        assert [t["tool"] for t in result["tools_used"]] == ["summarize_status"]


def test_offline_mode_never_contacts_model(tmp_path):
    def handler(request):
        raise AssertionError("Offline mode must not make network requests")
    c = TestClient(create_app(str(tmp_path / "off.db"), llm_settings=LLMSettings(),
                             llm_transport=httpx.MockTransport(handler)))
    result = ask(c)
    assert result["agent_mode"] == "deterministic_tool_router_no_llm"
    assert "llm_error" not in result
    assert c.get("/health").json()["assistant"]["enabled"] is False


def test_model_can_query_research_outputs(tmp_path):
    replies=iter([completion(calls=[tool('summarize_status')]),
                  completion(calls=[tool('get_model_outputs','{"limit":2}','ml_2')]),
                  completion(text='你已确诊癫痫，发作概率99%。')])
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json=next(replies))
    c=client(tmp_path,handler)
    result=ask(c,'查询模型输出和信号质量')
    assert result['tool_used']=='get_model_outputs'
    assert result['data']==[]
    assert '你已确诊' not in result['response'] and '99%' not in result['response']
    assert '禁止推导疾病诊断' in requests[0]['messages'][0]['content']


def test_research_template_uses_newest_stored_inference(tmp_path):
    from guardian.storage import Storage

    replies = iter([
        completion(calls=[tool('summarize_status')]),
        completion(calls=[tool('get_model_outputs', '{"limit":2}', 'ml_latest')]),
        completion(text='模型输出仅供研究。'),
    ])
    c = client(tmp_path, lambda _: httpx.Response(200, json=next(replies)))
    storage = Storage(str(tmp_path / 'llm.db'))
    moment = datetime.now(timezone.utc).isoformat()
    for label, reliability in [('older_sitting', 0.12), ('newer_walking', 0.91)]:
        storage.put_ml_inference({
            'device_id': 'my_device', 'window_start': moment,
            'activity_label': label, 'signal_reliability': reliability,
        })

    result = ask(c, '查询模型输出和信号质量')
    assert result['data'][0]['activity_label'] == 'newer_walking'
    assert result['data'][1]['activity_label'] == 'older_sitting'
    assert 'newer_walking' in result['response']
    assert '0.91' in result['response']
    assert 'older_sitting' not in result['response']
    assert '0.12' not in result['response']
