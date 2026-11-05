"""Phone-facing `/v1` contract used by the existing RareGuard Android APK.

The APK is the client; this module makes this prototype speak its exact field
structure (UUIDs, snake_case, Bearer pairing, batch receipts) instead of only
reusing endpoint paths.  The Agent stays read-only.
"""
import base64
import binascii
import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from .agent import AgentCoordinator
from .clock import Clock, SystemClock
from .investigation import InvestigationService
from .apk_models import (AckConfig, AlertBody, ConsentBody, DeviceBody, Ingest,
                         ObservationBody, PairingExchange, PublishConfig,
                         QuestionBody, ReceiptBody)
from .apk_storage import ApkStorage, digest, utc_text
from .apk_summary import summarize, validate_window
from .detection import DetectionEngine
from .storage import Storage


# Consent is scoped to the credential that owns it. A wearer consents to
# uploading their own data; a caregiver consents to AI analysis and remote
# alerts for the family they monitor. Legacy credentials keep the old
# unrestricted behaviour until they are replaced by role-scoped pairings.
CONSENT_ROLE_POLICY = {
    "upload": ("wearer",),
    "ai_analysis": ("caregiver",),
    "remote_alerts": ("caregiver",),
}


def fail(code: str, message: str, status: int = 400):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def sample_json(row: dict) -> dict:
    evidence = json.loads(row["evidence_json"])
    return {
        "sample_id": row["sample_id"], "device_id": row["device_id"],
        "metric": row["metric"], "value": float(row["value"]), "unit": row["unit"],
        "observed_at": row["observed_at"], "phone_received_at": row["phone_received_at"],
        "stored_at": row["stored_at"], "source": row["source"],
        "quality": row["quality"], "time_basis": row["time_basis"], **evidence,
    }


def investigation_summary(row: dict | None) -> dict | None:
    if not row:
        return None
    return {key: row.get(key) for key in
            ("id", "event_id", "device_id", "kind", "status", "ai_status",
             "model", "created_at", "started_at", "completed_at", "error",
             "evidence_count", "attempts", "max_attempts")}


def event_summary(telemetry_store: Storage, event: dict) -> dict:
    investigation = telemetry_store.investigation_for_event(event["id"])
    return {**event, "investigation": investigation_summary(investigation)}


def receipt_with_processing(base: dict, state: dict) -> dict:
    """Add durable processing state without changing legacy receipt fields.

    ``status``/``accepted``/``duplicates``/``received_at`` keep their original
    meaning (raw batch committed).  The additive fields tell the client
    whether the Agent adaptation ran and whether any downstream task is still
    pending or failed.
    """
    receipt = dict(base)
    pending = int(state.get("pending") or 0)
    dead = int(state.get("dead") or 0)
    if dead:
        agent_status = "failed"
    elif pending or state.get("batch_job") is None:
        agent_status = "pending"
    else:
        agent_status = "completed"
    receipt.update({
        "raw_committed": True,
        "agent_processed": bool(state.get("agent_processed")),
        "agent_status": agent_status,
        "pending_tasks": pending,
        "agent_error": state.get("error"),
    })
    return receipt


class PairingAttempts:
    """Small in-process brute-force limit for pairing exchanges.

    Behind the Cloudflare tunnel the client address arrives as
    `cf-connecting-ip`; direct localhost access is trusted and uses its socket
    address.  This is deliberately process-local: it slows guessing, it is not
    a distributed rate limiter.
    """

    def __init__(self, limit: int = 5, window_seconds: int = 600):
        self.limit = limit
        self.window_seconds = window_seconds
        self.lock = threading.RLock()
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        items = [stamp for stamp in self.failures.get(key, [])
                 if now - stamp < self.window_seconds]
        if items:
            self.failures[key] = items
        else:
            self.failures.pop(key, None)
        return items

    def blocked(self, key: str) -> bool:
        with self.lock:
            return len(self._recent(key, time.monotonic())) >= self.limit

    def fail(self, key: str) -> None:
        with self.lock:
            now = time.monotonic()
            self.failures.setdefault(key, []).append(now)
            self._recent(key, now)

    def reset(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)


def client_key(request: Request) -> str:
    forwarded = (request.headers.get("cf-connecting-ip") or "").strip()
    if 0 < len(forwarded) <= 64:
        return forwarded
    return request.client.host if request.client else "unknown"


def create_apk_router(storage: ApkStorage, telemetry_store: Storage,
                      agent: AgentCoordinator, engine: DetectionEngine,
                      ingest_lock: threading.RLock,
                      investigations: InvestigationService,
                      decision_engine=None,
                      clock: Clock | None = None,
                      processor=None) -> APIRouter:
    router = APIRouter()
    runtime_clock = clock or SystemClock()
    pairing_attempts = PairingAttempts()

    def actor(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.startswith("Bearer "):
            fail("authentication_required", "A paired App credential is required", 401)
        credential = storage.credential_by_token(digest(authorization[7:]))
        if not credential:
            fail("invalid_credential", "Credential is invalid or revoked", 401)
        return credential

    def require_consent(credential: dict, scope: str) -> None:
        if not storage.consent_granted(credential["guardian_id"], scope):
            fail("consent_required", f"Explicit {scope} consent is required", 403)

    def require_role(credential: dict, *allowed: str) -> None:
        """Legacy credentials keep pre-role behavior; new credentials are scoped."""
        role = credential.get("role") or "legacy"
        if role == "legacy":
            return
        if role not in allowed:
            fail("role_forbidden",
                 "This credential role cannot perform this action", 403)

    def device_or_404(credential: dict, device_id: str) -> dict:
        device = storage.device(str(device_id), credential["family_id"])
        if not device:
            fail("device_not_found", "Device is not available to this family", 404)
        return device

    @router.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @router.post("/v1/pairings/exchange")
    def exchange(body: PairingExchange, request: Request):
        key = client_key(request)
        if pairing_attempts.blocked(key):
            fail("too_many_attempts", "Too many pairing attempts; try again later", 429)
        pairing = storage.claim_pairing_code(digest(body.code))
        if not pairing:
            pairing_attempts.fail(key)
            fail("invalid_pairing_code", "Pairing code is invalid, used or expired", 401)
        pairing_attempts.reset(key)
        token = secrets.token_urlsafe(48)
        credential = storage.create_credential(digest(token), pairing["name"],
                                               role=pairing.get("role") or "legacy")
        return {"token": token, "guardian_id": credential["guardian_id"],
                "family_id": credential["family_id"], "name": credential["name"],
                "role": credential["role"]}

    @router.get("/v1/me")
    def me(credential: dict = Depends(actor)):
        llm = agent.llm
        return {
            "guardian_id": credential["guardian_id"], "family_id": credential["family_id"],
            "name": credential["name"], "role": credential.get("role") or "legacy",
            "service": {
                "hermes_configured": bool(llm),
                "hermes_profile": llm.settings.model if llm else None,
                "hermes_web_url": None, "console_url": None,
            },
            "consents": storage.consents(credential["guardian_id"]),
            "devices": [{"id": row["id"], "model": row["model"],
                         "firmware_version": row["firmware"]}
                        for row in storage.devices(credential["family_id"])],
        }

    @router.post("/v1/credentials/revoke")
    def revoke(credential: dict = Depends(actor)):
        storage.revoke_credential(credential["id"])
        return {"status": "revoked"}

    @router.post("/v1/consents")
    def grant(body: ConsentBody, credential: dict = Depends(actor)):
        """Grant or revoke this credential's own consent.

        The scope determines which role may act: wearers own upload consent,
        caregivers own AI-analysis and remote-alert consent. Revocation uses
        the same route with granted=false and is subject to the same checks.
        """
        require_role(credential, *CONSENT_ROLE_POLICY[body.scope])
        storage.set_consent(credential["guardian_id"], body.scope, body.granted,
                            body.policy_version)
        return {"scope": body.scope, "granted": body.granted}

    @router.post("/v1/devices")
    def register(body: DeviceBody, credential: dict = Depends(actor)):
        require_role(credential, "wearer")
        require_consent(credential, "upload")
        device_id = storage.register_device(
            credential["family_id"], str(body.local_id), body.model,
            body.firmware_version, body.protocol_version,
        )
        return {"device_id": device_id}

    @router.post("/v1/measurement-batches")
    def ingest(body: Ingest, credential: dict = Depends(actor)):
        require_role(credential, "wearer")
        require_consent(credential, "upload")
        device = device_or_404(credential, body.device_id)
        batch_id = str(body.batch_id)
        canonical = body.model_dump(mode="json")
        content_hash = digest(canonical)
        accepted_measurements = []
        with storage.lock:
            existing = storage.batch(device["id"], batch_id)
            if existing:
                if existing["content_hash"] != content_hash:
                    fail("batch_content_conflict",
                         "Same batch ID has different content", 409)
            else:
                accepted, duplicates = [], 0
                seen: dict[str, str] = {}
                for measurement in body.samples:
                    data = measurement.model_dump(mode="json")
                    content = digest(data)
                    sample_id = str(measurement.sample_id)
                    old = storage.sample(device["id"], sample_id)
                    if old:
                        if old["content_hash"] != content:
                            fail("sample_content_conflict",
                                 "Same sample ID has different content; "
                                 "entire batch rejected", 409)
                        duplicates += 1
                        continue
                    if sample_id in seen:
                        if seen[sample_id] != content:
                            fail("sample_content_conflict",
                                 "Same sample ID has different content; "
                                 "entire batch rejected", 409)
                        duplicates += 1
                        continue
                    seen[sample_id] = content
                    accepted.append({
                        "sample_id": str(measurement.sample_id),
                        "content_hash": content,
                        "metric": measurement.metric,
                        "value": float(measurement.value),
                        "unit": measurement.unit,
                        "observed_at": measurement.observed_at.isoformat(),
                        "phone_received_at": measurement.phone_received_at.isoformat(),
                        "stored_at": datetime.now(timezone.utc).isoformat(),
                        "source": measurement.source,
                        "quality": measurement.quality,
                        "time_basis": measurement.time_basis,
                        "evidence_json": json.dumps({
                            "vendor_sequence": measurement.vendor_sequence,
                            "firmware_version": measurement.firmware_version,
                            "decoder_version": measurement.decoder_version,
                            "device_local_time": measurement.device_local_time,
                        }, ensure_ascii=False),
                    })
                    accepted_measurements.append(measurement)
                receipt = {
                    "batch_id": batch_id, "status": "committed",
                    "accepted": len(accepted), "duplicates": duplicates,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
                batch_job = None
                if processor is not None:
                    batch_now = runtime_clock.now()
                    batch_job = {
                        "payload": processor.apk_job_payload(
                            device["id"], batch_id, accepted_measurements,
                            received_at=batch_now,
                            monotonic=runtime_clock.monotonic()),
                        "max_attempts": processor.max_attempts,
                        "created_at": receipt["received_at"],
                    }
                try:
                    storage.ingest_batch(
                        device["id"], batch_id, content_hash, receipt,
                        accepted, job=batch_job)
                except sqlite3.IntegrityError:
                    # A concurrent retry won the insert race.  It has the same
                    # content hash, so continue to the shared processing job.
                    existing = storage.batch(device["id"], batch_id)
                    if (not existing
                            or existing["content_hash"] != content_hash):
                        raise

        # The raw batch is committed.  Adaptation is a separate durable job;
        # a retry of the same batch_id resumes it instead of returning only a
        # stale receipt.
        job = telemetry_store.job_for_batch(device["id"], batch_id)
        if job is None and processor is not None:
            batch_now = runtime_clock.now()
            payload = processor.apk_job_payload(
                device["id"], batch_id, body.samples, received_at=batch_now,
                monotonic=runtime_clock.monotonic())
            stored = storage.batch(device["id"], batch_id)
            storage.ensure_batch_job(
                device["id"], batch_id, payload,
                max_attempts=processor.max_attempts,
                created_at=stored["created_at"] if stored else None)
            job = telemetry_store.job_for_batch(device["id"], batch_id)
        if processor is not None:
            with ingest_lock:
                # The batch adaptation job comes first (lowest id); a failed
                # child telemetry job is retried here even when adaptation
                # itself already completed.  Completed stages are skipped by
                # the processor, so a retry is safe.
                for item in telemetry_store.jobs_for_batch(
                        batch_id, device_id=device["id"]):
                    if (item["status"] != "completed"
                            and item["attempts"] < item["max_attempts"]):
                        processor.process_job(item["id"])

        stored = storage.batch(device["id"], batch_id)
        base = json.loads(stored["receipt_json"])
        if processor is None:
            return base
        state = telemetry_store.batch_processing_state(
            device["id"], batch_id)
        return receipt_with_processing(base, state)

    @router.get("/v1/measurements")
    def measurements(device_id: str, start: datetime = Query(alias="from"),
                     end: datetime = Query(alias="to"), metric: str | None = None,
                     cursor: str | None = None,
                     limit: int = Query(default=200, ge=1, le=500),
                     credential: dict = Depends(actor)):
        device = device_or_404(credential, device_id)
        try:
            validate_window(start, end, "UTC")
        except ValueError as error:
            fail("invalid_window", str(error), 422)
        after = ("", "")
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError()
                decoded = json.loads(base64.b64decode(
                    cursor.encode(), altchars=b"-_", validate=True).decode())
                if (not isinstance(decoded, list) or len(decoded) != 2
                        or not all(isinstance(value, str) for value in decoded)):
                    raise ValueError()
                observed = datetime.fromisoformat(decoded[0])
                if observed.tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError, binascii.Error):
                fail("invalid_cursor", "Invalid page cursor", 422)
            after = (utc_text(observed), decoded[1])
        rows = storage.samples_after(device["id"], start, end, after, limit + 1, metric)
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                [last["observed_at"], last["sample_id"]]).encode()).decode()
        return {"status": "data_available" if rows else "no_data",
                "data": [sample_json(row) for row in rows], "next_cursor": next_cursor}

    @router.get("/v1/summaries/night")
    def night(device_id: str, start: datetime = Query(alias="from"),
              end: datetime = Query(alias="to"),
              timezone_name: str = Query(default="UTC", alias="timezone"),
              credential: dict = Depends(actor)):
        device = device_or_404(credential, device_id)
        try:
            validate_window(start, end, timezone_name)
        except ValueError as error:
            fail("invalid_window", str(error), 422)
        return summarize(storage.samples_in(device["id"], start, end),
                         start, end, timezone_name)

    @router.post("/v1/observations")
    def observe(body: ObservationBody, credential: dict = Depends(actor)):
        require_role(credential, "wearer")
        require_consent(credential, "upload")
        device = device_or_404(credential, body.device_id)
        previous = storage.observation(str(body.id))
        if previous:
            same_time = datetime.fromisoformat(previous["occurred_at"]) == body.occurred_at
            if (previous["device_id"] != device["id"]
                    or previous["guardian_id"] != credential["guardian_id"]
                    or previous["text"] != body.text or not same_time):
                fail("observation_conflict", "Observation ID already has different content", 409)
        else:
            storage.put_observation(str(body.id), device["id"],
                                    credential["guardian_id"],
                                    body.occurred_at.isoformat(), body.text)
        return {"id": str(body.id), "status": "committed"}

    @router.get("/v1/observations")
    def observations(device_id: str, credential: dict = Depends(actor)):
        device = device_or_404(credential, device_id)
        return {"data": [{"id": row["id"], "text": row["text"],
                          "occurred_at": row["occurred_at"],
                          "recorded_at": row["recorded_at"],
                          "guardian_id": row["guardian_id"]}
                         for row in storage.observations(device["id"])]}

    @router.post("/v1/alerts")
    def alert(body: AlertBody, credential: dict = Depends(actor)):
        require_role(credential, "wearer")
        require_consent(credential, "remote_alerts")
        device = device_or_404(credential, body.device_id)
        content_hash = digest(body.model_dump(mode="json"))
        previous = storage.alert(str(body.id))
        if previous:
            if (previous["device_id"] != device["id"]
                    or previous["content_hash"] != content_hash):
                fail("alert_conflict", "Alert ID content conflict", 409)
        else:
            guardians = [row["guardian_id"] for row in
                         storage.family_credentials(credential["family_id"])
                         if storage.consent_granted(row["guardian_id"], "remote_alerts")]
            storage.put_alert(str(body.id), device["id"], credential["guardian_id"],
                              body.kind, body.text, body.evidence, content_hash, guardians)
        return {"id": str(body.id), "status": "stored",
                "delivery_status": "awaiting_device_receipts"}

    @router.get("/v1/alerts")
    def alerts(credential: dict = Depends(actor)):
        require_consent(credential, "remote_alerts")
        return {"data": [{
            "id": row["id"], "device_id": row["device_id"], "kind": row["kind"],
            "text": row["text"], "created_at": row["created_at"],
            "evidence": json.loads(row["evidence_json"]),
            "received_at": row["received_at"], "confirmed_at": row["confirmed_at"],
        } for row in storage.alerts_for(credential["family_id"],
                                        credential["guardian_id"])]}

    @router.post("/v1/alerts/{alert_id}/receipt")
    def alert_receipt(alert_id: str, body: ReceiptBody, credential: dict = Depends(actor)):
        require_consent(credential, "remote_alerts")
        alert = storage.alert(alert_id)
        if not alert:
            fail("alert_not_found", "Alert unavailable", 404)
        device_or_404(credential, alert["device_id"])
        if not storage.receipt(alert_id, credential["guardian_id"], body.status, body.note):
            fail("delivery_not_found", "No delivery assigned to this guardian", 404)
        return {"status": body.status}

    @router.get("/v1/alerts/{alert_id}/deliveries")
    def deliveries(alert_id: str, credential: dict = Depends(actor)):
        require_consent(credential, "remote_alerts")
        alert = storage.alert(alert_id)
        if not alert:
            fail("alert_not_found", "Alert unavailable", 404)
        device_or_404(credential, alert["device_id"])
        return {"data": [{"guardian_id": row["guardian_id"],
                          "received_at": row["received_at"],
                          "confirmed_at": row["confirmed_at"], "note": row["note"]}
                         for row in storage.deliveries(alert_id)]}

    @router.post("/v1/conversations")
    def conversation(credential: dict = Depends(actor)):
        require_consent(credential, "ai_analysis")
        conversation_id = storage.create_conversation(
            credential["family_id"], credential["guardian_id"])
        return {"id": conversation_id}

    @router.post("/v1/conversations/{conversation_id}/messages", status_code=202)
    def question(conversation_id: str, body: QuestionBody,
                 credential: dict = Depends(actor)):
        require_consent(credential, "ai_analysis")
        device = device_or_404(credential, body.device_id)
        conversation = storage.conversation(conversation_id)
        if (not conversation or conversation["guardian_id"] != credential["guardian_id"]
                or conversation["family_id"] != credential["family_id"]):
            fail("conversation_not_found", "Conversation unavailable", 404)
        try:
            context = summarize(storage.samples_in(device["id"], body.from_, body.to),
                                body.from_, body.to, body.timezone)
        except ValueError as error:
            fail("invalid_window", str(error), 422)
        context["observations"] = [
            {"text": row["text"], "occurred_at": row["occurred_at"]}
            for row in storage.observations(device["id"])
            if body.from_ <= datetime.fromisoformat(row["occurred_at"]) < body.to
        ]
        status, answer, error = "completed", None, None
        try:
            result = agent.answer(body.question, device["id"])
            answer = result.get("response")
            error = result.get("llm_error")
        except Exception:
            status, error = "failed", "agent_failed"
        request_id = storage.create_ai_request(
            conversation_id, device["id"], credential["guardian_id"], body.question,
            context, digest(context), status, answer, error)
        return {"request_id": request_id, "status": status}

    @router.get("/v1/ai-requests/{request_id}")
    def ai_result(request_id: str, credential: dict = Depends(actor)):
        require_consent(credential, "ai_analysis")
        request = storage.ai_request(request_id)
        if not request or request["guardian_id"] != credential["guardian_id"]:
            fail("request_not_found", "Request unavailable", 404)
        device_or_404(credential, request["device_id"])
        return {"request_id": request["id"], "status": request["status"],
                "answer": request["answer"], "error": request["error"],
                "context_hash": request["context_hash"],
                "context": json.loads(request["context_json"]),
                "created_at": request["created_at"]}

    @router.get("/v1/devices/{device_id}/config")
    def get_config(device_id: str, credential: dict = Depends(actor)):
        device = device_or_404(credential, device_id)
        row = storage.latest_config(device["id"])
        return {"version": row["version"] if row else 0,
                "parameters": json.loads(row["parameters_json"]) if row else None,
                "status": "published" if row else "not_configured"}

    @router.post("/v1/devices/{device_id}/config")
    def publish_config(device_id: str, body: PublishConfig,
                       credential: dict = Depends(actor)):
        require_role(credential, "caregiver")
        device = device_or_404(credential, device_id)
        version = storage.publish_config(device["id"], credential["guardian_id"],
                                         body.base_version,
                                         body.parameters.model_dump(mode="json"))
        if version is None:
            fail("config_version_conflict",
                 "Configuration changed; review the latest version", 409)
        return {"version": version, "status": "published", "applied": False}

    @router.post("/v1/devices/{device_id}/config-acks")
    def ack_config(device_id: str, body: AckConfig, credential: dict = Depends(actor)):
        require_role(credential, "wearer")
        device = device_or_404(credential, device_id)
        if not storage.ack_config(device["id"], credential["guardian_id"], body.version,
                                  body.status, body.reason):
            fail("config_not_found", "Published configuration unavailable", 404)
        return {"status": "recorded"}

    @router.get("/v1/export")
    def export(device_id: str, credential: dict = Depends(actor)):
        device = device_or_404(credential, device_id)
        return {"device_id": device["id"],
                "samples": [sample_json(row) for row in storage.all_samples(device["id"])]}

    @router.delete("/v1/devices/{device_id}/data")
    def delete_data(device_id: str, confirmation: str | None = None,
                    credential: dict = Depends(actor)):
        device = device_or_404(credential, device_id)
        if confirmation != "delete_health_data":
            fail("confirmation_required", "Explicit deletion confirmation required", 422)
        storage.delete_device_data(device["id"])
        # The Agent's read-only tables mirror ring hr/spo2; deletion must reach them too.
        telemetry_store.delete_device_data(device["id"])
        return {"status": "deleted"}

    # -- caregiver read-only monitoring / human acknowledgement -----------
    @router.get("/v1/subjects")
    def subjects(credential: dict = Depends(actor)):
        """Authorized monitoring subjects for a caregiver credential."""
        require_role(credential, "caregiver")
        data = []
        for device in storage.devices(credential["family_id"]):
            status = agent.status(device["id"])
            events = telemetry_store.events(device["id"], limit=50)
            latest_investigation = telemetry_store.recent_investigations(
                limit=1, device_id=device["id"])
            data.append({
                "device_id": device["id"], "model": device["model"],
                "firmware_version": device["firmware"],
                "latest": status.get("latest"),
                "data_mode": status.get("data_mode"),
                "stale": status.get("stale"),
                "age_seconds": status.get("age_seconds"),
                "model_output": status.get("model_output"),
                "active_event_count": sum(1 for event in events
                                          if event["status"] == "active"),
                "unacknowledged_event_count": sum(
                    1 for event in events
                    if event["status"] == "active" and not event["acknowledged"]),
                "latest_investigation": investigation_summary(
                    latest_investigation[0] if latest_investigation else None),
            })
        return {"data": data}

    @router.get("/v1/caregiver/events")
    def caregiver_events(
            device_id: str | None = Query(default=None, min_length=1, max_length=80,
                                          pattern=r"^[a-zA-Z0-9_-]+$"),
            limit: int = Query(default=50, ge=1, le=200),
            credential: dict = Depends(actor)):
        require_role(credential, "caregiver")
        if device_id is not None:
            devices = [device_or_404(credential, device_id)]
        else:
            devices = storage.devices(credential["family_id"])
        events = []
        for device in devices:
            events.extend(telemetry_store.recent_events(
                limit=limit, device_id=device["id"]))
        events.sort(key=lambda event: event["opened_at"], reverse=True)
        return {"data": [event_summary(telemetry_store, event)
                         for event in events[:limit]]}

    @router.get("/v1/caregiver/events/{event_id}")
    def caregiver_event_detail(event_id: str, credential: dict = Depends(actor)):
        require_role(credential, "caregiver")
        event = telemetry_store.get_event(event_id)
        if not event:
            fail("event_not_found", "Event unavailable", 404)
        device = device_or_404(credential, event["device_id"])
        investigation = telemetry_store.investigation_for_event(event_id)
        return {
            "event": event,
            "device": device,
            "investigation": investigation,
            "evidence": telemetry_store.evidence_for_event(event_id),
            "transitions": (telemetry_store.investigation_transitions(investigation["id"])
                            if investigation else []),
        }

    @router.post("/v1/caregiver/events/{event_id}/ack")
    def caregiver_ack(event_id: str, credential: dict = Depends(actor)):
        """Human acknowledgement; the client cannot mark an AI investigation complete."""
        require_role(credential, "caregiver")
        event = telemetry_store.get_event(event_id)
        if not event:
            fail("event_not_found", "Event unavailable", 404)
        device_or_404(credential, event["device_id"])
        result = telemetry_store.acknowledge(
            event_id, actor=credential.get("name") or "caregiver")
        if result is None:
            fail("event_not_found", "Event unavailable", 404)
        return result

    return router
