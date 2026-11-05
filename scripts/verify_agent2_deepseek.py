#!/usr/bin/env python
"""One real DeepSeek investigation using synthetic Agent 2.0 evidence.

Privacy: no real user or PPG-DaLiA data is sent.  The fixture is generated in
this script and marked synthetic/replay.  The redacted record contains no API
key and no prompt bodies.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.agent import AgentCoordinator  # noqa: E402
from guardian.investigation import InvestigationService  # noqa: E402
from guardian.llm import DeepSeekClient, LLMSettings  # noqa: E402
from guardian.storage import Storage  # noqa: E402
from guardian.synthetic import build  # noqa: E402


def write_record(record: dict) -> Path:
    path = ROOT / "docs" / "agent2-deepseek.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def main() -> int:
    settings = LLMSettings.from_env()
    if not settings.enabled:
        path = write_record({
            "status": "skipped",
            "reason": "LLM not configured (GUARDIAN_LLM_MODE/DEEPSEEK_API_KEY)",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"SKIPPED: {path}")
        return 1

    runtime = ROOT / ".runtime" / "agent2"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / "deepseek.db"
    if database.exists():
        database.unlink()
    storage = Storage(str(database))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    transition = storage.event_open(
        "guardian_001", "high_heart_rate", now,
        "SYNTHETIC fixture for Agent 2.0 verification; not a real person")
    event = storage.get_event(transition["event_id"])
    scenario = build("hr_with_motion", event)
    for reading in scenario.telemetry:
        storage.put_telemetry(reading)
    investigation, _ = storage.create_investigation(
        event["id"], event["device_id"], event["kind"])

    client = DeepSeekClient(settings)
    service = InvestigationService(
        storage, AgentCoordinator(storage, client), client,
        max_attempts=1, max_workers=1,
        max_llm_tokens=30000,
        deadline_seconds=40, total_deadline_seconds=60)
    started = time.monotonic()
    deadline = started + 90
    current = None
    while time.monotonic() < deadline:
        current = storage.investigation(investigation["id"])
        if current and current.get("completed_at"):
            break
        time.sleep(0.2)
    duration = round(time.monotonic() - started, 2)
    current = storage.investigation(investigation["id"]) or {}
    report = current.get("report") or {}
    evidence = storage.evidence_for_event(event["id"])
    record = {
        "run_id": "agent2-deepseek-" + now.strftime("%Y%m%d%H%M%S"),
        "status": current.get("status"),
        "ai_status": current.get("ai_status"),
        "ai_error": current.get("error") or (report.get("ai_error") if report else None),
        "model": current.get("model"),
        "duration_seconds": duration,
        "synthetic_fixture": True,
        "event": {"id": event["id"], "kind": event["kind"],
                  "device_id": event["device_id"]},
        "evidence_count": len(evidence),
        "evidence_refs": [item["evidence_id"] for item in evidence],
        "structured": current.get("structured"),
        "usage": current.get("usage"),
        "transitions": [
            {"from": item["from_status"], "to": item["to_status"],
             "reason": item["reason"]}
            for item in storage.investigation_transitions(investigation["id"])
        ],
        "fallback_used": current.get("ai_status") != "completed",
        "notes": [
            "Synthetic waveform and synthetic telemetry only; no real person or dataset signal was sent.",
            "Research prototype; not a diagnosis, seizure detector or emergency service.",
            "The model could only reference evidence ids supplied by the server.",
        ],
    }
    path = write_record(record)
    print(json.dumps({
        "record": str(path), "status": record["status"],
        "ai_status": record["ai_status"], "duration_seconds": duration,
        "evidence_count": record["evidence_count"],
        "structured_status": (record.get("structured") or {}).get("investigation_status"),
    }, ensure_ascii=False, indent=2))
    return 0 if current.get("ai_status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
