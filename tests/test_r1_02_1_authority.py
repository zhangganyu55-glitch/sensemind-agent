"""R1-02.1: single-authority ingress and legacy data integrity."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from guardian.clock import ManualClock
from guardian.decision import DecisionEngine
from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.models import Telemetry
from guardian.public import create_public_app
from guardian.storage import Storage


BASE = datetime(2026, 10, 28, tzinfo=timezone.utc)
DEVICE = "r1021_device"
PAIRING = "r1021-pairing-code-123"


def put(storage, device, seconds, *, received, source="ring", replay=False):
    telemetry = Telemetry(
        device_id=device, timestamp=BASE + timedelta(seconds=seconds),
        source=source, data_mode="replay" if replay else None,
        heart_rate=75, spo2=98, acc_x=0, acc_y=0, acc_z=1,
        signal_quality="good", reading_valid=True)
    storage.put_telemetry(telemetry, received_at=received)
    return telemetry


def reading(seconds, device=DEVICE):
    return {
        "device_id": device,
        "timestamp": (BASE + timedelta(seconds=seconds)).isoformat(),
        "source": "ring",
        "heart_rate": 75,
        "spo2": 98,
        "acc_x": 0, "acc_y": 0, "acc_z": 1,
        "signal_quality": "good",
        "reading_valid": True,
    }


def test_migration_quarantines_provable_future_rows_and_is_repeatable(tmp_path):
    database = str(tmp_path / "guardian.db")
    storage = Storage(database)
    put(storage, DEVICE, 0, received=BASE)
    put(storage, DEVICE, 20, received=BASE)          # future vs receipt
    put(storage, "replay_device", 20, received=BASE, source="simulator",
        replay=True)                                  # replay: leave for phase C

    # latest_valid scans past the newest future row even before migration.
    assert storage.latest_valid(DEVICE, now=BASE)["timestamp"].endswith("00:00Z")

    first = storage.quarantine_invalid_future_telemetry(now=BASE)
    second = storage.quarantine_invalid_future_telemetry(now=BASE)
    assert first["quarantined"] == 1
    assert second["quarantined"] == 0

    quarantined = storage.quarantined_telemetry(DEVICE)
    assert len(quarantined) == 1
    assert quarantined[0]["reason"] == "future_vs_receipt"
    assert quarantined[0]["data"]["timestamp"].endswith("00:20Z")

    # Valid t0 remains; replay future remains for the replay phase.
    assert storage.latest_valid(DEVICE, now=BASE)["timestamp"].endswith("00:00Z")
    assert storage.latest_valid("replay_device", now=BASE) is None

    with sqlite3.connect(database) as con:
        runs = con.execute("SELECT COUNT(*) FROM telemetry_migrations").fetchone()[0]
        telemetry_ids = [row[0] for row in con.execute(
            "SELECT original_id FROM telemetry_quarantine")]
    assert runs == 2
    assert len(telemetry_ids) == 1


def test_hydration_uses_valid_anchor_and_rejects_duplicate_after_restart(tmp_path):
    database = str(tmp_path / "guardian.db")
    storage = Storage(database)
    put(storage, DEVICE, 0, received=BASE)
    put(storage, DEVICE, 20, received=BASE)  # old future row, newest id

    clock = ManualClock(BASE + timedelta(seconds=2))
    app = create_app(database, allow_real_rules=True,
                     llm_settings=LLMSettings(mode="off"), clock=clock)
    client = TestClient(app)

    clock.advance(1)
    duplicate = client.post("/v1/telemetry", json=reading(0))
    assert duplicate.status_code == 409, duplicate.text

    history = client.get("/v1/history", params={"device_id": DEVICE}).json()
    assert [item["timestamp"] for item in history] == [
        (BASE).isoformat().replace("+00:00", "Z")]


def test_old_future_row_does_not_appear_in_live_status_or_observe(tmp_path):
    database = str(tmp_path / "guardian.db")
    storage = Storage(database)
    put(storage, DEVICE, 20, received=BASE)  # only a future row

    clock = ManualClock(BASE)
    app = create_app(database, allow_real_rules=True,
                     llm_settings=LLMSettings(mode="off"), clock=clock)
    client = TestClient(app)

    status = client.get("/v1/live/status",
                        params={"device_id": DEVICE}).json()
    assert status["latest"] is None

    engine = DecisionEngine(storage, None, enabled=True)
    decisions = engine.evaluate(DEVICE, now=BASE)
    assert decisions
    assert all(item["decision_type"] != "OBSERVE" for item in decisions)
    assert all(item["reason_code"] != "normal_observe" for item in decisions)


def test_rejected_future_sample_does_not_run_decisions(tmp_path):
    database = str(tmp_path / "guardian.db")
    clock = ManualClock(BASE)
    app = create_app(database, allow_real_rules=True,
                     llm_settings=LLMSettings(mode="off"), clock=clock,
                     decision_engine_enabled=True,
                     decision_background=False)
    client = TestClient(app)

    clock.advance(1)
    response = client.post("/v1/telemetry", json=reading(20))
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is False

    decisions = client.get("/v1/decisions",
                           params={"device_id": DEVICE}).json()["data"]
    assert decisions == []
    assert client.get("/v1/events",
                      params={"device_id": DEVICE}).json() == []


def test_public_proxy_forwards_apk_and_blocks_legacy(tmp_path):
    database = str(tmp_path / "guardian.db")
    clock = ManualClock(BASE)
    local_app = create_app(
        database, allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=clock,
        apk_pairing_code=PAIRING)
    transport = httpx.ASGITransport(app=local_app)
    public_app = create_public_app(
        local_base_url="http://local", transport=transport)
    public = TestClient(public_app)

    assert public.get("/healthz").status_code == 200
    assert public.get("/v1/telemetry").status_code == 404
    assert public.get("/console").status_code == 404
    assert public.get("/docs").status_code == 404

    paired = public.post("/v1/pairings/exchange", json={"code": PAIRING})
    assert paired.status_code == 200, paired.text
    headers = {"Authorization": "Bearer " + paired.json()["token"]}
    assert public.post(
        "/v1/consents",
        json={"scope": "upload", "granted": True, "policy_version": "demo-1"},
        headers=headers).status_code == 200
    registered = public.post(
        "/v1/devices",
        json={"local_id": str(uuid4()), "model": "MR11",
              "firmware_version": "1.0.0", "protocol_version": "1"},
        headers=headers)
    assert registered.status_code == 200, registered.text
    device_id = registered.json()["device_id"]

    measurement = {
        "sample_id": str(uuid4()), "metric": "hr", "value": 75,
        "unit": "bpm", "observed_at": BASE.isoformat(),
        "phone_received_at": (BASE + timedelta(seconds=1)).isoformat(),
        "time_basis": "device", "quality": "good", "source": "live_device",
    }
    ingested = public.post(
        "/v1/measurement-batches",
        json={"schema_version": 1, "batch_id": str(uuid4()),
              "device_id": device_id, "app_version": "test-1",
              "samples": [measurement]},
        headers=headers)
    assert ingested.status_code == 200, ingested.text

    local = TestClient(local_app)
    history = local.get("/v1/history",
                        params={"device_id": device_id}).json()
    assert len(history) == 1
    assert history[0]["heart_rate"] == 75


def test_concurrent_local_and_public_ingest_share_one_event_lifecycle(tmp_path):
    database = str(tmp_path / "guardian.db")
    clock = ManualClock(BASE)
    local_app = create_app(
        database, allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=clock,
        apk_pairing_code=PAIRING)
    proxy_app = create_public_app(
        local_base_url="http://local",
        transport=httpx.ASGITransport(app=local_app))

    async def run():
        local_transport = httpx.ASGITransport(app=local_app)
        proxy_transport = httpx.ASGITransport(app=proxy_app)
        async with (httpx.AsyncClient(transport=local_transport,
                                      base_url="http://local",
                                      trust_env=False) as local,
                    httpx.AsyncClient(transport=proxy_transport,
                                      base_url="http://public",
                                      trust_env=False) as public):
            paired = await public.post("/v1/pairings/exchange",
                                       json={"code": PAIRING})
            assert paired.status_code == 200, paired.text
            headers = {"Authorization": "Bearer " + paired.json()["token"]}
            grant = await public.post(
                "/v1/consents",
                json={"scope": "upload", "granted": True,
                      "policy_version": "demo-1"}, headers=headers)
            assert grant.status_code == 200, grant.text
            registered = await public.post(
                "/v1/devices",
                json={"local_id": str(uuid4()), "model": "MR11",
                      "firmware_version": "1.0.0", "protocol_version": "1"},
                headers=headers)
            assert registered.status_code == 200, registered.text
            device_id = registered.json()["device_id"]

            def sample(seconds):
                return {
                    "device_id": device_id,
                    "timestamp": (BASE + timedelta(seconds=seconds)).isoformat(),
                    "source": "ring", "heart_rate": 130, "spo2": 98,
                    "acc_x": 0, "acc_y": 0, "acc_z": 1,
                    "signal_quality": "good", "reading_valid": True,
                }

            first = await local.post("/v1/telemetry", json=sample(0))
            assert first.status_code == 200, first.text
            clock.advance(1)
            second = await local.post("/v1/telemetry", json=sample(1))
            assert second.status_code == 200, second.text
            clock.advance(1)

            measurement = {
                "sample_id": str(uuid4()), "metric": "hr", "value": 130,
                "unit": "bpm",
                "observed_at": (BASE + timedelta(seconds=2)).isoformat(),
                "phone_received_at": (BASE + timedelta(seconds=3)).isoformat(),
                "time_basis": "device", "quality": "good",
                "source": "live_device",
            }
            results = await asyncio.gather(
                local.post("/v1/telemetry", json=sample(2)),
                public.post("/v1/measurement-batches", headers=headers,
                            json={"schema_version": 1,
                                  "batch_id": str(uuid4()),
                                  "device_id": device_id,
                                  "app_version": "test-1",
                                  "samples": [measurement]}),
                return_exceptions=True)
            assert all(not isinstance(item, Exception) for item in results)
            return device_id

    device_id = asyncio.run(run())
    with sqlite3.connect(database) as con:
        rows = con.execute(
            "SELECT timestamp, COUNT(*) FROM telemetry WHERE device_id=? "
            "GROUP BY timestamp", (device_id,)).fetchall()
        active = con.execute(
            "SELECT COUNT(*) FROM events WHERE device_id=? AND kind=? "
            "AND status='active'", (device_id, "high_heart_rate")).fetchone()[0]
    assert len(rows) == 3
    assert all(count == 1 for _, count in rows)
    assert active == 1
