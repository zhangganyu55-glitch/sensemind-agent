"""Application factory. Local app: `uvicorn guardian.main:app`."""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from threading import RLock

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import AgentCoordinator, TOOLS
from .apk_api import create_apk_router
from .apk_storage import ApkStorage
from .clock import Clock, SystemClock
from .console import (CONSOLE_HTML, acknowledge as console_acknowledge,
                      console_token_ok, event_detail, snapshot)
from .decision import POLICY_VERSION as DECISION_POLICY_VERSION, DecisionEngine
from .detection import DetectionEngine
from .investigation import InvestigationService
from .llm import DeepSeekClient, LLMSettings
from .ml.models import AnalyzeRequest
from .ml.registry import SensorModels
from .models import Telemetry, AskRequest, ToolCall
from .processing import BOOT_ID, IngestionProcessor
from .storage import Storage


class AckBody(BaseModel):
    confirmed_by: str = Field(default="local_operator", min_length=1, max_length=80)


def _runtime_setting(name: str, default: str, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


def _runtime_float(name: str, default: str, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError:
        raise RuntimeError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


def create_app(db_path: str | None = None, allow_real_rules: bool | None = None,
               llm_settings: LLMSettings | None = None,
               llm_transport: httpx.BaseTransport | None = None,
               apk_pairing_code: str | None = None,
               public_only: bool = False,
               investigation_max_tool_calls: int | None = None,
               investigation_deadline_seconds: int | None = None,
               notification_ttl_seconds: int | None = None,
               notification_cooldown_seconds: int | None = None,
               investigation_max_attempts: int | None = None,
               investigation_max_workers: int | None = None,
               investigation_max_llm_tokens: int | None = None,
               investigation_total_deadline_seconds: int | None = None,
               investigation_retry_backoff_seconds: float | None = None,
               investigation_duplicate_window_seconds: int | None = None,
               decision_engine_enabled: bool | None = None,
               decision_real_sustained: bool | None = None,
               decision_background: bool | None = None,
               rule_open_dwell_seconds: float | None = None,
               rule_close_dwell_seconds: float | None = None,
               rule_max_sample_gap_seconds: float | None = None,
               rule_max_sample_lag_seconds: float | None = None,
               processing_recovery: bool | None = None,
               processing_max_attempts: int | None = None,
               clock: Clock | None = None):
    """Build the app.

    `public_only=True` registers *only* the authenticated APK `/v1` contract
    and `/healthz`; docs and every legacy/local route are absent.  The local
    app keeps the STM32/Agent/WebSocket routes on 127.0.0.1.

    This factory path is retained for contract tests and backward
    compatibility.  Do not run it as a second writer in production: the
    production public entry is ``guardian.public:app``, a controlled proxy to
    the single authoritative local app.
    """
    settings = llm_settings if llm_settings is not None else LLMSettings.from_env()
    database = db_path or os.getenv("GUARDIAN_DB", "guardian.db")
    enable_real = (os.getenv("GUARDIAN_EXPERIMENTAL_REAL_RULES", "false").lower() == "true") if allow_real_rules is None else allow_real_rules
    credential_days = _runtime_setting("GUARDIAN_APK_CREDENTIAL_DAYS", "30", 1, 365)
    storage = Storage(database)
    runtime_clock = clock or SystemClock()
    # Idempotent legacy-data quarantine: future-at-ingestion rows are moved to
    # an audit table so they cannot become live anchors or drive decisions.
    storage.quarantine_invalid_future_telemetry(now=runtime_clock.now())
    open_dwell = (rule_open_dwell_seconds
                  if rule_open_dwell_seconds is not None
                  else _runtime_float("GUARDIAN_RULE_OPEN_DWELL_SECONDS", "2", 0, 30))
    close_dwell = (rule_close_dwell_seconds
                   if rule_close_dwell_seconds is not None
                   else _runtime_float("GUARDIAN_RULE_CLOSE_DWELL_SECONDS", "10", 1, 120))
    sample_gap = (rule_max_sample_gap_seconds
                  if rule_max_sample_gap_seconds is not None
                  else _runtime_float("GUARDIAN_RULE_MAX_SAMPLE_GAP_SECONDS", "5", 1, 60))
    sample_lag = (rule_max_sample_lag_seconds
                  if rule_max_sample_lag_seconds is not None
                  else _runtime_float("GUARDIAN_RULE_MAX_SAMPLE_LAG_SECONDS", "5", 0, 300))
    engine = DetectionEngine(
        storage, allow_real_rules=enable_real,
        open_dwell_seconds=open_dwell,
        close_dwell_seconds=close_dwell,
        max_sample_gap_seconds=sample_gap,
        max_sample_lag_seconds=sample_lag,
        clock=runtime_clock)
    agent = AgentCoordinator(
        storage,
        DeepSeekClient(settings, llm_transport) if settings.enabled else None)
    ingest_lock = RLock()
    apk_storage = ApkStorage(database, credential_ttl_days=credential_days)
    sensor_models = SensorModels()
    investigations = InvestigationService(
        storage, agent, agent.llm,
        max_tool_calls=(investigation_max_tool_calls if investigation_max_tool_calls is not None
                        else _runtime_setting("GUARDIAN_INVESTIGATION_MAX_TOOL_CALLS", "8", 5, 12)),
        deadline_seconds=(investigation_deadline_seconds if investigation_deadline_seconds is not None
                          else _runtime_setting("GUARDIAN_INVESTIGATION_DEADLINE_SECONDS", "20", 1, 60)),
        notification_ttl_seconds=(notification_ttl_seconds if notification_ttl_seconds is not None
                                  else _runtime_setting("GUARDIAN_NOTIFICATION_TTL_SECONDS", "60", 1, 3600)),
        notification_cooldown_seconds=(notification_cooldown_seconds if notification_cooldown_seconds is not None
                                       else _runtime_setting("GUARDIAN_NOTIFICATION_COOLDOWN_SECONDS", "300", 0, 3600)),
        max_attempts=(investigation_max_attempts if investigation_max_attempts is not None
                      else _runtime_setting("GUARDIAN_INVESTIGATION_MAX_ATTEMPTS", "2", 1, 4)),
        max_workers=(investigation_max_workers if investigation_max_workers is not None
                     else _runtime_setting("GUARDIAN_INVESTIGATION_MAX_WORKERS", "1", 1, 4)),
        max_llm_tokens=(investigation_max_llm_tokens if investigation_max_llm_tokens is not None
                        else _runtime_setting("GUARDIAN_INVESTIGATION_MAX_LLM_TOKENS", "8000", 100, 100000)),
        total_deadline_seconds=(investigation_total_deadline_seconds
                                if investigation_total_deadline_seconds is not None
                                else _runtime_setting("GUARDIAN_INVESTIGATION_TOTAL_DEADLINE_SECONDS", "30", 1, 120)),
        retry_backoff_seconds=(investigation_retry_backoff_seconds
                               if investigation_retry_backoff_seconds is not None
                               else float(os.getenv("GUARDIAN_INVESTIGATION_RETRY_BACKOFF_SECONDS", "0.2"))),
        duplicate_window_seconds=(investigation_duplicate_window_seconds
                                  if investigation_duplicate_window_seconds is not None
                                  else _runtime_setting("GUARDIAN_INVESTIGATION_DUPLICATE_WINDOW_SECONDS", "300", 0, 3600)),
    )
    decision_on = ((os.getenv("GUARDIAN_DECISION_ENGINE", "false").lower() == "true")
                   if decision_engine_enabled is None else decision_engine_enabled)
    real_sustained = ((os.getenv("GUARDIAN_DECISION_REAL_SUSTAINED", "false").lower() == "true")
                      if decision_real_sustained is None else decision_real_sustained)
    decision_worker = ((os.getenv("GUARDIAN_DECISION_BACKGROUND", "true").lower() == "true")
                       if decision_background is None else decision_background)
    decision_worker_allowed = decision_on and decision_worker and not public_only
    decisions = DecisionEngine(
        storage, investigations, enabled=decision_on,
        allow_real_sustained=real_sustained, clock=runtime_clock.now)
    processing_recovery_on = (
        os.getenv("GUARDIAN_PROCESSING_RECOVERY", "true").lower() == "true"
        if processing_recovery is None else processing_recovery)
    processing_attempts = (
        _runtime_setting("GUARDIAN_PROCESSING_MAX_ATTEMPTS", "5", 1, 20)
        if processing_max_attempts is None else processing_max_attempts)
    processor = IngestionProcessor(
        storage, engine, investigations, decisions,
        clock=runtime_clock, ingest_lock=ingest_lock,
        max_attempts=processing_attempts)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if decision_worker_allowed:
            decisions.start_background()
        try:
            yield
        finally:
            decisions.stop_background()

    api = FastAPI(
        title="SenseMind Research Prototype API", version="0.7.0",
        description="Non-clinical, independent demo backend",
        docs_url=None if public_only else "/docs",
        redoc_url=None if public_only else "/redoc",
        openapi_url=None if public_only else "/openapi.json",
        lifespan=lifespan,
    )
    api.state.decision_engine = decisions
    api.state.decision_worker_allowed = decision_worker_allowed
    api.state.processor = processor
    # Resume accepted-but-unprocessed work before serving.  The public-only
    # factory path is retained for contract tests; it must not become a second
    # writer, so recovery runs only in the authoritative local app.
    if processing_recovery_on and not public_only:
        try:
            processor.recover()
        except Exception:
            # A recovery error must not prevent the service from starting;
            # the failed/stale job rows remain for the next startup.
            pass

    @api.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # The APK reads top-level `code`/`message`; keep legacy string details unchanged.
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail,
                                headers=exc.headers)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                            headers=exc.headers)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Expose paths and messages only; request bodies can contain health data.
        details = [{"path": list(error["loc"]), "message": error["msg"]}
                   for error in exc.errors()]
        return JSONResponse(status_code=422, content={
            "code": "invalid_request", "message": "Request validation failed",
            "details": details})

    # Phone-facing contract used by the existing RareGuard APK. The Agent stays read-only.
    api.include_router(create_apk_router(
        apk_storage, storage, agent, engine, ingest_lock, investigations,
        decision_engine=decisions, clock=runtime_clock,
        processor=processor))

    if public_only:
        return api

    ttl = _runtime_setting("GUARDIAN_APK_PAIRING_TTL_MINUTES", "10", 1, 60)
    configured_code = (apk_pairing_code or os.getenv("GUARDIAN_APK_PAIRING_CODE", "").strip()
                       or None)
    if configured_code is not None and not 12 <= len(configured_code) <= 100:
        raise RuntimeError("APK pairing code must be 12-100 characters")
    issued = apk_storage.issue_pairing_code("守护天使用户", ttl_minutes=ttl,
                                            code=configured_code)
    if sys.stdout.isatty():
        # Interactive terminal only: never write the code into redirected logs.
        print(f"[guardian] APK 配对码：{issued['code']}（{ttl} 分钟内有效，仅可使用一次）")
    else:
        print("[guardian] 已生成一次性 APK 配对码；当前输出不是终端，"
              "请运行 python -m guardian.pairing 生成并查看。")

    @api.get("/health")
    def health():
        return {"ok": True, "version": "0.7.0", "real_device_rules_enabled": enable_real,
                "apk_contract": "v1", "assistant": settings.public_status(),
                "investigations": {"async": True,
                                   "max_tool_calls": investigations.max_tool_calls,
                                   "deadline_seconds": investigations.deadline_seconds,
                                   "max_attempts": investigations.max_attempts,
                                   "max_workers": investigations.max_workers,
                                   "max_llm_tokens": investigations.max_llm_tokens,
                                   "total_deadline_seconds": investigations.total_deadline_seconds},
                "decision_engine": {"enabled": decision_on,
                                    "real_sustained": real_sustained,
                                    "background": decision_worker_allowed,
                                    "policy_version": DECISION_POLICY_VERSION,
                                    "thread_alive": decisions.status()["thread_alive"],
                                    "last_sweep_at": decisions.last_sweep_at,
                                    "last_error": decisions.last_error,
                                    "last_error_at": decisions.last_error_at},
                "rule_stability": {"open_dwell_seconds": engine.open_dwell_seconds,
                                   "close_dwell_seconds": engine.close_dwell_seconds,
                                   "max_sample_gap_seconds": engine.max_sample_gap_seconds,
                                   "max_sample_lag_seconds": engine.max_sample_lag_seconds}}

    @api.post("/v1/telemetry")
    def telemetry(reading: Telemetry):
        # Keep receipt time, ordering, persistence and rule evaluation serial
        # in this single-process prototype.  Capturing the clocks inside the
        # lock prevents concurrent requests from inverting receipt time.
        with ingest_lock:
            received_at = runtime_clock.now()
            monotonic = runtime_clock.monotonic()

            def persist(sample):
                # Telemetry and its outbox job commit together.  The row can
                # never exist without a durable processing task.
                return storage.put_telemetry_with_job(
                    sample, received_at=received_at, monotonic=monotonic,
                    boot_id=BOOT_ID, max_attempts=processor.max_attempts)

            evaluation = engine.analyze(
                reading, now=received_at, received_at=received_at,
                monotonic=monotonic, hydrate=True, persist=persist,
                audit=lambda sample, reason, continuity:
                    storage.record_telemetry_rejection(
                        sample, received_at=received_at, reason=reason,
                        continuity=continuity),
                defer_processing=True)
            if (not evaluation.get("accepted", True)
                    and evaluation.get("time_status") in
                    ("duplicate", "out_of_order")):
                if evaluation.get("time_status") == "duplicate":
                    # The client retry is the recovery trigger: the sample is
                    # already accepted, so only pending/failed downstream work
                    # may be retried.  The response keeps the 409 contract.
                    try:
                        processor.recover_duplicate(
                            reading.device_id, reading.timestamp)
                    except Exception:
                        pass
                raise HTTPException(
                    status_code=409,
                    detail="Out-of-order or duplicate timestamp for this device")
            if not evaluation.get("accepted", True):
                # Rejection is audited and returned; it must not trigger
                # investigations or a decision based on a rejected sample.
                return evaluation
            reference = evaluation.get("persisted") or {}
            if reference.get("job_id"):
                outcome = processor.process_job(reference["job_id"])
                if outcome:
                    evaluation["processing"] = outcome["processing"]
                    evaluation["processing_pending"] = (
                        outcome.get("status") != "completed")
                    evaluation["transitions"] = outcome["transitions"]
                    evaluation["rule_evaluated"] = outcome["rule_evaluated"]
                    if outcome.get("evaluation"):
                        evaluation["evaluation"] = outcome["evaluation"]
                    if outcome.get("signals"):
                        evaluation["signals"] = outcome["signals"]
        return evaluation

    @api.get("/v1/live/status")
    def status(device_id: str = Query("guardian_001", min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")):
        return agent.status(device_id)

    @api.get("/v1/history")
    def history(device_id: str = Query("guardian_001", min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"), limit: int = Query(20, ge=1, le=200)):
        return storage.history(device_id, limit)

    @api.get("/v1/events")
    def events(device_id: str = Query("guardian_001", min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"), limit: int = Query(20, ge=1, le=200)):
        return storage.events(device_id, limit)

    @api.post("/v1/events/{event_id}/ack")
    def acknowledge(event_id: str, request: Request, body: AckBody | None = None):
        # Human confirmation only exists on the local app; the AI has no write tools.
        if not console_token_ok(request.headers.get("x-console-token")):
            raise HTTPException(status_code=401, detail={
                "code": "console_auth_required",
                "message": "A console token is required for this action"})
        confirmed_by = body.confirmed_by if body else "local_operator"
        result = console_acknowledge(storage, event_id, confirmed_by)
        if result is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return result

    @api.get("/console", response_class=HTMLResponse)
    def console_page():
        return CONSOLE_HTML

    @api.get("/v1/console/snapshot")
    def console_snapshot(device_id: str = Query(
            "*", pattern=r"^\*$|^[a-zA-Z0-9_-]+$", min_length=1, max_length=80)):
        return snapshot(storage, agent, device_id)

    @api.get("/v1/console/events/{event_id}")
    def console_event(event_id: str):
        detail = event_detail(storage, agent, event_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Event not found")
        return detail

    @api.get("/v1/events/{event_id}/investigation")
    def event_investigation(event_id: str):
        investigation = storage.investigation_for_event(event_id)
        if not investigation:
            raise HTTPException(status_code=404, detail="Investigation not found")
        return investigation

    @api.get("/v1/investigations/{investigation_id}")
    def investigation_detail(investigation_id: str):
        investigation = storage.investigation(investigation_id)
        if not investigation:
            raise HTTPException(status_code=404, detail="Investigation not found")
        return {**investigation,
                "transitions": storage.investigation_transitions(investigation_id),
                "evidence": storage.evidence_for_event(investigation["event_id"]),
                "decisions": storage.decisions_for_event(investigation["event_id"])}

    @api.get("/v1/decisions")
    def decision_list(device_id: str | None = Query(
            None, min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"),
            limit: int = Query(50, ge=1, le=200),
            decision_type: str | None = Query(
                None, pattern=r"^(OBSERVE|VERIFY|INVESTIGATE|INSIGHT)$")):
        return {"data": storage.decisions(device_id, limit, decision_type)}

    @api.get("/v1/decisions/{decision_id}")
    def decision_detail(decision_id: str):
        decision = storage.decision(decision_id)
        if not decision:
            raise HTTPException(status_code=404, detail="Decision not found")
        return decision

    @api.get("/v1/events/{event_id}/decisions")
    def event_decisions(event_id: str):
        if not storage.get_event(event_id):
            raise HTTPException(status_code=404, detail="Event not found")
        return {"event_id": event_id,
                "decisions": storage.decisions_for_event(event_id)}

    @api.get("/v1/monitoring/{device_id}")
    def monitoring_state(device_id: str):
        return storage.monitoring(device_id, now=runtime_clock.now())

    @api.get("/v1/events/{event_id}/evidence")
    def event_evidence(event_id: str):
        if not storage.get_event(event_id):
            raise HTTPException(status_code=404, detail="Event not found")
        return {"event_id": event_id,
                "evidence": storage.evidence_for_event(event_id)}

    @api.get("/v1/evidence/{evidence_id}")
    def evidence_detail(evidence_id: str):
        item = storage.evidence_by_id(evidence_id)
        if not item:
            raise HTTPException(status_code=404, detail="Evidence not found")
        return item

    @api.post("/v1/investigations/{investigation_id}/retry")
    def retry_investigation(investigation_id: str, request: Request):
        if not console_token_ok(request.headers.get("x-console-token")):
            raise HTTPException(status_code=401, detail={
                "code": "console_auth_required",
                "message": "A console token is required for this action"})
        try:
            investigation = investigations.retry(investigation_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        if investigation is None:
            raise HTTPException(status_code=404, detail="Investigation not found")
        return {"requeued": True, "investigation": investigation}

    @api.get("/v1/runtime/status")
    def runtime_status():
        return {"models": sensor_models.status(), "assistant": settings.public_status(),
                "hardware_tested": False, "modes": ["live", "replay"],
                "note": "Configured API does not prove provider connectivity; no ESP32 acceptance yet."}

    @api.get("/v1/ml/status")
    def ml_status():
        return sensor_models.status()

    @api.post("/v1/ml/analyze")
    def ml_analyze(body: AnalyzeRequest):
        result = sensor_models.analyze(body.model_dump(mode="json"))
        storage.put_ml_inference(result)
        return result

    @api.get("/v1/ml/inferences")
    def ml_inferences(device_id: str = Query(
            ..., min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"),
            limit: int = Query(20, ge=1, le=100)):
        return {"data": storage.ml_inferences(device_id, limit)}

    @api.get("/v1/tools")
    def tools():
        return TOOLS

    @api.post("/v1/tools/call")
    def tool_call(req: ToolCall):
        return {"tool": req.tool, "result": agent.call(req.tool, req.device_id, req.limit)}

    @api.post("/v1/assistant")
    def assistant(req: AskRequest):
        return agent.answer(req.question, req.device_id)

    @api.websocket("/ws/live")
    async def live(ws: WebSocket, device_id: str = "guardian_001"):
        # Local prototype only; the console polls this socket every second.
        await ws.accept()
        try:
            while True:
                pending = storage.pending_notifications(
                    None if device_id == "*" else device_id)
                await ws.send_json(snapshot(storage, agent, device_id))
                if pending:
                    storage.mark_notifications_sent([item["id"] for item in pending])
                    # Deliver the corrected status in the same tick; the first
                    # frame necessarily described the pre-send state.
                    await ws.send_json(snapshot(storage, agent, device_id))
                await asyncio.sleep(1)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return api
