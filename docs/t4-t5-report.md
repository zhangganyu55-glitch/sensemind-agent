# 知微 SenseMind：T4/T5 验收报告

基准：b13be85，分支 codex/ppg-training；本机 CPU，未重新训练或合并 main。

## 结果与范围

三个真实模型均可加载：PAMAP2 活动识别、PPG-DaLiA HR、PPG HR 一致性代理分类。
`/v1/ml/status` 保留 activity / ppg 原字段，新增 models 中三个独立状态及加载错误。
损坏、缺失、特征版本不符的产物不可用；推理失败在对应模型分支降级。
原有 Agent、数据库、规则告警保留；API 继续将结果存入原 ml_inferences 表。

真实科研数据验收通过 FastAPI TestClient 调用完整路由、真实模型、临时 SQLite、遥测规则、Agent 本地查询及控制台 HTML。
这是进程内 HTTP 验收，未将它表述为外部网络或浏览器交互验收。CLI 的真实时间模式为 --speed 1。
S13/S14/S15 是训练记录中的独立测试受试者。每人静坐、步行各 20 秒，共 120 秒、120 次遥测、78 次 8 秒滑窗推理，78 条推理持久化成功。
质量门控隐藏 HR 共 46 窗，剩余 32 窗提供 HR。前 7 秒为预热，之后每秒一个完整窗口。
详见 [真实响应与结果](t4-t5-replay-results.json)。这些是集成结果，不是新的准确率实验。
Agent 通过 get_model_outputs 读取当前设备最近模型结果，原 list_events 保留历史事件查询；DeepSeek 工具交互使用模拟传输自动测试，未消费真实 API 或声称已验证外部 DeepSeek 连通性。
涉及模型证据的最终解释使用受限事实模板，禁止把分数转换成诊断或发作概率。

## 安全降级

- ECG、label、rpeaks、reference_hr 等额外字段被 API 拒绝；PPG-DaLiA 请求不允许 hr 参考通道。
- NaN、Infinity、错误 ACC 列数、未来时间戳被请求契约拒绝。
- PPG 仅支持 E4 BVP 64 Hz、ACC 32 Hz、8 秒完整窗（512/256 点）。缺 ACC、平线、极端振幅、不支持的数据域返回 unavailable。
- 代理可靠性 <0.7 时 estimated_hr=null、hr_available=false；质量结果仍可查，遥测 reading_valid=false。
- PPG 域不调用 PAMAP2 活动模型。PAMAP2 活动仅接受 100 Hz、512 点、m/s² 对应数据域；单位真实性仍由调用者保证。
- 时长分别按 BVP 和 ACC 的采样率计算，窗口结束时间不再把 32 Hz ACC 误当 64 Hz。

腕部遥测摘要按 32 Hz 截取最近一秒，将 g 换算为 m/s² 后按原遥测契约限幅；推理用原始 E4 g。PAMAP2 独立测试受试者 105 另回放步行 12 秒、7 次活动推理，验证活动模型 API 链路。

## 四项审计

1. **泄漏**：训练按受试者划分；本阶段仅使用 S13-S15 验收，不用回放结果调参。回放请求仅含 BVP、ACC、来源与时间，不含 ECG/标签。pkl 文件本身同时含参考数据，读取后只使用在线通道；验收显式移除参考字段，另有毒化标签的自动测试。活动 ID 仅用于选择片段，不进入模型。
2. **标签循环/捷径**：质量目标由既有 PPG_HR 与 ECG_HR 的误差生成，而 PPG_HR 同时是输入特征。没有在线读取 ECG 的直接泄漏，但目标依赖同一个启发式估计器，可能形成特征捷径；它不是独立 SQI，更不证明新 HR 回归器的误差概率。当前保留基线并明确限制，未来需独立人工质量标注和外部验证。
3. **时间戳**：原始片段用 4 Hz 活动网格定位，再映射 64/32 Hz 通道。回放 UTC 映射到近过去，保留 source_start_seconds；窗口为尾随 8 秒，首个在第 8 秒结束。训练标签 2 秒步长；在线每秒推理，重叠更高，不能将相邻窗口当独立样本。加速回放时间可能显示 stale，这是历史回放语义。
4. **分布不一致**：已阻止把 E4 g/32 Hz 数据送 PAMAP2 m/s²/100 Hz 模型；不会伪造跨域活动标签。训练仅每人前约 40 分钟，步行可能超出其活动覆盖；本次三人步行全部隐藏 HR。E4 与目标硬件仍有域差异，未建立可靠 OOD 检测，不能仅凭输入边界断言分布一致。

## API 示例

状态：`GET /v1/ml/status`，逐项结果见结果 JSON 的 status.models。
缺测请求（合法 JSON）：

```json
{"device_id":"demo","window_start":"2026-10-31T06:00:00Z","dataset":"ppg-dalia","participant_id":"S13","sampling_hz":64,"acc_sampling_hz":32,"ppg":[],"acc":[]}
```

返回 HTTP 200、available=false，signal.reason=insufficient_ppg，activity.reason=unsupported_activity_domain；不生成 HR。
完整信号请求可用以下代码生成并发送，实际响应示例见结果 JSON 中每段 first_response：

```python
from guardian.ml.data import read_ppg_subject
from pathlib import Path
from datetime import datetime, timezone, timedelta
import httpx
s = read_ppg_subject(Path('data/raw/PPG_FieldStudy/S13/S13.pkl'))
p = dict(device_id='demo', dataset='ppg-dalia', participant_id='S13',
         window_start=(datetime.now(timezone.utc)-timedelta(seconds=8)).isoformat(),
         sampling_hz=64, acc_sampling_hz=32,
         ppg=s['bvp'][:512].tolist(), acc=s['acc'][:256].tolist())
print(httpx.post('http://127.0.0.1:8000/v1/ml/analyze', json=p).json())
```

## 重建与模型恢复

Python 3.13；精确依赖快照 [environment-lock.txt](environment-lock.txt)。

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r docs/environment-lock.txt
# 从可信本地备份恢复 artifacts/activity/ 和 artifacts/ppg/ 后校验所有产物
.venv/bin/python scripts/verify_artifacts.py
# 或在对应公开数据已本地解压后重新训练
.venv/bin/python scripts/train_activity.py
.venv/bin/python scripts/train_ppg.py
.venv/bin/python -m pytest -q
.venv/bin/python -u scripts/verify_ml_replay.py
.venv/bin/python -m uvicorn guardian.main:app --host 127.0.0.1 --port 8000
# 另一个终端，按真实时间回放独立测试受试者
.venv/bin/python scripts/replay_dataset.py --dataset ppg --subject S13 --activity 7 --seconds 60 --speed 1
```

原始数据和模型不在 Git；PPG zip SHA-256、模型 SHA-256 见 runs/20261031-ppg-dalia-rf-local.json，PAMAP2 见对应 run。精确恢复须使用匹配的可信模型备份；重训生成的模型字节及时间元数据可能改变，须另建 run 记录。joblib 仅载入可信来源文件。恢复后重启服务刷新一次性加载缓存。

## 自动化测试

`.venv/bin/python -m pytest -q`：118 passed，2 warnings，13.40 秒。覆盖 API 缺失模型、模型文件损坏、异常输入、参考字段拒绝、低质量 HR 降级、双采样率时间轴、回放无参考通道、Agent 模型工具和既有功能回归。

## 已知问题

质量阈值 0.7 未经独立校准；不可靠类测试召回率 47.08%；HR 验证 MAE 17.18 bpm，大于测试 7.76 bpm；质量准确率 75.33% 未超过多数类 75.75%。PAMAP2 walking 召回率 0.21。模型可加载不代表当前输入可推理。
现有常量轴相关系数计算有 NumPy 除零警告，后续 nan_to_num 归零；测试另有 Starlette/httpx 弃用警告。
本轮未修改告警阈值，未验证实际疾病识别、真硬件采集或外部 DeepSeek 可达性。

本次 PAMAP2 步行短片的 7 个活动预测均未判为 walking（预测计数见 activity_replay.activities）；这进一步提示基线 walking 弱项。接口链路成功不等于活动识别正确。
