# Guardian Angel 开发进度（v0.6 进行中）

> 保存时间：2026-10-30
> 当前状态：**PAMAP2 活动模型已训练并跑通回放；PPG-DaLiA 完整包尚未下载成功（网络波动，待重试或手动提供）**。
> 自动化测试：**97 passed**（v0.5 的 87 项 + v0.6 ML 6 项 + 回放 4 项）。

## v0.6 本轮已完成

| # | 任务 | 状态 |
|---|------|------|
| 1 | 六个数据集官方信息、许可、兼容性评估 | ✅ [docs/dataset-evaluation.md](docs/dataset-evaluation.md) |
| 2 | 选择 PAMAP2 + PPG-DaLiA 作为两个模型数据源 | ✅ 已定 |
| 3 | 轻量 ML 依赖（numpy/scipy/scikit-learn/joblib） | ✅ 已安装并写入 requirements |
| 4 | 数据集加载与按参与者窗口化 | ✅ `guardian/ml/data.py` |
| 5 | 活动/PPG 特征工程 | ✅ `guardian/ml/features.py` |
| 6 | 模型注册、懒加载、推理服务 | ✅ `guardian/ml/registry.py`、`models.py` |
| 7 | 两个训练脚本（参与者隔离 split） | ✅ `scripts/train_activity.py`、`train_ppg.py` |
| 8 | Guardian Angel ML API | ✅ `/v1/ml/status`、`/v1/ml/analyze`、`/v1/ml/inferences` |
| 9 | Agent 调查工具读取模型证据 | ✅ `get_sensor_intelligence` + 报告/控制台展示 |
| 10 | 无数据集/无模型时的自动化测试 | ✅ 6 项 ML + 4 项回放，97 项全通过 |
| 11 | 下载 PAMAP2 + PPG-DaLiA | 🟡 PAMAP2 完成；PPG-DaLiA 下载中 |
| 12 | 训练两个模型并产出指标/元数据 | 🟡 PAMAP2 活动模型完成；PPG 待数据 |
| 13 | 数据集回放脚本 `scripts/replay_dataset.py` | ✅ 合成数据 + 真实 PAMAP2 验证 |
| 14 | README/版本记录升到 v0.6 | ⬜ 暂缓，避免把未完成版本写成已交付 |

## 2026-10-30 进展

- **PAMAP2 已下载并解压**。UCI 的 `pamap2.zip` 是外层包，里面还有 `PAMAP2_Dataset.zip`；已解压出 Protocol 9 个受试者。
- **活动模型已训练**：`artifacts/activity/activity_rf.joblib`、`activity_metadata.json`。
  - 7,402 个窗口；训练 4,526 / 验证 886 / 测试 1,990；测试受试者 105、106，验证 107。
  - 测试准确率 **75.03%**，macro F1 **73.94%**，多数类基线 11.16%。
  - 已知弱项：walking 召回率仅 0.21（常被判成 standing/ascending_stairs）；running / nordic_walking / cycling 的 F1 在 0.94–0.98。
- **`scripts/replay_dataset.py` 已实现并验证**：
  - 只回放单个受试者的一段连续活动，不拼接不同受试者或活动。
  - 每秒调用 `/v1/ml/analyze`（窗口填满后才推理，避免短窗口误判），再向 `/v1/telemetry` 发 1 Hz 摘要，保留 `dataset/participant_id/sampling_hz`。
  - 设备 ID 默认自动选未使用的 `guardian_replay*`，重复回放不会撞 `409`。
  - 合成数据（PAMAP2 + PPG）和真实 PAMAP2（subject 105 running）均已跑通；真实回放产出 `unusual_motion` / `data_quality` 规则事件和调查记录。
- **PPG-DaLiA 下载**：UCI 与 Siegen 两个源的速度都随时段/连接波动；Siegen 的 Range 续传路径不可用（返回 206 但速率接近 0），只能整包下载。当前从 UCI 官方源重试（约 0.8–2.6 MB/s），后台下载中。`data/raw/` 现有三个不完整的分包，完整包完成后再统一核对，不拿不完整 zip 训练。

## 数据集下载状态

**PAMAP2（已完成）**

- UCI 外层包 `pamap2.zip` 为 688,226,940 bytes，里面还有 `PAMAP2_Dataset.zip` 和 `readme.pdf`。
- 需要解压两次：

```bash
unzip -q data/raw/pamap2.zip -d data/raw/
unzip -q data/raw/PAMAP2_Dataset.zip -d data/raw/
find data/raw/PAMAP2_Dataset -maxdepth 2 -type d   # Protocol/ + Optional/
```

- 解压后 `data/raw/PAMAP2_Dataset/Protocol/subject101..109.dat`，约 1.7 GB。

**PPG-DaLiA（未完成）**

- 官方文件约 2,864,337,783 bytes（UCI 的 `ppg+dalia.zip`；Siegen Sciebo 镜像同名内容）。
- 两个源的速度都随时段/连接大幅波动：UCI 在 13 KB/s–2.6 MB/s 之间，Siegen 在 0–4 MB/s 之间。
- Siegen 虽然返回 206，但 Range 续传路径实测接近 0 B/s，不能靠 `-C -` 分段下载；只能整包一次拉完。
- 当前 `data/raw/` 有三个不完整分包，均不可用于训练：

```text
data/raw/ppg-dalia.zip.part        # Siegen 直连，约 304 MiB
data/raw/ppg-dalia-uci.zip.part    # UCI，约 243 MiB
data/raw/ppg-dalia-sciebo.zip.part # 代理实测，约 1.1 MiB
```

- 下载完成后先 `unzip -l` 确认包结构，PPG-DaLiA 外层可能还嵌套 `data.zip`；**任何不完整 zip 都不得解压训练**。
- 如果网络持续不稳定，可以让用户手动下载后放到 `data/raw/ppg-dalia.zip`，后续命令不变。

> 磁盘实测约 138 GB 可用，空间充足；之前记录的「约 13 GB」不准确。

## 下一步

```bash
source .venv/bin/activate

# 1. 等 PPG-DaLiA 下载完，解压并核对 pkl 对齐
unzip -q data/raw/ppg-dalia.zip -d data/raw/
# 若外层里还有 data.zip，继续解压
python scripts/train_ppg.py
# 产物：artifacts/ppg/ppg_quality_rf.joblib + ppg_hr_rf.joblib + ppg_metadata.json

# 2. 重启服务检查两个模型都 available
uvicorn guardian.main:app --host 127.0.0.1 --port 8000
curl -s http://127.0.0.1:8000/v1/ml/status

# 3. 跑一次 PPG 回放（真实数据）
python scripts/replay_dataset.py --dataset ppg --subject S1 --activity 7 --seconds 60 --speed 4

# 4. 全量测试
python -m pytest -q
```

## 接下来还需要补的代码/文档

1. 拿到 PPG-DaLiA 后核对 `iter_ppg_windows` 的 8 s 窗口 / 2 s 步长标签对齐；若有偏移需修 `guardian/ml/data.py`。
2. 训练后确认 `SensorModels` 能加载两个 artifact，`/v1/ml/analyze` 返回活动、可靠性、异常评分、HR 估计和模型版本。
3. 在监控控制台观察一次「数据集回放 → 模型证据 → 规则事件 → Agent 调查」。
4. 把 README 升到 v0.6，补：数据集评估摘要、模型指标、许可限制、模型仅辅助的声明、回放命令。
5. 用真实训练结果更新进度文件和最终交付说明。

## v0.6 已实现的关键文件

- `docs/dataset-evaluation.md`：六个数据集的地址、格式、规模、采样、标签、许可、兼容性与选择理由。
- `guardian/ml/features.py`：活动特征（PAMAP2 腕部 ACC 100 Hz）与 PPG 特征（BVP 64 Hz + ACC）；特征名是模型契约。
- `guardian/ml/data.py`：PAMAP2 / PPG-DaLiA 加载器；按受试者分段/窗口；PPG 的 8 s/2 s 对齐假设。
- `guardian/ml/registry.py`：`SensorModels` 懒加载 joblib，输出 activity/confidence/reliability/anomaly/estimated HR/model version。
- `guardian/ml/models.py`：`/v1/ml/analyze` 请求校验；不接受参考标签，避免泄漏。
- `guardian/storage.py`：`ml_inferences` 表；删除设备数据时同步清理。
- `guardian/factory.py`：本地 ML API（公共入口不注册）。
- `guardian/investigation.py`：`get_sensor_intelligence` 只读工具；报告增加 `model_evidence`；模型证据不覆盖规则。
- `guardian/console.py`：模型辅助证据面板。
- `tests/test_ml_api.py`、`test_ml_registry.py`、`test_ml_data.py`：无数据集即可运行。
- `tests/test_investigation.py`：新增模型证据进入调查报告的测试。
- `tests/test_replay.py`：回放脚本的补全/连续段/受试者文件选择测试（4 项）。

## 已知风险/待核对

- **PPG-DaLiA 标签对齐**：当前按官方说明的 8 s 窗口 / 2 s 步长，以 `label[i]` 起始于 `i*2 s` 处理；拿到 pkl 后必须用 readme 和少量样本核对，若有偏移需改 `iter_ppg_windows`。
- **PPG 质量标签是代理标签**：`|PPG_HR - ECG_HR| ≤ 10 bpm` 且至少 4 个心搏；不是医学 SQI，不能宣传为诊断。
- **许可**：PPG-DaLiA 的 UCI 页面标 CC BY 4.0，但原始 Siegen 页面被描述为科研非商业；商业使用前必须联系作者。PAMAP2 为 CC BY 4.0。
- **采样差异**：PAMAP2 100 Hz / m/s² 与 MPU6050 的 g/50–100 Hz 需要适配；PPG-DaLiA E4 64/32 Hz 与 MAX30102 需要重采样。
- **活动模型弱项**：walking 召回率 0.21，常被判成 standing / ascending_stairs；`/v1/ml/analyze` 的输出只作辅助证据，不能覆盖规则或作为安全判断。
- **模型状态**：活动模型已 available；PPG 模型在数据集下载完成前仍 unavailable（预期状态，不是故障）。
- 本轮没有连接真实 STM32、戒指或手机；所有测试使用模拟/本地数据。

## v0.5 已完成（保留）

自主调查 Agent、结构化证据链、通知/人工确认闭环、控制台、公共入口隔离、APK 契约层；v0.5 的 87 项测试全部保留并通过。新增 6 项 ML 测试和 4 项回放测试后共 97 项。
