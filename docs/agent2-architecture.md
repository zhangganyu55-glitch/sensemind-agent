# SenseMind Agent 2.0 — 自主调查与多模态证据引擎

基准：`ee5b93b`（`codex/ppg-training`，v0.7 第一阶段）
开发分支：`claude/agent2`
状态：**已实现并测试，未合并 main**。硬件协议 H1、nRF/ESP32 固件和已有模型均未修改。

## 1. 组件

| 文件 | 职责 |
| --- | --- |
| `guardian/evidence.py` | 统一证据模型、来源采集、可用性、时间对齐、持久化 |
| `guardian/reasoner.py` | 结构化推理契约、证据引用/数值/危险建议校验、确定性降级 |
| `guardian/investigation.py` | 调查状态机、队列/并发、去重、重试、恢复、成本上限、后续证据 |
| `guardian/synthetic.py` | 10 个明确标记 synthetic 的多模态场景 |
| `guardian/storage.py` | `evidence`、`investigation_transitions` 和调查扩展字段 |
| `tests/test_agent2_*.py` | 证据、推理、管理器、场景、API 访问控制测试 |

## 2. 证据模型

每条证据包含：

- `evidence_id`（`ev_...`）、`event_id`、`source_type`、`device_id`
- `timestamp`、`value`、`unit`、`quality_status`
- `data_mode`（`live` / `replay` / `unknown`）
- `model_version`（如适用）
- `time_sync_status`、`clock_uncertainty_ms`
- `synthetic`、`source_ref`、`metadata`

`source_type`：`ppg`、`heart_rate_estimate`、`signal_quality`、`activity`、
`imu_raw`、`radar_status`、`breathing_rate`、`device_online`、
`historical_event`、`device_status`。

约束：

- 同一 `(event_id, source_ref)` 幂等；重试不会重复写入。
- 未接入/无可用数据的传感器在 `availability` 中明确 `available=false` 并给出原因
  （`sensor_not_connected`、`raw_ppg_not_ingested`、`no_usable_data` 等）。
- `data_mode` 由来源推导；synthetic/replay 永不标成 live。
- `alignment.fusion_allowed` 只有在跨设备的时钟状态为 `synced`/`gateway_estimated`
  且不确定度不超过容忍值时才为 true；否则保留各设备原始证据并说明原因，不强融。
- 额外物理证据默认必须 `synthetic=true`；没有硬件来源时拒绝伪装 live。

## 3. 调查状态机

```text
queued ──▶ running ──▶ completed
             │  ▲
             ▼  │
          retrying
             │
             └──▶ failed（不可恢复/次数耗尽）
```

- 合法边：`queued→running`、`running→completed|failed|retrying`、
  `retrying→running|failed`、`failed→queued`（人工重试）。
- 每次转换写入 `investigation_transitions`，包含 from/to/reason/时间。
- 服务重启：只恢复 `queued/running/retrying` 且未完成的调查；`completed` 永不重跑。
- 已完成调查的后续遥测通过 `supplement()` 追加证据，不重复调用模型。

## 4. 管理器能力

| 能力 | 实现 |
| --- | --- |
| 自动触发 | 规则引擎 `opened` 转换自动创建调查 |
| 重复事件合并 | 同一设备/类型在 `duplicate_window_seconds` 内标记 `duplicate_of_event_id`，复用证据链，不重复调用模型 |
| 超时 | LLM 单次 deadline + 调查总 deadline |
| 并发 | `max_workers`（1–4），队列 + 多 worker |
| 重试 | 仅对 `timeout/network_error/rate_limited/provider_unavailable`，有上限和退避 |
| 幂等 | 证据 `source_ref` 唯一；已完成调查不重跑；重试复用证据 |
| 持久化 | 调查状态、转换、证据、结构化结果、usage 均落 SQLite |
| 后续证据 | `supplement` / `supplement_active` 追加并更新 `follow_up_evidence_count` |
| 成本限制 | `max_tool_calls`、`max_llm_tokens`；超限安全降级 |

默认值：`max_attempts=2`、`max_workers=1`、`max_llm_tokens=8000`、
`total_deadline_seconds=30`、`duplicate_window_seconds=300`。
均可通过 `GUARDIAN_INVESTIGATION_*` 环境变量或 `create_app` 参数覆盖。

## 5. 结构化推理

输出契约（`StructuredAnalysis`）：

```json
{
  "event_id": "...",
  "investigation_status": "completed|inconclusive",
  "observations": [{"text": "...", "evidence_refs": ["ev_..."]}],
  "evidence_refs": ["ev_..."],
  "uncertainties": ["..."],
  "conflicting_evidence": ["..."],
  "missing_data": ["..."],
  "conclusion": "...",
  "recommended_action": ["..."]
}
```

校验：

- `event_id` 必须匹配；所有 evidence_refs 必须存在。
- 每条 observation 至少绑定一个真实 evidence_id。
- observation/conclusion 中的数字必须在引用证据、事件或上下文中出现；
  未出现的数字按 `fabricated_measurement` 拒绝。
- 含“发送报警/触发告警/拨打急救”等高风险建议按 `unsafe_recommended_action` 拒绝。
- 证据不足时 `completed` 自动降级为 `inconclusive`。
- 校验失败不采用模型文本，改用确定性证据摘要，并记录错误码。
- 模型没有写工具；不能确认/取消事件或发送告警。

## 6. API

本地应用新增（public app 不注册，返回 404）：

- `GET /v1/investigations/{id}`：调查 + 状态转换 + 证据
- `GET /v1/events/{event_id}/evidence`
- `GET /v1/evidence/{evidence_id}`
- `POST /v1/investigations/{id}/retry`：需要 console token（配置时），只允许失败/超时等可重试状态

## 7. 合成多模态场景

`guardian/synthetic.py` 提供 10 个场景，全部 `synthetic=true`、`data_mode=replay`：

1. 心率变化伴随运动
2. 心率变化但 PPG 质量差
3. 雷达目标丢失
4. 雷达呼吸数据中断
5. 手环离线
6. 多传感器时间戳不一致
7. 多传感器结果矛盾
8. 正常翻身
9. 重复事件
10. Agent API 超时

这些场景只验证软件逻辑，不声明模型具备疾病检测能力。

## 8. 实际测试结果

```text
.venv/bin/python -m pytest -q
161 passed, 1 warning in 12.70s
```

新增覆盖：

- 证据字段、幂等、事件/设备隔离、replay/live、不可用传感器、时间对齐
- 状态机转换、重复合并、重试、恢复、成本上限、后续证据、双 worker
- 结构化引用/数值/危险建议校验、证据不足降级、确定性 fallback
- 10 个合成场景
- 新增接口的 public 404 与 console token 访问控制

真实 DeepSeek 调用（`docs/agent2-deepseek.json`，纯合成数据）：

```text
status=completed  ai_status=completed
evidence=13  structured_status=inconclusive
observations=4  evidence_refs=11
usage={prompt_tokens: 4290, completion_tokens: 918, total_tokens: 5208}
duration=5.32s
```

模型正确识别了 replay/simulator 来源、IMU 恒定值、缺少原始 PPG/活动/雷达/呼吸数据、
时间同步不确定度，并给出 inconclusive 与保守建议。没有发送真实用户或数据集信号。

## 9. 已知限制

- 没有真实硬件联调；雷达/呼吸/设备在线只有合成证据。
- 原始 PPG 未入库，`ppg` 源返回 `raw_ppg_not_ingested`。
- 质量标签仍是 HR 一致性代理，不是独立医学 SQI。
- 大模型输出存在波动；校验拦截了不存在的引用和编造数值，但不能证明模型推理正确。
- 成本上限按 token 计数，不是美元计费；DeepSeek 价格变化需另行核算。
- 本轮未修改 H1 协议、nRF/ESP32 固件和已训练模型；未合并 main。

## 10. 复现

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_artifacts.py
.venv/bin/python scripts/verify_agent2_deepseek.py   # 使用 .env，产生 API 费用
```

访问控制：public app 不注册上述新接口；本地 retry 在设置
`GUARDIAN_CONSOLE_TOKEN` 时要求 `X-Console-Token`。
