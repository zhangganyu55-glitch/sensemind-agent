"""Registry/feature contract tests using tiny synthetic model artifacts."""
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

from guardian.ml.features import (ACTIVITY_FEATURE_VERSION, PPG_FEATURE_VERSION,
                                  activity_features, ppg_features)
from guardian.ml.registry import SensorModels


def test_sensor_models_load_synthetic_artifacts_and_infer(tmp_path):
    rng = np.random.default_rng(42)
    acc = rng.normal(size=(512, 3))
    bvp = np.sin(2 * np.pi * 1.2 * np.arange(512) / 64.0)
    ppg_acc = rng.normal(scale=0.02, size=(512, 3))

    activity_vector, activity_names = activity_features(acc, np.array([75.0, 76.0]))
    ppg_vector, ppg_names, _ = ppg_features(bvp, ppg_acc, 64.0)
    activity_X = np.vstack([activity_vector, activity_vector * 1.05,
                            activity_vector * 0.95, activity_vector * 1.1])
    activity_y = np.array([0, 1, 0, 1])
    ppg_X = np.vstack([ppg_vector, ppg_vector * 1.03,
                       ppg_vector * 0.97, ppg_vector * 1.06])
    quality_y = np.array([1, 0, 1, 0])
    hr_y = np.array([72.0, 80.0, 74.0, 82.0])

    activity_model = RandomForestClassifier(n_estimators=15, random_state=0).fit(
        activity_X, activity_y)
    quality_model = RandomForestClassifier(n_estimators=15, random_state=0).fit(
        ppg_X, quality_y)
    hr_model = RandomForestRegressor(n_estimators=15, random_state=0).fit(
        ppg_X, hr_y)

    (tmp_path / "activity").mkdir()
    (tmp_path / "ppg").mkdir()
    joblib.dump({"model": activity_model, "feature_names": activity_names,
                 "feature_version": ACTIVITY_FEATURE_VERSION,
                 "version": "test-activity", "activity_names": {0: "sitting", 1: "running"}},
                tmp_path / "activity" / "activity_rf.joblib")
    joblib.dump({"model": quality_model, "feature_names": ppg_names,
                 "feature_version": PPG_FEATURE_VERSION, "version": "test-ppg"},
                tmp_path / "ppg" / "ppg_quality_rf.joblib")
    joblib.dump({"model": hr_model, "feature_names": ppg_names,
                 "feature_version": PPG_FEATURE_VERSION, "version": "test-ppg"},
                tmp_path / "ppg" / "ppg_hr_rf.joblib")

    models = SensorModels(tmp_path)
    status = models.status()
    assert status["activity"]["available"] is True
    assert status["ppg"]["available"] is True

    result = models.analyze({
        "device_id": "guardian_001",
        "window_start": datetime.now(timezone.utc).isoformat(),
        "dataset": "pamap2", "participant_id": "p1", "sampling_hz": 100.0,
        "acc": acc.tolist(), "ppg": bvp.tolist(), "hr": [75.0, 76.0],
    })
    assert result["available"] is True
    assert result["activity"]["available"] is True
    assert result["activity"]["label"] in {"sitting", "running"}
    assert 0.0 <= result["activity"]["confidence"] <= 1.0
    result = models.analyze({"device_id": "guardian_001",
        "window_start": datetime.now(timezone.utc).isoformat(),
        "dataset": "ppg-dalia", "participant_id": "S13", "sampling_hz": 64,
        "acc_sampling_hz": 32, "ppg": bvp.tolist(), "acc": ppg_acc[::2].tolist()})
    assert result["signal"]["available"] is True
    assert 0.0 <= result["signal"]["reliability"] <= 1.0
    assert result["signal"]["estimated_hr"] is None or np.isfinite(result["signal"]["estimated_hr"])
    assert result["model_version"]
    assert result["window_end"] >= result["window_start"]
