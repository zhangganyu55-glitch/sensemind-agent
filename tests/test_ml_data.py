"""Synthetic-file tests for dataset loaders and participant windows."""
import pickle
from pathlib import Path

import numpy as np
import pytest

from guardian.ml.data import (iter_pamap2_windows, iter_ppg_windows,
                              read_pamap2, read_ppg_subject)


def test_pamap2_loader_windows_and_subject(tmp_path):
    rows = 600
    data = np.zeros((rows, 54))
    data[:, 0] = np.arange(rows) / 100.0
    data[:, 1] = 4  # walking
    data[:, 2] = 100.0
    data[:, 4:7] = np.random.default_rng(0).normal(size=(rows, 3))
    path = tmp_path / "subject101.dat"
    np.savetxt(path, data)

    loaded = read_pamap2(path)
    assert loaded["subject"] == "101"
    assert loaded["sampling_hz"] == 100.0
    windows = list(iter_pamap2_windows(path, window_samples=100, step_samples=50))
    assert windows
    assert windows[0]["activity"] == "walking"
    assert windows[0]["subject"] == "101"
    assert windows[0]["acc"].shape == (100, 3)


def test_ppg_loader_windows_and_reference(tmp_path):
    seconds = 20
    bvp = np.sin(2 * np.pi * 1.2 * np.arange(seconds * 64) / 64.0)
    acc = np.zeros((seconds * 32, 3))
    subject = {
        "subject": "S1",
        "signal": {"wrist": {"BVP": bvp.reshape(-1, 1), "ACC": acc}},
        "label": np.full(8, 72.0),
        "activity": np.full(seconds * 4, 1.0),
        "rpeaks": np.array([], dtype=int),
    }
    path = tmp_path / "S1.pkl"
    with open(path, "wb") as handle:
        pickle.dump(subject, handle)

    loaded = read_ppg_subject(path)
    assert loaded["subject"] == "S1"
    assert loaded["bvp"].shape == (seconds * 64,)
    windows = list(iter_ppg_windows(path, window_seconds=8, step_seconds=2,
                                    max_windows=2))
    assert windows
    assert windows[0]["reference_hr"] == 72.0
    assert windows[0]["activity"] == "sitting"
    assert len(windows[0]["bvp"]) == 8 * 64
    assert windows[0]["acc"].shape == (8 * 32, 3)


def _mock_ppg(monkeypatch, *, seconds=20, labels=None, acc_samples=None):
    data = {
        "subject": "S1",
        "bvp": np.arange(seconds * 64, dtype=float),
        "acc": np.repeat(np.arange(acc_samples or seconds * 32)[:, None], 3, axis=1),
        "heart_labels": np.arange(7, dtype=float) + 60 if labels is None else np.array(labels),
        # Sitting before t=8, walking afterwards.
        "activity": np.r_[np.ones(8 * 4), np.full((seconds - 8) * 4, 7)],
    }
    monkeypatch.setattr("guardian.ml.data.read_ppg_subject", lambda path: data)
    return data


def test_ppg_label_grid_has_no_offset_and_includes_final_full_window(monkeypatch):
    data = _mock_ppg(monkeypatch)
    windows = list(iter_ppg_windows(Path("S1.pkl")))
    assert len(windows) == 7
    for index, window in enumerate(windows):
        assert window["start_time"] == index * 2
        assert window["end_time"] == index * 2 + 8
        assert window["reference_hr"] == 60 + index
        np.testing.assert_array_equal(window["bvp"], data["bvp"][index * 128:index * 128 + 512])
        np.testing.assert_array_equal(window["acc"], data["acc"][index * 64:index * 64 + 256])
    assert windows[0]["activity"] == "sitting"
    assert windows[4]["activity"] == "walking"


def test_ppg_stride_and_invalid_labels_preserve_original_timestamps(monkeypatch):
    _mock_ppg(monkeypatch, labels=[60, 61, np.nan, 63, 64, 65, 66])
    windows = list(iter_ppg_windows(Path("S1.pkl"), stride=2, max_windows=2))
    assert [w["reference_hr"] for w in windows] == [60, 64]
    assert [w["start_time"] for w in windows] == [0, 8]
    assert windows[1]["bvp"][0] == 8 * 64
    assert windows[1]["acc"][0, 0] == 8 * 32


@pytest.mark.parametrize("acc_samples", [20 * 32, 19 * 32])
def test_ppg_stops_at_shortest_signal_even_with_extra_labels(monkeypatch, acc_samples):
    _mock_ppg(monkeypatch, labels=np.arange(10) + 60, acc_samples=acc_samples)
    windows = list(iter_ppg_windows(Path("S1.pkl")))
    assert len(windows) == (7 if acc_samples == 20 * 32 else 6)
    assert all(len(w["bvp"]) == 512 and len(w["acc"]) == 256 for w in windows)


@pytest.mark.parametrize("kwargs", [
    {"window_seconds": 4}, {"step_seconds": 4}, {"stride": 0}, {"max_windows": 0},
])
def test_ppg_rejects_parameters_that_break_published_grid(kwargs):
    with pytest.raises(ValueError):
        list(iter_ppg_windows(Path("does-not-need-to-exist.pkl"), **kwargs))
