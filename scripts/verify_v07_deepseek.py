"""Real DeepSeek acceptance using ONLY generated fictional monitoring records."""
import json,sys,tempfile,time
import numpy as np
from pathlib import Path
from datetime import datetime,timezone,timedelta
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from guardian.main import create_app
from guardian.llm import LLMSettings
from guardian.storage import Storage
from guardian.ml.registry import SensorModels

def main():
    settings=LLMSettings.from_env()
    if not settings.enabled:raise SystemExit('DeepSeek not configured')
    out={'data_policy':'Only generated fictional records; no PPG-DaLiA, ECG, real device or personal health records sent.','model':settings.model,'real_provider':True}
    with tempfile.TemporaryDirectory() as temp:
        db=str(Path(temp)/'synthetic.db')
        with TestClient(create_app(db,llm_settings=settings,investigation_deadline_seconds=60)) as c:
            store=Storage(db);now=datetime.now(timezone.utc)-timedelta(seconds=10)
            models=SensorModels();models.load()
            acc=np.random.default_rng(7).normal(0,.01,(256,3));acc[:,2]+=1
            signal=models._ppg(np.sin(2*np.pi*1.2*np.arange(512)/64),acc,64)
            assert signal['available']
            out['synthetic_model_fixture']={'origin':'Generated 1.2 Hz sinusoid + seeded synthetic wrist ACC; real trained weights, no personal measurements.','signal':signal}
            store.put_ml_inference({'device_id':'fictional_v07','window_start':now.isoformat(),'dataset':'synthetic','participant_id':'fictional','activity_label':None,'signal_reliability':signal['reliability'],'signal':signal,'model_version':signal['hr_model_version']})
            # One normal synthetic history row, so the first forced status read has data.
            base={'device_id':'fictional_v07','source':'simulator','heart_rate':75,'reading_valid':True,'signal_quality':'good','timestamp':now.isoformat()}
            assert c.post('/v1/telemetry',json=base).status_code==200
            start=time.monotonic()
            a=c.post('/v1/assistant',json={'device_id':'fictional_v07','question':'这是完全虚构的软件测试。请先查 get_model_outputs 获取心率估计与信号质量，再查 get_history 历史记录，不要诊断。'}).json()
            out['assistant']={k:a.get(k) for k in ['agent_mode','model','usage','llm_error','response']}
            out['assistant']['tools']=[x['tool'] for x in a.get('tools_used',[])]
            out['assistant']['elapsed_seconds']=time.monotonic()-start
            print('assistant',out['assistant'],flush=True)
            for i in range(3):
                row={**base,'timestamp':(now+timedelta(seconds=i+1)).isoformat(),'heart_rate':130}
                assert c.post('/v1/telemetry',json=row).status_code==200
            events=c.get('/v1/events',params={'device_id':'fictional_v07'}).json()
            event=next(e for e in events if e['kind']=='high_heart_rate')
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                response=c.get(f'/v1/events/{event["id"]}/investigation')
                inv=response.json() if response.status_code==200 else {}
                if inv.get('status') in ('completed','failed'):break
                time.sleep(.25)
            report=inv.get('report') or {}
            out['investigation']={'status':inv.get('status'),'ai_status':inv.get('ai_status'),'error':inv.get('error'),'analysis_source':report.get('analysis_source'),'tools':[x.get('tool') for x in report.get('tool_calls',[])],'human_acknowledged':event['acknowledged'],'evidence_report':report}
            out['status']='done' if a.get('agent_mode')=='llm_tool_agent' and 'get_model_outputs' in out['assistant']['tools'] and 'get_history' in out['assistant']['tools'] and inv.get('ai_status')=='completed' else 'failed'
    (ROOT/'docs/v07-deepseek.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    print('status',out['status'],flush=True)
    if out['status']!='done':raise SystemExit(1)
if __name__=='__main__':main()
