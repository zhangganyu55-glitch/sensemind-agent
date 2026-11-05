"""Deterministic signal features shared by training and inference.

No model code lives here. Feature names are part of the artifact contract:
changing them requires a new feature/model version.
"""
import numpy as np
from scipy import signal


ACTIVITY_FEATURE_VERSION = "pamap2-wrist-acc-hr-v1"
ACTIVITY_RATE_HZ = 100.0
ACTIVITY_WINDOW_SAMPLES = 512  # 5.12 s at 100 Hz

PPG_FEATURE_VERSION = "ppg-dalia-bvp-acc-v1"
PPG_RATE_HZ = 64.0
PPG_WINDOW_SAMPLES = 512  # 8 s at 64 Hz


def _fill_nan(values: np.ndarray) -> np.ndarray | None:
    values = np.asarray(values, dtype=float)
    if values.ndim == 1:
        values = values[:, None]
    if len(values) < 4:
        return None
    if (np.isnan(values).sum(axis=0) > len(values) * 0.5).any():
        return None
    for column in range(values.shape[1]):
        series = values[:, column]
        missing = np.isnan(series)
        if missing.all():
            return None
        if missing.any():
            index = np.arange(len(series))
            series = series.copy()
            series[missing] = np.interp(index[missing], index[~missing], series[~missing])
            values[:, column] = series
    return values


def resample_columns(values: np.ndarray, target_samples: int) -> np.ndarray:
    values = _fill_nan(values)
    if values is None:
        raise ValueError("Not enough valid samples")
    if len(values) == target_samples:
        return values
    source = np.linspace(0.0, 1.0, len(values))
    target = np.linspace(0.0, 1.0, target_samples)
    return np.column_stack([np.interp(target, source, values[:, column])
                            for column in range(values.shape[1])])


def _stats(series: np.ndarray) -> dict:
    series = np.asarray(series, dtype=float)
    if len(series) == 0 or not np.isfinite(series).any():
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
                "median": 0.0, "q25": 0.0, "q75": 0.0, "rms": 0.0,
                "zcr": 0.0}
    centered = series - np.mean(series)
    return {
        "mean": float(np.mean(series)),
        "std": float(np.std(series)),
        "min": float(np.min(series)),
        "max": float(np.max(series)),
        "median": float(np.median(series)),
        "q25": float(np.percentile(series, 25)),
        "q75": float(np.percentile(series, 75)),
        "rms": float(np.sqrt(np.mean(series ** 2))),
        "zcr": float(np.mean(np.diff(np.signbit(centered).astype(int)) != 0)),
    }


def _spectral(series: np.ndarray, sampling_hz: float) -> dict:
    series = np.asarray(series, dtype=float)
    series = series - np.mean(series)
    if len(series) < 8:
        return {"dominant_hz": 0.0, "entropy": 0.0, "centroid_hz": 0.0}
    window = np.hanning(len(series))
    spectrum = np.abs(np.fft.rfft(series * window)) ** 2
    frequencies = np.fft.rfftfreq(len(series), 1.0 / sampling_hz)
    total = spectrum.sum()
    if total <= 0:
        return {"dominant_hz": 0.0, "entropy": 0.0, "centroid_hz": 0.0}
    probability = spectrum / total
    probability = probability[probability > 0]
    entropy = float(-(probability * np.log(probability)).sum() / np.log(len(spectrum)))
    return {
        "dominant_hz": float(frequencies[int(np.argmax(spectrum))]),
        "entropy": entropy,
        "centroid_hz": float((frequencies * spectrum).sum() / total),
    }


def _band_power(series: np.ndarray, sampling_hz: float,
                low: float, high: float) -> float:
    series = np.asarray(series, dtype=float)
    series = series - np.mean(series)
    if len(series) < 8:
        return 0.0
    spectrum = np.abs(np.fft.rfft(series * np.hanning(len(series)))) ** 2
    frequencies = np.fft.rfftfreq(len(series), 1.0 / sampling_hz)
    mask = (frequencies >= low) & (frequencies < high)
    total = spectrum.sum()
    return float(spectrum[mask].sum() / total) if total > 0 else 0.0


def hjorth(series: np.ndarray) -> tuple[float, float]:
    series = np.asarray(series, dtype=float)
    first = np.diff(series)
    second = np.diff(first)
    variance = np.var(series)
    if variance <= 0 or np.var(first) <= 0:
        return 0.0, 0.0
    mobility = float(np.sqrt(np.var(first) / variance))
    complexity = float(np.sqrt(np.var(second) / np.var(first)) / mobility) if mobility else 0.0
    return mobility, complexity


def _correlation(first, second):
    # Constant axes have undefined correlation; preserve existing zero-filled feature semantics.
    if np.std(first) == 0 or np.std(second) == 0:
        return 0.0
    return np.corrcoef(first, second)[0, 1]


def activity_features(acc: np.ndarray, hr: np.ndarray | None = None) -> tuple[np.ndarray, list[str]]:
    """Features for a roughly 5.12 s wrist-ACC window (any input rate)."""
    acc = resample_columns(np.asarray(acc, dtype=float), ACTIVITY_WINDOW_SAMPLES)
    if acc.shape[1] != 3:
        raise ValueError("acc must have three columns")
    values, names = [], []

    def add(name, value):
        names.append(name)
        values.append(float(value))

    for axis, label in enumerate(("x", "y", "z")):
        for key, value in _stats(acc[:, axis]).items():
            add(f"acc_{label}_{key}", value)
    magnitude = np.linalg.norm(acc, axis=1)
    for key, value in _stats(magnitude).items():
        add(f"acc_mag_{key}", value)
    add("acc_sma", np.mean(np.abs(acc).sum(axis=1)))
    add("acc_jerk_rms", np.sqrt(np.mean(np.diff(magnitude) ** 2)))
    add("acc_corr_xy", _correlation(acc[:, 0], acc[:, 1]))
    add("acc_corr_xz", _correlation(acc[:, 0], acc[:, 2]))
    add("acc_corr_yz", _correlation(acc[:, 1], acc[:, 2]))
    for key, value in _spectral(magnitude, ACTIVITY_RATE_HZ).items():
        add(f"acc_mag_{key}", value)
    for low, high in ((0.5, 3.0), (3.0, 8.0), (8.0, 20.0)):
        add(f"acc_mag_band_{low}_{high}", _band_power(magnitude, ACTIVITY_RATE_HZ, low, high))
    if hr is not None and len(hr) > 0:
        hr_values = np.asarray(hr, dtype=float)
        hr_values = hr_values[np.isfinite(hr_values)]
        if len(hr_values):
            for key, value in _stats(hr_values).items():
                add(f"hr_{key}", value)
            add("hr_slope", np.polyfit(np.arange(len(hr_values)), hr_values, 1)[0]
                if len(hr_values) > 1 else 0.0)
            add("hr_missing", 0.0)
        else:
            for key in ("mean", "std", "min", "max", "median", "q25", "q75", "rms", "zcr"):
                add(f"hr_{key}", 0.0)
            add("hr_slope", 0.0)
            add("hr_missing", 1.0)
    else:
        for key in ("mean", "std", "min", "max", "median", "q25", "q75", "rms", "zcr"):
            add(f"hr_{key}", 0.0)
        add("hr_slope", 0.0)
        add("hr_missing", 1.0)

    vector = np.nan_to_num(np.asarray(values, dtype=np.float32),
                           nan=0.0, posinf=0.0, neginf=0.0)
    return vector, names


def _bandpass(values: np.ndarray, sampling_hz: float,
              low: float = 0.5, high: float = 4.0) -> np.ndarray:
    nyquist = sampling_hz / 2.0
    if not (0 < low < high < nyquist) or len(values) < 32:
        return values - np.mean(values)
    b, a = signal.butter(3, [low / nyquist, high / nyquist], btype="band")
    try:
        return signal.filtfilt(b, a, values)
    except ValueError:
        return values - np.mean(values)


def ppg_features(bvp: np.ndarray, acc: np.ndarray | None = None,
                 sampling_hz: float = PPG_RATE_HZ) -> tuple[np.ndarray, list[str], dict]:
    """Features for an ~8 s BVP window plus optional wrist ACC."""
    bvp = np.asarray(bvp, dtype=float).reshape(-1)
    if len(bvp) < 64 or not np.isfinite(bvp).any():
        raise ValueError("Not enough BVP samples")
    bvp = resample_columns(bvp, PPG_WINDOW_SAMPLES)[:, 0]
    bvp = _bandpass(bvp, PPG_RATE_HZ, 0.5, 4.0)

    values, names = [], []

    def add(name, value):
        names.append(name)
        values.append(float(value))

    for key, value in _stats(bvp).items():
        add(f"bvp_{key}", value)
    centered = bvp - np.mean(bvp)
    std = np.std(centered)
    if std > 0:
        normalized = centered / std
        add("bvp_skew", np.mean(normalized ** 3))
        add("bvp_kurtosis", np.mean(normalized ** 4))
    else:
        add("bvp_skew", 0.0)
        add("bvp_kurtosis", 0.0)
    mobility, complexity = hjorth(bvp)
    add("bvp_hjorth_mobility", mobility)
    add("bvp_hjorth_complexity", complexity)
    for key, value in _spectral(bvp, PPG_RATE_HZ).items():
        add(f"bvp_{key}", value)
    for low, high in ((0.5, 1.5), (1.5, 2.5), (2.5, 4.0)):
        add(f"bvp_band_{low}_{high}", _band_power(bvp, PPG_RATE_HZ, low, high))

    prominence = max(0.2 * std, 1e-9)
    peaks, _ = signal.find_peaks(bvp, distance=int(0.33 * PPG_RATE_HZ),
                                 prominence=prominence)
    rr = np.diff(peaks) / PPG_RATE_HZ
    rr = rr[(rr > 0.33) & (rr < 2.0)]
    beat_count = int(len(peaks))
    ppg_hr = float(60.0 / np.median(rr)) if len(rr) else 0.0
    add("bvp_peak_count", beat_count)
    add("bvp_rr_std", float(np.std(rr)) if len(rr) else 0.0)
    add("bvp_ppg_hr", ppg_hr)
    add("bvp_peak_rate", beat_count / (len(bvp) / PPG_RATE_HZ))

    if acc is not None:
        acc = resample_columns(np.asarray(acc, dtype=float), PPG_WINDOW_SAMPLES)
        if acc.shape[1] != 3:
            raise ValueError("acc must have three columns")
        for axis, label in enumerate(("x", "y", "z")):
            for key, value in _stats(acc[:, axis]).items():
                add(f"acc_{label}_{key}", value)
        magnitude = np.linalg.norm(acc, axis=1)
        for key, value in _stats(magnitude).items():
            add(f"acc_mag_{key}", value)
        add("acc_jerk_rms", np.sqrt(np.mean(np.diff(magnitude) ** 2)))
        add("acc_motion_intensity", float(np.std(magnitude)))
        add("acc_corr_xy", _correlation(acc[:, 0], acc[:, 1]))
        add("acc_corr_xz", _correlation(acc[:, 0], acc[:, 2]))
        add("acc_corr_yz", _correlation(acc[:, 1], acc[:, 2]))
    else:
        for label in ("x", "y", "z"):
            for key in ("mean", "std", "min", "max", "median", "q25", "q75", "rms", "zcr"):
                add(f"acc_{label}_{key}", 0.0)
        for key in ("mean", "std", "min", "max", "median", "q25", "q75", "rms", "zcr"):
            add(f"acc_mag_{key}", 0.0)
        add("acc_jerk_rms", 0.0)
        add("acc_motion_intensity", 0.0)
        for pair in ("xy", "xz", "yz"):
            add(f"acc_corr_{pair}", 0.0)

    vector = np.nan_to_num(np.asarray(values, dtype=np.float32),
                           nan=0.0, posinf=0.0, neginf=0.0)
    meta = {"ppg_hr": ppg_hr, "beat_count": beat_count,
            "sampling_hz": PPG_RATE_HZ, "window_samples": PPG_WINDOW_SAMPLES}
    return vector, names, meta


def reliability_label(ppg_hr: float, reference_hr: float, beat_count: int,
                      max_error_bpm: float = 10.0, min_beats: int = 4) -> int:
    """Proxy label: is PPG-derived HR close to the ECG reference? Not clinical."""
    if beat_count < min_beats or not np.isfinite(ppg_hr) or not np.isfinite(reference_hr):
        return 0
    return int(abs(ppg_hr - reference_hr) <= max_error_bpm)
