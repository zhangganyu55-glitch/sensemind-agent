"""Test-only clock helpers.

Production code uses ``guardian.clock``.  Existing API tests model a 1 Hz live
stream by advancing a manual clock once per ingestion request; batch/dwell
tests use the clock explicitly and do not auto-advance.
"""
from fastapi.testclient import TestClient

from guardian.clock import ManualClock


class ClockedClient(TestClient):
    def __init__(self, app, clock: ManualClock, advance_seconds: float = 1.0,
                 **kwargs):
        super().__init__(app, **kwargs)
        self.clock = clock
        self.advance_seconds = advance_seconds

    def post(self, url, *args, **kwargs):
        path = url.split("?", 1)[0]
        if path in ("/v1/telemetry", "/v1/measurement-batches"):
            self.clock.advance(self.advance_seconds)
        return super().post(url, *args, **kwargs)
