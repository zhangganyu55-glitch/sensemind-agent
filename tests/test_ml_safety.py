import json
from datetime import datetime, timezone
import joblib
import numpy as np
import pytest
from fastapi.testclient import TestClient
from guardian.main import create_app
from guardian.llm import LLMSettings
from guardian.ml.registry import SensorModels


def payload():
    return dict(device_id='safety', window_start=datetime.now(timezone.utc).isoformat(),
                dataset='ppg-dalia', participant_id='S13', sampling_hz=64, acc_sampling_hz=32,
                ppg=np.sin(np.arange(512)).tolist(), acc=np.zeros((256,3)).tolist())


@pytest.mark.parametrize('change', [{'reference_hr':72}, {'ecg':[1]}, {'acc':[[1,2]]}, {'ppg':[float('nan')]}, {'ppg':[float('inf')]}, {'hr':[72.]}, {'window_start':'2999-01-01T00:00:00Z'}])
def test_api_rejects_reference_and_malformed(tmp_path, change):
    c=TestClient(create_app(str(tmp_path/'test.db'),llm_settings=LLMSettings(mode='off')))
    p=payload();p.update(change)
    assert c.post('/v1/ml/analyze', content=json.dumps(p),headers={'content-type':'application/json'}).status_code==422


def test_corrupt_and_wrong_contract_are_unavailable(tmp_path):
    (tmp_path/'ppg').mkdir()
    (tmp_path/'ppg/ppg_hr_rf.joblib').write_bytes(b'broken')
    joblib.dump({'model':None},tmp_path/'ppg/ppg_quality_rf.joblib')
    status=SensorModels(tmp_path).status()
    assert not any(v['available'] for v in status['models'].values())


def test_domain_and_timestamp(tmp_path):
    m=SensorModels(tmp_path);p=payload();r=m.analyze(p)
    assert not r['activity']['available']
    assert (datetime.fromisoformat(r['window_end'])-datetime.fromisoformat(r['window_start'])).total_seconds()==8
    assert not r['available']


def test_poor_quality_suppresses_hr():
    class Model:
        n_features_in_=64
        classes_=[0,1]
        def predict_proba(self,x):return np.array([[.9,.1]])
        def predict(self,x):return np.array([72.])
    m=SensorModels();m._loaded=True
    m.ppg_quality={'model':Model()};m.ppg_hr={'model':Model()}
    r=m.analyze(payload())['signal']
    assert r['available'] and r['estimated_hr'] is None and not r['hr_available']
    p=payload();p['acc']=[]
    assert not m.analyze(p)['signal']['available']
    p=payload();p['ppg']=[0.]*512
    assert not m.analyze(p)['signal']['available']
