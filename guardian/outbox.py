"""Durable ingestion outbox.

An accepted telemetry row and its processing job are inserted in the same
SQLite transaction.  The job is the source of truth for downstream work:
Monitoring, rule evaluation, investigation creation, decision evaluation and
evidence supplementation each have their own stage row, so a process restart
can resume at the first stage that did not complete.

The module contains schema/constants and connection-level helpers only.  It has
no dependency on the web layer or on the detection engine.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

TELEMETRY_STAGES = (
    "monitoring",
    "rules",
    "investigations",
    "decisions",
    "supplement",
)
APK_BATCH_STAGES = ("adapt",)

JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"

STAGE_PENDING = "pending"
STAGE_PROCESSING = "processing"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"
FINAL_STAGE_STATES = (STAGE_COMPLETED, STAGE_SKIPPED)

DEFAULT_MAX_ATTEMPTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    telemetry_id INTEGER,
    batch_id TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT,
    last_error_at TEXT,
    next_attempt_at TEXT,
    transitions_json TEXT NOT NULL DEFAULT '[]',
    progress_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ingest_jobs_status
    ON ingest_jobs(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS ix_ingest_jobs_batch
    ON ingest_jobs(batch_id, id);
CREATE INDEX IF NOT EXISTS ix_ingest_jobs_telemetry
    ON ingest_jobs(telemetry_id);
CREATE TABLE IF NOT EXISTS ingest_job_stages (
    job_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    last_error TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (job_id, stage)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def _existing_job(con, job_key: str):
    row = con.execute(
        "SELECT id FROM ingest_jobs WHERE job_key=?", (job_key,)).fetchone()
    return row[0] if row else None


def insert_job(con, *, kind: str, device_id: str, payload: dict,
               stages: Iterable[str], telemetry_id: int | None = None,
               batch_id: str | None = None,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               now: str | None = None) -> int:
    """Insert a job and its stage rows using the caller's transaction.

    ``job_key`` is deterministic:
    * telemetry  -> ``telemetry:<telemetry row id>``
    * APK batch  -> ``apk_batch:<device_id>:<batch_id>``

    The APK key makes a retried batch resume the existing job rather than
    creating a second adaptation job.  The telemetry id is allocated by the
    same transaction as the telemetry insert, so the key cannot collide with a
    different accepted sample.
    """
    moment = now or utcnow()
    job_key = (f"telemetry:{telemetry_id}" if kind == "telemetry"
               else f"apk_batch:{device_id}:{batch_id}")
    existing = _existing_job(con, job_key)
    if existing is not None:
        return int(existing)
    cursor = con.execute(
        "INSERT INTO ingest_jobs(job_key,kind,device_id,telemetry_id,batch_id,"
        "payload_json,status,attempts,max_attempts,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (job_key, kind, device_id, telemetry_id, batch_id,
         json.dumps(payload, ensure_ascii=False, sort_keys=True),
         JOB_PENDING, 0, max_attempts, moment, moment))
    job_id = int(cursor.lastrowid)
    for stage in stages:
        con.execute(
            "INSERT INTO ingest_job_stages(job_id,stage,status,attempts,"
            "result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, stage, STAGE_PENDING, 0, "{}", moment, moment))
    return job_id


def stage_names(kind: str) -> tuple[str, ...]:
    if kind == "telemetry":
        return TELEMETRY_STAGES
    if kind == "apk_batch":
        return APK_BATCH_STAGES
    raise ValueError(f"unknown ingestion job kind: {kind}")
