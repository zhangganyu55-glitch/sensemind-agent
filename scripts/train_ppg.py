#!/usr/bin/env python
"""Train PPG-DaLiA signal-quality and HR baselines with participant splits.

The quality target is a proxy: PPG-derived HR within 10 bpm of the ECG
reference. It is not a clinical signal-quality label.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import (accuracy_score, classification_report, mean_absolute_error,
                             mean_squared_error, root_mean_squared_error)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.ml.data import iter_ppg_windows, ppg_files  # noqa: E402
from guardian.ml.features import (PPG_FEATURE_VERSION, ppg_features,
                                  reliability_label)  # noqa: E402


def quality_metrics(y_true, y_pred):
    report = classification_report(y_true, y_pred, labels=[0, 1], output_dict=True,
                                   zero_division=0, target_names=["unreliable", "reliable"])
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_f1": float(report["macro avg"]["f1-score"]),
        "weighted_f1": float(report["weighted avg"]["f1-score"]),
        "report": report,
    }


def regression_metrics(y_true, y_pred):
    return {
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(root_mean_squared_error(y_true, y_pred)),
        "count": int(len(y_true)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default=str(ROOT / "data" / "raw"))
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "ppg"))
    parser.add_argument("--test-subjects", default="S13,S14,S15")
    parser.add_argument("--val-subjects", default="S11,S12")
    parser.add_argument("--max-windows-per-subject", type=int, default=400)
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--n-estimators", type=int, default=250)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    test_subjects = {item.strip() for item in args.test_subjects.split(",") if item.strip()}
    val_subjects = {item.strip() for item in args.val_subjects.split(",") if item.strip()}
    files = ppg_files(Path(args.raw))
    if not files:
        raise SystemExit("No PPG-DaLiA files found. Download and extract data/raw first.")

    rows, feature_names = [], None
    subject_windows = {}
    quality, heart_targets, subjects = [], [], []
    for path in files:
        subject = path.stem
        count = 0
        for window in iter_ppg_windows(
                path, max_windows=args.max_windows_per_subject, stride=args.stride):
            try:
                vector, names, meta = ppg_features(
                    window["bvp"], window["acc"], window["sampling_hz"])
            except ValueError:
                continue
            feature_names = names
            rows.append(vector)
            quality.append(reliability_label(
                meta["ppg_hr"], window["reference_hr"], meta["beat_count"]))
            heart_targets.append(float(window["reference_hr"]))
            subjects.append(subject)
            count += 1
        print(f"{path.name}: {count} windows")
        subject_windows[subject] = count

    if not rows:
        raise SystemExit("No PPG windows could be built.")
    X = np.vstack(rows)
    y_quality = np.asarray(quality)
    y_hr = np.asarray(heart_targets)
    groups = np.asarray(subjects)
    train_mask = ~np.isin(groups, list(test_subjects | val_subjects))
    val_mask = np.isin(groups, list(val_subjects))
    test_mask = np.isin(groups, list(test_subjects))
    if not train_mask.any() or not test_mask.any():
        raise SystemExit("Bad participant split; check subject IDs.")

    quality_model = RandomForestClassifier(
        n_estimators=args.n_estimators, class_weight="balanced_subsample",
        random_state=args.seed, n_jobs=-1, min_samples_leaf=2)
    quality_model.fit(X[train_mask], y_quality[train_mask])
    quality_baseline = DummyClassifier(strategy="most_frequent")
    quality_baseline.fit(X[train_mask], y_quality[train_mask])

    hr_model = RandomForestRegressor(
        n_estimators=args.n_estimators, random_state=args.seed, n_jobs=-1,
        min_samples_leaf=2)
    hr_model.fit(X[train_mask], y_hr[train_mask])
    hr_baseline = DummyRegressor(strategy="mean")
    hr_baseline.fit(X[train_mask], y_hr[train_mask])

    test_quality = quality_metrics(y_quality[test_mask],
                                   quality_model.predict(X[test_mask]))
    baseline_quality = quality_metrics(y_quality[test_mask],
                                       quality_baseline.predict(X[test_mask]))
    test_hr = regression_metrics(y_hr[test_mask], hr_model.predict(X[test_mask]))
    baseline_hr = regression_metrics(y_hr[test_mask],
                                     hr_baseline.predict(X[test_mask]))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    version = f"ppg-dalia-rf-v0.6.0-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    joblib.dump({
        "model": quality_model, "feature_names": feature_names,
        "feature_version": PPG_FEATURE_VERSION, "version": version,
        "sampling_hz": 64.0, "window_samples": 512,
    }, out / "ppg_quality_rf.joblib")
    joblib.dump({
        "model": hr_model, "feature_names": feature_names,
        "feature_version": PPG_FEATURE_VERSION, "version": version,
        "sampling_hz": 64.0, "window_samples": 512,
    }, out / "ppg_hr_rf.joblib")
    metadata = {
        "version": version,
        "dataset": "PPG-DaLiA",
        "dataset_doi": "10.24432/C53890",
        "license": "UCI lists CC BY 4.0; original Siegen terms describe non-commercial research. Confirm commercial use with authors.",
        "feature_version": PPG_FEATURE_VERSION,
        "feature_count": len(feature_names),
        "window_samples": 512,
        "sampling_hz": 64.0,
        "window_seconds": 8.0,
        "label_step_seconds": 2.0,
        "stride": args.stride,
        "effective_step_seconds": 2.0 * args.stride,
        "max_windows_per_subject": args.max_windows_per_subject,
        "sampling_policy": "first eligible windows per subject, subsampled by label stride",
        "subject_windows": subject_windows,
        "train_subjects": sorted(set(groups[train_mask])),
        "val_subjects": sorted(set(groups[val_mask])),
        "test_subjects": sorted(set(groups[test_mask])),
        "train_windows": int(train_mask.sum()),
        "val_windows": int(val_mask.sum()),
        "test_windows": int(test_mask.sum()),
        "seed": args.seed,
        "n_estimators": args.n_estimators,
        "quality_label": "proxy: |PPG HR - ECG HR| <= 10 bpm and >=4 beats",
        "quality_test": test_quality,
        "quality_baseline_test": baseline_quality,
        "hr_test": test_hr,
        "hr_baseline_test": baseline_hr,
        "quality_val": quality_metrics(y_quality[val_mask], quality_model.predict(X[val_mask]))
            if val_mask.any() else None,
        "hr_val": regression_metrics(y_hr[val_mask], hr_model.predict(X[val_mask]))
            if val_mask.any() else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "Quality label is a proxy derived from HR agreement, not a medical SQI.",
            "E4 BVP/ACC hardware differs from MAX30102/MPU6050; resampling required.",
            "Experimental research baseline; not validated for diagnosis or alarms.",
            "Default sampling uses only the first ~40 minutes per subject; later activities are underrepresented.",
        ],
    }
    (out / "ppg_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"version": version, "windows": len(X),
                      "quality_accuracy": test_quality["accuracy"],
                      "quality_macro_f1": test_quality["macro_f1"],
                      "hr_mae": test_hr["mae"],
                      "hr_baseline_mae": baseline_hr["mae"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
