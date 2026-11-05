#!/usr/bin/env python
"""Train the PAMAP2 wrist-IMU activity baseline with participant-wise splits.

Example:
    python scripts/train_activity.py
    python scripts/train_activity.py --test-subjects 105,106 --val-subjects 107
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.ml.data import PAMAP2_ACTIVITIES, iter_pamap2_windows, pamap2_files  # noqa: E402
from guardian.ml.features import ACTIVITY_FEATURE_VERSION, activity_features  # noqa: E402


def metrics(y_true, y_pred, labels):
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_f1": float(classification_report(
            y_true, y_pred, labels=labels, output_dict=True, zero_division=0)["macro avg"]["f1-score"]),
        "weighted_f1": float(classification_report(
            y_true, y_pred, labels=labels, output_dict=True, zero_division=0)["weighted avg"]["f1-score"]),
        "report": classification_report(
            y_true, y_pred, labels=labels, output_dict=True, zero_division=0,
            target_names=[PAMAP2_ACTIVITIES.get(label, str(label)) for label in labels]),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=labels).tolist(),
        "labels": [int(label) for label in labels],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default=str(ROOT / "data" / "raw"))
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "activity"))
    parser.add_argument("--test-subjects", default="105,106")
    parser.add_argument("--val-subjects", default="107")
    parser.add_argument("--include-optional", action="store_true")
    parser.add_argument("--max-windows-per-subject", type=int, default=1200)
    parser.add_argument("--n-estimators", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    test_subjects = {item.strip() for item in args.test_subjects.split(",") if item.strip()}
    val_subjects = {item.strip() for item in args.val_subjects.split(",") if item.strip()}
    files = [path for path in pamap2_files(Path(args.raw))
             if args.include_optional or "Protocol" in str(path)]
    if not files:
        raise SystemExit("No PAMAP2 files found. Download and extract data/raw/PAMAP2_Dataset first.")

    rows, labels, subjects, names = [], [], [], None
    for path in files:
        subject = path.stem.replace("subject", "")
        count = 0
        for window in iter_pamap2_windows(path):
            vector, feature_names = activity_features(window["acc"], window["heart_rate"])
            names = feature_names
            rows.append(vector)
            labels.append(window["activity_id"])
            subjects.append(subject)
            count += 1
            if count >= args.max_windows_per_subject:
                break
        print(f"{path.name}: {count} windows")

    X = np.vstack(rows)
    y = np.asarray(labels)
    groups = np.asarray(subjects)
    train_mask = ~np.isin(groups, list(test_subjects | val_subjects))
    val_mask = np.isin(groups, list(val_subjects))
    test_mask = np.isin(groups, list(test_subjects))
    if not train_mask.any() or not test_mask.any():
        raise SystemExit("Bad participant split; check subject IDs.")

    present = sorted(set(y[train_mask]))
    labels_order = [label for label in sorted(PAMAP2_ACTIVITIES) if label in set(y)]
    model = RandomForestClassifier(
        n_estimators=args.n_estimators, class_weight="balanced_subsample",
        random_state=args.seed, n_jobs=-1, min_samples_leaf=2)
    model.fit(X[train_mask], y[train_mask])
    baseline = DummyClassifier(strategy="most_frequent")
    baseline.fit(X[train_mask], y[train_mask])

    val_metrics = metrics(y[val_mask], model.predict(X[val_mask]), labels_order)
    test_metrics = metrics(y[test_mask], model.predict(X[test_mask]), labels_order)
    baseline_metrics = metrics(y[test_mask], baseline.predict(X[test_mask]), labels_order)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    version = f"pamap2-rf-v0.6.0-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    artifact = {
        "model": model,
        "feature_names": names,
        "feature_version": ACTIVITY_FEATURE_VERSION,
        "sampling_hz": 100.0,
        "window_samples": 512,
        "activity_names": PAMAP2_ACTIVITIES,
        "version": version,
    }
    joblib.dump(artifact, out / "activity_rf.joblib")
    metadata = {
        "version": version,
        "dataset": "PAMAP2",
        "dataset_doi": "10.24432/C5NW2H",
        "license": "CC BY 4.0 (attribution required)",
        "feature_version": ACTIVITY_FEATURE_VERSION,
        "feature_count": len(names),
        "window_samples": 512,
        "sampling_hz": 100.0,
        "train_subjects": sorted(set(groups[train_mask])),
        "val_subjects": sorted(set(groups[val_mask])),
        "test_subjects": sorted(set(groups[test_mask])),
        "train_windows": int(train_mask.sum()),
        "val_windows": int(val_mask.sum()),
        "test_windows": int(test_mask.sum()),
        "seed": args.seed,
        "model": "RandomForestClassifier",
        "labels": labels_order,
        "val_metrics": val_metrics,
        "test_metrics": test_metrics,
        "baseline_test_metrics": baseline_metrics,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "Experimental activity classifier; not a clinical or safety device.",
            "PAMAP2 wrist-IMU data, 9 participants; limited demographics.",
            "Requires units/frequency adaptation before use with MPU6050.",
        ],
    }
    joblib.dump(metadata, out / "activity_metadata.joblib")
    (out / "activity_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"version": version, "windows": len(X),
                      "test_accuracy": test_metrics["accuracy"],
                      "test_macro_f1": test_metrics["macro_f1"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
