"""Contract tests for the phone-facing `/v1` API used by the RareGuard APK."""
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from guardian.apk_storage import ApkStorage
from guardian.clock import ManualClock
from guardian.llm import LLMSettings
from guardian.main import create_app
from tests.support import ClockedClient


PAIRING_CODE = "test-pairing-code-123"
BASE_TIME = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)


@pytest.fixture()
def client(tmp_path):
    database = str(tmp_path / "guardian.db")
    clock = ManualClock(BASE_TIME)
    app = create_app(database, llm_settings=LLMSettings(mode="off"),
                     apk_pairing_code=PAIRING_CODE, clock=clock)
    test_client = ClockedClient(app, clock)
    test_client.apk_db = database  # used only to issue fresh single-use codes
    return test_client


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def pair(client, code=PAIRING_CODE, headers=None):
    return client.post("/v1/pairings/exchange", json={"code": code}, headers=headers)


def fresh_code(client, name="测试用户"):
    return ApkStorage(client.apk_db).issue_pairing_code(name)["code"]


def paired(client, headers=None):
    response = pair(client, fresh_code(client), headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    return data, {"Authorization": "Bearer " + data["token"]}


def grant(client, headers, scope):
    response = client.post("/v1/consents", json={
        "scope": scope, "granted": True, "policy_version": "demo-1"}, headers=headers)
    assert response.status_code == 200, response.text
    return response


def register(client, headers, local_id=None):
    response = client.post("/v1/devices", json={
        "local_id": str(local_id or uuid4()), "model": "MR11 ring",
        "firmware_version": "1.0.0", "protocol_version": "1"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["device_id"]


def measurement(observed_at, *, metric="hr", value=72.0, unit="bpm",
                quality="good", source="live_device", time_basis="device",
                sample_id=None, phone_received_at=None):
    return {
        "sample_id": str(sample_id or uuid4()), "metric": metric, "value": value,
        "unit": unit, "observed_at": iso(observed_at),
        "phone_received_at": iso(phone_received_at or observed_at + timedelta(seconds=2)),
        "time_basis": time_basis, "quality": quality, "source": source,
    }


def ingest(client, headers, device_id, samples, batch_id=None):
    return client.post("/v1/measurement-batches", json={
        "schema_version": 1, "batch_id": str(batch_id or uuid4()),
        "device_id": device_id, "app_version": "test-1", "samples": samples,
    }, headers=headers)


def error_code(response):
    body = response.json()
    if isinstance(body.get("detail"), dict):
        return body["detail"]["code"]
    return body.get("code")


def allow_upload(client, headers):
    grant(client, headers, "upload")
    return register(client, headers)


def test_pairing_auth_and_revocation(client):
    wrong = pair(client, "wrong-code-123")
    assert wrong.status_code == 401
    # The APK surfaces top-level code/message, not FastAPI's `detail` wrapper.
    assert wrong.json()["code"] == "invalid_pairing_code" and wrong.json()["message"]

    # The code issued at startup is single-use.
    assert pair(client, PAIRING_CODE).status_code == 200
    reused = pair(client, PAIRING_CODE)
    assert reused.status_code == 401
    assert reused.json()["code"] == "invalid_pairing_code"

    data, headers = paired(client)
    assert data["name"] and data["family_id"] and data["guardian_id"]

    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer invalid"}).status_code == 401

    me = client.get("/v1/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    assert body["guardian_id"] == data["guardian_id"]
    assert body["consents"] == {}
    assert body["devices"] == []
    assert "hermes_configured" in body["service"]

    assert client.post("/v1/credentials/revoke", headers=headers).status_code == 200
    assert client.get("/v1/me", headers=headers).status_code == 401


def test_consent_gates_device_registration_and_ai(client):
    _, headers = paired(client)

    denied = client.post("/v1/devices", json={
        "local_id": str(uuid4()), "model": "MR11", "protocol_version": "1"}, headers=headers)
    assert denied.status_code == 403
    assert error_code(denied) == "consent_required"

    denied_ai = client.post("/v1/conversations", headers=headers)
    assert denied_ai.status_code == 403
    assert error_code(denied_ai) == "consent_required"

    grant(client, headers, "upload")
    assert register(client, headers)


def test_batch_upload_is_idempotent_and_conflict_aware(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    first = measurement(BASE_TIME)
    second = measurement(BASE_TIME + timedelta(seconds=10),
                         metric="spo2", value=98, unit="%")
    batch_id = uuid4()

    response = ingest(client, headers, device_id, [first, second], batch_id)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "committed"
    assert receipt["accepted"] == 2 and receipt["duplicates"] == 0

    repeated = ingest(client, headers, device_id, [first, second], batch_id)
    assert repeated.status_code == 200
    assert repeated.json()["accepted"] == receipt["accepted"]
    assert repeated.json()["duplicates"] == receipt["duplicates"]

    changed = ingest(client, headers, device_id, [measurement(BASE_TIME)], batch_id)
    assert changed.status_code == 409
    assert error_code(changed) == "batch_content_conflict"

    fresh = measurement(BASE_TIME + timedelta(seconds=20))
    partial = ingest(client, headers, device_id, [first, fresh])
    assert partial.status_code == 200
    assert partial.json()["accepted"] == 1
    assert partial.json()["duplicates"] == 1

    conflicting = measurement(BASE_TIME + timedelta(seconds=30),
                              sample_id=second["sample_id"], value=90)
    rejected = ingest(client, headers, device_id, [fresh, conflicting])
    assert rejected.status_code == 409
    assert error_code(rejected) == "sample_content_conflict"

    # Nothing from the rejected batch may have been committed.
    listed = client.get("/v1/measurements", params={
        "device_id": device_id, "from": iso(BASE_TIME - timedelta(minutes=1)),
        "to": iso(BASE_TIME + timedelta(minutes=5)), "limit": 500}, headers=headers)
    assert len(listed.json()["data"]) == 3

    # Duplicate sample IDs inside one new batch are collapsed or rejected.
    fresh_two = measurement(BASE_TIME + timedelta(seconds=40))
    same_twice = ingest(client, headers, device_id, [fresh_two, fresh_two])
    assert same_twice.status_code == 200
    assert same_twice.json()["accepted"] == 1
    assert same_twice.json()["duplicates"] == 1
    inner_conflict = ingest(client, headers, device_id, [fresh_two, measurement(
        BASE_TIME + timedelta(seconds=50), sample_id=fresh_two["sample_id"], value=90)])
    assert inner_conflict.status_code == 409
    assert error_code(inner_conflict) == "sample_content_conflict"


def test_measurement_window_metric_and_pagination(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    start = BASE_TIME
    samples = [measurement(start + timedelta(seconds=60 * index), value=70 + index)
               for index in range(5)]
    samples.append(measurement(start + timedelta(seconds=90), metric="spo2",
                               value=97, unit="%"))
    assert ingest(client, headers, device_id, samples).status_code == 200

    params = {"device_id": device_id, "from": iso(start), "to": iso(start + timedelta(hours=1)),
              "limit": 2}
    collected, cursor = [], None
    for _ in range(5):
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        response = client.get("/v1/measurements", params=query, headers=headers)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["status"] == "data_available"
        collected.extend(page["data"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(collected) == 6
    assert [row["value"] for row in collected] == [70, 71, 97, 72, 73, 74]

    oxygens = client.get("/v1/measurements", params={
        **params, "limit": 1, "metric": "spo2"}, headers=headers)
    assert oxygens.status_code == 200
    rows = oxygens.json()["data"]
    assert len(rows) == 1 and rows[0]["metric"] == "spo2" and rows[0]["unit"] == "%"
    assert oxygens.json()["next_cursor"] is None

    bad_window = client.get("/v1/measurements", params={
        "device_id": device_id, "from": iso(start), "to": iso(start)}, headers=headers)
    assert bad_window.status_code == 422
    assert error_code(bad_window) == "invalid_window"

    bad_cursor = client.get("/v1/measurements", params={
        **params, "cursor": "!!!"}, headers=headers)
    assert bad_cursor.status_code == 422
    assert error_code(bad_cursor) == "invalid_cursor"

    unknown = client.get("/v1/measurements", params={
        "device_id": str(uuid4()), "from": iso(start), "to": iso(start + timedelta(hours=1))},
        headers=headers)
    assert unknown.status_code == 404
    assert error_code(unknown) == "device_not_found"


def test_night_summary_reports_gaps_without_claims(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    samples = [measurement(BASE_TIME, value=60),
               measurement(BASE_TIME + timedelta(seconds=30), value=62),
               measurement(BASE_TIME + timedelta(hours=1), value=64)]
    assert ingest(client, headers, device_id, samples).status_code == 200

    window = {"device_id": device_id, "timezone": "Asia/Shanghai",
              "from": iso(BASE_TIME - timedelta(minutes=5)),
              "to": iso(BASE_TIME + timedelta(hours=2))}
    response = client.get("/v1/summaries/night", params=window, headers=headers)
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["status"] == "data_available"
    assert summary["continuous_coverage"] is None
    hr = summary["metrics"]["hr"]
    assert hr["count"] == 3 and hr["usable_numeric_count"] == 3
    assert hr["min"] == 60 and hr["max"] == 64 and hr["median"] == 62
    assert hr["maximum_interval_seconds"] == 3570
    assert len(summary["limitations"]) == 2

    empty = client.get("/v1/summaries/night", params={
        **window, "from": iso(BASE_TIME + timedelta(hours=3)),
        "to": iso(BASE_TIME + timedelta(hours=4))}, headers=headers)
    assert empty.json()["status"] == "no_data"
    assert empty.json()["metrics"] == {}

    bad_zone = client.get("/v1/summaries/night", params={
        **window, "timezone": "Mars/Olympus"}, headers=headers)
    assert bad_zone.status_code == 422
    assert error_code(bad_zone) == "invalid_window"


def test_observations_are_idempotent_and_family_scoped(client):
    first, first_headers = paired(client)
    second, second_headers = paired(client)
    assert first["family_id"] == second["family_id"]
    device_id = allow_upload(client, first_headers)

    observation = {"id": str(uuid4()), "device_id": device_id,
                   "occurred_at": iso(BASE_TIME), "text": "夜间翻身较多"}
    created = client.post("/v1/observations", json=observation, headers=first_headers)
    assert created.status_code == 200 and created.json()["status"] == "committed"
    repeated = client.post("/v1/observations", json=observation, headers=first_headers)
    assert repeated.status_code == 200

    conflict = client.post("/v1/observations", json={
        **observation, "text": "内容不同"}, headers=first_headers)
    assert conflict.status_code == 409
    assert error_code(conflict) == "observation_conflict"

    listed = client.get("/v1/observations", params={"device_id": device_id},
                        headers=second_headers)
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert len(rows) == 1 and rows[0]["text"] == "夜间翻身较多"
    assert rows[0]["guardian_id"] == first["guardian_id"]


def test_alert_delivery_and_receipts(client):
    creator, creator_headers = paired(client)
    reader, reader_headers = paired(client)
    grant(client, creator_headers, "upload")
    grant(client, creator_headers, "remote_alerts")
    grant(client, reader_headers, "remote_alerts")
    device_id = register(client, creator_headers)

    alert = {"id": str(uuid4()), "device_id": device_id, "kind": "delivery_test",
             "text": "演示报警", "evidence": {"source": "test"}}
    stored = client.post("/v1/alerts", json=alert, headers=creator_headers)
    assert stored.status_code == 200, stored.text
    assert stored.json()["status"] == "stored"

    repeat = client.post("/v1/alerts", json=alert, headers=creator_headers)
    assert repeat.status_code == 200

    conflict = client.post("/v1/alerts", json={**alert, "text": "不同内容"},
                           headers=creator_headers)
    assert conflict.status_code == 409
    assert error_code(conflict) == "alert_conflict"

    inbox = client.get("/v1/alerts", headers=reader_headers)
    assert inbox.status_code == 200
    rows = inbox.json()["data"]
    assert len(rows) == 1 and rows[0]["received_at"] is None

    received = client.post(f"/v1/alerts/{alert['id']}/receipt",
                           json={"status": "received"}, headers=reader_headers)
    assert received.status_code == 200 and received.json()["status"] == "received"
    confirmed = client.post(f"/v1/alerts/{alert['id']}/receipt",
                            json={"status": "confirmed", "note": "已读"},
                            headers=reader_headers)
    assert confirmed.status_code == 200

    inbox = client.get("/v1/alerts", headers=reader_headers).json()["data"][0]
    assert inbox["received_at"] and inbox["confirmed_at"]

    deliveries = client.get(f"/v1/alerts/{alert['id']}/deliveries",
                            headers=creator_headers)
    assert deliveries.status_code == 200
    rows = deliveries.json()["data"]
    assert {row["guardian_id"] for row in rows} == {creator["guardian_id"], reader["guardian_id"]}
    reader_row = next(row for row in rows if row["guardian_id"] == reader["guardian_id"])
    assert reader_row["note"] == "已读"

    no_consent, no_consent_headers = paired(client)
    denied = client.get("/v1/alerts", headers=no_consent_headers)
    assert denied.status_code == 403
    assert error_code(denied) == "consent_required"


def test_ai_request_uses_device_scoped_context(client):
    first, first_headers = paired(client)
    second, second_headers = paired(client)
    device_id = allow_upload(client, first_headers)
    grant(client, first_headers, "ai_analysis")
    grant(client, second_headers, "ai_analysis")
    assert ingest(client, first_headers, device_id,
                  [measurement(BASE_TIME, value=71)]).status_code == 200

    conversation = client.post("/v1/conversations", headers=first_headers)
    assert conversation.status_code == 200
    conversation_id = conversation.json()["id"]

    question = client.post(f"/v1/conversations/{conversation_id}/messages", json={
        "device_id": device_id, "question": "最近心率怎么样？",
        "from": iso(BASE_TIME - timedelta(minutes=5)), "to": iso(BASE_TIME + timedelta(minutes=5)),
        "timezone": "Asia/Shanghai"}, headers=first_headers)
    assert question.status_code == 202, question.text
    result = question.json()
    assert result["status"] == "completed"

    answer = client.get(f"/v1/ai-requests/{result['request_id']}", headers=first_headers)
    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "completed" and body["answer"]
    assert len(body["context_hash"]) == 64
    assert body["context"]["metrics"]["hr"]["count"] == 1

    hidden = client.get(f"/v1/ai-requests/{result['request_id']}", headers=second_headers)
    assert hidden.status_code == 404
    assert error_code(hidden) == "request_not_found"

    missing = client.post(f"/v1/conversations/{uuid4()}/messages", json={
        "device_id": device_id, "question": "？",
        "from": iso(BASE_TIME - timedelta(minutes=5)), "to": iso(BASE_TIME),
        "timezone": "UTC"}, headers=first_headers)
    assert missing.status_code == 404
    assert error_code(missing) == "conversation_not_found"

    bad_window = client.post(f"/v1/conversations/{conversation_id}/messages", json={
        "device_id": device_id, "question": "？",
        "from": iso(BASE_TIME), "to": iso(BASE_TIME - timedelta(minutes=5)),
        "timezone": "UTC"}, headers=first_headers)
    assert bad_window.status_code == 422
    assert error_code(bad_window) == "invalid_window"


def test_experimental_config_versioning_and_acks(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    empty = client.get(f"/v1/devices/{device_id}/config", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == {"version": 0, "parameters": None, "status": "not_configured"}

    parameters = {"enabled": False, "hr_low": None, "hr_high": None, "spo2_low": None,
                  "duration_seconds": 60, "max_gap_seconds": 10, "required_signals": 1,
                  "allow_unknown_quality": False}
    publish = client.post(f"/v1/devices/{device_id}/config", json={
        "base_version": 0, "parameters": parameters,
        "confirmed_experimental_use": True}, headers=headers)
    assert publish.status_code == 200, publish.text
    assert publish.json()["version"] == 1

    latest = client.get(f"/v1/devices/{device_id}/config", headers=headers).json()
    assert latest["version"] == 1 and latest["status"] == "published"
    assert latest["parameters"]["duration_seconds"] == 60

    stale = client.post(f"/v1/devices/{device_id}/config", json={
        "base_version": 0, "parameters": parameters,
        "confirmed_experimental_use": True}, headers=headers)
    assert stale.status_code == 409
    assert error_code(stale) == "config_version_conflict"

    next_version = client.post(f"/v1/devices/{device_id}/config", json={
        "base_version": 1, "parameters": parameters,
        "confirmed_experimental_use": True}, headers=headers)
    assert next_version.json()["version"] == 2

    ack = client.post(f"/v1/devices/{device_id}/config-acks", json={
        "version": 2, "status": "applied"}, headers=headers)
    assert ack.status_code == 200
    unknown = client.post(f"/v1/devices/{device_id}/config-acks", json={
        "version": 99, "status": "applied"}, headers=headers)
    assert unknown.status_code == 404
    assert error_code(unknown) == "config_not_found"

    unsafe = client.post(f"/v1/devices/{device_id}/config", json={
        "base_version": 2, "parameters": {**parameters, "enabled": True},
        "confirmed_experimental_use": True}, headers=headers)
    assert unsafe.status_code == 422


def test_ring_samples_feed_agent_and_deletion_clears_both_stores(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    samples = [measurement(BASE_TIME, value=70),
               measurement(BASE_TIME + timedelta(seconds=10), value=60, quality="invalid")]
    assert ingest(client, headers, device_id, samples).status_code == 200

    status = client.get("/v1/live/status", params={"device_id": device_id})
    assert status.status_code == 200
    latest = status.json()["latest"]
    assert latest["source"] == "ring"
    assert latest["heart_rate"] == 70
    assert latest["reading_valid"] is True

    exported = client.get("/v1/export", params={"device_id": device_id}, headers=headers)
    assert len(exported.json()["samples"]) == 2

    missing = client.delete(f"/v1/devices/{device_id}/data", headers=headers)
    assert missing.status_code == 422
    assert error_code(missing) == "confirmation_required"

    deleted = client.delete(f"/v1/devices/{device_id}/data",
                            params={"confirmation": "delete_health_data"}, headers=headers)
    assert deleted.status_code == 200

    assert client.get("/v1/export", params={"device_id": device_id},
                      headers=headers).json()["samples"] == []
    assert client.get("/v1/live/status", params={"device_id": device_id}).json()["latest"] is None


def test_wire_validation_rejects_bad_samples(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    def rejected(sample):
        response = ingest(client, headers, device_id, [sample])
        assert response.status_code == 422, response.text

    rejected(measurement(BASE_TIME, metric="hr", unit="%"))
    rejected(measurement(BASE_TIME, value=-1))
    rejected(measurement(BASE_TIME, metric="unknown", unit="bpm"))
    rejected(measurement(BASE_TIME, source="device_history", time_basis="phone_receive"))
    naive = measurement(BASE_TIME)
    naive["observed_at"] = "2026-10-28T00:00:00"
    rejected(naive)
    extra = measurement(BASE_TIME)
    extra["unexpected"] = True
    validation = ingest(client, headers, device_id, [extra])
    assert validation.status_code == 422
    assert validation.json()["code"] == "invalid_request"
    assert validation.json()["details"]


def test_health_exposes_apk_contract(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["apk_contract"] == "v1"


def test_expired_pairing_code_is_rejected(client):
    code = fresh_code(client)
    with sqlite3.connect(client.apk_db) as con:
        con.execute("UPDATE apk_pairing_codes SET expires_at=? WHERE consumed_at IS NULL",
                    ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
    response = pair(client, code)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_pairing_code"


def test_pairing_attempts_are_rate_limited_per_client(client):
    attacker = {"cf-connecting-ip": "203.0.113.9"}
    for _ in range(5):
        assert pair(client, "wrong-code-123", headers=attacker).status_code == 401
    blocked = pair(client, "wrong-code-123", headers=attacker)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"

    # Lockout applies even to a correct code from the same client.
    valid = fresh_code(client)
    assert pair(client, valid, headers=attacker).status_code == 429

    other = {"cf-connecting-ip": "203.0.113.10"}
    assert pair(client, fresh_code(client), headers=other).status_code == 200


def test_expired_credential_is_rejected(client):
    data, headers = paired(client)
    with sqlite3.connect(client.apk_db) as con:
        con.execute("UPDATE apk_credentials SET expires_at=? WHERE guardian_id=?",
                    ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                     data["guardian_id"]))
    assert client.get("/v1/me", headers=headers).status_code == 401


def test_another_familys_device_is_not_visible(client):
    _, headers = paired(client)
    foreign_device = ApkStorage(client.apk_db).register_device(
        "other-family", str(uuid4()), "MR11", None, "1")
    for path in ("/v1/export",):
        response = client.get(path, params={"device_id": foreign_device}, headers=headers)
        assert response.status_code == 404
        assert error_code(response) == "device_not_found"
    window = {"device_id": foreign_device, "from": iso(BASE_TIME - timedelta(minutes=1)),
              "to": iso(BASE_TIME + timedelta(minutes=1))}
    assert client.get("/v1/measurements", params=window, headers=headers).status_code == 404
    assert client.get("/v1/summaries/night", params=window, headers=headers).status_code == 404


def test_protected_apk_endpoints_require_bearer(client):
    device = str(uuid4())
    alert = str(uuid4())
    conversation = str(uuid4())
    now = iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    later = iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    window = {"device_id": device, "from": now, "to": later}
    requests = [
        ("GET", "/v1/me", None, None),
        ("POST", "/v1/credentials/revoke", None, None),
        ("POST", "/v1/consents", {"scope": "upload", "granted": True,
                                  "policy_version": "demo-1"}, None),
        ("POST", "/v1/devices", {"local_id": str(uuid4()), "model": "MR11",
                                 "protocol_version": "1"}, None),
        ("POST", "/v1/measurement-batches", {"schema_version": 1, "batch_id": str(uuid4()),
                                             "device_id": device, "app_version": "test",
                                             "samples": [measurement(
                                                 datetime.now(timezone.utc) - timedelta(minutes=1))]},
         None),
        ("GET", "/v1/measurements", None, window),
        ("GET", "/v1/summaries/night", None, {**window, "timezone": "UTC"}),
        ("POST", "/v1/observations", {"id": str(uuid4()), "device_id": device,
                                      "occurred_at": now, "text": "x"}, None),
        ("GET", "/v1/observations", None, {"device_id": device}),
        ("POST", "/v1/alerts", {"id": alert, "device_id": device,
                                "kind": "delivery_test", "text": "x"}, None),
        ("GET", "/v1/alerts", None, None),
        ("POST", f"/v1/alerts/{alert}/receipt", {"status": "received"}, None),
        ("GET", f"/v1/alerts/{alert}/deliveries", None, None),
        ("POST", "/v1/conversations", None, None),
        ("POST", f"/v1/conversations/{conversation}/messages",
         {"device_id": device, "question": "x", "from": now, "to": later,
          "timezone": "UTC"}, None),
        ("GET", f"/v1/ai-requests/{conversation}", None, None),
        ("GET", f"/v1/devices/{device}/config", None, None),
        ("POST", f"/v1/devices/{device}/config",
         {"base_version": 0, "confirmed_experimental_use": True, "parameters": {
             "enabled": False, "hr_low": None, "hr_high": None, "spo2_low": None,
             "duration_seconds": 60, "max_gap_seconds": 10, "required_signals": 1,
             "allow_unknown_quality": False}}, None),
        ("POST", f"/v1/devices/{device}/config-acks",
         {"version": 1, "status": "applied"}, None),
        ("GET", "/v1/export", None, {"device_id": device}),
        ("DELETE", f"/v1/devices/{device}/data", None,
         {"confirmation": "delete_health_data"}),
    ]
    for method, path, body, params in requests:
        response = client.request(method, path, json=body, params=params)
        assert response.status_code == 401, f"{method} {path}: {response.text}"
        assert response.json()["code"] == "authentication_required"


def test_ring_live_data_drives_event_detection_when_enabled(tmp_path):
    database = str(tmp_path / "guardian.db")
    clock = ManualClock(BASE_TIME)
    app = create_app(database, allow_real_rules=True,
                     llm_settings=LLMSettings(mode="off"),
                     apk_pairing_code=PAIRING_CODE, clock=clock)
    client = ClockedClient(app, clock)
    client.apk_db = database
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    # Three separate live batches are needed: one batch shares one server
    # receipt tick and must not satisfy the open dwell by itself.
    for index in range(3):
        assert ingest(client, headers, device_id,
                      [measurement(BASE_TIME + timedelta(seconds=index),
                                   value=130)]).status_code == 200

    events = client.get("/v1/events", params={"device_id": device_id}).json()
    assert any(event["kind"] == "high_heart_rate" and event["status"] == "active"
               for event in events)


def test_single_batch_does_not_satisfy_rule_dwell(tmp_path):
    database = str(tmp_path / "guardian.db")
    clock = ManualClock(BASE_TIME)
    app = create_app(database, allow_real_rules=True,
                     llm_settings=LLMSettings(mode="off"),
                     apk_pairing_code=PAIRING_CODE, clock=clock)
    client = ClockedClient(app, clock)
    client.apk_db = database
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    samples = [measurement(BASE_TIME + timedelta(seconds=index), value=130)
               for index in range(3)]
    assert ingest(client, headers, device_id, samples).status_code == 200
    assert client.get("/v1/events", params={"device_id": device_id}).json() == []


def test_history_uploads_do_not_enter_the_agent_stream(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)
    historical = measurement(BASE_TIME, value=80, source="device_history")
    assert ingest(client, headers, device_id, [historical]).status_code == 200
    latest = client.get("/v1/live/status", params={"device_id": device_id}).json()["latest"]
    assert latest is None


def test_two_devices_keep_separate_latest_state(client):
    _, headers = paired(client)
    first = allow_upload(client, headers)
    second = register(client, headers)
    assert ingest(client, headers, first,
                  [measurement(BASE_TIME, value=70)]).status_code == 200
    assert ingest(client, headers, second,
                  [measurement(BASE_TIME + timedelta(seconds=1), value=90)]).status_code == 200

    first_latest = client.get("/v1/live/status", params={"device_id": first}).json()["latest"]
    second_latest = client.get("/v1/live/status", params={"device_id": second}).json()["latest"]
    assert first_latest["device_id"] == first and first_latest["heart_rate"] == 70
    assert second_latest["device_id"] == second and second_latest["heart_rate"] == 90


def test_live_future_sample_does_not_block_or_mirror(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)

    future = BASE_TIME + timedelta(seconds=20)
    assert ingest(client, headers, device_id, [measurement(future)]).status_code == 200

    normal = BASE_TIME + timedelta(seconds=1)
    assert ingest(client, headers, device_id, [measurement(normal)]).status_code == 200

    history = client.get("/v1/history", params={"device_id": device_id}).json()
    assert len(history) == 1
    assert datetime.fromisoformat(
        history[0]["timestamp"].replace("Z", "+00:00")) == normal


def test_live_duplicate_sample_is_not_mirrored_twice(client):
    _, headers = paired(client)
    device_id = allow_upload(client, headers)
    stamp = BASE_TIME + timedelta(seconds=1)

    assert ingest(client, headers, device_id, [measurement(stamp)]).status_code == 200
    # A new sample_id evades APK-level dedupe, so the time state machine must
    # reject the duplicate before it can reach the legacy store.
    assert ingest(client, headers, device_id, [measurement(stamp)]).status_code == 200

    history = client.get("/v1/history", params={"device_id": device_id}).json()
    assert len(history) == 1
    assert datetime.fromisoformat(
        history[0]["timestamp"].replace("Z", "+00:00")) == stamp


def test_public_only_app_exposes_only_the_apk_contract(tmp_path):
    database = str(tmp_path / "guardian.db")
    app = create_app(database, llm_settings=LLMSettings(mode="off"), public_only=True)
    public = TestClient(app)
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM apk_pairing_codes").fetchone()[0] == 0

    assert public.get("/healthz").status_code == 200
    for path in ("/health", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc",
                 "/v1/assistant", "/v1/telemetry", "/v1/live/status",
                 "/v1/history", "/v1/events", "/v1/tools", "/ws/live"):
        assert public.get(path).status_code == 404, path
    assert public.get("/v1/me").status_code == 401

    code = ApkStorage(database).issue_pairing_code("公共入口测试")["code"]
    paired_up = public.post("/v1/pairings/exchange", json={"code": code})
    assert paired_up.status_code == 200
