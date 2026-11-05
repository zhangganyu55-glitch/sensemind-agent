"""End-to-end checks against real uvicorn processes over loopback HTTP.

This exercises the same ASGI apps Cloudflare forwards to: the local app on one
port and the public-only app on another. TLS termination by Cloudflare and a
physical phone are NOT covered here; those need a manual device test.
"""
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from guardian.apk_storage import ApkStorage


ROOT = Path(__file__).resolve().parent.parent
PAIRING_CODE = "e2e-pairing-code-123"

# The host has a system-wide proxy configured; never send loopback traffic to it.
HTTP = httpx.Client(trust_env=False, timeout=10)


def free_ports(count: int = 2) -> list[int]:
    """Reserve several distinct ports at once; closing then reopening can repeat."""
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def wait_health(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if HTTP.get(url + "/healthz", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"server at {url} did not become healthy")


class Servers:
    def __init__(self, database: str, log_dir: Path):
        self.database = database
        self.log_dir = log_dir
        self.env = {
            **os.environ,
            "GUARDIAN_DB": database,
            "GUARDIAN_LLM_MODE": "off",
            "DEEPSEEK_API_KEY": "",
            "GUARDIAN_APK_PAIRING_CODE": PAIRING_CODE,
            "GUARDIAN_APK_PAIRING_TTL_MINUTES": "10",
            "GUARDIAN_EXPERIMENTAL_REAL_RULES": "true",
        }
        self.local_port, self.public_port = free_ports()
        # The public app is a controlled proxy to the local authority.
        self.env["GUARDIAN_LOCAL_URL"] = f"http://127.0.0.1:{self.local_port}"
        self.local = None
        self.public = None

    def _spawn(self, module: str, port: int, name: str):
        log = open(self.log_dir / f"{name}.log", "ab")
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=self.env, stdout=log, stderr=subprocess.STDOUT,
        )

    def start(self):
        self.local = self._spawn("guardian.main:app", self.local_port, "local")
        self.public = self._spawn("guardian.public:app", self.public_port, "public")
        wait_health(f"http://127.0.0.1:{self.local_port}")
        wait_health(f"http://127.0.0.1:{self.public_port}")

    def stop(self):
        for process in (self.local, self.public):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self.local = self.public = None

    @property
    def local_url(self):
        return f"http://127.0.0.1:{self.local_port}"

    @property
    def public_url(self):
        return f"http://127.0.0.1:{self.public_port}"


def test_apk_contract_end_to_end_with_restart_and_retry(tmp_path):
    database = str(tmp_path / "guardian.db")
    servers = Servers(database, tmp_path)
    servers.start()
    public, local = servers.public_url, servers.local_url
    try:
        # The public surface must not include legacy or debugging routes.
        assert HTTP.get(public + "/healthz").status_code == 200
        for path in ("/health", "/docs", "/openapi.json", "/redoc",
                     "/v1/assistant", "/v1/telemetry", "/v1/live/status", "/v1/tools"):
            assert HTTP.get(public + path).status_code == 404, path

        # Pair through the public app, then prove the code is single-use.
        paired = HTTP.post(public + "/v1/pairings/exchange",
                           json={"code": PAIRING_CODE})
        assert paired.status_code == 200, paired.text
        token = paired.json()["token"]
        headers = {"Authorization": "Bearer " + token}
        assert HTTP.post(public + "/v1/pairings/exchange",
                         json={"code": PAIRING_CODE}).status_code == 401

        for scope in ("upload", "ai_analysis", "remote_alerts"):
            response = HTTP.post(public + "/v1/consents", headers=headers, json={
                "scope": scope, "granted": True, "policy_version": "demo-1"})
            assert response.status_code == 200, response.text
        assert HTTP.get(public + "/v1/me", headers=headers).json()["consents"] == {
            "upload": True, "ai_analysis": True, "remote_alerts": True}

        device = HTTP.post(public + "/v1/devices", headers=headers, json={
            "local_id": str(uuid4()), "model": "MR11 e2e",
            "firmware_version": "1.0.0", "protocol_version": "1"}).json()["device_id"]

        base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=2)
        # One batch shares one server receipt tick.  Send three live batches
        # with real elapsed time so the monotonic open dwell can be satisfied.
        for index in range(3):
            sample = {
                "sample_id": str(uuid4()), "metric": "hr",
                "value": 130.0 + index, "unit": "bpm",
                "observed_at": iso(base + timedelta(seconds=index)),
                "phone_received_at": iso(base + timedelta(seconds=index + 1)),
                "time_basis": "device", "quality": "good",
                "source": "live_device",
            }
            batch = {"schema_version": 1, "batch_id": str(uuid4()),
                     "device_id": device, "app_version": "e2e-1.0",
                     "samples": [sample]}
            receipt = HTTP.post(public + "/v1/measurement-batches",
                                headers=headers, json=batch)
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["accepted"] == 1
            if index < 2:
                time.sleep(1.1)

        # Not trusting HTTP 200: verify persistence, Agent view and events.
        with sqlite3.connect(database) as con:
            assert con.execute("SELECT COUNT(*) FROM apk_samples").fetchone()[0] == 3
            assert con.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 3
            assert con.execute(
                "SELECT COUNT(*) FROM apk_credentials WHERE revoked=0").fetchone()[0] == 1

        latest = HTTP.get(local + "/v1/live/status",
                           params={"device_id": device}).json()["latest"]
        assert latest["source"] == "ring"
        assert latest["heart_rate"] == 132.0
        assert latest["reading_valid"] is True

        answer = HTTP.post(local + "/v1/assistant", json={
            "device_id": device, "question": "当前心率是多少？"}).json()
        assert answer["agent_mode"] == "deterministic_tool_router_no_llm"
        assert answer["data"]["latest"]["heart_rate"] == 132.0

        events = HTTP.get(local + "/v1/events",
                           params={"device_id": device}).json()
        assert any(event["kind"] == "high_heart_rate" and event["status"] == "active"
                   for event in events)

        # The public app's detector triggers the same autonomous investigation.
        event = next(item for item in events if item["kind"] == "high_heart_rate")
        deadline = time.monotonic() + 6
        investigation = None
        while time.monotonic() < deadline:
            detail = HTTP.get(local + "/v1/console/events/" + event["id"]).json()
            investigation = detail.get("investigation")
            # The worker marks status completed before the report write is
            # visible to a concurrent reader; wait for both in this E2E poll.
            if (investigation and investigation["status"] == "completed"
                    and investigation.get("report")):
                break
            time.sleep(0.05)
        assert investigation and investigation["status"] == "completed"
        assert investigation["report"]["tool_calls"]
        assert investigation["report"]["device_id"] == device
        assert HTTP.get(local + "/console").status_code == 200
        assert HTTP.get(public + "/console").status_code == 404
        assert HTTP.get(public + "/v1/console/snapshot").status_code == 404

        window = {"device_id": device, "timezone": "UTC",
                  "from": iso(base - timedelta(minutes=1)),
                  "to": iso(base + timedelta(minutes=5))}
        summary = HTTP.get(public + "/v1/summaries/night",
                            params=window, headers=headers).json()
        assert summary["status"] == "data_available"
        assert summary["metrics"]["hr"]["count"] == 3
        exported = HTTP.get(public + "/v1/export",
                             params={"device_id": device}, headers=headers).json()
        assert len(exported["samples"]) == 3

        # A retry with the same batch ID returns the original receipt.
        again = HTTP.post(public + "/v1/measurement-batches",
                           headers=headers, json=batch)
        assert again.json() == receipt.json()

        # Simulate a network drop, restart both servers, then retry the upload.
        servers.stop()
        with pytest.raises(httpx.HTTPError):
            HTTP.get(public + "/healthz", timeout=1)
        servers.start()
        resumed = HTTP.post(public + "/v1/measurement-batches",
                             headers=headers, json=batch)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json() == receipt.json()
        with sqlite3.connect(database) as con:
            assert con.execute("SELECT COUNT(*) FROM apk_samples").fetchone()[0] == 3
        latest = HTTP.get(local + "/v1/live/status",
                           params={"device_id": device}).json()["latest"]
        assert latest["heart_rate"] == 132.0
        events = HTTP.get(local + "/v1/events",
                           params={"device_id": device}).json()
        assert any(event["kind"] == "high_heart_rate" for event in events)

        # Invalid and revoked credentials are rejected.
        assert HTTP.get(public + "/v1/me",
                         headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert HTTP.post(public + "/v1/credentials/revoke",
                          headers=headers).status_code == 200
        assert HTTP.get(public + "/v1/me", headers=headers).status_code == 401

        # A fresh single-use code still allows a new family device to pair.
        new_code = ApkStorage(database).issue_pairing_code("重启后用户")["code"]
        assert HTTP.post(public + "/v1/pairings/exchange",
                          json={"code": new_code}).status_code == 200
    finally:
        servers.stop()
