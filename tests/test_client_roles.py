"""Role authorization and caregiver read/acknowledge contract tests."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from guardian.apk_storage import ApkStorage
from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.storage import Storage


@pytest.fixture()
def client(tmp_path):
    database = str(tmp_path / "roles.db")
    app = create_app(database, llm_settings=LLMSettings(mode="off"))
    test_client = TestClient(app)
    test_client.apk_db = database
    return test_client


def role_code(client, role, name):
    return ApkStorage(client.apk_db).issue_pairing_code(name, role=role)["code"]


def pair(client, role, name):
    response = client.post("/v1/pairings/exchange",
                           json={"code": role_code(client, role, name)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == role
    return body, {"Authorization": "Bearer " + body["token"]}


def grant(client, headers, scope):
    response = client.post("/v1/consents", json={
        "scope": scope, "granted": True, "policy_version": "demo-1"}, headers=headers)
    assert response.status_code == 200, response.text


def register_wearer_device(client, headers):
    grant(client, headers, "upload")
    response = client.post("/v1/devices", json={
        "local_id": str(uuid4()), "model": "SenseMind Band",
        "firmware_version": "0.7.0", "protocol_version": "1"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["device_id"]


def ingest_live(client, headers, device_id):
    now = datetime.now(timezone.utc)
    response = client.post("/v1/measurement-batches", json={
        "schema_version": 1, "batch_id": str(uuid4()), "device_id": device_id,
        "app_version": "test-role", "samples": [{
            "sample_id": str(uuid4()), "metric": "hr", "value": 72.0,
            "unit": "bpm", "observed_at": now.isoformat(),
            "phone_received_at": now.isoformat(), "time_basis": "device",
            "quality": "good", "source": "live_device"}]}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_roles_are_issued_and_reported(client):
    _, wearer = pair(client, "wearer", "佩戴者")
    _, caregiver = pair(client, "caregiver", "照护者")
    assert client.get("/v1/me", headers=wearer).json()["role"] == "wearer"
    assert client.get("/v1/me", headers=caregiver).json()["role"] == "caregiver"


def test_wearer_can_upload_but_not_use_caregiver_views(client):
    wearer_body, wearer = pair(client, "wearer", "佩戴者")
    _, caregiver = pair(client, "caregiver", "照护者")
    device_id = register_wearer_device(client, wearer)
    ingest_live(client, wearer, device_id)

    denied = client.get("/v1/subjects", headers=wearer)
    assert denied.status_code == 403
    assert denied.json()["code"] == "role_forbidden"

    # Caregiver cannot impersonate the wearer's upload path.
    denied = client.post("/v1/devices", json={
        "local_id": str(uuid4()), "model": "x",
        "firmware_version": "1", "protocol_version": "1"}, headers=caregiver)
    assert denied.status_code == 403

    subjects = client.get("/v1/subjects", headers=caregiver)
    assert subjects.status_code == 200, subjects.text
    rows = subjects.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["device_id"] == device_id
    assert row["latest"]["heart_rate"] == 72.0
    assert row["data_mode"] == "live"
    assert row["stale"] is False
    assert wearer_body["family_id"] == client.get("/v1/me", headers=caregiver).json()["family_id"]


def test_caregiver_reads_investigation_and_acknowledges_event(client):
    _, wearer = pair(client, "wearer", "佩戴者")
    caregiver_body, caregiver = pair(client, "caregiver", "照护者")
    device_id = register_wearer_device(client, wearer)
    ingest_live(client, wearer, device_id)

    store = Storage(client.apk_db)
    now = datetime.now(timezone.utc)
    transition = store.event_open(device_id, "high_heart_rate", now,
                                  "role test event")
    event = store.get_event(transition["event_id"])
    investigation, _ = store.create_investigation(
        event["id"], device_id, event["kind"])
    store.update_investigation(investigation["id"], status="completed",
                               ai_status="completed", evidence_count=1)
    store.add_investigation_transition(investigation["id"], "queued", "running",
                                       "worker_started")
    store.put_evidence({
        "event_id": event["id"], "device_id": device_id,
        "source_type": "heart_rate_estimate", "value": 72.0, "unit": "bpm",
        "quality_status": "good", "data_mode": "live", "synthetic": False,
        "time_sync_status": "gateway_estimated", "clock_uncertainty_ms": 1000,
        "source_ref": "role-test:hr:1"})

    listing = client.get("/v1/caregiver/events", headers=caregiver)
    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()["data"]] == [event["id"]]

    detail = client.get(f"/v1/caregiver/events/{event['id']}", headers=caregiver)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["event"]["id"] == event["id"]
    assert body["investigation"]["ai_status"] == "completed"
    assert body["evidence"][0]["source_type"] == "heart_rate_estimate"
    assert body["transitions"][0]["to_status"] == "running"

    # The wearer cannot acknowledge; only the caregiver credential can.
    denied = client.post(f"/v1/caregiver/events/{event['id']}/ack", headers=wearer)
    assert denied.status_code == 403

    acknowledged = client.post(f"/v1/caregiver/events/{event['id']}/ack",
                               headers=caregiver)
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["acknowledged_by"] == caregiver_body["name"]
    assert acknowledged.json()["already_acknowledged"] is False

    repeated = client.post(f"/v1/caregiver/events/{event['id']}/ack",
                           headers=caregiver)
    assert repeated.status_code == 200
    assert repeated.json()["already_acknowledged"] is True


def test_pre_role_database_migrates_to_legacy(tmp_path):
    database = str(tmp_path / "pre-role.db")
    con = sqlite3.connect(database)
    con.executescript("""
        CREATE TABLE apk_credentials (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
            guardian_id TEXT NOT NULL, family_id TEXT NOT NULL,
            name TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE apk_pairing_codes (
            id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, consumed_at TEXT);
    """)
    con.close()
    storage = ApkStorage(database)
    con = sqlite3.connect(database)
    credential_columns = {row[1] for row in con.execute("PRAGMA table_info(apk_credentials)")}
    pairing_columns = {row[1] for row in con.execute("PRAGMA table_info(apk_pairing_codes)")}
    con.close()
    assert "role" in credential_columns and "role" in pairing_columns
    issued = storage.issue_pairing_code("迁移测试", role="caregiver")
    assert issued["role"] == "caregiver"


def test_caregiver_event_detail_is_family_scoped(client):
    _, caregiver = pair(client, "caregiver", "照护者")
    store = Storage(client.apk_db)
    transition = store.event_open("not-my-device", "data_quality",
                                  datetime.now(timezone.utc), "unregistered")
    response = client.get(f"/v1/caregiver/events/{transition['event_id']}",
                          headers=caregiver)
    assert response.status_code == 404
    assert response.json()["code"] == "device_not_found"


def consent(client, headers, scope, granted):
    return client.post("/v1/consents", json={
        "scope": scope, "granted": granted, "policy_version": "demo-1"},
        headers=headers)


def test_consent_scopes_are_role_scoped(client):
    _, wearer = pair(client, "wearer", "佩戴者")
    _, caregiver = pair(client, "caregiver", "照护者")

    # Each role owns only the consent scopes for which it is the actor.
    assert consent(client, caregiver, "remote_alerts", True).status_code == 200
    assert consent(client, caregiver, "ai_analysis", True).status_code == 200
    assert consent(client, caregiver, "upload", True).status_code == 403
    assert consent(client, wearer, "upload", True).status_code == 200
    assert consent(client, wearer, "remote_alerts", True).status_code == 403
    assert consent(client, wearer, "ai_analysis", True).status_code == 403

    denied = consent(client, wearer, "remote_alerts", True)
    assert denied.json()["code"] == "role_forbidden"
    assert consent(client, {}, "upload", True).status_code == 401
    assert consent(client, caregiver, "unknown_scope", True).status_code == 422


def test_remote_alerts_consent_gates_access_and_revocation(client):
    _, caregiver = pair(client, "caregiver", "照护者")

    denied = client.get("/v1/alerts", headers=caregiver)
    assert denied.status_code == 403
    assert denied.json()["code"] == "consent_required"

    granted = consent(client, caregiver, "remote_alerts", True)
    assert granted.status_code == 200, granted.text
    assert client.get("/v1/alerts", headers=caregiver).status_code == 200
    consents = client.get("/v1/me", headers=caregiver).json()["consents"]
    assert consents["remote_alerts"] is True

    revoked = consent(client, caregiver, "remote_alerts", False)
    assert revoked.status_code == 200, revoked.text
    denied = client.get("/v1/alerts", headers=caregiver)
    assert denied.status_code == 403
    assert denied.json()["code"] == "consent_required"
    consents = client.get("/v1/me", headers=caregiver).json()["consents"]
    assert consents["remote_alerts"] is False


def test_consent_is_per_credential_not_family_wide(client):
    _, first = pair(client, "caregiver", "照护者甲")
    _, second = pair(client, "caregiver", "照护者乙")
    assert consent(client, first, "remote_alerts", True).status_code == 200
    assert client.get("/v1/alerts", headers=first).status_code == 200
    assert client.get("/v1/alerts", headers=second).status_code == 403
    assert consent(client, first, "remote_alerts", False).status_code == 200
    assert client.get("/v1/alerts", headers=first).status_code == 403
