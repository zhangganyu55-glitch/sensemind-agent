# 知微 SenseMind — 研究监测与只读调查原型 (v0.7)

> **公开快照（2026-11-05）**：`wip/r1-02.2-r1`。**NOT READY FOR R2**；P0-2 仍未解决。详见 [STATUS.md](STATUS.md) 与 [SECURITY.md](SECURITY.md)。
>
> **研究与黑客松演示原型；不是医疗器械，不具备经过验证的癫痫发作识别能力，不用于真实健康监护或紧急报警。**
>
> 本工程是独立编写的最小实现，没有从 RareGuard 或 APK 中复制源代码。MCP 是以后可选的外部工具协议，不是检测算法。
>
> **v0.6 Sensor Intelligence 基础功能**：数据集评估、ML 特征/加载器/推理 API、Agent 模型工具和数据集回放脚本已就绪；PAMAP2 活动模型已训练（测试准确率 75.0%），PPG 三模型接口与独立受试者回放已验收（研究原型），详见 [T4/T5 报告](docs/t4-t5-report.md)。进度见 `APK兼容层进度.md`。

> **v0.7 第一阶段**：新增 Live / Replay 来源模式、控制台模型状态与 HR 展示；真实 DeepSeek 使用安全合成数据完成工具与自主调查联调。质量审计、稳定性结果及运行命令见 [v0.7 报告](docs/v07-report.md)。

## 目前完成

- FastAPI 实时测量接入（POST `/v1/telemetry`）和 Pydantic 输入校验
- SQLite 历史数据、实验事件持久化与事件确认
- 已有 STM32F103 心率固件通过 ST-Link 只读接入；每秒上传、固件匹配检查、有效性处理及连接中断标记
- 可复现的演示数据发送脚本
- 实验规则（连续3次触发、连续3次恢复）、多信号并发标记、信号质量降级
- DeepSeek 大模型问答：先查设备状态，再按问题调用只读工具，根据真实查询结果生成中文解释；模型不可用时明确退回本地关键词查询
- 模型工具白名单、设备范围绑定、参数校验、调用次数/输出长度限制，以及不含密钥和原始对话的 SQLite 调用审计
- GET `/v1/tools` + POST `/v1/tools/call`（MCP-ready 设计思路，但 **不是** MCP server）
- **RareGuard APK `/v1` 契约层**：配对换 Bearer 凭证、三项独立授权、设备注册、批量上传（幂等/冲突检测）、测量分页、夜间摘要、观察记录、提醒与回执、AI 问答、实验配置版本、导出与删除
- APK 错误格式对齐：非 2xx 返回顶层 `code` / `message`，App 能直接显示具体原因
- 旧版 App 路径 `/v1/live/status`、`/v1/history`、`/v1/events`、`/v1/assistant` 继续保留；`/ws/live` WebSocket 状态推送（每秒1次）
- APK 上传的心率/血氧会镜像到 Agent 只读表，来源标记为 `ring`；`live_device` 数据在启用实验规则时会进入检测引擎
- **独立公共入口** `guardian.public:app`：只注册 APK `/v1` 契约与 `/healthz`，不注册文档、OpenAPI、STM32 遥测、Agent、WebSocket 或内部接口
- **配对安全**：配对码一次性、默认 10 分钟过期、每客户端 5 次失败后锁定 10 分钟；凭证默认 30 天过期，可立即撤销
- **统一遥测适配层** `guardian/adapters.py`：APK 每指标记录与 STM32 整包记录统一为 `UnifiedReading`，保留来源、质量和原始数据
- **自主调查 Agent**：规则事件自动触发后台调查，读取触发时刻、前后窗口、质量、相似事件和多传感器数据，输出结构化证据链；模型不可用/超时/非法工具调用时退回本地确定性调查
- **事件闭环**：基础告警由规则引擎创建，通知状态 `pending/sent/failed/suppressed_duplicate` 可追踪；人工确认与规则恢复分别记录，AI 没有确认或取消权限
- **本地监控控制台** `http://127.0.0.1:8000/console`：深色监测总览、心率/SpO₂/呼吸/运动、实时趋势、设备来源与新鲜度、系统/模型健康、调查状态、Evidence Engine 证据详情、工具调用、通知状态、人工确认和事件时间线；公共入口不提供。详见 `docs/Mac上位机使用说明.md`
- **v0.6 Sensor Intelligence（开发中）**：PAMAP2 活动模型已训练（测试准确率 75.0%，macro F1 73.9%）；`/v1/ml/*` 推理 API 与调查 Agent 的 `get_sensor_intelligence` 已接通
- **数据集回放** `scripts/replay_dataset.py`：单受试者连续活动 → ML 推理 → 1 Hz 遥测 → 规则事件/调查

## macOS / Linux 快速开始（Python 3.11 或以上）

macOS 可直接双击 `启动守护天使.command`。首次启动会创建独立 Python 环境并安装依赖，然后只在本机运行服务。
当前电脑的 `.env` 已单独配置；分享工程时不要附带这个文件。其他电脑按下面的步骤配置。

```bash
cd guardian-angel-agent-starter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，将 DEEPSEEK_API_KEY 填为你自己的 DeepSeek 官方密钥。
python -m pytest -q
uvicorn guardian.main:app --reload --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/docs` 查看交互接口文档；打开 `http://127.0.0.1:8000/console` 查看监控控制台。也可以双击 `启动Mac上位机.command`，服务就绪后自动打开控制台。Mac 上位机说明见 `docs/Mac上位机使用说明.md`。

另开终端（同项目目录）执行：

```bash
source .venv/bin/activate
python scripts/send_demo.py --scenario abnormal
```

然后查看 `http://127.0.0.1:8000/v1/events?device_id=guardian_001`，应看到异常信号的**实验事件**；同时刷新 `http://127.0.0.1:8000/console`，可看到 Agent 自动调查、工具调用、通知状态和人工确认按钮。事件在恢复阶段变为 `resolved`。

```bash
curl 'http://127.0.0.1:8000/v1/live/status?device_id=guardian_001'
curl -X POST 'http://127.0.0.1:8000/v1/assistant' -H 'Content-Type: application/json' -d '{"device_id":"guardian_001","question":"有异常事件吗？"}'
```

## 现有 RareGuard APK 接入

后端按 APK 的 `/v1` 契约提供接口，不只是复用路径：字段、UUID、批量收据、幂等规则、分页游标和错误码都对齐。`tests/test_apk.py` 覆盖契约流程，`tests/test_e2e_server.py` 用真实 uvicorn 进程覆盖联调与重启重试。

### 连接步骤

1. 双击 `启动守护天使.command` 启动**本地服务**（`127.0.0.1:8000`）。终端会打印一次性配对码：

   ```text
   [guardian] APK 配对码：xxxxxxxxxxxx（10 分钟内有效，仅可使用一次）
   ```

   需要给另一位家属发码时，在项目目录执行：

   ```bash
   source .venv/bin/activate
   python -m guardian.pairing --name 家属 --ttl 10
   ```

   `.env` 里的 `GUARDIAN_APK_PAIRING_CODE` 只作为**启动引导码**（每次启动重新生效、仍有 10 分钟有效期），日常请用上面的命令生成随机码。

2. **正式版 APK 只接受 HTTPS。** 本机演示有两种方式：
   - **临时隧道（推荐）**：本地服务启动后，双击 `手机接入HTTPS.command`。脚本会启动独立公共入口（`127.0.0.1:8010`，只暴露 APK `/v1` 与 `/healthz`），再打印 `https://xxxx.trycloudflare.com`，把这个地址填进 App 的「业务入口」。需要已安装 `cloudflared`（`brew install cloudflared`）。
   - **调试版 APK + USB 回环**：手机接 USB、开启调试，执行 `adb reverse tcp:8010 tcp:8010`，入口填 `http://127.0.0.1:8010`。正式版 APK 会拒绝这个地址。
3. 在 App「服务与隐私」里填业务入口和配对码，完成配对。配对码只能用一次；输错 5 次后该客户端会锁定 10 分钟。
4. 按 App 提示分别开启 **上传 / AI 分析 / 远程提醒** 授权。设备注册和上传需要 `upload`，问答需要 `ai_analysis`，提醒与回执需要 `remote_alerts`。
5. 配对成功后，戒指数据进入 APK 契约表；其中 `live_device` 的 `hr` / `spo2` 会镜像到 Agent 只读表（`source=ring`），可直接用 `/v1/live/status?device_id=<服务端设备UUID>` 查看。`device_history` / `replay` / `synthetic` 不会改写 Agent 的「最新数据」。

### 公共入口与本地链路（v0.4.1）

| 端口 | 应用 | 暴露内容 |
|------|------|----------|
| `127.0.0.1:8000` | `guardian.main:app` | 本地完整功能：STM32 遥测、Agent、WebSocket、APK 契约、`/docs` |
| `127.0.0.1:8010` | `guardian.public:app` | 仅 `/healthz` + 已认证 APK `/v1` 契约；文档/OpenAPI/旧接口一律 404 |

`cloudflared` 只指向 8010，因此公共 HTTPS 入口无法访问 `/v1/telemetry`、`/v1/assistant`、`/v1/tools`、`/ws/live` 或 `/docs`。STM32 → Python Bridge → `127.0.0.1:8000` 的本地链路保持不变。

### 契约与行为要点

- 所有需要登录的接口都要求 `Authorization: Bearer <token>`；token 只在配对时返回一次，服务端只存 SHA-256 哈希。凭证默认 30 天过期（`GUARDIAN_APK_CREDENTIAL_DAYS`），撤销后立即失效。
- 同一 `batch_id` 重复上传返回原收据；同 ID 不同内容返回 409。同一 `sample_id` 同理，整个批次会被拒绝。
- `GET /v1/measurements` 支持 `from` / `to` / `metric` / `cursor` / `limit` 分页；窗口最长 31 天。
- `GET /v1/summaries/night` 只做描述性统计和空窗说明，明确标注「不能证明连续监测或没有事件」。
- 删除设备数据（`DELETE /v1/devices/{id}/data?confirmation=delete_health_data`）会清理 APK 表，并同时删除镜像到 Agent 的心率/血氧、事件和问答审计。
- 真实设备（`stm32` / `ring`）默认不触发实验规则；只有 `GUARDIAN_EXPERIMENTAL_REAL_RULES=true` 才会运行。

### 安全边界（演示级，务必了解）

- 配对码：单次使用、默认 10 分钟过期、每客户端失败 5 次锁定 10 分钟（`GUARDIAN_APK_PAIRING_TTL_MINUTES`）。锁定计数在进程内，重启清零；它不是分布式限流。
- 失败计数按 `cf-connecting-ip` 区分客户端；只有隧道会写这个头。若公共入口被直接暴露，该头可被伪造。
- 凭证可自助撤销，也可由本机管理员撤销失窃设备：

  ```bash
  python -m guardian.pairing --list-credentials
  python -m guardian.pairing --revoke-credential <credential-id>
  ```

- 配对码只在**交互式终端**打印；输出被重定向到文件时不会写入代码（用 `python -m guardian.pairing` 查看）。公共入口进程不会生成或打印配对码。日志中不记录 Bearer token、配对码明文或请求体。
- 旧接口仍然没有鉴权，但它们只在 `127.0.0.1:8000` 上，公共入口不注册这些路由。不要把 8000 端口直接映射到公网。
- 演示结束按 `Control+C` 关闭隧道和公共入口。

## 统一遥测适配层（v0.4.1）

`guardian/adapters.py` 提供设备无关的内部模型，不改变任何对外契约：

```text
UnifiedReading(device_id, device_type, source, timestamp, metrics, quality, raw)
MetricSample(value, unit, quality, source, raw_metric)
```

- APK 每指标一条记录 → `readings_from_apk()`：按 `observed_at + source` 合并同一时刻的心率/血氧，质量取最差，原始记录完整保留在 `raw`。
- STM32/模拟器整包 → `reading_from_legacy()`：保留 `device_type`（`stm32_f103` / `simulator_v1`）与质量。
- 反向转换 → `to_legacy_telemetry()`：只接受 legacy 能无损表示的 `simulator` / `stm32` / `ring`；超出范围（如 hr > 250）、未来时间、`invalid` 质量直接丢弃，不猜测、不写错字段。
- 设备身份：APK 使用服务端设备 UUID，STM32 使用 `stm32_f103_001`，两者不会互相覆盖；`device_type` 用于区分。
- 扩展新硬件：调用 `register_metric()` / `register_device_type()`；无法映射到 legacy 存储的新传输会安全返回 `None`。
- Agent 只消费 `live_device` 且按时间单调递增的数据；历史/回放批次只留在 APK 表中，避免把「最新数据」倒退。

局限：统一模型目前是**内存转换层**，没有新增持久化表。nRF52840 / ESP32 / MPU6050 接入时，只需增加 adapter 与设备身份映射，不需要改 APK 契约。

## v0.5 自主调查与事件闭环

当规则引擎创建 `opened` 事件时，服务立即创建基础告警，并在同一进程内启动后台调查任务。**规则引擎始终是告警来源；AI 全程只读，不能确认、解除或发送告警。**

```text
遥测 / APK 数据 → 规则引擎创建事件
                    ├─ 立即创建基础告警（不依赖模型）
                    └─ 后台 Agent 调查
                         读取事件、最新数据、前后窗口、质量、相似事件
                         → 可选补充工具 → 结构化报告
控制台 WebSocket ← 通知状态（pending / sent / failed / suppressed_duplicate）
照护者在控制台人工确认（独立于规则恢复）
```

### 调查工具与边界

- 工具：`get_event`、`get_latest`、`get_window`、`get_quality`、`find_similar_events`、`get_history`、`summarize_status`
- 设备身份由服务端绑定，工具定义里没有 `device_id` 参数，模型不能跨设备读取
- 固定基础证据 5 次只读调用；模型最多追加到 `GUARDIAN_INVESTIGATION_MAX_TOOL_CALLS`（默认 8）
- 总时限 `GUARDIAN_INVESTIGATION_DEADLINE_SECONDS`（默认 20 秒）；超时、网络错误或非法工具调用时退回本地确定性调查，事件与基础告警不受影响
- 报告分层：`facts`（工具读取的可验证事实）、`rule_findings`（规则引擎判断）、`ai_inferences`（模型推测）、`uncertainties`、`recommended_actions`
- 本机只读查询：`GET /v1/events/{event_id}/investigation` 返回完整调查记录，`GET /v1/console/events/{event_id}` 返回证据链与时间线
- 数据质量不足、数据过期、时钟偏差、无历史都会写入不确定性；设备离线或信号无效不会被解释为正常

### 事件闭环与通知

- 每个事件一条通知记录（`event_notifications`），状态为 `pending` → `sent` / `failed` / `suppressed_duplicate`
- 只有控制台 WebSocket 实际发送成功才标记 `sent`；默认 60 秒内没有控制台接收会标记 `failed` 并记录原因
- 同一设备、同一异常类型在冷却期（默认 300 秒）内重复触发，新通知标记为 `suppressed_duplicate`，避免通知风暴
- 人工确认记录 `acknowledged_at` / `acknowledged_by`，与规则解除 `status=resolved` / `closed_at` 分开；确认不会关闭事件，恢复也不会自动确认
- AI 工具集中没有确认、解除、删除或发送告警的操作

### 监控控制台

本地服务启动后打开 `http://127.0.0.1:8000/console`（公共入口不提供此页面）：

- 顶部：活动事件、调查中、待发送告警、待人工确认
- 左侧：设备状态与事件列表
- 右侧：调查状态、数据质量、可验证事实、规则判断、AI 推测、不确定性、建议、关键历史、工具调用记录、通知状态、事件时间线
- 人工确认：填写确认人；若设置 `GUARDIAN_CONSOLE_TOKEN`，还需输入控制台令牌

### 配置

```dotenv
GUARDIAN_INVESTIGATION_MAX_TOOL_CALLS=8
GUARDIAN_INVESTIGATION_DEADLINE_SECONDS=20
GUARDIAN_NOTIFICATION_TTL_SECONDS=60
GUARDIAN_NOTIFICATION_COOLDOWN_SECONDS=300
# GUARDIAN_CONSOLE_TOKEN=可选的本地确认令牌
```

## v0.6 Sensor Intelligence（开发中）

v0.6 把公共数据集训练的实验模型接进调查链路。模型只作为辅助证据，不改变规则引擎的告警权，也不能确认、解除或发送告警。

### 数据集与许可

- **PAMAP2**：腕部 IMU 100 Hz + 心率，CC BY 4.0；使用 Protocol 的 9 名受试者。用于活动识别。
- **PPG-DaLiA**：Empatica E4 BVP 64 Hz / ACC 32 Hz + ECG 参考心率；UCI 页面标 CC BY 4.0，原始 Siegen 页面写科研非商业，商业使用前必须联系作者。用于 PPG 质量/HR 基线。
- 六个候选数据集的比较见 [docs/dataset-evaluation.md](docs/dataset-evaluation.md)。

### 模型与指标

| 模型 | 数据 | 指标 | 产物 |
|---|---|---|---|
| 活动识别 RandomForest | PAMAP2，受试者隔离 split | 测试准确率 75.0%，macro F1 73.9%，多数类基线 11.2% | `artifacts/activity/activity_rf.joblib` |
| PPG 质量 / HR | PPG-DaLiA | 已训练，受质量门控保护 | `artifacts/ppg/` |

> 活动模型的 walking 召回率仅 0.21，常被判成 standing / ascending_stairs；running / nordic_walking / cycling 的 F1 在 0.94–0.98。模型输出只作辅助证据，不能替代规则或人工判断。

### ML API

- `GET /v1/ml/status`：模型是否可加载、版本和特征版本。
- `POST /v1/ml/analyze`：提交一个窗口的 `acc` / `ppg` / `hr`，返回活动、置信度、PPG 可靠性、异常评分、HR 估计和模型版本；请求不接受参考标签，避免标签泄漏。
- `GET /v1/ml/inferences?device_id=...`：最近的推理记录。
- 调查 Agent 的只读工具 `get_sensor_intelligence` 会把模型证据写进报告的 `model_evidence`；模型证据不覆盖规则判断。

### 数据集回放

```bash
# 终端 1
uvicorn guardian.main:app --host 127.0.0.1 --port 8000

# 终端 2：回放 PAMAP2 某个受试者的一段连续活动
python scripts/replay_dataset.py --dataset pamap2 --subject 105 --activity 5 --seconds 60 --speed 4

# 回放 PPG-DaLiA（必须显式传 --subject；不会默认选择 S13）
python scripts/replay_dataset.py --dataset ppg --subject S1 --activity 7 --seconds 60 --speed 4
```

回放只使用单个受试者的一段连续活动，不会拼接不同受试者或活动；每秒调用 `/v1/ml/analyze`（窗口填满后才推理），再向 `/v1/telemetry` 发送 1 Hz 摘要，并在遥测行保留 `dataset` / `participant_id` / `sampling_hz`。设备 ID 默认自动选择未使用的 `guardian_replay*`。

### 当前限制（v0.6）

- 模型是实验基线，未经临床验证，不能用于诊断、急救或安全告警。
- PPG 质量标签是代理标签（PPG HR 与 ECG HR 误差 ≤10 bpm），不是医学 SQI。
- PAMAP2 100 Hz / m/s² 与 MPU6050、PPG-DaLiA 64/32 Hz 与 MAX30102 都需要单位/采样率适配后才能用于真实硬件。
- PPG 模型已本地训练；T4/T5 验收、恢复说明和限制见 [验收报告](docs/t4-t5-report.md)。
- 尚未在真实 STM32 / 戒指 / 手机上完成端到端验证。

## 版本记录

- **v0.5.0（2026-10-29）**：自主调查 Agent、结构化证据链、通知闭环、人工确认、监控控制台；新增 13 项调查测试（总计 87 项）。
- **v0.4.1（2026-10-29）**：独立公共入口与路由隔离；配对码单次使用/过期/失败锁定；凭证过期与 CLI 撤销；统一遥测适配层；APK `live_device` 数据接入检测引擎；新增 17 项测试（总计 74 项）。
- **v0.4.0**：RareGuard APK `/v1` 契约层、错误格式对齐、HTTPS 接入脚本、12 项契约测试。
- **v0.3**：STM32/ST-Link 只读接入、DeepSeek 只读工具 Agent、实验规则。

## 大模型接入与使用

项目自动加载根目录 `.env`，同名系统环境变量优先。默认模型为 `deepseek-flash`，请求发送至 DeepSeek 官方 `https://api.deepseek.com/chat/completions`，使用非思考模式与 Function Calling。接口依据：[DeepSeek 工具调用文档](https://api-docs.deepseek.com/guides/tool_calls/)。

```dotenv
GUARDIAN_LLM_MODE=deepseek
GUARDIAN_LLM_MODEL=deepseek-flash
DEEPSEEK_API_KEY=你的官方密钥
GUARDIAN_LLM_TIMEOUT=45
```

- 打开 `/health`，`assistant.enabled=true` 表示密钥已配置；真正的连通性需用 `/v1/assistant` 验证。
- 调用 `/v1/assistant` 时，填 `device_id` 和 `question`，例如「当前设备数据新鲜吗？」或「最近 10 条心率有什么变化？」。
- 第一轮强制查询 `summarize_status`，后续由模型选择 `get_latest`、`get_history`、`list_events` 或 `summarize_status`。设备编号由服务端绑定，模型不能改选其他设备。
- 响应中的 `response` 是中文解释；`tools_used` 是实际查询及原始结果；`usage` 是本次累计 token 用量；`request_id` 可与数据库 `assistant_runs` 中的审计记录对应。单次问答最多 4 次模型请求、8 次只读工具执行，每次最多生成 1200 token。
- 成功时 `agent_mode=llm_tool_agent`。超时、网络错误、余额不足、服务错误或非法工具调用时，返回本地查询，`agent_mode=deterministic_tool_router_no_llm`，并通过 `llm_error` 说明失败原因；不会把失败伪装成模型回答。
- 设置 `GUARDIAN_LLM_MODE=off` 并重启可使用离线查询；没有配置密钥时也只做本地查询。
- 每次问答独立处理，暂不保留跨轮对话；「刚才那条」这类追问需在问题中补全范围。
- 模型请求会把本次问题及工具查询得到的设备记录发送到 DeepSeek 官方服务，并消耗相应 API 额度。演示请用模拟数据；是否发送真实个人健康数据需由数据本人确认。
- 密钥只放 `.env` 或系统环境变量，不能写入源代码、截图或分享包。SQLite 审计仅存请求编号、设备、模型、工具名/参数、用量及错误码，不保存问题、回答和思考内容。

## 现有 STM32 心率工程接入

本机使用已有 `~/stm32-projects/f103-blink` 工程、ST-Link 和 OpenOCD 读取真实设备数据，不改固件，不执行烧录、复位或暂停操作。

1. 双击 `启动守护天使.command` 启动 Agent 服务；服务已运行时不要重复启动。
2. 保持开发板、MAX30102 和 ST-Link 连接，双击 `连接STM32.command`。
3. 连接程序先核对本地 ELF/BIN 和板上固件是否一致，再每秒读取并上传一次。设备编号为 `stm32_f103_001`。
4. 在 `/v1/assistant` 使用这个设备编号提问；例如「查询设备当前心率、血氧和采样状态，说明哪些读数有效」。模型调用才会触发 DeepSeek 请求，数据采集本身不会反复调用模型。

命令行方式（macOS/Linux）：

```bash
.venv/bin/python -m guardian.stm32_bridge --project "$HOME/stm32-projects/f103-blink"
```

- 运行环境需已有 OpenOCD、配套 Arm 工具链以及 `build/f103-blink.elf` 和 `.bin`。换电脑时仅有旧压缩包里的 BIN 不够，需保留或重新生成配套 ELF。
- 读取程序会启动自己的本机 OpenOCD 子进程，退出时只关闭这个子进程。不要同时启动其他占用同一个 ST-Link 的调试工具。重复运行本项目的连接程序会被锁阻止。
- 接入的记录带 `source=stm32` 和 `device_status`：采样计数、采样状态、手指检测、固件有效标记、弱信号标记及溢出计数；血氧明确标记为固件估算值。
- 首次读取或采样计数重置时为 `pending`，等计数增长后才认可新测量；同一计数的旧读数不重复上传为有效记录。计数停止增长 3 秒后为 `stalled`，调试连接断开为 `disconnected`，两者的测量值均置空并标为无效。
- 无手指、弱信号或该指标未有效时，该指标为 `null`，不会伪造为 0 或正常值。心率、血氧分别检查有效性；`reading_valid` 表示至少有一个可用指标，不能表示所有字段都有值。
- 当前固件没有运动数据，所有加速度和陀螺仪字段保持 `null`。现有 ST-Link 读内存方式用于桌面演示，不能作为已验证的照护监测。
- 设备中断后会尝试重连。关闭连接窗口后再次运行可重新接入。真实数据的自动生理事件规则仍保持默认关闭。

## 样例数据协议

```json
{
  "device_id": "guardian_001",
  "timestamp": "2026-10-28T09:00:00Z",
  "source": "simulator",
  "heart_rate": 75,
  "spo2": 98,
  "acc_x": 0,
  "acc_y": 0,
  "acc_z": 1,
  "signal_quality": "good",
  "reading_valid": true
}
```

`timestamp` 必须包含时区；`acc_*` 单位约定为 g；真实 STM32 接入时应标记 `source="stm32"`。实际时钟同步、采样频率、单位及测量质量判定需在固件端确认。

## 安全设计

- **默认真实硬件数据只存储，不自动根据生理数值报警**。仅用于隔离的实验时，可显式设置 `GUARDIAN_EXPERIMENTAL_REAL_RULES=true` 启用相同的实验规则；绝不能将结果用于照护决策。
- `heart_rate>=120`、`spo2<=93`、`|acc|>=2.5g` 仅是**演示用软件测试数字**，既非医疗阈值，也不构成癫痫判定标准。
- poor/unknown 信号无法证明健康安全：系统会产生 `data_quality` 事件，并暂停生理信号判断；不能据此解除既有生理事件。
- 缺少某个测量值时，该信号的连续计数重置，不能把缺测当成恢复；缺测也会计入 `data_quality`。并发信号不足两项但还有缺测时，不解除已有并发事件。
- 使用实际时间值比较先后顺序，支持同一秒内的多条采样；超前服务器时钟 30 秒以上的时间戳会被拒绝，避免错误时间卡住后续接收。
- 事件需要3个连续样本才触发或解除；**实际采样频率未固定，不可等同于持续3秒**。
- SQLite 单实例、内存计数器、同步写入仅为单进程演示，不适合多进程生产环境；重启时计数器重置，但事件历史保留。
- APK 契约层有 Bearer 配对认证、三步授权、凭证过期与撤销；公共入口只注册这一层。旧接口（`/v1/assistant`、`/v1/telemetry`、`/v1/tools` 等）**没有鉴权**，但只存在于 `127.0.0.1:8000`，不会出现在公共入口。不要把 8000 端口直接暴露到公网、校园网或医疗场景。
- 自主调查只使用只读工具；AI 工具集中没有确认、解除、删除或发送告警的操作。基础告警由规则引擎创建，模型失败不会撤销。
- 监控控制台只在本地应用注册；人工确认要求填写确认人，可选 `GUARDIAN_CONSOLE_TOKEN` 令牌。人工确认与规则恢复分开记录，确认不会改变事件状态。
- 通知状态和失败原因保存在 SQLite；同一设备同一类型的重复告警在冷却期内标记为 `suppressed_duplicate`。
- 调查报告包含健康数据、工具结果摘要和 AI 叙述，保存在本地 SQLite；分享工程时不要附带 `guardian.db`。
- 设备断连不会自动创建规则事件，但调查报告、`/v1/live/status` 和控制台会把 `stale`/无数据标记为不确定性，不能解释为正常。
- 仅存储演示所需数据，避免将姓名、医疗病历等个人敏感信息传入开发数据库。

## 已知限制与剩余问题（v0.5 / v0.6）

- 未完成真实手机 + Cloudflare TLS 联调，也未在本轮连接真实 STM32/戒指；自动化 E2E 使用明确标记的模拟数据和 loopback 上的相同 ASGI 应用。
- 调查 worker 是单进程后台线程，不是持久化任务队列；进程崩溃时 `running`/`queued` 记录可能停留在原状态，需要人工重新触发。
- 通知渠道只有本地 WebSocket 控制台；没有 Android 推送、短信或电话，控制台未连接时通知会在 TTL 后标记失败。
- 配对失败锁定与通知去重都是单机 SQLite/进程级实现，不适合多副本或高并发部署。
- `ai_inferences` 是模型基于有限样本的解释，未经人工验证；AI 叙述可能出错，事实部分才来自工具读取。
- 工具调用记录保存摘要而非完整原始样本；关键历史最多 12 条，完整数据仍在 SQLite。
- 控制台没有角色/多用户权限，只有本机绑定和可选共享令牌 `GUARDIAN_CONSOLE_TOKEN`。
- 所有配对凭证共享同一个 `family_id`，没有多家庭审批模型。
- 实验规则沿用旧引擎：某个信号缺失会重置该信号计数；交替上传且时间戳不同的 hr/spo2 可能无法累积连续事件。
- 导出接口一次返回设备全部样本，没有分页；未实现令牌刷新、设备证书或审计告警。
- PAMAP2 活动模型是受试者隔离的实验基线；walking 召回率 0.21，模型输出只作辅助证据，不能覆盖规则。
- PPG 模型与真实科研数据回放已验收，详见 [T4/T5 报告](docs/t4-t5-report.md)。
- 数据集回放使用公共数据集，不是真实硬件数据；PAMAP2 的 m/s²/100 Hz 与 PPG-DaLiA 的 64/32 Hz 都需要适配后才能用于 MPU6050/MAX30102。

## 下一阶段任务（从高到低）

1. 在独立验证集上研究 HR 质量门控的过度隐藏与分布偏移；当前阈值保持 0.7，不能用测试片段直接调低阈值。
2. 在真实 STM32 和真实手机上完成“遥测 → 规则事件 → 调查 → 通知 → 人工确认”的端到端验证。
3. 把调查队列和通知重试持久化（任务状态恢复、失败重试、多通知渠道），避免进程重启丢失队列。
4. 为控制台增加角色权限、操作审计导出，并把人工确认接入 APK/短信等真实通知渠道。
5. 对 AI 调查做提示注入与安全评估，限制模型输出长度和行动建议范围，补充结构化报告回归测试。
6. 保留现有 STM32/ST-Link 接入；nRF52840 到货后按实际传感器补充 adapter 与设备身份映射。
7. 如确有需要，再按官方 MCP 协议实现只读资源/工具适配层，并在授权设备范围内调用。

## 独立开发声明

项目架构自主实现、代码独立编写；只参考通用数据管理思路、MCP工具化访问思想。使用前应检查相关依赖许可证以及比赛规则。
