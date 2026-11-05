# 项目状态

最后更新：2026-11-03 by Claude（Agent 3.0 Decision Engine V0.1.2 T12-1 R1-01 修复完成；T12-2 至 T12-6 未开始）
项目：知微 SenseMind Agent 3.0（Decision Engine V0.1.2 Timebase）
工程路径：`<repo>`
分支：`main`（合并 `integration/client-loop` `72294ae`；合并提交 `dfd8d61`）；当前工作分支 `claude/agent3-decision-v0.1.2`

## 当前目标

Android → FastAPI → Agent 2.0 → Android → 人工确认 → Mac 上位机的软件闭环已合并 main。两个客户端共用同一服务和数据库；真实 DeepSeek 只用于明确标记 synthetic/replay 的测试事件；remote_alerts 已有角色/范围校验、撤销和 Android 开关。ADB 无设备，真机验收依据人工操作 + 服务端日志，未采集手机截图。Mac 上位机与 Android App 功能分别见 `docs/Mac上位机使用说明.md`、`docs/client-apps-and-security.md`、`android/README.md`。

## 正在运行

| run id | 启动 | 命令 | 日志 | 状态 |
| --- | --- | --- | --- | --- |
| 20261031-ppg-dalia-rf-local | 见 run JSON | `.venv/bin/python scripts/train_ppg.py` | `logs/20261031-ppg-dalia-rf-local.log` | 已完成，exit 0；本次无残留训练 |
| client-loop-8001 | 2026-10-31 18:14 | `uvicorn guardian.main:app --host 0.0.0.0 --port 8001` | 后台任务 `baus40enn` | 运行中；共享演示库 `outputs/SenseMind-交接包/android-backend/guardian.db`；`GUARDIAN_LLM_MODE=deepseek`、调查 token 上限 30000 |

未停止无关的 `127.0.0.1:18707` 旧演示服务。ADB 无设备，真机验收依据手机请求日志 + 数据库记录；未采集手机截图。

## 暂停/恢复入口（2026-11-03）

- 状态：T12-1 R1-01 已实现，今天暂停，等待 ChatGPT 第三轮独立审查。
- 分支/HEAD：`claude/agent3-decision-v0.1.2` / `f108838dadf670a18e27df50d5dfbca34f02f263`
- 工作区：6 个已修改文件，0 staged，0 untracked；未提交、未 push、未合并 main。
- 全量测试：**230 passed / 1 warning**；`tests/test_v012_timebase.py` 21 passed。
- 检查点：`artifacts/checkpoints/2026-11-03-r1-01/`（源码 ZIP、R1-01 diff、完整 diff、`CHECKPOINT.md`）。
- 恢复时先读 `STATE.md`、`TASKS.md`、`AGENTS.md`，保留工作区修改，再按审查结论决定下一步。

## 最近完成

- **Agent 3.0 Decision Engine V0.1.2 T12-1 R1-01 修复（2026-11-03，分支 `claude/agent3-decision-v0.1.2`）**
  - 回退重新锚定必须有该设备 `DeviceStatus.sample_counter` 证明候选样本产生于前跳之后；32 位回绕按 `0 < delta < 2^31` 判断
  - `received_at` 与 monotonic 必须严格前进（`delta > 0`），同一 tick 的候选样本拒绝
  - 无 counter（APK/ring）不立即重锚，保持 `clock_uncertain`，等设备时间线越过临时前跳锚点后恢复；这是现有 Telemetry 契约的限制
  - APK `vendor_sequence` 只在 APK 原始行，未进入 Telemetry；本轮不扩展契约
  - 新增 5 项测试；全量 **230 passed / 1 warning**
  - 未改 Agent 2.0、BLE、Android、H1

- **Agent 3.0 Decision Engine V0.1.2 T12-1 R1 时钟回退误判修复（2026-11-03，分支 `claude/agent3-decision-v0.1.2`）**
  - 新增每设备 `_clock_jump_state`：只有本设备实际接受过前跳样本后，后续新鲜回退才允许重新锚定
  - 回退样本必须晚于前跳前锚点；重复前跳前样本、无前跳的迟到数据继续 `out_of_order_ignored`
  - 多设备状态隔离；回退成功后清除状态，下一次前跳可重新记录
  - 新增 4 项测试；全量 **225 passed / 1 warning**
  - Replay 更新 live Monitoring 仍为 T12-4 阻塞项，本轮未扩大修改
  - 未改 Agent 2.0、BLE、Android、H1

- **Agent 3.0 Decision Engine V0.1.2 T12-1 补充修复（2026-11-03，分支 `claude/agent3-decision-v0.1.2`）**
  - 设备时钟前跳后回到正常时间线：识别“新鲜且大幅回退”的样本并重新锚定，不再持续拒绝正常数据；延迟/重复样本仍按乱序拒绝
  - 未来时间戳：live 使用 5s 上界，拒绝样本不更新 Monitoring/last_valid、不成为排序锚点；replay 保留 30s 模型层容忍，但同样不更新 live Monitoring
  - 关闭真实设备规则时仍先更新 Monitoring，再返回 `real_data_rules_disabled`
  - 新增 3 项针对性测试；全量 **221 passed / 1 warning**
  - 未改 Agent 2.0、BLE、Android、H1

- **Agent 3.0 Decision Engine V0.1.2 T12-1（2026-11-03，分支 `claude/agent3-decision-v0.1.2`）**
  - 新增可注入 `SystemClock`/`ManualClock`；区分 sample_time、server received_at、monotonic_time
  - dwell 同时要求 monotonic 跨度和 sample_time 跨度；单批次不能瞬间打开事件
  - live 历史延迟超过 5s 不进入实时规则；设备时钟前跳/回拨/乱序安全处理
  - `events.opened_at/closed_at` 使用服务器 UTC；新增 `source_started_at` 保留样本时间
  - `telemetry.received_at` / `received_at_source` 迁移；旧行标记 `legacy_estimated`，不伪造接收时间
  - 全量 **218 passed / 1 warning**（208 基线 + 10 新增）；报告 `docs/decision-engine-v0.1.2-t12-1.md`
  - **未合并 main**；T12-2 至 T12-6 未开始；未改 BLE/Android/H1

- **Agent 3.0 Decision Engine V0.1.1（2026-11-03，分支 `claude/agent3-decision-v0.1.1`）**
  - 规则事件改为真实时间稳定性：open dwell 2s、close dwell 10s、数据缺口 5s 重置候选；短暂恢复不再重复建案
  - 没有新增全局冷却；每种规则事件独立判断，确定性安全路径和原有通知逻辑未改
  - 新增 `device_monitoring` 表与 `NORMAL/UNKNOWN/STALE/NO_DATA` 状态、`last_valid_at`；活动事件数据中断不自动 resolved，恢复后重新验证
  - `sustained_hr_change` 冷却改用 `COALESCE(closed_at, opened_at)`；长事件关闭后短期重现被抑制
  - sweep 增加 `last_sweep_at/last_error/last_error_at/thread_alive`；仅本地 app 启动 sweep，public_only app 不启动
  - 全量测试 **208 passed / 1 warning**（194 基线 + 14 新增）；报告 `docs/decision-engine-v0.1.1.md`
  - **未合并 main**；未改 BLE/H1/Android/模型
  - 与 `claude/hardware-api-v1` / `claude/v09-ble-reconnect` 的冲突集中在 STATE/TASKS、`apk_api.py`、`detection.py`、`factory.py`；解决方向见报告
  - 当前版本按**单进程部署**，不承诺多进程共享 SQLite 的并发安全

- **Agent 3.0 Decision Engine V0.1（2026-11-03，分支 `claude/agent3-decision-engine`）**
  - 新增 `guardian/decision.py`、`decisions` 审计表、只读接口；接入 `/v1/telemetry`、APK ingest、后台 sweep
  - 决策类型 OBSERVE / VERIFY / INVESTIGATE / INSIGHT；所有决策记录来源、依据、质量、时间、类型、处理状态
  - `GUARDIAN_DECISION_ENGINE` **默认 off**；真实设备持续变化另需 `GUARDIAN_DECISION_REAL_SUSTAINED=on`
  - `sustained_hr_change` 创建内部事件与调查，但**不创建照护者通知**；原有规则通知行为未改变
  - 持续变化使用真实时间跨度、采样间隔、基线排除候选期；基线不足不建案
  - 全量测试 **194 passed / 1 warning**（169 基线 + 25 新增）；报告 `docs/decision-engine-v0.1.md`
  - **未合并 main**；H1 `/v1/hardware/packets` 仍只在 `claude/hardware-api-v1`，本轮未合并、未改动
  - 下一步：评审后决定 V0.1.1；真实数据校准阈值；H1 合并时在 `guardian/hardware/ingest.py` 补 `decision_engine.evaluate(...)`

- **合并客户端业务闭环到 main（2026-10-31）**
  - 源分支 `integration/client-loop` `72294ae` 已推送远程；合并前 `origin/main` 为 `ef3d18f`，无新提交
  - 非快进合并提交 `dfd8d61`，保留 Mac/Android 两条分支历史，无冲突
  - main 全量 **169 passed / 1 warning**；角色/APK/E2E 定向 **30 passed**；Live/Replay/证据 **22 passed**；旧库迁移脚本通过
  - 未强制推送；未修改硬件 WIP 分支
  - **仅代表软件层验收**；Android 页面视觉、BLE 实物、H1 上传、毫米波雷达联调仍未通过实物验收
  - 报告：`docs/client-loop-merge-2026-10-31.md`

- **完整业务闭环验收（2026-10-31，分支 `integration/client-loop`）**
  - 合并 `claude/mac-console` `b9ffbb0` 与 `claude/android-app` `d9ac302`；仅 `TASKS.md`/`STATE.md` 冲突已人工合并
  - 后端增加按角色的同意范围策略：`upload→wearer`、`ai_analysis→caregiver`、`remote_alerts→caregiver`；撤销同路由 `granted=false`
  - 后端 **169 passed / 1 warning**；Android JVM 单元测试 **5 passed**；APK `0.2.0-debug`，SHA-256 `c88112a8…f72d`
  - 真实 DeepSeek：事件 `e3e47606…`（`guardian_002`，replay）；首次 8k token 上限 `cost_limit`，提高上限重试后 `ai_status=completed`、`analysis_source=ai`、5,768 tokens、16 条 replay 证据、结论 `inconclusive`
  - Android 真机：事件详情 200、人工确认 200；DB `acknowledged_by=照护者-测试设备` @ `2026-10-31T10:38:07Z`
  - Mac 控制台同一事件显示 `acknowledged=true`，时间线含 `human_acknowledged`
  - remote_alerts 真机：授权后 `/v1/alerts` 200×4；撤销后停止请求；此前 403 轮询不再出现
  - 报告：`docs/client-loop-acceptance-2026-10-31.md`、`outputs/SenseMind-交接包/完整业务闭环验收-2026-10-31.md`
  - 验收时未合并 main；现已由上方合并提交进入 main；未修改硬件 WIP 分支

- **Mac 上位机（2026-10-31，分支 `claude/mac-console`）**
  - commit `20343a7`；新增趋势、系统健康、证据详情与调查状态转换，保留原 API/人工确认
  - 回归 **161 passed / 1 warning**；数据库副本启动成功；截图 `Mac控制台-总览.png`、`Mac控制台-证据详情.png`
  - 当前任务看板：M1–M5 全部 ✅ Completed（5/5，100%）；未合并 main

- **Android 客户端与角色后端（2026-10-31，分支 `claude/android-app`）**
  - commit `4e07f78`；Kotlin + Compose 双角色 App、APK、BLE H1 客户端、演示数据隔离
  - 后端角色 `wearer/caregiver/legacy` 与照护者接口；**166 passed / 1 warning**
  - APK debug 构建/签名成功；Gradle 单元测试 5 passed；release 构建 + lintVital 通过
  - HTTP E2E：角色隔离、live 上传、调查/证据、人工确认、H1 404 不 ACK 全部通过
  - 任务看板：9/13 完成（69%），3 项阻塞（真机、BLE 实物、H1 后端），1 项待确认（合并 main）
  - **未合并 main；未修改硬件 WIP 分支**

- **合并到 main（2026-10-31）**
  - 非快进 Merge Commit `1a6a1ce` 合并 `integration/sensemind-v07-agent2`（源验收提交 `4c15eb1`）
  - main 重新执行全量测试 **161 passed / 1 warning**；六模型、DB 迁移、Live/Replay 再次通过
  - 未包含 H1/H2、nRF52840、ESP32 硬件 WIP；硬件分支未修改
  - 版本标记 `v0.7.0-agent2`；报告 `docs/integration-acceptance-2026-10-31.md`

- **软件集成验收（2026-10-31，源分支 `integration/sensemind-v07-agent2`）**
  - v0.7（`ee5b93b`）与 Agent 2.0（`f022b2d`）为直接父子关系，集成无冲突、无删除文件
  - 全量测试 **161 passed / 1 warning**；六个模型产物 SHA-256 与加载/推理通过
  - 旧库迁移、当前库读取、Live/Replay 隔离、PPG 真实回放、合成 DeepSeek 调查均通过
  - 未修改 H1 协议、硬件分支或模型；已由上述合并进入 main；报告 `docs/integration-acceptance-2026-10-31.md`

- **Agent 2.0（2026-10-31，分支 `claude/agent2`）**
  - Evidence Engine：10 类证据、可用性、replay/live、时钟不确定度与保守对齐；`guardian/evidence.py`
  - Investigation Manager：状态机、重复事件合并、瞬时错误重试、重启恢复、并发 worker、token 上限、后续证据；`guardian/investigation.py`
  - Structured Reasoner：强制 evidence_id、数值来源校验、危险建议拦截、证据不足降级 inconclusive；`guardian/reasoner.py`
  - 10 个 synthetic 多模态场景；新增证据/调查接口与访问控制
  - 全量测试 **161 passed / 1 warning**
  - 真实 DeepSeek（纯合成数据）：首轮 5,208 tokens / 5.32 秒；集成验收复跑 9,258 tokens / 6.54 秒，均为 `ai_status=completed`、`structured=inconclusive`、13 条证据；记录 `docs/agent2-deepseek.json`
  - 未合并 main；未修改 H1 协议、nRF/ESP32 固件或已有模型

- V7 稳定性：修复后真实时间 600 秒，含加载 603.51 秒；600 遥测/593 推理持久化一致，HTTP 错误 0，最慢请求 0.594 秒，三模型仍 available。测试子服务已自行关闭，演示控制台保留。


- V7：遥测 data_mode/live/replay 来源约束与持久化；ESP32 预留，默认真实规则关闭；控制台新增模式、模型 HR、质量、来源、加载状态与连接说明。未做 ESP32 实物验收。
- 真实 DeepSeek：安全合成波形经过实际 PPG 模型，HR 69.91 / 质量代理 0.8454；真实工具读取模型/历史，异常候选自主调查完成，人工确认未代办，证据 `docs/v07-deepseek.json`。
- 78 窗离线 ECG 审计：32 窗有效（41.03%），46 窗因质量代理 <0.7 隐藏，34 个隐藏窗口的回归误差仍 ≤10 bpm；明显保守。保留窗口 MAE 1.91 / 全部 4.92 bpm。阈值未下降，ECG 不进入在线或外部模型。
- 全量测试：127 passed / 1 warning；NumPy 常量轴除零已按原归零语义修复，保留 Starlette/httpx 弃用提示；六个原模型产物 SHA-256 均通过。
- 另纠正 T5 摘要单位：遥测契约 g，PPG 保持 g，PAMAP2 摘要除以 9.80665；模型输入不变，原规则阈值不变。
- 本轮演示服务：PID 6279，127.0.0.1:18707，数据库 `.runtime/v07/console-final.db`，`GUARDIAN_LLM_MODE=off`（科研回放不外发）。保留供用户查看 `/console`；没有操作其他进程。


- 交接后三处修复：PPG 回放必须显式指定 `--subject`（CLI 在连接服务前报错）；模型解释模板按 created_at DESC 使用首条最新记录；旧测试摘要纠正为 118 passed / 2 warnings。
- 本次修复验证：`.venv/bin/python -m pytest -q` → **121 passed / 2 warnings**（既有 NumPy 除零 + Starlette/httpx 弃用）；`.venv/bin/python scripts/verify_artifacts.py` 六项 SHA-256 全部通过。


- T4/T5：统一三模型状态与加载契约；API 输入域/时长/异常保护；低质量 HR 降级；Agent 增加只读模型工具和受限研究解释。
- 独立 S13/S14/S15 静坐/步行共 120 秒、78 次推理，46 窗隐藏 HR；API/SQLite/遥测规则/Agent/控制台 HTML 通过进程内 HTTP 验收。无在线 ECG。
- PAMAP2 测试受试者 105 步行 12 秒/7 次推理链路通过，但 7 窗均误判；腕部遥测已修正为 32 Hz 最近一秒及 m/s² 单位。
- 全量测试 118 passed，2 warnings；所有六个训练产物 SHA-256 复核通过。模型、数据和数据库未入库。
- 详细真实响应 `docs/t4-t5-replay-results.json`；重建/恢复说明与风险审计 `docs/t4-t5-report.md`。未调用真实 DeepSeek 网络；工具交互已用模拟传输验证。


- **T1 完整包解压**：`data/raw/ppg-dalia-sciebo.zip`，2,864,337,783 bytes。
  - SHA-256：`fcda4d13f6475e88a24f1ef9658627dc98e04dd5c5dd790cd2b0a1a22e31913b`
  - 完整解压到 `data/raw/PPG_FieldStudy/`，找到 S1-S15 共 15 个 `S*/S*.pkl`；解压 CRC 检查通过。
  - 早期 `.part` 未使用、未删除。
- **T2 标签核对**：按本地 `PPG_FieldStudy_readme.pdf` 第 1、3-4 页核对。
  - 标签 i 对应 `[2*i, 2*i+8)` 秒；BVP 512 点 / 128 点步长，ACC 256 点 / 64 点步长；无固定偏移。
  - S1/S10/S15 抽查共 15 窗，ECG R 峰重算与标签最大差 4.27e-14 bpm。
  - 加入固定 8/2 秒参数保护及 8 个新增测试；详见 `docs/ppg-alignment.md`。
- **T3 PPG 本地训练完成**：run `20261031-ppg-dalia-rf-local`。
  - `artifacts/ppg/ppg_quality_rf.joblib`、`ppg_hr_rf.joblib`、`ppg_metadata.json`。
  - train S1-S10 / val S11-S12 / test S13-S15；4000 / 800 / 1200 窗，受试者隔离。
  - 测试质量 acc **75.33%** / macro F1 **65.95%**；多数类基线 acc **75.75%** / macro F1 **43.10%**。
  - 测试 HR MAE **7.76 bpm** / RMSE **12.12 bpm**；均值基线 MAE **20.79 bpm**。
  - 验证质量 acc **64.38%** / macro F1 **61.55%**；HR MAE **17.18 bpm** / RMSE **22.62 bpm**。
  - 训练命令、数据版本、环境、split、完整指标和产物 SHA-256 已记录到 `runs/20261031-ppg-dalia-rf-local.json`；`experiments/INDEX.md` 已更新。
  - `SensorModels().status()` 本地模型加载检查：activity / ppg 都 available；T4 的进程内 API 端到端验证现已通过。
- **T8 全量测试通过**：`.venv/bin/python -m pytest -q` → **118 passed / 2 warnings**（既有 NumPy 除零 + Starlette/httpx 弃用）。
- **PAMAP2 活动模型**（2026-10-30）：测试 acc 75.03% / macro F1 73.94%，多数类基线 11.16%；train 4526 / val 886 / test 1990；walking 召回率 0.21。
- 合成数据 + 真实 PAMAP2 回放此前已通过；ML API 已接通，PPG 真实回放已通过 T5。

## 下一步

1. 评审 T12-1 R1-01 修复；T12-1 已获正式验收，许可进入 T12-2（GAP/STALE 统一连续性语义），本轮不开始。
2. T12-2 至 T12-6 未开始，不提前合并 main。
3. 用独立验证集研究 HR 门控校准和过度隐藏，不用测试片段调阈值。

## 阻塞/风险

- T12-1 的 `max_sample_lag_seconds` 是演示参数（默认 5s），未用真实设备延迟校准。
- T12-1 未实现 GAP/STALE、monitoring best-effort、sweep lease、H1 契约；这些属于 T12-2 至 T12-5。
- 无 T2/T3 阻塞。
- 本次保留默认每人前 400 窗、stride=3（窗口起点每隔 6 秒，覆盖约前 40 分钟），后续活动代表性不足；指标不代表完整全天活动表现。
- 质量标签是 HR 一致性代理（|PPG_HR-ECG_HR| ≤ 10 bpm 且至少 4 个峰），不是医学 SQI；不可靠类测试召回率仅 47.08%。
- 质量准确率未超过多数类基线；HR 验证误差高于测试误差，受试者差异明显。
- 研究/黑客松原型，未针对诊断或告警有效性验证。
- 数据、模型、日志均不入库；换机器需要复制或重建。

## 环境

- Python：`.venv/bin/python`；本机 CPU，无 GPU。
- 数据：`data/raw/`、`data/processed/`（当前约 29 GiB，不入库）。
- 模型：`artifacts/activity/`、`artifacts/ppg/`（合计约 76 MiB，不入库）。
- 测试：`.venv/bin/python -m pytest -q`。
- 服务：`uvicorn guardian.main:app --host 127.0.0.1 --port 8000`。
