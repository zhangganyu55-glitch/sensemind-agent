"""Local FastAPI/SQLite replay acceptance; no DeepSeek network calls or ECG inputs."""
import json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from guardian.main import create_app
from guardian.llm import LLMSettings
import replay_dataset as replay


def main():
    output={'transport':'FastAPI TestClient (in-process HTTP, real SQLite and models)', 'runs':[]}
    with tempfile.TemporaryDirectory() as temp:
        client=TestClient(create_app(str(Path(temp)/'replay.db'),llm_settings=LLMSettings(mode='off')))
        output['status']=client.get('/v1/ml/status').json()
        original=replay.read_ppg_subject
        for subject in ['S13','S14','S15']:
            data=original(Path('data/raw/PPG_FieldStudy')/subject/f'{subject}.pkl')
            # Explicit online view: reference labels and R peaks never reach replay.
            online={k:v for k,v in data.items() if k not in ('heart_labels','rpeaks')}
            replay.read_ppg_subject=lambda path:online
            for activity in [1,7]:
                device=f'replay_{subject}_{activity}'
                responses=[]
                def post(url,payload):
                    assert not {'reference_hr','label','rpeaks','ecg','heart_labels'} & payload.keys()
                    r=client.post(url,json=payload)
                    assert r.status_code==200,r.text
                    result=r.json()
                    if url.endswith('/analyze'):
                        from datetime import datetime
                        assert (datetime.fromisoformat(result['window_end'])-datetime.fromisoformat(result['window_start'])).total_seconds()==8
                        responses.append(result)
                    return result
                replay._post_json=post;replay._pace=lambda *args:None
                args=SimpleNamespace(raw='data/raw',subject=subject,offset=0,seconds=20,activity=activity,speed=1000)
                summary=replay._replay_ppg(args,'',device)
                history=client.get('/v1/ml/inferences',params={'device_id':device}).json()['data']
                assert len(history)==13 and len(responses)==13
                agent=client.post('/v1/assistant',json={'device_id':device,'question':'查询模型输出与信号质量'}).json()
                assert agent['tool_used']=='get_model_outputs' and agent['data']
                assert client.get('/console').status_code==200
                summary.update(degraded=sum(not r['signal'].get('hr_available',False) for r in responses),first_response=responses[0],agent_tool=agent['tool_used'],stored_inferences=len(history))
                output['runs'].append(summary)
            del data,online
        def activity_post(url,payload):
            response=client.post(url,json=payload)
            assert response.status_code==200,response.text
            return response.json()
        replay._post_json=activity_post
        args=SimpleNamespace(raw='data/raw',subject='105',offset=0,seconds=12,activity=4,speed=1000)
        output['activity_replay']=replay._replay_pamap2(args,'','replay_pamap2_105')
        assert output['activity_replay']['analyses']==7
    Path('docs/t4-t5-replay-results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':main()
