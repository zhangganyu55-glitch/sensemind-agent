"""V0.1.2 T12-1: sample/server/monotonic time separation and lifecycle."""
import sqlite3
from datetime import datetime, timedelta, timezone

from guardian import monitoring
from guardian.clock import ManualClock
from guardian.detection import DetectionEngine
from guardian.models import DeviceStatus, Telemetry
from guardian.storage import Storage


NOW = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
DEVICE = "guardian_001"


def make_engine(tmp_path, *, name="timebase.db", lag=5.0, future=5.0,
                allow_real_rules=True):
    storage = Storage(str(tmp_path / name))
    clock = ManualClock(NOW)
    engine = DetectionEngine(
        storage, allow_real_rules=allow_real_rules,
        open_dwell_seconds=2.0, close_dwell_seconds=10.0,
        max_sample_gap_seconds=5.0, max_sample_lag_seconds=lag,
        max_future_skew_seconds=future, clock=clock)
    return storage, engine, clock


def sample(storage, engine, *, when, received, monotonic, hr=130,
           source="ring", spo2=98, device=DEVICE, sample_counter=None):
    status = None
    if sample_counter is not None:
        status = DeviceStatus(
            transport="stlink", sampling_status="live",
            sample_counter=sample_counter)
    reading = Telemetry(
        device_id=device, timestamp=when, source=source,
        heart_rate=hr, spo2=spo2, acc_x=0, acc_y=0, acc_z=1,
        signal_quality="good", reading_valid=True, device_status=status)
    storage.put_telemetry(reading, received_at=received)
    return engine.analyze(
        reading, now=received, received_at=received,
        monotonic=monotonic)


def active_events(storage):
    return storage.events(DEVICE, active_only=True)


def test_single_batch_cannot_satisfy_dwell(tmp_path):
    storage, engine, clock = make_engine(tmp_path)
    batch_time = clock.now()
    for index in range(3):
        result = sample(
            storage, engine,
            when=batch_time + timedelta(seconds=index),
            received=batch_time, monotonic=0)
        assert result["transitions"] == []
    assert not active_events(storage)

    # A later real-time tick can satisfy the monotonic window.  The sample
    # window was already satisfied by the batch itself.
    clock.advance(2)
    result = sample(
        storage, engine,
        when=batch_time + timedelta(seconds=3),
        received=clock.now(), monotonic=clock.monotonic())
    assert len(result["transitions"]) == 1
    assert active_events(storage)[0]["kind"] == "high_heart_rate"


def test_dwell_requires_both_server_and_sample_span(tmp_path):
    storage, engine, _clock = make_engine(tmp_path)
    base = NOW
    # Server span reaches 2 s, but sample span is only 0.2 s.
    for index in range(3):
        result = sample(
            storage, engine,
            when=base + timedelta(seconds=0.1 * index),
            received=base + timedelta(seconds=index),
            monotonic=float(index))
        assert result["transitions"] == []
    assert not active_events(storage)

    # The fourth sample makes both spans >= 2 s.
    result = sample(
        storage, engine,
        when=base + timedelta(seconds=2.1),
        received=base + timedelta(seconds=3),
        monotonic=3.0)
    assert len(result["transitions"]) == 1


def test_normal_realtime_samples_satisfy_dwell(tmp_path):
    storage, engine, clock = make_engine(tmp_path)
    for index in range(3):
        result = sample(
            storage, engine, when=clock.now(), received=clock.now(),
            monotonic=clock.monotonic())
        clock.advance(1.0)
        if index < 2:
            assert result["transitions"] == []
    assert len(result["transitions"]) == 1
    assert active_events(storage)


def test_delayed_backlog_batches_are_not_live_evidence(tmp_path):
    storage, engine, clock = make_engine(tmp_path)
    historical = clock.now() - timedelta(seconds=60)
    for index in range(3):
        result = sample(
            storage, engine,
            when=historical + timedelta(seconds=index),
            received=clock.now(), monotonic=clock.monotonic())
        assert result["evaluation"] == "delayed_sample_ignored"
        assert result["rule_evaluated"] is False
        assert result["continuity"]["delayed_sample"] is True
        clock.advance(1.0)
    assert not active_events(storage)


def test_device_clock_jump_does_not_open_immediately(tmp_path):
    storage, engine, clock = make_engine(tmp_path)
    for _ in range(2):
        sample(storage, engine, when=clock.now(), received=clock.now(),
               monotonic=clock.monotonic())
        clock.advance(1.0)
    assert not active_events(storage)

    jump_time = clock.now() + timedelta(seconds=20)
    result = sample(
        storage, engine, when=jump_time, received=clock.now(),
        monotonic=clock.monotonic())
    assert result["continuity"]["clock_jump"] is True
    assert result["transitions"] == []
    assert not active_events(storage)


def test_device_clock_rollback_is_ignored_without_event(tmp_path):
    storage, engine, clock = make_engine(tmp_path)
    for _ in range(2):
        sample(storage, engine, when=clock.now(), received=clock.now(),
               monotonic=clock.monotonic())
        clock.advance(1.0)

    result = sample(
        storage, engine,
        when=clock.now() - timedelta(seconds=5),
        received=clock.now(), monotonic=clock.monotonic())
    assert result["evaluation"] == "out_of_order_ignored"
    assert result["rule_evaluated"] is False
    assert result["continuity"]["out_of_order"] is True
    assert not active_events(storage)


def test_clock_forward_jump_then_normal_data_recovers(tmp_path):
    # Use a wide future tolerance so the jump packet itself is anchored; the
    # regression is about the normal samples that follow it.  A device sample
    # counter proves that the correction packet was produced after the jump.
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=1)
    clock.advance(1.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=2)
    clock.advance(1.0)

    jump_time = clock.now() + timedelta(seconds=20)
    result = sample(storage, engine, when=jump_time, received=clock.now(),
                    monotonic=clock.monotonic(), source="stm32",
                    sample_counter=3)
    assert result["continuity"]["clock_jump"] is True

    # The device returns to its normal timeline.  This fresh sample is far
    # behind the temporary anchor; counter 4 proves it follows the jump.
    clock.advance(1.0)
    result = sample(storage, engine, when=clock.now(), received=clock.now(),
                    monotonic=clock.monotonic(), source="stm32",
                    sample_counter=4)
    assert result["evaluation"] != "out_of_order_ignored"
    assert result["continuity"]["clock_rollback"] is True

    clock.advance(1.0)
    result = sample(storage, engine, when=clock.now(), received=clock.now(),
                    monotonic=clock.monotonic(), source="stm32",
                    sample_counter=5)
    assert result["evaluation"] != "out_of_order_ignored"
    assert result["continuity"]["out_of_order"] is False
    assert storage.monitoring(DEVICE, now=clock.now())["state"] == "NORMAL"


def test_late_data_without_clock_jump_is_rejected(tmp_path):
    storage, engine, clock = make_engine(tmp_path)
    # A device slightly ahead of server time is accepted (within 5 s).
    first_time = clock.now() + timedelta(seconds=4.9)
    first = sample(storage, engine, when=first_time, received=clock.now(),
                   monotonic=clock.monotonic())
    assert first["evaluation"] == "demo_experimental_rules"

    # Six seconds behind the first sample, but only 2.1 s behind server time:
    # without a recorded forward jump this is late data, not a rollback.
    second_time = first_time - timedelta(seconds=6)
    second = sample(
        storage, engine, when=second_time,
        received=second_time + timedelta(seconds=2.1), monotonic=1.0)
    assert second["evaluation"] == "out_of_order_ignored"
    assert second["continuity"]["clock_rollback"] is False


def test_zero_progress_rollback_is_rejected(tmp_path):
    # R1-01: normal T, jump T+6, then a pre-jump late packet at T+0.5.  The
    # jump packet and the late packet share the same server/monotonic tick.
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic())
    clock.advance(1.0)

    jump = sample(storage, engine,
                  when=clock.now() + timedelta(seconds=6),
                  received=clock.now(), monotonic=clock.monotonic())
    assert jump["continuity"]["clock_jump"] is True

    late = sample(storage, engine,
                  when=clock.now() - timedelta(seconds=0.5),
                  received=clock.now(), monotonic=clock.monotonic())
    assert late["evaluation"] == "out_of_order_ignored"
    assert late["continuity"]["clock_rollback"] is False
    assert late["continuity"]["clock_uncertain"] is True
    assert engine._clock_jump_state


def test_zero_progress_with_counter_is_still_rejected(tmp_path):
    # Even counter-proven ordering cannot re-anchor on an equal receipt tick.
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=1)
    clock.advance(1.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=2)
    clock.advance(1.0)

    jump = sample(storage, engine,
                  when=clock.now() + timedelta(seconds=20),
                  received=clock.now(), monotonic=clock.monotonic(),
                  source="stm32", sample_counter=3)
    assert jump["continuity"]["clock_jump"] is True

    candidate = sample(storage, engine, when=clock.now(),
                       received=clock.now(), monotonic=clock.monotonic(),
                       source="stm32", sample_counter=4)
    assert candidate["evaluation"] == "out_of_order_ignored"
    assert candidate["continuity"]["clock_rollback"] is False
    assert candidate["continuity"]["clock_uncertain"] is True


def test_advancing_server_does_not_validate_pre_jump_sample(tmp_path):
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=1)
    clock.advance(1.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=2)
    clock.advance(1.0)

    jump = sample(storage, engine,
                  when=clock.now() + timedelta(seconds=20),
                  received=clock.now(), monotonic=clock.monotonic(),
                  source="stm32", sample_counter=3)
    assert jump["continuity"]["clock_jump"] is True

    # Server and monotonic time advance, but the old counter proves this is a
    # late pre-jump sample, not a post-jump correction.
    late = sample(storage, engine,
                  when=clock.now() - timedelta(seconds=0.5),
                  received=clock.now() + timedelta(seconds=1),
                  monotonic=clock.monotonic() + 1,
                  source="stm32", sample_counter=2)
    assert late["evaluation"] == "out_of_order_ignored"
    assert late["continuity"]["clock_rollback"] is False
    assert late["continuity"]["clock_uncertain"] is True


def test_duplicate_after_clock_jump_does_not_reanchor(tmp_path):
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=1)
    clock.advance(1.0)
    pre_jump = clock.now()
    sample(storage, engine, when=pre_jump, received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=2)
    clock.advance(1.0)

    jump = sample(storage, engine,
                  when=clock.now() + timedelta(seconds=20),
                  received=clock.now(), monotonic=clock.monotonic(),
                  source="stm32", sample_counter=3)
    assert jump["continuity"]["clock_jump"] is True

    # A duplicate of the pre-jump sample is far behind the temporary anchor,
    # but its old counter cannot prove that it follows the jump.
    duplicate = sample(storage, engine, when=pre_jump,
                       received=clock.now() + timedelta(seconds=1),
                       monotonic=clock.monotonic() + 1,
                       source="stm32", sample_counter=2)
    assert duplicate["evaluation"] == "out_of_order_ignored"
    assert duplicate["continuity"]["clock_rollback"] is False
    assert duplicate["continuity"]["clock_uncertain"] is True

    # The recorded anomaly is still usable for the real correction.
    rollback = sample(storage, engine,
                      when=pre_jump + timedelta(seconds=1),
                      received=clock.now() + timedelta(seconds=1),
                      monotonic=clock.monotonic() + 1,
                      source="stm32", sample_counter=4)
    assert rollback["continuity"]["clock_rollback"] is True
    assert rollback["evaluation"] != "out_of_order_ignored"


def test_clock_jump_state_is_per_device(tmp_path):
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    device_a = "clock_device_a"
    device_b = "clock_device_b"

    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), device=device_a,
           source="stm32", sample_counter=1)
    clock.advance(1.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), device=device_a,
           source="stm32", sample_counter=2)
    clock.advance(1.0)
    jump = sample(storage, engine,
                  when=clock.now() + timedelta(seconds=20),
                  received=clock.now(), monotonic=clock.monotonic(),
                  device=device_a, source="stm32", sample_counter=3)
    assert jump["continuity"]["clock_jump"] is True

    # Device B never jumped.  Its slightly-ahead first sample followed by a
    # 6-second-late sample must remain out-of-order.
    first_b = clock.now() + timedelta(seconds=4.9)
    sample(storage, engine, when=first_b, received=clock.now(),
           monotonic=clock.monotonic(), device=device_b)
    second_b = sample(
        storage, engine, when=first_b - timedelta(seconds=6),
        received=clock.now() + timedelta(seconds=1),
        monotonic=clock.monotonic() + 1, device=device_b)
    assert second_b["evaluation"] == "out_of_order_ignored"
    assert second_b["continuity"]["clock_rollback"] is False

    # Device A's pending correction is unaffected by device B.
    rollback_a = sample(storage, engine, when=clock.now(),
                        received=clock.now() + timedelta(seconds=1),
                        monotonic=clock.monotonic() + 1,
                        device=device_a, source="stm32", sample_counter=4)
    assert rollback_a["continuity"]["clock_rollback"] is True


def test_clock_jump_can_recur_after_recovery(tmp_path):
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=1)
    clock.advance(1.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=2)
    clock.advance(1.0)

    first_jump = sample(storage, engine,
                        when=clock.now() + timedelta(seconds=20),
                        received=clock.now(), monotonic=clock.monotonic(),
                        source="stm32", sample_counter=3)
    assert first_jump["continuity"]["clock_jump"] is True
    clock.advance(1.0)
    first_rollback = sample(storage, engine, when=clock.now(),
                            received=clock.now(),
                            monotonic=clock.monotonic(),
                            source="stm32", sample_counter=4)
    assert first_rollback["continuity"]["clock_rollback"] is True

    clock.advance(1.0)
    normal = sample(storage, engine, when=clock.now(), received=clock.now(),
                    monotonic=clock.monotonic(), source="stm32",
                    sample_counter=5)
    assert normal["evaluation"] != "out_of_order_ignored"

    second_jump = sample(storage, engine,
                         when=clock.now() + timedelta(seconds=20),
                         received=clock.now(), monotonic=clock.monotonic(),
                         source="stm32", sample_counter=6)
    assert second_jump["continuity"]["clock_jump"] is True
    clock.advance(1.0)
    second_rollback = sample(storage, engine, when=clock.now(),
                             received=clock.now(),
                             monotonic=clock.monotonic(),
                             source="stm32", sample_counter=7)
    assert second_rollback["continuity"]["clock_rollback"] is True
    assert second_rollback["evaluation"] != "out_of_order_ignored"


def test_no_sequence_rollback_waits_for_catch_up(tmp_path):
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic())
    clock.advance(1.0)

    jump = sample(storage, engine,
                  when=clock.now() + timedelta(seconds=6),
                  received=clock.now(), monotonic=clock.monotonic())
    assert jump["continuity"]["clock_jump"] is True

    clock.advance(1.0)
    candidate = sample(storage, engine,
                       when=clock.now() - timedelta(seconds=1),
                       received=clock.now(), monotonic=clock.monotonic())
    assert candidate["evaluation"] == "out_of_order_ignored"
    assert candidate["continuity"]["clock_rollback"] is False
    assert candidate["continuity"]["clock_uncertain"] is True

    # Without a device counter we cannot prove ordering, so wait until the
    # device timeline passes the temporary anchor.  Until then, reject.
    result = None
    for _ in range(6):
        clock.advance(1.0)
        result = sample(storage, engine,
                        when=clock.now() - timedelta(seconds=1),
                        received=clock.now(), monotonic=clock.monotonic())
        assert result["evaluation"] == "out_of_order_ignored"

    clock.advance(1.0)
    result = sample(storage, engine,
                    when=clock.now() - timedelta(seconds=1),
                    received=clock.now(), monotonic=clock.monotonic())
    assert result["evaluation"] != "out_of_order_ignored"
    assert engine._clock_jump_state == {}


def test_reset_state_clears_clock_jump_state(tmp_path):
    storage, engine, clock = make_engine(tmp_path, future=30.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=1)
    clock.advance(1.0)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic(), source="stm32", sample_counter=2)
    clock.advance(1.0)
    jump = sample(storage, engine,
                  when=clock.now() + timedelta(seconds=20),
                  received=clock.now(), monotonic=clock.monotonic(),
                  source="stm32", sample_counter=3)
    assert jump["continuity"]["clock_jump"] is True
    assert engine._clock_jump_state

    engine.reset_state()
    assert engine._clock_jump_state == {}


def test_future_timestamp_does_not_pollute_monitoring(tmp_path):
    storage, engine, clock = make_engine(tmp_path)
    sample(storage, engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic())
    state = storage.monitoring(DEVICE, now=clock.now())
    assert state["state"] == "NORMAL"
    valid_at = state["last_valid_at"]
    assert valid_at is not None

    future_time = clock.now() + timedelta(seconds=20)
    result = sample(storage, engine, when=future_time, received=clock.now(),
                    monotonic=clock.monotonic())
    assert result["evaluation"] == "clock_skew_ignored"
    assert result["continuity"]["future_skew"] is True
    state = storage.monitoring(DEVICE, now=clock.now())
    assert state["state"] == "NORMAL"
    assert state["last_valid_at"] == valid_at
    assert state["last_seen_at"] == valid_at

    direct = monitoring.record(
        storage, DEVICE,
        {"device_id": DEVICE,
         "timestamp": future_time.isoformat(),
         "signal_quality": "good", "reading_valid": True},
        now=clock.now())
    assert direct["state"] == "NORMAL"
    assert direct["last_valid_at"] == valid_at

    # The rejected future packet must not become the ordering anchor.
    clock.advance(1.0)
    result = sample(storage, engine, when=clock.now(), received=clock.now(),
                    monotonic=clock.monotonic())
    assert result["evaluation"] != "out_of_order_ignored"

    # A new device with only a future sample stays NO_DATA, not NORMAL.
    direct = monitoring.record(
        storage, "future-direct",
        {"device_id": "future-direct",
         "timestamp": (clock.now() + timedelta(seconds=20)).isoformat(),
         "signal_quality": "good", "reading_valid": True},
        now=clock.now())
    assert direct["state"] == "NO_DATA"
    assert direct["last_valid_at"] is None
    assert direct["last_seen_at"] is None


def test_disabled_real_rules_keep_updating_monitoring(tmp_path):
    storage, engine, clock = make_engine(tmp_path, allow_real_rules=False)
    result = sample(storage, engine, when=clock.now(), received=clock.now(),
                    monotonic=clock.monotonic())
    assert result["evaluation"] == "real_data_rules_disabled"
    state = storage.monitoring(DEVICE, now=clock.now())
    assert state["state"] == "NORMAL"
    assert state["last_valid_at"] == clock.now().isoformat()
    assert not active_events(storage)


def test_restart_resets_unfinished_dwell_candidates(tmp_path):
    storage, first_engine, clock = make_engine(tmp_path)
    for _ in range(2):
        sample(storage, first_engine, when=clock.now(), received=clock.now(),
               monotonic=clock.monotonic())
        clock.advance(1.0)
    assert not active_events(storage)

    second_engine = DetectionEngine(
        storage, allow_real_rules=True,
        open_dwell_seconds=2.0, close_dwell_seconds=10.0,
        max_sample_gap_seconds=5.0, max_sample_lag_seconds=5.0,
        clock=clock)
    result = sample(storage, second_engine, when=clock.now(),
                    received=clock.now(), monotonic=clock.monotonic())
    assert result["transitions"] == []
    clock.advance(1.0)
    sample(storage, second_engine, when=clock.now(), received=clock.now(),
           monotonic=clock.monotonic())
    clock.advance(1.0)
    result = sample(storage, second_engine, when=clock.now(),
                    received=clock.now(), monotonic=clock.monotonic())
    assert len(result["transitions"]) == 1


def test_event_lifecycle_uses_server_utc_and_keeps_source_start(tmp_path):
    storage, engine, _clock = make_engine(tmp_path)
    base = NOW
    # Sample time and server receipt time are intentionally offset by 2 s.
    for index in range(3):
        sample(storage, engine,
               when=base - timedelta(seconds=3 - index),
               received=base - timedelta(seconds=3 - index),
               monotonic=float(index))
    event = active_events(storage)[0]
    assert event["opened_at"] == (base - timedelta(seconds=1)).isoformat()
    assert event["source_started_at"] == (base - timedelta(seconds=3)).isoformat()

    for index in range(11):
        when = base + timedelta(seconds=index)
        sample(storage, engine, when=when, received=when,
               monotonic=float(3 + index), hr=70)
    resolved = storage.get_event(event["id"])
    assert resolved["status"] == "resolved"
    assert resolved["closed_at"] == (base + timedelta(seconds=10)).isoformat()


def test_legacy_database_migration_preserves_data_and_marks_estimate(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL, timestamp TEXT NOT NULL,
            data_json TEXT NOT NULL)""")
        con.execute("""CREATE TABLE events (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL,
            opened_at TEXT NOT NULL, closed_at TEXT,
            acknowledged INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL)""")
        con.execute(
            "INSERT INTO telemetry(device_id,timestamp,data_json) VALUES(?,?,?)",
            ("legacy", NOW.isoformat(),
             '{"device_id":"legacy","timestamp":"%s","heart_rate":70}'
             % NOW.isoformat()))
        con.execute(
            "INSERT INTO events(id,device_id,kind,status,opened_at,note) "
            "VALUES(?,?,?,?,?,?)",
            ("ev_old", "legacy", "high_heart_rate", "active",
             NOW.isoformat(), "old"))

    storage = Storage(path)
    with sqlite3.connect(path) as con:
        telemetry_columns = {
            row[1] for row in con.execute("PRAGMA table_info(telemetry)")}
        event_columns = {
            row[1] for row in con.execute("PRAGMA table_info(events)")}
        assert {"received_at", "received_at_source"} <= telemetry_columns
        assert "source_started_at" in event_columns

    old = storage.latest("legacy", with_receipt=True)
    assert old["heart_rate"] == 70
    assert old["_received_at"] is None
    assert old["_received_at_source"] == "legacy_estimated"
    assert storage.get_event("ev_old")["note"] == "old"

    reading = Telemetry(
        device_id="legacy", timestamp=NOW, source="ring",
        heart_rate=71, spo2=98, acc_x=0, acc_y=0, acc_z=1,
        signal_quality="good", reading_valid=True)
    storage.put_telemetry(reading, received_at=NOW + timedelta(seconds=1))
    latest = storage.latest("legacy", with_receipt=True)
    assert latest["_received_at"] == (NOW + timedelta(seconds=1)).isoformat()
    assert latest["_received_at_source"] == "server"
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 2
