"""Offline ECG comparison only. Never invokes Agent or external API."""
import json,sys
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from guardian.ml.data import read_ppg_subject
from guardian.ml.features import ppg_features,reliability_label
from guardian.ml.registry import SensorModels

def main():
    runs=json.loads((ROOT/'docs/t4-t5-replay-results.json').read_text())['runs']
    models=SensorModels();models.load();rows=[];segments=[]
    for subject in ['S13','S14','S15']:
        d=read_ppg_subject(ROOT/f'data/raw/PPG_FieldStudy/{subject}/{subject}.pkl')
        for run in [x for x in runs if x['subject']==subject]:
            segment=[];longest=current=0
            for tick in range(8,21):
                start=run['source_start_seconds']+tick-8
                b=d['bvp'][round(start*64):round((start+8)*64)]
                a=d['acc'][round(start*32):round((start+8)*32)]
                # Reference is calculated AFTER extracting inference inputs, offline only.
                peaks=d['rpeaks'][(d['rpeaks']>=start*700)&(d['rpeaks']<(start+8)*700)]
                reference=float(np.mean(60*700/np.diff(peaks)))
                vector,names,meta=ppg_features(b,a,64)
                prediction=float(models.ppg_hr['model'].predict([vector])[0])
                signal=models._ppg(b,a,64);accepted=bool(signal.get('hr_available'))
                current=0 if accepted else current+1;longest=max(longest,current)
                row={'subject':subject,'activity':run['device'].rsplit('_',1)[-1],'tick':tick,'source_start_seconds':start,'reference_ecg_hr':reference,'offline_unfiltered_hr':prediction,'absolute_hr_error':abs(prediction-reference),'proxy_target':reliability_label(meta['ppg_hr'],reference,meta['beat_count']),'reliability':signal.get('reliability'),'accepted':accepted,'hidden_reason':signal.get('degraded_reason')}
                rows.append(row);segment.append(row)
            segments.append({'subject':subject,'activity':segment[0]['activity'],'windows':13,'accepted':sum(r['accepted'] for r in segment),'longest_hidden_update_slots_seconds':longest,'longest_initial_unavailable_including_warmup_seconds':next((r['tick']-1 for r in segment if r['accepted']),20)})
        del d
    accepted=[r for r in rows if r['accepted']];hidden=[r for r in rows if not r['accepted']]
    assert len(rows)==78 and len(hidden)==46
    result={'threshold':.7,'reference_method':'Offline mean instantaneous ECG HR from corrected R peaks within exact 8 s windows; not passed to inference or external model.','windows':78,'accepted':len(accepted),'hidden':len(hidden),'output_ratio':len(accepted)/78,'segments':segments,'mae':{label:float(np.mean([r['absolute_hr_error'] for r in subset])) for label,subset in [('all_unfiltered',rows),('accepted',accepted),('hidden_unfiltered',hidden)]},'hidden_with_regressor_error_le_10':sum(r['absolute_hr_error']<=10 for r in hidden),'hidden_with_proxy_target_reliable':sum(r['proxy_target']==1 for r in hidden),'accepted_with_regressor_error_gt_10':sum(r['absolute_hr_error']>10 for r in accepted),'rows':rows,'limits':['13 overlapping windows per short segment are correlated, not independent samples.','ECG HR agreement does not establish visually normal PPG or medical signal quality.','Do not concatenate six disjoint segments into one gap duration.','One unavailable update slot is counted as one second; first/last sample timestamps span one fewer second.','Warmup contributes seven seconds before the first complete 8 s window.']}
    (ROOT/'docs/v07-quality.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
