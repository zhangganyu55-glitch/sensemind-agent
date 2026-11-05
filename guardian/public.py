"""Public HTTPS surface: a controlled proxy to the authoritative local app.

Run behind the tunnel: `uvicorn guardian.public:app --host 127.0.0.1 --port 8010`

This process intentionally owns no SQLite database and no ``DetectionEngine``.
It only forwards the authenticated APK contract paths to the local authority
(``guardian.main:app`` on ``127.0.0.1:8000`` by default).  Keeping a single
writer process means STM32 and APK ingress share one device time state machine,
one Monitoring state and one event lifecycle.
"""
from __future__ import annotations

import os
from typing import Iterable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.routing import Match

from .apk_api import create_apk_router


DEFAULT_LOCAL_URL = "http://127.0.0.1:8000"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding",
}


def _apk_routes():
    """Return the APK router routes without constructing a writer app.

    The handlers close over dependencies that are only touched on a request,
    so route registration is safe with ``None`` placeholders.
    """
    router = create_apk_router(None, None, None, None, None, None)
    return router.routes


def _match_route(routes: Iterable, method: str, path: str) -> str:
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    partial = False
    for route in routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return "full"
        if match == Match.PARTIAL:
            partial = True
    return "partial" if partial else "none"


def _forward_headers(headers) -> dict:
    return {key: value for key, value in headers.items()
            if key.lower() not in HOP_BY_HOP}


def create_public_app(*, local_base_url: str | None = None,
                      transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    authority = (local_base_url or os.getenv("GUARDIAN_LOCAL_URL",
                                               DEFAULT_LOCAL_URL)).rstrip("/")
    routes = _apk_routes()
    app = FastAPI(title="SenseMind Public APK Proxy", docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.authoritative_url = authority

    async def upstream_request(method: str, path: str, *, query: str = "",
                               headers=None, content: bytes | None = None,
                               timeout: float = 30.0) -> httpx.Response:
        url = authority + path
        if query:
            url += "?" + query
        async with httpx.AsyncClient(transport=transport, timeout=timeout,
                                     trust_env=False) as client:
            return await client.request(method, url, headers=headers,
                                        content=content)

    @app.get("/healthz")
    async def healthz():
        try:
            upstream = await upstream_request("GET", "/health", timeout=3.0)
        except httpx.HTTPError:
            upstream = None
        if upstream is not None and upstream.status_code == 200:
            return {"status": "ok", "authority": "local", "local_url": authority}
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable",
                     "code": "authoritative_service_unavailable",
                     "message": "Local authoritative service is not reachable"})

    @app.api_route("/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def proxy(request: Request, path: str):
        decision = _match_route(routes, request.method, request.url.path)
        if decision == "partial":
            return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"})
        if decision != "full":
            # Do not expose /v1/telemetry, /console, /docs, OpenAPI or Agent routes.
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        try:
            upstream = await upstream_request(
                request.method, request.url.path,
                query=request.url.query,
                headers=_forward_headers(request.headers),
                content=await request.body())
        except httpx.HTTPError as error:
            return JSONResponse(
                status_code=503,
                content={"code": "authoritative_service_unavailable",
                         "message": f"Local authoritative service failed: {type(error).__name__}"})

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=_forward_headers(upstream.headers),
            media_type=upstream.headers.get("content-type"))

    return app


app = create_public_app()
