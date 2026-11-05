#!/usr/bin/env python
"""Replay one participant/activity run through the ML API and telemetry.

The stream never stitches different participants or activities: it picks a
single contiguous run of one activity for one participant, feeds trailing
windows to `/v1/ml/analyze`, and sends a 1 Hz summary to `/v1/telemetry`.
Provenance (`dataset`, `participant_id`, `sampling_hz`) is kept on every
telemetry row; replay timestamps are anchored in the recent past so a replay
can run faster than real time without tripping the future-clock guard.

Examples:
    python scripts/replay_dataset.py --dataset pamap2 --subject 101 --activity 4 --seconds 60
    python scripts/replay_dataset.py --dataset ppg --subject S1 --activity 7 --seconds 60 --speed 4
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.ml.data import (  # noqa: E402
    PAMAP2_RATE_HZ, PPG_ACC_RATE_HZ, PPG_BVP_RATE_HZ,
    pamap2_activity_name, pamap2_files, ppg_activity_name, ppg_files,
    read_pamap2, read_ppg_subject,
)

PAMAP2_WINDOW_SAMPLES = 512  # 5.12 s at 100 Hz, the training window
PPG_WINDOW_SECONDS = 8.0
ACC_LIMIT = 32.0  # Telemetry contract bounds


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    request = Request(url, data=json.dumps(payload, allow_nan=False).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _get_json(url: str, timeout: float = 10.0) -> dict:
    with urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _fill_missing(values, max_missing_fraction: float = 0.5):
    """Interpolate NaNs per column; return None when too much is missing."""
    values = np.asarray(values, dtype=float)
    if values.ndim == 1:
        values = values[:, None]
    if len(values) == 0:
        return None
    missing = ~np.isfinite(values)
    if missing.all(axis=0).any() or missing.mean(axis=0).max() > max_missing_fraction:
        return None
    filled = values.copy()
    index = np.arange(len(filled))
    for column in range(filled.shape[1]):
        mask = missing[:, column]
        if mask.any():
            filled[mask, column] = np.interp(index[mask], index[~mask],
                                             filled[~mask, column])
    return filled


def _contiguous_run(activity: np.ndarray, activity_id: int, need: int,
                    offset: int = 0) -> int | None:
    mask = np.nan_to_num(np.asarray(activity, dtype=float)).astype(int) == int(activity_id)
    start = max(0, offset)
    while start + need <= len(mask):
        if mask[start:start + need].all():
            return start
        start += 1
    return None


def _listed_activities(activity: np.ndarray, name_of) -> str:
    values = np.nan_to_num(np.asarray(activity, dtype=float))
    ids = sorted({int(value) for value in values})
    return ", ".join(f"{value}:{name_of(value)}" for value in ids)


def _unique_device(base_url: str, base: str) -> str:
    """Pick an unused device id so rerunning a replay never hits 409s."""
    candidates = [base] + [f"{base}_{index}" for index in range(2, 100)]
    for candidate in candidates:
        status = _get_json(base_url.rstrip("/") + "/v1/live/status?"
                           + urlencode({"device_id": candidate}))
        if status.get("latest") is None:
            return candidate
    raise SystemExit("No free replay device id; pass --device explicitly.")


def _resolve_pamap2(raw: Path, subject: str | None) -> Path:
    files = [path for path in pamap2_files(raw) if "Protocol" in str(path)]
    if not files:
        raise SystemExit(f"No PAMAP2 files under {raw}. Extract the dataset first.")
    if subject:
        for path in files:
            if path.stem == f"subject{subject}":
                return path
        for path in pamap2_files(raw):
            if path.stem == f"subject{subject}":
                return path
        raise SystemExit(f"subject{subject} not found. Available: "
                         + ", ".join(sorted(path.stem for path in pamap2_files(raw))))
    return files[0]


def _resolve_ppg(raw: Path, subject: str | None) -> Path:
    if not subject:
        raise SystemExit("PPG replay requires --subject (e.g. --subject S13)")
    files = ppg_files(raw)
    if not files:
        raise SystemExit(f"No PPG-DaLiA files under {raw}. Extract the dataset first.")
    if subject:
        for path in files:
            if path.stem in (subject, f"S{subject}"):
                return path
        raise SystemExit(f"Subject {subject} not found. Available: "
                         + ", ".join(sorted(path.stem for path in files)))


def _summary_acc(acc: np.ndarray, sampling_hz: int = 100) -> tuple[list[float], bool]:
    """Mean of the latest second, clamped to the telemetry contract."""
    tail = acc[-sampling_hz:]
    finite = tail[np.isfinite(tail).all(axis=1)]
    if len(finite) == 0:
        return [0.0, 0.0, 0.0], False
    mean = finite.mean(axis=0)
    return [float(np.clip(value, -ACC_LIMIT, ACC_LIMIT)) for value in mean], True


def _replay_pamap2(args, base_url: str, device: str) -> dict:
    path = _resolve_pamap2(Path(args.raw), args.subject)
    data = read_pamap2(path)
    rate = int(PAMAP2_RATE_HZ)
    offset = int(args.offset * rate)
    max_seconds = max(0, (len(data["timestamp"]) - offset) // rate)
    seconds = min(args.seconds, max_seconds)
    if seconds < 1:
        raise SystemExit("Not enough samples for a replay.")
    run_start = _contiguous_run(data["activity"], args.activity, seconds * rate, offset)
    if run_start is None:
        raise SystemExit(
            f"Activity {args.activity} ({pamap2_activity_name(args.activity)}) has no "
            f"contiguous {seconds}s run for subject {data['subject']}. "
            f"Available: {_listed_activities(data['activity'], pamap2_activity_name)}")
    available = (len(data["timestamp"]) - run_start) // rate
    if available < seconds:
        seconds = available
        if seconds < 1:
            raise SystemExit("Not enough samples after the activity run start.")
    if seconds < args.seconds:
        print(f"[replay] only {seconds}s available; replay shortened from {args.seconds}s")

    anchor = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    counts, analysis_ok = {}, 0
    for tick in range(1, seconds + 1):
        end = run_start + tick * rate
        begin = max(run_start, end - PAMAP2_WINDOW_SAMPLES)
        acc = data["acc"][begin:end]
        filled = _fill_missing(acc)
        hr = data["heart_rate"][begin:end]
        hr = hr[np.isfinite(hr)]
        timestamp = anchor + timedelta(seconds=tick)
        window_start = timestamp - timedelta(seconds=len(acc) / PAMAP2_RATE_HZ)

        ml_result = None
        if filled is not None and len(acc) >= PAMAP2_WINDOW_SAMPLES:
            payload = {
                "device_id": device,
                "window_start": window_start.isoformat(),
                "dataset": "pamap2",
                "participant_id": data["subject"],
                "sampling_hz": PAMAP2_RATE_HZ,
                "acc": filled.tolist(),
                "hr": hr.tolist(),
            }
            ml_result = _post_json(base_url + "/v1/ml/analyze", payload)
            analysis_ok += 1
            label = ml_result.get("activity_label")
            if label:
                counts[label] = counts.get(label, 0) + 1

        acc_summary, acc_ok = _summary_acc(acc / 9.80665)
        heart_rate = float(np.median(hr)) if len(hr) else None
        quality = "good" if (acc_ok and heart_rate is not None) else "poor"
        telemetry = {
            "device_id": device,
            "timestamp": timestamp.isoformat(),
            "source": "simulator",
            "data_mode": "replay",
            "heart_rate": round(heart_rate, 1) if heart_rate is not None else None,
            "acc_x": acc_summary[0], "acc_y": acc_summary[1], "acc_z": acc_summary[2],
            "signal_quality": quality,
            "reading_valid": quality == "good",
            "dataset": "pamap2",
            "participant_id": data["subject"],
            "sampling_hz": PAMAP2_RATE_HZ,
        }
        result = _post_json(base_url + "/v1/telemetry", telemetry)
        activity = (ml_result or {}).get("activity") or {}
        print(f"[{tick:03d}/{seconds}] hr={heart_rate if heart_rate is not None else '-'} "
              f"activity={activity.get('label', '-')} "
              f"conf={activity.get('confidence', '-')} "
              f"acc_ok={acc_ok} transitions={result.get('transitions', [])}")
        _pace(args.speed, tick)

    return {"device": device, "ticks": seconds, "analyses": analysis_ok,
            "activities": counts, "subject": data["subject"],
            "dataset": "pamap2"}


def _replay_ppg(args, base_url: str, device: str) -> dict:
    path = _resolve_ppg(Path(args.raw), args.subject)
    data = read_ppg_subject(path)
    rate = int(PPG_BVP_RATE_HZ)
    offset = int(args.offset * 4)
    max_seconds = max(0, (len(data["activity"]) - offset) // 4)
    seconds = min(args.seconds, max_seconds)
    if seconds < 1:
        raise SystemExit("Not enough samples for a replay.")
    run_start = _contiguous_run(data["activity"], args.activity, seconds * 4, offset)
    if run_start is None:
        raise SystemExit(
            f"Activity {args.activity} ({ppg_activity_name(args.activity)}) has no "
            f"contiguous {seconds}s run for subject {data['subject']}. "
            f"Available: {_listed_activities(data['activity'], ppg_activity_name)}")
    available = (len(data["activity"]) - run_start) // 4
    if available < seconds:
        seconds = available
        if seconds < 1:
            raise SystemExit("Not enough samples after the activity run start.")
    if seconds < args.seconds:
        print(f"[replay] only {seconds}s available; replay shortened from {args.seconds}s")

    run_bvp = run_start * rate // 4             # 4 Hz activity grid -> 64 Hz BVP grid
    run_acc = run_start * int(PPG_ACC_RATE_HZ) // 4  # -> 32 Hz ACC grid
    anchor = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    analysis_ok = 0
    global _PACING_START
    _PACING_START = time.monotonic()
    for tick in range(1, seconds + 1):
        bvp_end = run_bvp + tick * rate
        bvp_begin = max(run_bvp, bvp_end - int(PPG_WINDOW_SECONDS * rate))
        acc_end = run_acc + tick * int(PPG_ACC_RATE_HZ)
        acc_begin = max(run_acc, acc_end - int(PPG_WINDOW_SECONDS * PPG_ACC_RATE_HZ))
        bvp = data["bvp"][bvp_begin:bvp_end]
        acc = data["acc"][acc_begin:acc_end]
        bvp_filled = _fill_missing(bvp)
        acc_filled = _fill_missing(acc) if acc.ndim == 2 else None
        timestamp = anchor + timedelta(seconds=tick)
        window_start = timestamp - timedelta(seconds=len(bvp) / PPG_BVP_RATE_HZ)

        ml_result = None
        if bvp_filled is not None and len(bvp) >= int(PPG_WINDOW_SECONDS * rate):
            payload = {
                "device_id": device,
                "window_start": window_start.isoformat(),
                "dataset": "ppg-dalia",
                "participant_id": data["subject"],
                "sampling_hz": PPG_BVP_RATE_HZ,
                "acc_sampling_hz": PPG_ACC_RATE_HZ,
                "ppg": bvp_filled[:, 0].tolist(),
                "acc": acc_filled.tolist() if acc_filled is not None else [],
            }
            ml_result = _post_json(base_url + "/v1/ml/analyze", payload)
            analysis_ok += 1

        signal = (ml_result or {}).get("signal") or {}
        reliability = signal.get("reliability")
        if reliability is None:
            quality = "unknown"
        elif reliability >= 0.7:
            quality = "good"
        elif reliability >= 0.4:
            quality = "poor"
        else:
            quality = "unknown"
        heart_rate = signal.get("estimated_hr") if signal.get("available") else None
        acc_summary, acc_ok = _summary_acc(acc, int(PPG_ACC_RATE_HZ)) if acc.ndim == 2 else ([0.0] * 3, False)
        telemetry = {
            "device_id": device,
            "timestamp": timestamp.isoformat(),
            "source": "simulator",
            "data_mode": "replay",
            "heart_rate": heart_rate,
            "acc_x": acc_summary[0], "acc_y": acc_summary[1], "acc_z": acc_summary[2],
            "signal_quality": quality,
            "reading_valid": quality == "good" and heart_rate is not None,
            "dataset": "ppg-dalia",
            "participant_id": data["subject"],
            "sampling_hz": PPG_BVP_RATE_HZ,
        }
        result = _post_json(base_url + "/v1/telemetry", telemetry)
        print(f"[{tick:03d}/{seconds}] hr={heart_rate if heart_rate is not None else '-'} "
              f"quality={quality} reliability={reliability} "
              f"acc_ok={acc_ok} transitions={result.get('transitions', [])}")
        _pace(args.speed, tick)

    return {"device": device, "ticks": seconds, "analyses": analysis_ok,
            "subject": data["subject"], "dataset": "ppg-dalia",
            "source_start_seconds": run_start / 4, "acc_sampling_hz": 32,
            "inference_step_seconds": 1, "ecg_reference_input": False}


def _pace(speed: float, tick: int) -> None:
    target = _PACING_START + tick / speed
    delay = target - time.monotonic()
    if delay > 0:
        time.sleep(delay)


_PACING_START = time.monotonic()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--raw", default=str(ROOT / "data" / "raw"))
    parser.add_argument("--dataset", choices=("pamap2", "ppg"), default="pamap2")
    parser.add_argument("--subject", default=None,
                        help="Required for PPG replay (e.g. --subject S13); optional PAMAP2 subject number (e.g. 105)")
    parser.add_argument("--activity", type=int, default=None,
                        help="Activity id; defaults to walking (PAMAP2 4, PPG 7)")
    parser.add_argument("--seconds", type=int, default=60, help="Seconds to replay")
    parser.add_argument("--offset", type=float, default=0.0,
                        help="Seconds to skip before searching for the activity run")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Realtime factor; 4 means four stream seconds per wall second")
    parser.add_argument("--device", default=None,
                        help="Device id to write to; defaults to an unused guardian_replay*")
    args = parser.parse_args()
    if args.dataset == "ppg" and not args.subject:
        raise SystemExit("PPG replay requires --subject (e.g. --subject S13)")
    if args.speed <= 0:
        raise SystemExit("--speed must be positive")
    if args.seconds < 1:
        raise SystemExit("--seconds must be at least 1")
    if args.activity is None:
        args.activity = 4 if args.dataset == "pamap2" else 7

    base_url = args.url.rstrip("/")
    try:
        _get_json(base_url + "/health")
    except (URLError, HTTPError) as error:
        raise SystemExit(f"Cannot reach {base_url}: {error}. Start guardian.main first.")
    device = args.device or _unique_device(base_url, "guardian_replay")

    global _PACING_START
    _PACING_START = time.monotonic()
    try:
        if args.dataset == "pamap2":
            summary = _replay_pamap2(args, base_url, device)
        else:
            summary = _replay_ppg(args, base_url, device)
    except HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {error.code} from {error.url}: {body}")
    except URLError as error:
        raise SystemExit(f"Cannot reach {base_url}: {error.reason}")

    print("[replay] done " + json.dumps(summary, ensure_ascii=False))
    print(f"[replay] events: {base_url}/v1/events?device_id={device}")
    print(f"[replay] console: {base_url}/console   (device {device})")
    print(f"[replay] inferences: {base_url}/v1/ml/inferences?device_id={device}")


if __name__ == "__main__":
    main()
