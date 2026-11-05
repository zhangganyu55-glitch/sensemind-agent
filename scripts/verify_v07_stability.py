"""600 wall-clock seconds, real loopback HTTP/uvicorn and held-out raw PPG."""
import hashlib, json, os, socket, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
import httpx
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import replay_dataset as replay

def main():
    local=ROOT/'.runtime/v07';local.mkdir(parents=True,exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    env={**os.environ,'GUARDIAN_DB':str(local/f'stability-{port}.db'),'GUARDIAN_LLM_MODE':'off'}
    latencies=[];counts={};errors=[]
    source_hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['guardian/models.py', 'guardian/agent.py', 'guardian/factory.py', 'guardian/detection.py', 'guardian/console.py', 'guardian/ml/features.py', 'scripts/replay_dataset.py']}
    with (local/'server.log').open('w') as log, httpx.Client(trust_env=False,timeout=15) as client:
        server=subprocess.Popen([sys.executable,'-m','uvicorn','guardian.main:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
        try:
            for _ in range(100):
                try:
                    if client.get(url+'/health').is_success:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:raise RuntimeError('server startup failed')
            def post(endpoint,payload):
                started=time.monotonic();response=client.post(endpoint,json=payload)
                latencies.append(time.monotonic()-started)
                counts[endpoint.rsplit('/',1)[-1]]=counts.get(endpoint.rsplit('/',1)[-1],0)+1
                if response.status_code!=200:
                    errors.append(response.status_code);response.raise_for_status()
                return response.json()
            replay._post_json=post
            started=time.monotonic()
            args=SimpleNamespace(raw=str(ROOT/'data/raw'),subject='S13',offset=0,seconds=600,activity=8,speed=1)
            summary=replay._replay_ppg(args,url,f'v07_stability_{port}')
            elapsed=time.monotonic()-started
            assert elapsed>=600 and summary['ticks']==600 and summary['analyses']==593
            result={'status':'done','source_sha256':source_hashes,'transport':'real loopback HTTP + uvicorn + SQLite','wall_seconds_including_load':elapsed,'paced_seconds':600,'summary':summary,'requests':counts,'http_errors':errors,'max_request_latency_seconds':max(latencies),'models':client.get(url+'/v1/ml/status').json(),'runtime':client.get(url+'/v1/runtime/status').json()}
            assert not errors
            (ROOT/'docs/v07-stability.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        finally:
            server.terminate();server.wait(timeout=15) # Only the child created by this script.
if __name__=='__main__':main()
