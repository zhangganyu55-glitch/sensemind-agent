"""Lazy loading and inference for the experimental sensor models."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np

from .features import (ACTIVITY_FEATURE_VERSION, PPG_FEATURE_VERSION,
                       activity_features, ppg_features)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_DIR = ROOT / "artifacts"


class SensorModels:
    """Load model artifacts once; never mutate them during inference."""

    def __init__(self, artifact_dir: str | Path | None = None):
        self.artifact_dir = Path(artifact_dir or os.getenv(
            "GUARDIAN_ML_DIR", str(DEFAULT_ARTIFACT_DIR)))
        self._lock = threading.RLock()
        self._loaded = False
        self.activity = None
        self.ppg_quality = None
        self.ppg_hr = None
        self.load_errors: dict[str, str] = {}

    def _load_file(self, name: str):
        path = self.artifact_dir / name
        try:
            artifact = joblib.load(path)
            expected = ACTIVITY_FEATURE_VERSION if name.startswith("activity/") else PPG_FEATURE_VERSION
            method = "predict" if "hr_rf" in name else "predict_proba"
            if (not isinstance(artifact, dict) or artifact.get("feature_version") != expected
                    or not callable(getattr(artifact.get("model"), method, None))):
                raise ValueError("invalid_artifact_contract")
            return artifact
        except FileNotFoundError:
            self.load_errors[name] = "artifact_not_found"
        except Exception as error:
            self.load_errors[name] = f"load_failed:{type(error).__name__}"
        return None

    def load(self):
        with self._lock:
            if self._loaded:
                return
            self.activity = self._load_file("activity/activity_rf.joblib")
            self.ppg_quality = self._load_file("ppg/ppg_quality_rf.joblib")
            self.ppg_hr = self._load_file("ppg/ppg_hr_rf.joblib")
            self._loaded = True

    def status(self) -> dict:
        self.load()
        return {
            "artifact_dir": str(self.artifact_dir),
            "models": {name: {"available": artifact is not None,
                       "version": (artifact or {}).get("version"),
                       "error": self.load_errors.get(path)}
                       for name, artifact, path in [
                           ("activity", self.activity, "activity/activity_rf.joblib"),
                           ("ppg_quality", self.ppg_quality, "ppg/ppg_quality_rf.joblib"),
                           ("ppg_hr", self.ppg_hr, "ppg/ppg_hr_rf.joblib")]},
            "activity": {
                "available": self.activity is not None,
                "version": (self.activity or {}).get("version"),
                "feature_version": (self.activity or {}).get("feature_version"),
                "error": self.load_errors.get("activity/activity_rf.joblib"),
            },
            "ppg": {
                "available": self.ppg_quality is not None and self.ppg_hr is not None,
                "quality_version": (self.ppg_quality or {}).get("version"),
                "hr_version": (self.ppg_hr or {}).get("version"),
                "feature_version": (self.ppg_quality or {}).get("feature_version"),
                "error": self.load_errors.get("ppg/ppg_quality_rf.joblib")
                         or self.load_errors.get("ppg/ppg_hr_rf.joblib"),
            },
            "limitations": [
                "Experimental baselines only; not clinically validated.",
                "Models are auxiliary evidence and never override safety rules.",
            ],
        }

    def _activity(self, acc: np.ndarray, hr: np.ndarray) -> dict:
        if self.activity is None:
            return {"available": False, "reason": "activity_model_not_trained"}
        if acc.ndim != 2 or acc.shape[1] != 3 or len(acc) < 64:
            return {"available": False, "reason": "insufficient_acc"}
        try:
            vector, names = activity_features(acc, hr)
        except ValueError:
            return {"available": False, "reason": "invalid_acc"}
        model = self.activity["model"]
        if len(names) != getattr(model, "n_features_in_", -1):
            return {"available": False, "reason": "feature_contract_mismatch"}
        probabilities = model.predict_proba([vector])[0]
        classes = list(model.classes_)
        index = int(np.argmax(probabilities))
        label = int(classes[index])
        confidence = float(probabilities[index])
        names_map = self.activity.get("activity_names", {})
        return {
            "available": True,
            "label": names_map.get(label, str(label)),
            "label_id": label,
            "confidence": round(confidence, 4),
            "anomaly_score": round(1.0 - confidence, 4),
            "model_version": self.activity.get("version"),
            "feature_version": self.activity.get("feature_version", ACTIVITY_FEATURE_VERSION),
            "input_samples": int(len(acc)),
        }

    def _ppg(self, bvp: np.ndarray, acc: np.ndarray, sampling_hz: float) -> dict:
        if self.ppg_quality is None or self.ppg_hr is None:
            return {"available": False, "reason": "ppg_model_not_trained"}
        if len(bvp) != 512:
            return {"available": False, "reason": "insufficient_ppg"}
        if not np.isfinite(bvp).all() or np.std(bvp) < 1e-8 or np.max(np.abs(bvp)) > 1e6:
            return {"available": False, "reason": "invalid_ppg"}
        if acc.shape != (256, 3) or not np.isfinite(acc).all() or np.max(np.abs(acc)) > 16:
            return {"available": False, "reason": "invalid_or_missing_wrist_acc"}
        acc_input = acc if acc.ndim == 2 and acc.shape[1] == 3 and len(acc) else None
        try:
            vector, names, meta = ppg_features(bvp, acc_input, sampling_hz)
        except ValueError:
            return {"available": False, "reason": "invalid_ppg"}
        quality = self.ppg_quality["model"]
        hr_model = self.ppg_hr["model"]
        if (len(names) != getattr(quality, "n_features_in_", -1)
                or len(names) != getattr(hr_model, "n_features_in_", -1)):
            return {"available": False, "reason": "feature_contract_mismatch"}
        classes = list(quality.classes_)
        probabilities = quality.predict_proba([vector])[0]
        reliability = float(probabilities[classes.index(1)]) if 1 in classes else 0.0
        estimated_hr = float(hr_model.predict([vector])[0])
        return {
            "available": True,
            "reliability": round(reliability, 4),
            "anomaly_score": round(1.0 - reliability, 4),
            "estimated_hr": round(estimated_hr, 2) if reliability >= 0.7 else None,
            "hr_available": reliability >= 0.7,
            "degraded_reason": None if reliability >= 0.7 else "low_proxy_reliability",
            "quality_target": "HR agreement proxy, not medical SQI",
            "ppg_hr": round(meta["ppg_hr"], 2),
            "beat_count": meta["beat_count"],
            "quality_model_version": self.ppg_quality.get("version"),
            "hr_model_version": self.ppg_hr.get("version"),
            "feature_version": self.ppg_quality.get("feature_version", PPG_FEATURE_VERSION),
            "input_samples": int(len(bvp)),
        }

    def analyze(self, payload: dict) -> dict:
        self.load()
        acc = np.asarray(payload.get("acc") or [], dtype=float)
        bvp = np.asarray(payload.get("ppg") or [], dtype=float).reshape(-1)
        hr = np.asarray(payload.get("hr") or [], dtype=float).reshape(-1)
        sampling_hz = float(payload.get("sampling_hz") or 0.0)
        start = payload["window_start"]
        if isinstance(start, str):
            start = datetime.fromisoformat(start)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        window_start = start.astimezone(timezone.utc)

        acc_rate = float(payload.get("acc_sampling_hz") or sampling_hz)
        activity = {"available": False, "reason": "unsupported_activity_domain"}
        signal = {"available": False, "reason": "unsupported_ppg_domain"}
        # No cross-dataset activity predictions: E4 g/32Hz differs from PAMAP2 m/s²/100Hz.
        if payload.get("dataset") == "pamap2" and sampling_hz == 100 and acc_rate == 100:
            if acc.shape == (512, 3) and np.isfinite(acc).all() and np.isfinite(hr).all() and np.max(np.abs(acc)) <= 160:
                try:
                    activity = self._activity(acc, hr)
                except Exception:
                    activity = {"available": False, "reason": "activity_inference_failed"}
            else:
                activity = {"available": False, "reason": "invalid_activity_input"}
        if payload.get("dataset") == "ppg-dalia" and sampling_hz == 64 and acc_rate == 32:
            try:
                signal = self._ppg(bvp, acc, sampling_hz)
            except Exception:
                signal = {"available": False, "reason": "ppg_inference_failed"}
        if self.activity is None:
            activity = {"available": False, "reason": "activity_model_not_trained"}
        if self.ppg_quality is None or self.ppg_hr is None:
            signal = {"available": False, "reason": "ppg_model_not_trained"}
        duration = max((len(acc) / acc_rate) if sampling_hz else 0.0,
                       (len(bvp) / sampling_hz) if sampling_hz else 0.0, 0.0)
        window_end = window_start + timedelta(seconds=duration)
        versions = [item[key] for item in (activity, signal) for key in ("model_version", "quality_model_version", "hr_model_version") if item.get(key)]
        result = {
            "device_id": payload["device_id"],
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "dataset": payload.get("dataset"),
            "participant_id": payload.get("participant_id"),
            "sampling_hz": sampling_hz,
            "activity": activity,
            "signal": signal,
            "activity_label": activity.get("label"),
            "activity_confidence": activity.get("confidence"),
            "signal_reliability": signal.get("reliability"),
            "anomaly_score": max(
                [value for value in (activity.get("anomaly_score"),
                                     signal.get("anomaly_score"))
                 if value is not None], default=None),
            "model_version": ";".join(sorted(set(versions))) if versions else None,
            "available": bool(activity.get("available") or signal.get("available")),
            "limitations": [
                "Experimental model output; not a diagnosis or safety guarantee.",
                "Activity model trained on PAMAP2; PPG quality trained on PPG-DaLiA proxy labels.",
                "Input sampling and hardware must match the dataset assumptions.",
            ],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return result
