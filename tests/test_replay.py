"""Pure helpers used by scripts/replay_dataset.py.

These tests do not need a dataset or a running server.
"""
import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_replay():
    spec = importlib.util.spec_from_file_location(
        "replay_dataset", ROOT / "scripts" / "replay_dataset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay = load_replay()


def test_fill_missing_interpolates_and_rejects_gaps():
    values = np.array([[1.0, np.nan, 3.0],
                       [np.nan, 2.0, 2.0],
                       [3.0, 4.0, np.nan]])
    filled = replay._fill_missing(values)
    assert filled is not None
    assert np.isfinite(filled).all()
    assert np.allclose(filled[:, 1], [2.0, 2.0, 4.0])

    too_missing = np.array([[np.nan, 1.0], [np.nan, 2.0], [np.nan, 3.0]])
    assert replay._fill_missing(too_missing) is None


def test_contiguous_run_respects_activity_and_offset():
    activity = np.array([0, 0, 4, 4, 4, 0, 4, 4])
    assert replay._contiguous_run(activity, 4, 3) == 2
    assert replay._contiguous_run(activity, 4, 2, offset=2) == 2
    assert replay._contiguous_run(activity, 4, 2, offset=4) == 6
    assert replay._contiguous_run(activity, 4, 4, offset=2) is None


def test_resolve_pamap2_prefers_protocol_over_optional(tmp_path):
    protocol = tmp_path / "PAMAP2_Dataset" / "Protocol" / "subject101.dat"
    optional = tmp_path / "PAMAP2_Dataset" / "Optional" / "subject101.dat"
    protocol.parent.mkdir(parents=True)
    optional.parent.mkdir(parents=True)
    protocol.write_text("")
    optional.write_text("")
    chosen = replay._resolve_pamap2(tmp_path, "101")
    assert "Protocol" in str(chosen)
    assert chosen == protocol


def test_listed_activities_uses_friendly_names():
    text = replay._listed_activities(np.array([0, 0, 4, 4]), replay.pamap2_activity_name)
    assert "0:activity_0" in text
    assert "4:walking" in text


def test_ppg_online_payload_has_no_ecg_and_aligned_rates(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime
    data={'subject':'S13','activity':np.ones(48),
          'bvp':np.sin(np.arange(768)), 'acc':np.zeros((384,3)),
          'heart_labels':np.full(3,9999.),'rpeaks':np.array([9999])}
    monkeypatch.setattr(replay,'_resolve_ppg',lambda *a:None)
    monkeypatch.setattr(replay,'read_ppg_subject',lambda *a:data)
    monkeypatch.setattr(replay,'_pace',lambda *a:None)
    calls=[]
    def post(url,p):
        calls.append((url,p))
        return {'signal':{'available':True,'reliability':.1,'estimated_hr':None}}
    monkeypatch.setattr(replay,'_post_json',post)
    replay._replay_ppg(SimpleNamespace(raw='.',subject='S13',offset=0,seconds=12,activity=1,speed=1),'','device')
    analyses=[p for u,p in calls if u.endswith('/analyze')]
    assert len(analyses)==5
    for p in analyses:
        assert not {'ecg','hr','reference_hr','heart_labels','rpeaks'} & p.keys()
        assert p['sampling_hz']==64 and p['acc_sampling_hz']==32
        assert len(p['ppg'])==512 and len(p['acc'])==256
    assert (datetime.fromisoformat(analyses[1]['window_start'])-datetime.fromisoformat(analyses[0]['window_start'])).total_seconds()==1
    assert all(not p['reading_valid'] and p['heart_rate'] is None for u,p in calls if u.endswith('/telemetry'))


def test_summary_acc_uses_one_second_at_sensor_rate():
    values=np.repeat((np.arange(64)/100)[:,None],3,axis=1)
    mean,valid=replay._summary_acc(values,32)
    assert valid
    np.testing.assert_allclose(mean,[.475]*3)


def test_ppg_requires_explicit_subject_with_or_without_dataset(tmp_path):
    import pytest

    for has_data in (False, True):
        if has_data:
            path = tmp_path / 'PPG_FieldStudy' / 'S13' / 'S13.pkl'
            path.parent.mkdir(parents=True)
            path.touch()
        with pytest.raises(SystemExit) as error:
            replay._resolve_ppg(tmp_path, None)
        assert str(error.value) == 'PPG replay requires --subject (e.g. --subject S13)'
    assert replay._resolve_ppg(tmp_path, 'S13') == path


def test_ppg_cli_requires_subject_before_contacting_server(monkeypatch):
    import pytest

    monkeypatch.setattr('sys.argv', ['replay_dataset.py', '--dataset', 'ppg'])
    def no_network(*args):
        raise AssertionError('Missing subject must fail before network access')
    monkeypatch.setattr(replay, '_get_json', no_network)
    with pytest.raises(SystemExit) as error:
        replay.main()
    assert str(error.value) == 'PPG replay requires --subject (e.g. --subject S13)'
