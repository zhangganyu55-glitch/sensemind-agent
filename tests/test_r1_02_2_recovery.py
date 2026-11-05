"""R1-02.2: durable ingestion outbox and processing recovery.

Every test uses a temporary SQLite database.  Failures are injected by
monkeypatching a storage/investigation method for one call, which exercises
the same stage/retry path as a real transient write failure.
"""
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from guardian import outbox as outbox_module
from guardian.clock import ManualClock
from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.models import Telemetry
from guardian.processing import BOOT_ID


BASE = datetime(2026, 10, 28, tzinfo=timezone.utc)
DEVICE = "r1022_device"
PAIRING = "r1022-pairing-code-123"


def reading(seconds, *, hr=75, spo2=98, source="simulator", device=DEVICE):
    return {
        "device_id": device,
        "timestamp": (BASE + timedelta(seconds=seconds)).isoformat(),
        "source": source,
        "heart_rate": hr,
        "spo2": spo2,
        "acc_x": 0, "acc_y": 0, "acc_z": 1,
        "signal_quality": "good",
        "reading_valid": True,
    }


def telemetry(seconds, *, hr=75, source="simulator", device=DEVICE):
    return Telemetry(
        device_id=device, timestamp=BASE + timedelta(seconds=seconds),
        source=source, heart_rate=hr, spo2=98,
        acc_x=0, acc_y=0, acc_z=1,
        signal_quality="good", reading_valid=True)


def make_app(tmp_path, *, clock=None, recovery=False, **kwargs):
    database = str(tmp_path / "guardian.db")
    moment = clock or ManualClock(BASE)
    app = create_app(
        database, allow_real_rules=True,
        llm_settings=LLMSettings(mode="off"), clock=moment,
        processing_recovery=recovery, **kwargs)
    return app, database, moment


def parse_ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def counts(database):
    with sqlite3.connect(database) as con:
        return {
            "telemetry": con.execute(
                "SELECT COUNT(*) FROM telemetry").fetchone()[0],
            "jobs": con.execute(
                "SELECT COUNT(*) FROM ingest_jobs").fetchone()[0],
            "events": con.execute(
                "SELECT COUNT(*) FROM events").fetchone()[0],
            "investigations": con.execute(
                "SELECT COUNT(*) FROM investigations").fetchone()[0],
            "evidence": con.execute(
                "SELECT COUNT(*) FROM evidence").fetchone()[0],
        }


def post(client, clock, seconds, **changes):
    clock.advance(1)
    return client.post("/v1/telemetry", json=reading(seconds, **changes))


# 1. telemetry write -> Monitoring raises; HTTP duplicate retry recovers.
def test_monitoring_failure_is_recovered_by_duplicate_retry(tmp_path):
    app, database, clock = make_app(tmp_path)
    processor = app.state.processor
    storage = processor.storage
    original = storage.update_monitoring
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("monitoring write failed")
        return original(*args, **kwargs)

    storage.update_monitoring = flaky
    client = TestClient(app)
    first = post(client, clock, 0)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["accepted"] is True
    assert body["processing"]["status"] == "failed"
    assert body["processing"]["stages"][0]["stage"] == "monitoring"
    assert body["processing"]["stages"][0]["status"] == "failed"
    job_id = body["processing"]["job_id"]
    assert counts(database)["telemetry"] == 1

    storage.update_monitoring = original
    # The client retry is a 409 duplicate, but it must also resume the job.
    retry = post(client, clock, 0)
    assert retry.status_code == 409
    job = storage.job(job_id)
    assert job["status"] == "completed"
    assert storage.monitoring(DEVICE, now=clock.now())["state"] == "NORMAL"
    assert counts(database) == {
        "telemetry": 1, "jobs": 1, "events": 0,
        "investigations": 0, "evidence": 0}


# 1b. Telemetry and its outbox job must commit or roll back together.
def test_telemetry_and_job_are_committed_atomically(tmp_path):
    app, database, clock = make_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    original = outbox_module.insert_job
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("job insert failed")
        return original(*args, **kwargs)

    outbox_module.insert_job = flaky
    try:
        failed = client.post("/v1/telemetry", json=reading(0))
    finally:
        outbox_module.insert_job = original
    assert failed.status_code == 500
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM ingest_jobs").fetchone()[0] == 0

    retried = client.post("/v1/telemetry", json=reading(0))
    assert retried.status_code == 200, retried.text
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM ingest_jobs").fetchone()[0] == 1


# 2. rules raise after monitoring; retry evaluates the same counter state once.
def test_rule_failure_rolls_back_counters_and_recovers_one_event(tmp_path):
    app, database, clock = make_app(tmp_path)
    processor = app.state.processor
    storage = processor.storage
    original = storage.event_open
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("event write failed")
        return original(*args, **kwargs)

    storage.event_open = flaky
    client = TestClient(app)
    post(client, clock, 0, hr=130)
    post(client, clock, 1, hr=130)
    third = post(client, clock, 2, hr=130)
    body = third.json()
    assert body["accepted"] is True
    assert body["processing"]["status"] == "failed"
    assert [item["stage"] for item in body["processing"]["stages"]
            if item["status"] == "failed"] == ["rules"]
    assert storage.events(DEVICE, active_only=True) == []
    job_id = body["processing"]["job_id"]

    storage.event_open = original
    outcome = processor.process_job(job_id)
    assert outcome["status"] == "completed"
    active = storage.events(DEVICE, active_only=True)
    assert len(active) == 1 and active[0]["kind"] == "high_heart_rate"

    # Re-running a completed job must not create a second active event.
    processor.process_job(job_id)
    processor.process_job(job_id)
    assert len(storage.events(DEVICE, active_only=True)) == 1
    assert counts(database)["events"] == 1


# 3b. A rule failure after one event opened must keep that event and recover
# the remaining rules without duplicating the first event.
def test_partial_rule_failure_keeps_opened_event_and_recovers(tmp_path):
    app, _database, clock = make_app(tmp_path)
    processor = app.state.processor
    storage = processor.storage
    original = storage.event_open
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second event write failed")
        return original(*args, **kwargs)

    storage.event_open = flaky
    client = TestClient(app)
    for index in range(3):
        body = post(client, clock, index, hr=130, spo2=91).json()
    assert body["processing"]["status"] == "failed"
    job_id = body["processing"]["job_id"]
    active = storage.events(DEVICE, active_only=True)
    assert {event["kind"] for event in active} == {"high_heart_rate"}
    persisted = storage.job(job_id)["transitions"]
    assert [item["kind"] for item in persisted] == ["high_heart_rate"]

    storage.event_open = original
    outcome = processor.process_job(job_id)
    assert outcome["status"] == "completed"
    active = storage.events(DEVICE, active_only=True)
    assert {event["kind"] for event in active} == {
        "high_heart_rate", "low_spo2", "concurrent_signals"}
    assert len(active) == 3
    transitions = storage.job(job_id)["transitions"]
    assert len(transitions) == 3

    # Repeated recovery must not duplicate the already-open event.
    processor.process_job(job_id)
    assert len(storage.events(DEVICE, active_only=True)) == 3


# 3 + 4. interrupted / pending / failed jobs recover on the next app start.
def test_restart_recovers_interrupted_pending_and_failed_jobs(tmp_path):
    app, database, _clock = make_app(tmp_path)
    storage = app.state.processor.storage
    refs = []
    for seconds, status in ((0, "processing"), (1, "failed"), (2, "pending")):
        ref = storage.put_telemetry_with_job(
            telemetry(seconds), received_at=BASE, monotonic=float(seconds),
            boot_id=BOOT_ID)
        job_id = ref["job_id"]
        if status == "processing":
            storage.start_job(job_id)
        elif status == "failed":
            storage.start_job(job_id)
            storage.fail_job(job_id, "simulated process exit")
        refs.append((job_id, status))

    # A fresh app on the same database runs startup recovery.
    restarted, _, _ = make_app(tmp_path, clock=ManualClock(BASE), recovery=True)
    restarted_storage = restarted.state.processor.storage
    for job_id, status in refs:
        job = restarted_storage.job(job_id)
        assert job["status"] == "completed", (status, job)
        assert all(row["status"] in ("completed", "skipped")
                   for row in restarted_storage.job_stages(job_id))
    assert restarted_storage.monitoring(
        DEVICE, now=BASE)["state"] == "NORMAL"
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry").fetchone()[0] == 3


# 5. A duplicate HTTP retry must not create another sample/job/event.
def test_duplicate_retry_does_not_duplicate_rows_or_events(tmp_path):
    app, database, clock = make_app(tmp_path)
    storage = app.state.processor.storage
    client = TestClient(app)
    for index in range(3):
        assert post(client, clock, index, hr=130).status_code == 200
    active = storage.events(DEVICE, active_only=True)
    assert len(active) == 1
    repeated = post(client, clock, 2, hr=130)
    assert repeated.status_code == 409
    summary = counts(database)
    assert summary["telemetry"] == 3
    assert summary["jobs"] == 3
    assert summary["events"] == 1
    assert len(storage.events(DEVICE, active_only=True)) == 1
    with sqlite3.connect(database) as con:
        rejection = con.execute(
            "SELECT reason FROM telemetry_rejections WHERE device_id=?",
            (DEVICE,)).fetchone()
    assert rejection[0] == "duplicate"


# APK helpers ---------------------------------------------------------------
def pair_apk(client):
    paired = client.post("/v1/pairings/exchange", json={"code": PAIRING})
    assert paired.status_code == 200, paired.text
    headers = {"Authorization": "Bearer " + paired.json()["token"]}
    granted = client.post(
        "/v1/consents",
        json={"scope": "upload", "granted": True, "policy_version": "demo-1"},
        headers=headers)
    assert granted.status_code == 200, granted.text
    registered = client.post(
        "/v1/devices",
        json={"local_id": str(uuid4()), "model": "MR11",
              "firmware_version": "1.0.0", "protocol_version": "1"},
        headers=headers)
    assert registered.status_code == 200, registered.text
    return headers, registered.json()["device_id"]


def measurement(observed_at, value=75):
    return {
        "sample_id": str(uuid4()), "metric": "hr", "value": value,
        "unit": "bpm", "observed_at": observed_at.isoformat(),
        "phone_received_at": (observed_at + timedelta(seconds=1)).isoformat(),
        "time_basis": "device", "quality": "good", "source": "live_device",
    }


def apk_ingest(client, headers, device_id, batch_id, observed_at, sample=None):
    return client.post(
        "/v1/measurement-batches",
        json={"schema_version": 1, "batch_id": str(batch_id),
              "device_id": device_id, "app_version": "test-1",
              "samples": [sample or measurement(observed_at)]},
        headers=headers)


# 5b. APK raw batch and adaptation job must commit or roll back together.
def test_apk_batch_and_job_are_committed_atomically(tmp_path):
    app, database, _clock = make_app(tmp_path, apk_pairing_code=PAIRING)
    client = TestClient(app, raise_server_exceptions=False)
    headers, device_id = pair_apk(client)
    batch_id = uuid4()
    sample = measurement(BASE)

    original = outbox_module.insert_job
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("job insert failed")
        return original(*args, **kwargs)

    outbox_module.insert_job = flaky
    try:
        failed = apk_ingest(client, headers, device_id, batch_id, BASE,
                            sample=sample)
    finally:
        outbox_module.insert_job = original
    assert failed.status_code == 500
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM apk_batches").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM apk_samples").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM ingest_jobs").fetchone()[0] == 0

    retried = apk_ingest(client, headers, device_id, batch_id, BASE,
                         sample=sample)
    assert retried.status_code == 200, retried.text
    assert retried.json()["agent_status"] == "completed"
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM apk_batches").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM apk_samples").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry WHERE device_id=?",
            (device_id,)).fetchone()[0] == 1


# 6. APK raw commit survives an adaptation failure; same batch retries it.
def test_apk_batch_adaptation_failure_resumes_on_same_batch_id(tmp_path):
    app, database, _clock = make_app(tmp_path, apk_pairing_code=PAIRING)
    processor = app.state.processor
    client = TestClient(app)
    headers, device_id = pair_apk(client)
    batch_id = uuid4()
    observed = BASE
    sample = measurement(observed)

    original = processor.engine.analyze
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("agent adaptation failed")
        return original(*args, **kwargs)

    processor.engine.analyze = flaky
    first = apk_ingest(client, headers, device_id, batch_id, observed,
                       sample=sample)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "committed"          # raw batch is durable
    assert body["raw_committed"] is True
    assert body["agent_processed"] is False
    assert body["agent_status"] in ("pending", "failed")
    assert body["pending_tasks"] >= 1
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM apk_samples").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry").fetchone()[0] == 0

    processor.engine.analyze = original
    retried = apk_ingest(client, headers, device_id, batch_id, observed,
                         sample=sample)
    assert retried.status_code == 200, retried.text
    retried_body = retried.json()
    assert retried_body["agent_processed"] is True
    assert retried_body["agent_status"] == "completed"
    assert retried_body["pending_tasks"] == 0
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry WHERE device_id=?",
            (device_id,)).fetchone()[0] == 1

    # A third identical upload must not execute adaptation again.
    before = calls["n"]
    again = apk_ingest(client, headers, device_id, batch_id, observed,
                       sample=sample)
    assert again.status_code == 200
    assert calls["n"] == before
    assert again.json()["agent_status"] == "completed"
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry WHERE device_id=?",
            (device_id,)).fetchone()[0] == 1


# 6a. A failed adaptation job resumes on the next service start.
def test_apk_adaptation_job_is_recovered_after_restart(tmp_path):
    app, database, clock = make_app(tmp_path, apk_pairing_code=PAIRING)
    processor = app.state.processor
    client = TestClient(app)
    headers, device_id = pair_apk(client)
    batch_id = uuid4()
    sample = measurement(BASE)

    original = processor.engine.analyze
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("agent adaptation failed")
        return original(*args, **kwargs)

    processor.engine.analyze = flaky
    first = apk_ingest(client, headers, device_id, batch_id, BASE,
                       sample=sample)
    assert first.status_code == 200
    assert first.json()["agent_processed"] is False
    storage = processor.storage
    job = storage.job_for_batch(device_id, str(batch_id))
    assert job["status"] == "failed"
    assert job["last_error"]
    processor.engine.analyze = original

    # A new app instance on the same database runs startup recovery.
    restarted, _, _ = make_app(tmp_path, clock=ManualClock(BASE), recovery=True)
    restarted_storage = restarted.state.processor.storage
    recovered = restarted_storage.job_for_batch(device_id, str(batch_id))
    assert recovered["status"] == "completed"
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry WHERE device_id=?",
            (device_id,)).fetchone()[0] == 1


# 6b. Adaptation succeeds but a child processing stage fails; the same batch
# retry resumes the child even though the batch job itself is complete.
def test_apk_child_processing_failure_is_retried_on_batch_resubmit(tmp_path):
    app, database, _clock = make_app(tmp_path, apk_pairing_code=PAIRING)
    processor = app.state.processor
    storage = processor.storage
    client = TestClient(app)
    headers, device_id = pair_apk(client)
    batch_id = uuid4()
    sample = measurement(BASE)

    original = storage.update_monitoring
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("monitoring write failed")
        return original(*args, **kwargs)

    storage.update_monitoring = flaky
    first = apk_ingest(client, headers, device_id, batch_id, BASE,
                       sample=sample)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["raw_committed"] is True
    assert body["agent_processed"] is True
    assert body["agent_status"] == "pending"
    assert body["pending_tasks"] >= 1
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry WHERE device_id=?",
            (device_id,)).fetchone()[0] == 1

    storage.update_monitoring = original
    retried = apk_ingest(client, headers, device_id, batch_id, BASE,
                         sample=sample)
    assert retried.status_code == 200, retried.text
    retried_body = retried.json()
    assert retried_body["agent_status"] == "completed"
    assert retried_body["pending_tasks"] == 0
    assert storage.monitoring(device_id, now=BASE)["state"] == "NORMAL"
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM telemetry WHERE device_id=?",
            (device_id,)).fetchone()[0] == 1


# 7. Successful adaptation is not re-executed on a repeated batch.
def test_apk_successful_batch_is_not_adapted_twice(tmp_path):
    app, database, _clock = make_app(tmp_path, apk_pairing_code=PAIRING)
    processor = app.state.processor
    client = TestClient(app)
    headers, device_id = pair_apk(client)
    batch_id = uuid4()
    sample = measurement(BASE)

    original = processor._adapt
    calls = {"n": 0}

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    processor._adapt = counted
    first = apk_ingest(client, headers, device_id, batch_id, BASE, sample=sample)
    assert first.json()["agent_status"] == "completed"
    assert calls["n"] == 1
    repeated = apk_ingest(client, headers, device_id, batch_id, BASE,
                          sample=sample)
    assert repeated.json()["agent_status"] == "completed"
    assert calls["n"] == 1
    assert counts(database)["telemetry"] == 1


# 8. Recovering the investigations stage repeatedly creates no duplicates.
def test_repeated_recovery_has_no_duplicate_investigation_or_evidence(tmp_path):
    app, database, clock = make_app(tmp_path)
    processor = app.state.processor
    investigations = processor.investigations
    storage = processor.storage
    original = investigations.on_transitions
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("investigation enqueue failed")
        return original(*args, **kwargs)

    investigations.on_transitions = flaky
    client = TestClient(app)
    for index in range(3):
        body = post(client, clock, index, hr=130).json()
    assert body["processing"]["status"] == "failed"
    failed = [item["stage"] for item in body["processing"]["stages"]
              if item["status"] == "failed"]
    assert failed == ["investigations"]
    job_id = body["processing"]["job_id"]
    event = storage.events(DEVICE, active_only=True)
    assert len(event) == 1
    event_id = event[0]["id"]
    assert storage.investigation_for_event(event_id) is None

    investigations.on_transitions = original
    first = processor.process_job(job_id)
    second = processor.process_job(job_id)
    assert first["status"] == "completed"
    assert second["status"] == "completed"
    investigation = storage.investigation_for_event(event_id)
    assert investigation is not None
    # The notification and investigation are each keyed by event id.
    assert len(storage.notifications_for(event_id)) == 1
    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM investigations WHERE event_id=?",
            (event_id,)).fetchone()[0] == 1

    # The investigation worker writes evidence asynchronously.  Wait for it
    # to finish, then prove repeated job recovery cannot add a second copy.
    deadline = time.time() + 5
    while time.time() < deadline:
        current = storage.investigation_for_event(event_id) or {}
        if current.get("completed_at"):
            break
        time.sleep(0.02)
    with sqlite3.connect(database) as con:
        before_evidence = con.execute(
            "SELECT COUNT(*) FROM evidence WHERE event_id=?",
            (event_id,)).fetchone()[0]
    for _ in range(3):
        processor.process_job(job_id)
    with sqlite3.connect(database) as con:
        after_evidence = con.execute(
            "SELECT COUNT(*) FROM evidence WHERE event_id=?",
            (event_id,)).fetchone()[0]
    assert after_evidence == before_evidence
    assert storage.investigation_for_event(event_id)["status"] in (
        "completed", "failed")


# 9. A 19 s delayed upload must not refresh Monitoring or the live anchor.
def test_delayed_19s_upload_does_not_refresh_monitoring_or_anchor(tmp_path):
    app, _database, clock = make_app(tmp_path)
    processor = app.state.processor
    storage = processor.storage
    client = TestClient(app)

    first = post(client, clock, 0, source="ring").json()
    assert first["accepted"] is True
    clock_after_first = clock.now()
    assert storage.monitoring(DEVICE, now=clock_after_first)["state"] == "NORMAL"
    assert storage.monitoring(DEVICE, now=clock_after_first)["last_valid_at"] \
        == BASE.isoformat()

    # Move to BASE+35 and upload a sample observed at BASE+16 (19 s old).
    clock.advance(34)
    delayed = client.post("/v1/telemetry", json=reading(16, source="ring"))
    assert delayed.status_code == 200, delayed.text
    body = delayed.json()
    assert body["evaluation"] == "delayed_sample_ignored"
    assert body["accepted"] is True
    assert body["processing"]["status"] == "completed"
    stages = {item["stage"]: item["status"]
              for item in body["processing"]["stages"]}
    assert stages["monitoring"] == "skipped"
    assert stages["rules"] == "skipped"

    state = storage.monitoring(DEVICE, now=clock.now())
    assert state["state"] == "STALE"
    assert state["derived"] is True
    assert state["last_seen_at"] == BASE.isoformat()
    assert state["last_valid_at"] == BASE.isoformat()
    # The live ordering anchor is still BASE, not BASE+16.
    assert processor.engine._last_sample[DEVICE] == BASE
    assert parse_ts(storage.latest(DEVICE)["timestamp"]) == \
        BASE + timedelta(seconds=16)
    assert parse_ts(storage.latest_valid(
        DEVICE, now=clock.now(), max_lag_seconds=5.0)["timestamp"]) == BASE

    # The next real-time sample is accepted and restores NORMAL.
    next_sample = post(client, clock, 36, source="ring").json()
    assert next_sample["accepted"] is True
    assert next_sample["evaluation"] == "demo_experimental_rules"
    normal = storage.monitoring(DEVICE, now=clock.now())
    assert normal["state"] == "NORMAL"
    assert normal["last_valid_at"] == (BASE + timedelta(seconds=36)).isoformat()


# 10. Normal telemetry reaches completed with every stage final.
def test_normal_telemetry_processing_reaches_completed(tmp_path):
    app, database, clock = make_app(tmp_path)
    storage = app.state.processor.storage
    client = TestClient(app)
    body = post(client, clock, 0).json()
    assert body["processing"]["status"] == "completed"
    job = storage.job(body["processing"]["job_id"])
    assert job["status"] == "completed"
    stages = storage.job_stages(job["id"])
    assert {row["stage"] for row in stages} == {
        "monitoring", "rules", "investigations", "decisions", "supplement"}
    assert all(row["status"] in ("completed", "skipped") for row in stages)
    assert storage.job_for_timestamp(DEVICE, BASE)["id"] == job["id"]
    assert counts(database)["telemetry"] == 1
