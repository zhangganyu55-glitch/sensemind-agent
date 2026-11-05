"""ML service API tests without requiring downloaded datasets or models."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from guardian.llm import LLMSettings
from guardian.main import create_app


def test_ml_api_without_artifacts_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_ML_DIR", str(tmp_path / "missing-artifacts"))
    client = TestClient(create_app(str(tmp_path / "ml.db"),
                                   llm_settings=LLMSettings(mode="off")))
    status = client.get("/v1/ml/status").json()
    assert status["activity"]["available"] is False
    assert status["ppg"]["available"] is False

    moment = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    response = client.post("/v1/ml/analyze", json={
        "device_id": "guardian_001", "window_start": moment,
        "dataset": "synthetic", "participant_id": "p1", "sampling_hz": 100,
        "acc": [[0.0, 0.0, 1.0]] * 128, "ppg": [], "hr": [],
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["available"] is False
    assert result["activity"]["reason"] == "activity_model_not_trained"
    assert result["signal"]["reason"] == "ppg_model_not_trained"

    stored = client.get("/v1/ml/inferences",
                        params={"device_id": "guardian_001"}).json()["data"]
    assert len(stored) == 1
    assert stored[0]["result"]["activity"]["available"] is False


def test_public_app_does_not_expose_ml_routes(tmp_path):
    app = create_app(str(tmp_path / "public.db"), public_only=True,
                     llm_settings=LLMSettings(mode="off"))
    public = TestClient(app)
    assert public.get("/v1/ml/status").status_code == 404
    assert public.post("/v1/ml/analyze", json={}).status_code == 404
    assert public.get("/v1/ml/inferences?device_id=x").status_code == 404
