# SenseMind Agent 3.0 — Decision Engine V0.1

> 状态：**已在独立分支实现并测试，未合并 main**
> 分支：`claude/agent3-decision-engine`
> 基线：`main` @ `f397b1e`（169 passed）
> 本轮结果：**194 passed / 1 warning（169 基线 + 25 新增）**
> 范围：只做 Decision Engine V0.1；不含完整 State Engine、Personal Memory、Android UI、H1 合并。

## 1. 架构审查结论

### 1.1 State Engine 不存在

审查了当前仓库、15 个本地/远程分支和 9 个 worktree，`StateEngine`、`state_engine`、
`DecisionEngine`、`决策引擎` **零命中**。现有状态能力只有：

| 现状 | 位置 | 局限 |
| --- | --- | --- |
| 规则计数器 | `guardian/detection.py` | 内存 `defaultdict`，重启丢失 |
| 设备新鲜度 | `guardian/agent.py` | 只计算 stale/clock_skew，不产生决策 |
| 半窗趋势 | `guardian/investigation.py` | 只用于调查报告，不参与触发 |

因此 V0.1 **没有假设 State Engine 已完成**，也没有顺手实现通用状态机。决策所需状态全部从
`telemetry`、`events`、`decisions` 推导；唯一新增持久化是决策审计表。

### 1.2 现有触发链路

```text
/v1/telemetry
  → storage.put_telemetry
  → DetectionEngine.analyze
  → transitions
  → InvestigationService.on_transitions
  → event_notifications + investigations

APK /v1/upload
  → _adapt_to_agent (mirror to telemetry + DetectionEngine)
  → investigations.on_transitions
```

- 规则引擎是唯一事件源；正常时系统不产生任何记录。
- 真实设备 `allow_real_rules=False` 的保护没有改动。
- 调查状态机、去重、并发、重试、恢复、结构化校验和权限边界均复用 Agent 2.0。

## 2. Decision Engine 设计

### 2.1 决策类型

| 类型 | 含义 | 是否创建调查 | 是否通知 |
| --- | --- | --- | --- |
| `OBSERVE` | 持续观察，数据质量良好 | 否 | 否 |
| `VERIFY` | 先核实数据质量/新鲜度/上下文 | 否（规则已建则关联） | 沿用规则原有行为 |
| `INVESTIGATE` | 触发已有 Agent 调查 | 是（复用现有服务） | 规则事件沿用；`sustained_hr_change` 不通知 |
| `INSIGHT` | 非紧急状态洞察 | 否 | 否 |

### 2.2 决策记录字段

每条记录包含：

- `decision_id`、`device_id`、`event_id`、`investigation_id`
- `decision_type`、`source`、`reason_code`
- `basis`（JSON：质量、数据年龄、基线、偏离量、阈值、连续样本、事件类型等）
- `data_quality`、`data_mode`、`decided_at`
- `status`：`recorded` / `processed` / `suppressed` / `failed`
- `summary`、`policy_version`、`dedup_key`、`seen_count`

**没有 LLM 置信度字段。** Decision Engine 不调用 DeepSeek；DeepSeek 仍只在现有
Agent 2.0 调查里提供解释和建议，不能改变决策类型、通知策略或安全规则。

### 2.3 优先级（每次评估可产生多条决策）

1. 规则引擎 `opened` 生理事件 → `INVESTIGATE`，关联已有调查。
2. 规则引擎 `opened data_quality` → `VERIFY`，关联已有调查。
3. 规则引擎 `resolved` → `INSIGHT`，明确“规则解除不等于身体恢复”。
4. 质量差/未知/stale/`pending` → `VERIFY`，**绝不输出 OBSERVE**。
5. 持续变化 → `INVESTIGATE`（内部事件，不推送照护者）。
6. 质量恢复 → `INSIGHT`（采集恢复，不代表健康恢复）。
7. 其余 → `OBSERVE` 心跳（每设备 60 秒最多一条）。

同一 tick 的多个规则事件逐个记录，不会因为“单主决策”被丢弃。

### 2.4 持续变化判定

全部为确定性软件规则，**不是医学阈值**：

| 参数 | 默认值 |
| --- | --- |
| 基线窗口 | 最近 300 秒 |
| 基线最少样本 | 20 |
| 基线最短跨度 | 60 秒 |
| 持续变化最少样本 | 10 |
| 持续变化最短真实时长 | 30 秒 |
| 最大采样间隔 | 3 秒 |
| 偏离阈值 | `max(15 bpm, 基线的 15%)` |
| 冷却 | 600 秒 |

实现要点：

- 时间戳必须严格递增；重复时间戳按时间去重，乱序数据排序后判定。
- 用真实时间跨度、采样间隔和样本数共同判断，不只看样本数量。
- 两遍检测：第一遍定位最近候选 run；第二遍只用 **run 起点之前** 的数据重建基线，
  避免候选异常污染基线。
- 基线不足、采样不连续、方向不一致时**不产生事件**。
- 事件 `opened_at` 取第一个偏离样本的时间，后续判断解除时基线仍然排除候选期。
- 真实设备（`stm32/ring/esp32`）默认不执行持续变化检测；需要同时满足
  `GUARDIAN_DECISION_ENGINE=on` 和 `GUARDIAN_DECISION_REAL_SUSTAINED=on`。

### 2.5 通知策略

`InvestigationService.on_transitions` 增加了真实的 `notify` 分支：

```python
def on_transitions(self, device_id, transitions, *, notify=True):
    ...
    if notify:
        self.storage.create_notification(...)
```

- 原有规则调用不传 `notify`，默认 `True`，通知行为完全不变。
- Decision Engine 创建 `sustained_hr_change` 时显式传 `notify=False`：
  - 创建内部事件 ✅
  - 创建调查 ✅
  - 持久化证据/结果 ✅
  - 生成非紧急 Insight ✅
  - 不创建 `event_notifications` ✅
- 这不是“无效字段”：测试直接断言该事件的 `notifications` 为空，同时原有生理告警仍有通知。

### 2.6 幂等与审计增长控制

| 场景 | 处理 |
| --- | --- |
| 同一事件重复评估 | 相同 `dedup_key`；`seen_count += 1`，不新增行 |
| 冷却期内新发现 | 新 `suppressed` 记录，按冷却桶去重 |
| 已抑制的调查 | 关联已有 `event_id`，`status=suppressed` |
| 正常观察心跳 | 每设备 60 秒一条 OBSERVE |
| VERIFY | 每设备/原因 300 秒一条 |
| 质量恢复 Insight | 每个 VERIFY 决策最多一条 |
| 不同事件同一时间桶 | `dedup_key` 包含 `event_id`，不会错误合并 |
| 多进程并发写同一决策 | SQLite `dedup_key UNIQUE` + `IntegrityError` 回退，first writer wins |
| 并发 sweep/ingest | 决策 claim 先落库；事件创建再做 active 检查 |

### 2.7 故障隔离

- `/v1/telemetry`、APK ingest 调用 `decisions.evaluate` 均包裹 `try/except`。
- 决策模块异常不会阻断遥测入库、规则事件、通知或现有调查。
- 测试通过 monkeypatch 让 `evaluate` 直接抛异常，验证原有规则告警仍然创建。

## 3. 接入点

| 位置 | 调用 |
| --- | --- |
| `guardian/factory.py` `/v1/telemetry` | `decisions.evaluate(device_id, transitions)` |
| `guardian/apk_api.py` APK ingest | `decision_engine.evaluate(device_id, transitions)` |
| `guardian/decision.py` 后台 sweep | 每 10 秒扫描已知设备，`no_data/stale` → VERIFY |

后台 sweep 通过 FastAPI lifespan 启停；测试使用 `decision_background=False` 和显式时钟。

## 4. 开关

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `GUARDIAN_DECISION_ENGINE` | **off** | 主开关；开发和演示时显式设为 `on` |
| `GUARDIAN_DECISION_REAL_SUSTAINED` | **off** | 真实设备持续变化检测的二次开关 |
| `GUARDIAN_DECISION_BACKGROUND` | on | 引擎开启时是否启动 sweep |

当前版本默认不改变任何已有稳定功能的行为。

## 5. API

仅注册在本地 app；`public_only` 的 APK app 返回 404：

- `GET /v1/decisions?device_id=&limit=&decision_type=`
- `GET /v1/decisions/{decision_id}`
- `GET /v1/events/{event_id}/decisions`
- `GET /v1/investigations/{investigation_id}` 新增 `decisions` 字段（附加字段，不破坏原结构）
- `GET /health` 新增 `decision_engine` 状态

## 6. H1 接入检查

### 6.1 分支关系

| 分支 | 与 main 关系 | H1 状态 |
| --- | --- | --- |
| `main` @ `f397b1e` | — | **无 `/v1/hardware/packets`；文档明确当前 404** |
| `claude/hardware-api-v1` | 基于 main，领先 7 个提交 | 有完整 H1 receiver |
| `claude/v09-ble-reconnect` | 基于 main，领先 8 个提交 | 含 H1 相关改动 |
| `codex/v07-hardware-api` | 基于 `ee5b93b`，落后 main 16 个提交 | 早期 H1 实现 |
| `codex/v07-hardware-integration` | 基于 `ee5b93b`，落后 main 16 个提交 | 早期硬件集成 |

### 6.2 数据路径

`claude/hardware-api-v1` 的 `guardian/hardware/ingest.py` 已把认证后的 H1 数据镜像到
现有 `telemetry` 表，并调用 `engine.analyze(...)`、`investigations.on_transitions(...)`。
**将来合并 H1 时，只需在同一处 `on_transitions` 之后增加一行：**

```python
decision_engine.evaluate(packet.device_id, transitions)
```

本轮**没有合并、没有改动** H1 分支或协议；main 上 Decision Engine 目前只覆盖
`/v1/telemetry` 与 APK ingest 两条路径。

## 7. 测试结果

```text
.venv/bin/python -m pytest -q
194 passed, 1 warning in 17.50s
```

25 个新增测试覆盖：

| 编号 | 场景 | 测试 |
| --- | --- | --- |
| 1 | 正常状态不产生调查 | `test_normal_state_records_observe_without_investigation` |
| 2 | 数据不可靠优先核实 | `test_unreliable_data_records_verify_and_never_observe` |
| 3 | 无遥测不是正常 | `test_missing_telemetry_is_verify_not_observe` |
| 4 | 持续变化创建调查 | `test_sustained_change_creates_one_investigation_without_notification` |
| 5 | 重复事件不重复调查 | `test_repeated_sustained_detection_does_not_duplicate` |
| 6 | 已有生理事件时抑制 | `test_active_physiological_event_suppresses_sustained_creation` |
| 7 | DeepSeek 故障不丢事件 | `test_deepseek_failure_keeps_event_and_decision` |
| 8 | 数据中断不被误判正常 | `test_sweep_marks_stale_data_as_verify` |
| 9 | 默认关闭 | `test_decision_engine_is_off_by_default` |
| 10 | 未授权访问 404 | `test_public_app_does_not_expose_decisions` |
| 11 | 本地审计接口 | `test_local_app_exposes_decision_audit` |
| 12 | 决策故障不阻断告警 | `test_decision_failure_does_not_block_rule_alert` |
| 13 | 真实设备默认不触发 | `test_real_device_sustained_requires_explicit_flag` |
| 14 | 不连续样本不误判 | `test_discontinuous_samples_do_not_create_sustained_event` |
| 15 | 候选期不污染基线 | `test_candidate_period_does_not_pollute_baseline` |
| 16 | 多规则事件不遗漏 | `test_multiple_rule_events_are_not_dropped` |
| 17 | 重复/乱序样本 | `test_sorted_duplicate_and_out_of_order_samples_do_not_trigger` |
| 18 | OBSERVE 增长控制 | `test_observe_heartbeat_is_rate_limited` |
| 19 | 同事件重复评估 | `test_same_event_repeat_increments_seen_count` |
| 20 | 基线不足不强制建案 | `test_insufficient_baseline_never_forces_an_event` |
| 21 | sweep/ingest 并发 | `test_concurrent_ingest_and_sweep_create_one_investigation` |
| 22 | 重启恢复活动事件 | `test_restart_recovers_active_event_and_closes_on_recovery` |
| 23 | 重启保持心跳去重 | `test_restart_preserves_observe_dedup` |
| 24 | 审计字段完整、无 LLM 置信度 | `test_decision_audit_fields_are_complete` |
| 25 | 质量恢复 Insight | `test_quality_recovery_emits_non_urgent_insight` |

## 8. 实际完成 / 未验证

### 已完成

- 架构审查与最小改动方案。
- `decisions` 表、决策模块、质量门、持续变化检测、消息/审计去重。
- `/v1/telemetry`、APK ingest、后台 sweep 三条接入路径。
- `sustained_hr_change` 内部事件 + 调查 + 无照护者通知。
- 规则事件通知行为保持不变。
- 只读审计接口与访问边界。
- 25 个新测试和 169 个既有测试全部通过。

### 未验证 / 明确不做

- **没有真实硬件验收**：持续变化阈值未在真实 STM32/戒指/ESP32 数据上校准。
- **没有整夜或长期 soak**：只在合成数据和单测时钟下验证。
- **跨进程并发**只做了 SQLite 唯一约束与回退设计；没有多进程压力测试。
- H1 `/v1/hardware/packets` 不在 main；未合并、未改动，只记录了未来接入点。
- 未开发完整 State Engine、Personal Memory、Android UI 或控制台决策时间线。
- Decision Engine 默认关闭；V0.1 指标不代表临床有效性。

## 9. 回滚

- 关闭 `GUARDIAN_DECISION_ENGINE` 即可回到原有行为。
- 新增 `decisions` 表是附加表，不修改既有表结构；旧代码可继续运行。
- 分支未合并 main；回滚只需删除分支或保持开关关闭。
