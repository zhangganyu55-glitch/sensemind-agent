from datetime import datetime,timezone
import warnings
import numpy as np
import pytest
import httpx
from fastapi.testclient import TestClient
from guardian.main import create_app
from guardian.llm import LLMSettings
from guardian.ml.features import _correlation


def test_modes_persist_and_cannot_mislabel_replay(tmp_path):
    with TestClient(create_app(str(tmp_path/'modes.db'),llm_settings=LLMSettings(mode='off'))) as c:
        base={'timestamp':datetime.now(timezone.utc).isoformat(),'device_id':'replay','source':'simulator','dataset':'ppg-dalia'}
        assert c.post('/v1/telemetry',json={**base,'data_mode':'live'}).status_code==422
        assert c.post('/v1/telemetry',json=base).status_code==200
        assert c.get('/v1/live/status',params={'device_id':'replay'}).json()['data_mode']=='replay'
        assert c.get('/v1/history',params={'device_id':'replay'}).json()[-1]['data_mode']=='replay'
        live={**base,'device_id':'future_esp32','source':'esp32','dataset':None,'data_mode':'live'}
        assert c.post('/v1/telemetry',json=live).status_code==200
        assert c.get('/v1/live/status',params={'device_id':'future_esp32'}).json()['data_mode']=='live'
        assert c.get('/v1/runtime/status').json()['hardware_tested'] is False
        html=c.get('/console').text
        for label in ['Replay Mode','Live Mode','runtime-health','无硬件连接证据']:
            assert label in html


def test_constant_correlation_has_no_division_warning():
    with warnings.catch_warnings():
        warnings.simplefilter('error',RuntimeWarning)
        assert _correlation(np.ones(10),np.arange(10))==0
        assert _correlation(np.ones(10),np.ones(10))==0
    assert _correlation(np.arange(10),np.arange(10))==pytest.approx(1)


@pytest.mark.parametrize('error,code',[(httpx.ConnectError('test'),'network_error'),(httpx.ReadTimeout('test'),'timeout')])
def test_network_failure_falls_back_with_records(tmp_path,error,code):
    def handler(request):raise error
    with TestClient(create_app(str(tmp_path/'failure.db'),llm_settings=LLMSettings(mode='deepseek',api_key='synthetic-secret'),llm_transport=httpx.MockTransport(handler))) as c:
        response=c.post('/v1/assistant',json={'device_id':'fixture','question':'查询模型输出和信号质量'}).json()
        assert response['llm_error']==code
        assert response['agent_mode']=='deterministic_tool_router_no_llm'
        assert response['data']==[]
        assert 'synthetic-secret' not in str(response)


def test_esp32_rules_stay_disabled_without_opt_in(tmp_path):
    with TestClient(create_app(str(tmp_path/'live.db'),llm_settings=LLMSettings(mode='off'),allow_real_rules=False)) as c:
        r=c.post('/v1/telemetry',json={'device_id':'future_esp32','source':'esp32','heart_rate':200,'reading_valid':True,'signal_quality':'good','timestamp':datetime.now(timezone.utc).isoformat()})
        assert r.json()['evaluation']=='real_data_rules_disabled'
        assert not r.json()['transitions']


def test_replay_telemetry_normalizes_units_without_changing_inference(monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    spec=importlib.util.spec_from_file_location('replay_units',Path(__file__).resolve().parents[1]/'scripts/replay_dataset.py')
    replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)
    calls=[]
    monkeypatch.setattr(replay,'_pace',lambda *a:None)
    monkeypatch.setattr(replay,'_post_json',lambda url,p:(calls.append((url,p)) or {}))
    monkeypatch.setattr(replay,'_resolve_pamap2',lambda *a:None)
    monkeypatch.setattr(replay,'read_pamap2',lambda *a:{'subject':'105','activity':np.ones(600),'timestamp':np.arange(600)/100,'acc':np.tile([0,0,9.80665],(600,1)),'heart_rate':np.full(600,75.)})
    replay._replay_pamap2(SimpleNamespace(raw='.',subject='105',offset=0,seconds=6,activity=1,speed=1),'','fixture')
    inference=next(p for u,p in calls if u.endswith('/analyze'))
    telemetry=next(p for u,p in calls if u.endswith('/telemetry'))
    assert inference['acc'][0][2]==pytest.approx(9.80665)
    assert telemetry['acc_z']==pytest.approx(1)
    assert telemetry['data_mode']=='replay'
