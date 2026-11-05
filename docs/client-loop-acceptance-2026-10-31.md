# 知微 SenseMind 完整业务闭环验收报告

日期：2026-10-31 18:40（+08:00）
分支：`integration/client-loop`（基于 main `ef3d18f`；验收时未合并 main）
代码/测试提交：`1bf08b4`
后续状态：本分支已于 2026-10-31 合并到 main（merge `dfd8d61`）；见 `docs/client-loop-merge-2026-10-31.md`。
数据：本轮全部使用明确标记的 synthetic/replay 数据；没有真实人体测量。

## 1. 结论

软件闭环已实际跑通：

```text
Android 0.2.0
  -> FastAPI 0.7.0（0.0.0.0:8001，真实 DeepSeek）
  -> Agent 2.0 / Evidence Engine
  -> Android 事件详情（16 条 replay 证据）
  -> Android 照护者人工确认
  -> 同一 SQLite 数据库
  -> Mac 控制台
```

结论为**有条件通过**：

- 通过：服务、数据库、Agent 调查、证据、人工确认、Mac 同步、remote_alerts 授权/撤销、全量测试。
- 未完成：手机屏幕逐项视觉比对（无 ADB、无截图）；H1/BLE 硬件验收不在本轮范围。
- 未合并 main；是否合并待用户确认。

## 2. 实际服务与数据库（已重新核实）

| 项目 | 实际状态 |
| --- | --- |
| 后端 | `uvicorn guardian.main:app --host 0.0.0.0 --port 8001`，运行中 |
| 数据库 | `outputs/SenseMind-交接包/android-backend/guardian.db`，Mac 与 Android 共用 |
| API 版本 | `/health` → `0.7.0`，`apk_contract=v1` |
| LLM | `GUARDIAN_LLM_MODE=deepseek`，模型 `deepseek-flash`，调查 token 上限 30000 |
| Mac 控制台 | `http://127.0.0.1:8001/console`，同一数据库 |
| Android | `http://<LAN-IP>:8001`，照护者凭证「照护者-<测试设备>」 |
| 8000 端口 | 无监听；上一轮 Mac 服务已停止，未重启第二套服务 |
| 18707 旧服务 | 未操作，与本轮无关 |
| ADB | `adb devices` 为空，无法取得手机截图或 logcat |

数据库已保留两份本轮回放对象：`guardian_001`（旧 PPG-DaLiA replay）和 `guardian_002`（本轮 synthetic replay）。

## 3. 集成分支与代码变更

- `integration/client-loop` 从 main `ef3d18f` 建立。
- 合并 `claude/mac-console` `b9ffbb0`（Mac 控制台）。
- 合并 `claude/android-app` `d9ac302`（Android App、角色后端）。
- 冲突仅 `TASKS.md` / `STATE.md`，已人工合并，无代码冲突。
- 本轮代码提交 `1bf08b4`：
  - 后端同意范围角色策略：`upload→wearer`、`ai_analysis→caregiver`、`remote_alerts→caregiver`。
  - Android 设置页增加 `remote_alerts` 授予/撤销；未授权不请求 `/v1/alerts`；撤销立即清空并停止轮询。
  - 新增越权、撤销、按凭证隔离测试。
  - APK 升级到 `0.2.0-debug`。

## 4. 测试数据与事件

| 项目 | 值 |
| --- | --- |
| 设备/对象 | `guardian_002` |
| 模型显示 | `Synthetic Replay · Agent 2.0` |
| 数据来源 | `source=simulator`，`dataset=synthetic-loop-20261031` |
| 数据模式 | `replay`（不是 live） |
| 事件 | `e3e47606-6860-469f-9da5-456c1d839492`，`high_heart_rate` |
| 事件说明 | `DEMO ONLY: unvalidated signal rule; investigate source/quality; not diagnosis` |
| 证据 | 16 条；来源：`heart_rate_estimate`、`signal_quality`、`imu_raw`、`device_online` |
| 证据模式 | 全部 `replay`；质量全部 `good`；时钟不确定度 1000 ms |
| 缺失来源 | 原始 PPG、活动、雷达、呼吸率、历史同类事件 |

事件由 3 条连续高心率 replay 样本触发；第 4、5 条样本用于事件后窗口。Android 端按 `data_mode=replay` 显示 REPLAY，未显示为 Live。

脱敏证据记录：`完整业务闭环验收-2026-10-31.json`（无 token、无 API Key、无凭证哈希）。

## 5. 真实 DeepSeek 调查

| 尝试 | 结果 | 说明 |
| --- | --- | --- |
| 第 1 次 | `cost_limit` | 真实请求已发出，usage 7592 prompt + 933 completion = 8525 tokens；超过当时 8000 上限，服务端丢弃响应，结构化报告为空。**未算作成功。** |
| 第 2 次（重试） | `completed` | 上限提高到 30000；usage 5042 + 726 = 5768 tokens；`analysis_source=ai`；结构化结论 `inconclusive`。 |

最终调查：

- `status=completed`，`ai_status=completed`，`model=deepseek-flash`
- 证据数量 `evidence_count=16`
- 模型结论（节选）：现有证据仅显示回放数据中恒定的 135 bpm 心率估计，原始 PPG 未接入，无法判断是否反映真实生理状态，结论不确定。
- 不确定性包含：数据为 replay/synthetic-loop、心率恒定、设备状态/数据陈旧、时间同步为网关估计 ±1 s。
- 人工确认后 `ai_status` 仍为 `completed`，AI 结果未被覆盖。

## 6. Android 真机验收（日志 + 数据库）

新 APK：

```text
SenseMind-Android-0.2.0-debug.apk
package: com.sensemind.app.debug
version: 0.2.0-debug
SHA-256: c88112a8f44241fe157a4fbaf9f71182c3bb14210f82687f0ffd58d0d11cf72d
```

手机 IP `172.20.10.14` 的服务端记录：

| 操作 | 证据 |
| --- | --- |
| 重新配对 | `POST /v1/pairings/exchange` 200；凭证「照护者-测试设备」创建于 10:38:02Z |
| 打开测试事件 | `GET /v1/caregiver/events/e3e47606…` 200（两次） |
| 人工确认 | `POST /v1/caregiver/events/e3e47606…/ack` 200 |
| 数据库确认记录 | `acknowledged=1`，`acknowledged_by=照护者-测试设备`，`acknowledged_at=2026-10-31T10:38:07.396801Z` |
| 授权前 alerts | 221 次 `403 consent_required`（含旧 APK 轮询） |
| 授予 remote_alerts | `POST /v1/consents` 200；随后 `GET /v1/alerts` 连续 4 次 200 |
| 撤销 remote_alerts | 第二次 `POST /v1/consents` 200；此后不再出现 `/v1/alerts` 请求 |
| 0.2.0 gating | 重配对后每轮 `GET /v1/me` 200；未授权时不请求 alerts |

限制：没有 ADB 设备，未采集手机屏幕截图；不能声称对手机像素级显示逐项核对。服务端请求日志证明手机实际打开了事件详情并完成确认，数据库与 Mac 控制台内容一致。

## 7. Mac 上位机同步

同一数据库、同一服务：

- `/v1/console/events/e3e47606…` 显示 `acknowledged=true`、`acknowledged_by=照护者-测试设备`。
- 时间线包含 `human_acknowledged`，actor 为「照护者-测试设备」。
- 调查仍为 `completed/completed`，证据 16，结构化 `inconclusive`。
- `/v1/console/snapshot?device_id=*` 同时列出 `guardian_001`、`guardian_002`，两者均为 replay。
- Mac 控制台页面已打开；未做自动化点击/截图，数据层一致性已通过控制台 API 验证。

## 8. remote_alerts 授权实现

后端策略：

| scope | 允许角色 | 说明 |
| --- | --- | --- |
| `upload` | wearer | 佩戴者上传数据 |
| `ai_analysis` | caregiver | 照护者请求 AI 分析 |
| `remote_alerts` | caregiver | 照护者接收告警 |

- 授权与撤销共用 `POST /v1/consents`，`granted=false` 即时生效。
- `legacy` 凭证保持旧兼容行为；新凭证按角色限制。
- 后端仍保留 `require_consent`，没有为了消除 403 而取消校验。
- Android 只在 `consents.remote_alerts=true` 时轮询 `/v1/alerts`；撤销后清空本地告警并停止轮询。
- 越权测试：caregiver 授 upload 403；wearer 授 remote_alerts 403；未知 scope 422；未认证 401；按凭证隔离，不因同家庭另一凭证授权而放行。

## 9. 自动化与构建

| 检查 | 结果 |
| --- | --- |
| 后端全量 | **169 passed / 1 warning**（16.39 s） |
| 新增同意/越权测试 | `tests/test_client_roles.py` 8 passed（含 3 个新场景） |
| Android JVM 单元测试 | **5 passed** |
| Android debug 构建 | 成功；`0.2.0-debug`；11 MB |
| Android manifest | debug `usesCleartextTraffic=true`；release 仍为 false |
| 模型状态 | activity / ppg_quality / ppg_hr 三个 available |
| 六模型产物 | 6 项 SHA-256 全部 `verified` |
| 后端 5xx | 0 |
| 密钥扫描 | APK 未发现 `sk-*`、`DEEPSEEK_API_KEY`、GitHub token |

## 10. 已知问题与限制

1. **手机视觉核对未自动化**：无 ADB、无截图；只验证了手机请求、数据库和控制台 API，不能声称屏幕逐像素一致。
2. **首次 DeepSeek 调用被丢弃**：8,000 token 上限导致 `cost_limit`，8525 tokens 已实际消耗但未形成报告；重试才成功。本次已将运行上限提高到 30000，代码默认仍为 8000。
3. **重试证据的时态**：证据幂等写入，`device_online` 等证据保留首次采集时的值；模型重试时读取最新状态，可能出现“证据表显示在线、叙述称数据陈旧”的时序差异。需要后续把证据与调查尝试版本绑定。
4. **replay 证据的 `synthetic=false`**：遥测证据由 `data_mode=replay` 标识，`synthetic` 标志仍为 false；UI 以 data_mode 显示 REPLAY，没有误标 Live，但字段语义可进一步收紧。
5. **无硬件验收**：nRF52840 BLE、H1 `/v1/hardware/packets`、断线重连、MTU/吞吐尚未验证；H1 仍返回 404。
6. **APK 为 debug 签名**：可侧载；正式分发需发布密钥。Release APK 未签名。
7. **无远程推送/后台常驻**：告警为前台轮询；`/v1/alerts` 目前为空，remote_alerts 只验证了授权门禁。
8. **Mac 控制台未做视觉截图**：页面已打开，数据通过 console API 核对。

## 11. 是否达到合并 main 条件

**达到软件闭环的条件性验收，但不建议未经确认直接合并 main。**

理由：

- 软件链路、真实 DeepSeek、证据、真机人工确认、Mac 同步、授权撤销和回归测试均有实际证据。
- 尚未完成手机屏幕截图级核对；无硬件/BLE 验收；首次调查成本上限问题已记录。
- 硬件 WIP 分支未修改、未合并；main 保持 `ef3d18f` 不变。

建议：用户确认本报告后，再决定是否将 `integration/client-loop` 合并 main；合并前重新拉取远程 main 并复跑测试。

## 12. 关键 SHA

```text
main                              ef3d18f
claude/mac-console                b9ffbb0
claude/android-app                d9ac302
integration/client-loop 代码提交  1bf08b4
integration/client-loop HEAD     见最终 git log（本报告提交）
```

复现命令：

```sh
cd <worktree>
.venv/bin/python -m pytest -q
cd android && JAVA_HOME=/opt/homebrew/opt/openjdk@17 ./gradlew --offline :app:testDebugUnitTest :app:assembleDebug
```

服务启动：

```sh
cd <worktree>
set -a; source <repo>/.env; set +a
GUARDIAN_DB=<workspace>/outputs/SenseMind-交接包/android-backend/guardian.db \
GUARDIAN_ML_DIR=<repo>/artifacts \
GUARDIAN_INVESTIGATION_MAX_LLM_TOKENS=30000 \
<repo>/.venv/bin/python -m uvicorn guardian.main:app --host 0.0.0.0 --port 8001
```
