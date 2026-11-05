"""Dataset loaders and participant-isolated windowing.

Raw data is never redistributed. These loaders read local files downloaded
from the official repositories (see docs/dataset-evaluation.md).
"""
import pickle
from pathlib import Path

import numpy as np


PAMAP2_ACTIVITIES = {
    1: "lying", 2: "sitting", 3: "standing", 4: "walking", 5: "running",
    6: "cycling", 7: "nordic_walking", 9: "watching_tv", 10: "computer_work",
    11: "car_driving", 12: "ascending_stairs", 13: "descending_stairs",
    16: "vacuum_cleaning", 17: "ironing", 18: "folding_laundry",
    19: "house_cleaning", 20: "playing_soccer", 24: "rope_jumping",
}

PPG_ACTIVITIES = {
    0: "transient", 1: "sitting", 2: "stairs", 3: "table_soccer",
    4: "cycling", 5: "driving", 6: "lunch", 7: "walking", 8: "working",
}

PAMAP2_RATE_HZ = 100.0
PPG_BVP_RATE_HZ = 64.0
PPG_ACC_RATE_HZ = 32.0


def pamap2_activity_name(activity_id: int) -> str:
    return PAMAP2_ACTIVITIES.get(int(activity_id), f"activity_{int(activity_id)}")


def ppg_activity_name(activity_id: int) -> str:
    return PPG_ACTIVITIES.get(int(activity_id), f"activity_{int(activity_id)}")


def pamap2_files(raw_dir: Path) -> list[Path]:
    root = Path(raw_dir)
    bases = [root / "PAMAP2_Dataset", root]
    files = []
    for base in bases:
        files.extend(sorted(base.glob("Protocol/subject*.dat")))
        files.extend(sorted(base.glob("Optional/subject*.dat")))
        if files:
            break
    return sorted(set(files))


def ppg_files(raw_dir: Path) -> list[Path]:
    root = Path(raw_dir)
    bases = [root / "PPG_FieldStudy", root / "data" / "PPG_FieldStudy"]
    for base in bases:
        files = sorted(base.glob("S*/S*.pkl"))
        if files:
            return files
    return []


def read_pamap2(path: Path) -> dict:
    """Read one PAMAP2 subject file. Wrist IMU ACC16 columns are 4:7 (0-based)."""
    data = np.loadtxt(path, usecols=(0, 1, 2, 4, 5, 6), ndmin=2)
    return {
        "timestamp": data[:, 0].astype(float),
        "activity": np.nan_to_num(data[:, 1]).astype(int),
        "heart_rate": data[:, 2].astype(float),
        "acc": data[:, 3:6].astype(float),  # m/s^2, wrist/hand IMU
        "subject": Path(path).stem.replace("subject", ""),
        "source": "pamap2",
        "sampling_hz": PAMAP2_RATE_HZ,
    }


def _segments(timestamp: np.ndarray, gap_seconds: float = 0.2):
    starts = [0]
    for index in range(1, len(timestamp)):
        if not np.isfinite(timestamp[index]) or not np.isfinite(timestamp[index - 1]):
            starts.append(index)
        elif timestamp[index] - timestamp[index - 1] > gap_seconds:
            starts.append(index)
    starts.append(len(timestamp))
    for first, second in zip(starts, starts[1:]):
        if second > first:
            yield first, second


def iter_pamap2_windows(path: Path, window_samples: int = 512,
                        step_samples: int = 256, min_label_fraction: float = 0.9,
                        min_valid_acc: float = 0.8):
    """Yield labeled, contiguous windows from one participant only."""
    data = read_pamap2(path)
    for first, last in _segments(data["timestamp"]):
        segment_length = last - first
        if segment_length < window_samples:
            continue
        for start in range(first, last - window_samples + 1, step_samples):
            stop = start + window_samples
            activities = data["activity"][start:stop]
            labels, counts = np.unique(activities, return_counts=True)
            label = int(labels[np.argmax(counts)])
            if label not in PAMAP2_ACTIVITIES:
                continue
            if counts.max() / window_samples < min_label_fraction:
                continue
            acc = data["acc"][start:stop]
            if np.isfinite(acc).all(axis=1).mean() < min_valid_acc:
                continue
            heart = data["heart_rate"][start:stop]
            heart = heart[np.isfinite(heart)]
            yield {
                "subject": data["subject"],
                "dataset": "pamap2",
                "sampling_hz": data["sampling_hz"],
                "activity_id": label,
                "activity": pamap2_activity_name(label),
                "start_time": float(data["timestamp"][start]),
                "end_time": float(data["timestamp"][stop - 1]),
                "acc": acc.astype(float),
                "heart_rate": heart.astype(float),
            }


def read_ppg_subject(path: Path) -> dict:
    # The official files are Python 2 pickles; latin1 keeps numpy arrays readable.
    with open(path, "rb") as handle:
        obj = pickle.load(handle, encoding="latin1")
    wrist = obj["signal"]["wrist"]
    return {
        "subject": str(obj.get("subject") or Path(path).stem),
        "bvp": np.asarray(wrist["BVP"], dtype=float).reshape(-1),
        "acc": np.asarray(wrist["ACC"], dtype=float),
        "heart_labels": np.asarray(obj.get("label", []), dtype=float).reshape(-1),
        "activity": np.asarray(obj.get("activity", []), dtype=float).reshape(-1),
        "rpeaks": np.asarray(obj.get("rpeaks", []), dtype=int).reshape(-1),
        "source": "ppg-dalia",
        "bvp_sampling_hz": PPG_BVP_RATE_HZ,
        "acc_sampling_hz": PPG_ACC_RATE_HZ,
    }


def iter_ppg_windows(path: Path, window_seconds: float = 8.0,
                     step_seconds: float = 2.0, max_windows: int = 600,
                     stride: int = 1):
    """Yield 8 s BVP/ACC windows with ECG-derived reference HR.

    Label i describes [2*i, 2*i + 8) seconds, starting at t=0 (dataset
    readme sections I.2 and III.3). Stride subsamples this fixed label grid;
    it must never change the time represented by an individual label. The
    reliability target is a proxy (PPG HR vs ECG reference), not a clinical
    quality label.
    """
    if window_seconds != 8.0 or step_seconds != 2.0:
        raise ValueError("PPG-DaLiA HR labels require an 8 s window and 2 s step")
    if stride < 1 or max_windows < 1:
        raise ValueError("stride and max_windows must be positive")
    data = read_ppg_subject(path)
    bvp, acc = data["bvp"], data["acc"]
    labels = data["heart_labels"]
    activity = data["activity"]
    bvp_window = int(window_seconds * PPG_BVP_RATE_HZ)
    acc_window = int(window_seconds * PPG_ACC_RATE_HZ)
    produced = 0
    for index in range(0, len(labels), stride):
        start_seconds = index * step_seconds
        bvp_start = int(start_seconds * PPG_BVP_RATE_HZ)
        acc_start = int(start_seconds * PPG_ACC_RATE_HZ)
        bvp_stop, acc_stop = bvp_start + bvp_window, acc_start + acc_window
        if bvp_stop > len(bvp) or acc_stop > len(acc):
            break
        reference = float(labels[index])
        if not np.isfinite(reference):
            continue
        activity_slice = activity[int(start_seconds * 4):int((start_seconds + window_seconds) * 4)]
        activity_id = 0
        if len(activity_slice):
            values, counts = np.unique(activity_slice.astype(int), return_counts=True)
            activity_id = int(values[np.argmax(counts)])
        yield {
            "subject": data["subject"],
            "dataset": "ppg-dalia",
            "sampling_hz": PPG_BVP_RATE_HZ,
            "acc_sampling_hz": PPG_ACC_RATE_HZ,
            "reference_hr": reference,
            "activity_id": activity_id,
            "activity": ppg_activity_name(activity_id),
            "start_time": start_seconds,
            "end_time": start_seconds + window_seconds,
            "bvp": bvp[bvp_start:bvp_stop].copy(),
            "acc": acc[acc_start:acc_stop].copy(),
        }
        produced += 1
        if produced >= max_windows:
            break


def _activity_start(activity: np.ndarray, activity_id: int, need: int,
                    offset: int = 0) -> int | None:
    mask = activity.astype(int) == int(activity_id)
    for candidate in (offset, 0):
        start = candidate
        while start + need <= len(mask):
            if mask[start:start + need].all():
                return start
            start += 1
    return None


def find_ppg_activity_window(path: Path, activity_id: int,
                             window_seconds: float = 8.0) -> float | None:
    """Return a start time wholly inside one activity for one participant."""
    data = read_ppg_subject(path)
    start = _activity_start(data["activity"], activity_id, int(window_seconds * 4))
    return start / 4.0 if start is not None else None


def find_pamap2_activity_window(path: Path, activity_id: int,
                                seconds: float = 20.0,
                                offset_seconds: float = 0.0) -> tuple[int, int]:
    data = read_pamap2(path)
    start = _activity_start(data["activity"], activity_id,
                            int(seconds * PAMAP2_RATE_HZ),
                            int(offset_seconds * PAMAP2_RATE_HZ))
    if start is None:
        raise ValueError("No contiguous segment for this activity")
    return start, start + int(seconds * PAMAP2_RATE_HZ)


def read_pamap2_window(path: Path, activity_id: int, seconds: float = 20.0,
                       offset_seconds: float = 0.0) -> dict:
    data = read_pamap2(path)
    needed = int(seconds * PAMAP2_RATE_HZ)
    start = _activity_start(data["activity"], activity_id, needed,
                            int(offset_seconds * PAMAP2_RATE_HZ))
    if start is None:
        raise ValueError("No contiguous segment for this activity")
    stop = start + needed
    hr = data["heart_rate"][start:stop]
    hr = hr[np.isfinite(hr)]
    return {
        "subject": data["subject"], "dataset": "pamap2",
        "sampling_hz": PAMAP2_RATE_HZ, "activity_id": int(activity_id),
        "activity": pamap2_activity_name(activity_id),
        "acc": data["acc"][start:stop].copy(),
        "heart_rate": hr.astype(float),
        "timestamps": data["timestamp"][start:stop].copy(),
    }


def read_ppg_window(path: Path, activity_id: int, seconds: float = 16.0) -> dict:
    data = read_ppg_subject(path)
    start_seconds = _activity_start(data["activity"], activity_id, int(seconds * 4))
    if start_seconds is None:
        raise ValueError("No contiguous segment for this activity")
    start_seconds = start_seconds / 4.0
    bvp_start = int(start_seconds * PPG_BVP_RATE_HZ)
    bvp_stop = bvp_start + int(seconds * PPG_BVP_RATE_HZ)
    acc_start = int(start_seconds * PPG_ACC_RATE_HZ)
    acc_stop = acc_start + int(seconds * PPG_ACC_RATE_HZ)
    label_start = int(start_seconds / 2.0)
    label_stop = int((start_seconds + seconds) / 2.0)
    references = data["heart_labels"][label_start:label_stop]
    references = references[np.isfinite(references)]
    return {
        "subject": data["subject"], "dataset": "ppg-dalia",
        "sampling_hz": PPG_BVP_RATE_HZ, "acc_sampling_hz": PPG_ACC_RATE_HZ,
        "activity_id": int(activity_id), "activity": ppg_activity_name(activity_id),
        "bvp": data["bvp"][bvp_start:bvp_stop].copy(),
        "acc": data["acc"][acc_start:acc_stop].copy(),
        "reference_hr": float(np.median(references)) if len(references) else None,
        "start_time": start_seconds,
    }
