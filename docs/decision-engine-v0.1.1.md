# SenseMind Agent 3.0 — Decision Engine V0.1.1 Integration Readiness

> 状态：**独立分支完成，未合并 main**
> 分支：`claude/agent3-decision-v0.1.1`（基于 `claude/agent3-decision-engine` `4e99973`）
> 基线：V0.1 的 194 passed
> 本轮结果：**208 passed / 1 warning**（194 基线 + 14 新增）
> 范围：规则事件稳定性、监测中断状态、sustained 冷却、sweep 可靠性；不改 BLE/H1/Android/模型。

## 1. 修改文件清单

| 文件 | 修改 |
| --- | --- |
| `guardian/detection.py` | 打开/解除改为真实时间 dwell；数据缺口重置候选计数；监测状态记录；`analyze(t, now=None)` 支持测试时钟 |
| `guardian/monitoring.py` | **新增**：`NORMAL/UNKNOWN/STALE/NO_DATA` 分类、`last_valid_at` 记录 |
| `guardian/storage.py` | 新增 `device_monitoring` 表、`update_monitoring` / `monitoring` / `events_by_activity`；删除设备时清理监测状态 |
| `guardian/decision.py` | sweep 记录 `last_sweep_at/last_error/last_error_at/thread_alive`；sustained 冷却改用 `COALESCE(closed_at, opened_at)`；sweep 更新监测状态 |
| `guardian/factory.py` | 规则 dwell 配置；仅本地 app 启动 sweep；`/v1/monitoring/{device_id}`；health 增加规则稳定性和 sweep 状态 |
| `tests/test_v011_stability.py` | **新增 14 项测试** |
| `tests/test_api.py`、`tests/test_investigation.py`、`tests/test_agent2_manager.py` | 恢复夹具延长到新的 10 秒恢复窗口；断言语义不变 |
| `tests/test_decision_engine.py` | 固定时钟到分钟边界，避免 OBSERVE 分桶跨分钟抖动；调查等待上限从 6/8 秒放宽到 15 秒，避免全量测试负载下的时序误报 |

## 2. 事件生命周期设计

```text
inactive
  └─ 连续 bad 样本 ≥3 且真实时间 ≥ open_dwell(2s)
       └─ active
            ├─ 连续 good 样本 ≥3 且真实时间 ≥ close_dwell(10s)
            │    └─ resolved
            ├─ 数据缺口 > max_sample_gap(5s)
            │    └─ 重置候选计数；active 保持，不 resolved
            └─ 质量差/未知
                 └─ 重置生理候选计数；active 保持，不 resolved
```

关键约束：

- **打开**：`required_samples=3` 且 `open_dwell_seconds=2.0`；高采样率下不会因为 3 个样本（可能只有几十毫秒）立即开事件。
- **解除**：`required_samples=3` 且 `close_dwell_seconds=10.0`；短暂恢复不会关闭事件，也不会产生第二个 event/investigation。
- **数据缺口**：超过 `max_sample_gap_seconds=5.0` 重置所有候选计数；已有 active 事件保持 active，绝不自动 resolved。
- **质量差**：生理事件候选计数清零，已有 active 事件保持；`data_quality` 事件按自身 dwell 开闭。
- **没有新增全局冷却**：每种事件独立计数；真正的独立安全事件满足自身 dwell 后会立即打开，不会被其他事件或未验证冷却屏蔽。
- **原有通知逻辑不变**：`InvestigationService.on_transitions` 的默认 `notify=True`、通知 cooldown、规则事件确定性路径均未修改。

环境变量：

| 变量 | 默认 | 范围 |
| --- | --- | --- |
| `GUARDIAN_RULE_OPEN_DWELL_SECONDS` | 2 | 0–30 |
| `GUARDIAN_RULE_CLOSE_DWELL_SECONDS` | 10 | 1–120 |
| `GUARDIAN_RULE_MAX_SAMPLE_GAP_SECONDS` | 5 | 1–60 |

> 这些是演示用稳定性参数，不是医学阈值。回退到旧行为可设 `OPEN=0`、`CLOSE=1`、`GAP=60`，但旧行为会重新暴露抖动问题。

## 3. 数据中断状态设计

新增 `device_monitoring` 表：

```text
device_id | state | reason | last_seen_at | last_valid_at | updated_at
```

状态：

| 状态 | 条件 |
| --- | --- |
| `NORMAL` | 最新样本新鲜、`reading_valid=true`、质量 `good` |
| `UNKNOWN` | 质量 `poor/unknown/invalid`、`reading_valid=false`、`sampling_status=pending` |
| `STALE` | 样本时间超过 30 秒、`sampling_status=stalled/disconnected`、或查询时发现长期无新数据 |
| `NO_DATA` | 从未收到该设备遥测 |

- `last_valid_at` 只在新样本为 `NORMAL` 时前进，且不会因迟到/乱序数据而回退。
- 活动规则事件遇到 `STALE/UNKNOWN/NO_DATA` **不会自动 resolved**；只有连续 good 样本满足 close dwell 才解除。
- 数据恢复后，close 候选从恢复后的第一个 good 样本重新计时，执行重新验证。
- 接口：`GET /v1/monitoring/{device_id}`（仅本地 app；`public_only` app 不注册）。
- 监测状态是独立维度，不改变现有 event status（仍为 `active/resolved`），避免破坏 Agent 2.0 状态机。

## 4. sustained 冷却

- 冷却查询从 `opened_at` 改为 `COALESCE(closed_at, opened_at)`。
- 长事件打开 700 秒、40 秒前关闭：新候选会被冷却抑制。
- 事件关闭超过 600 秒后再次出现：允许创建新事件。
- 回归测试覆盖两种边界。

## 5. sweep 可靠性

`DecisionEngine` 现在暴露：

- `last_sweep_at`
- `last_error`
- `last_error_at`
- `thread_alive`

行为：

- 单设备异常不停止 sweep；错误记录为 `device:ExceptionType`。
- 下一次完全成功的 sweep 会清空 `last_error`。
- 后台线程异常也会记录。
- `stop_background()` 设置停止事件并 join（2 秒超时）；线程为 daemon。
- **仅本地 app 启动 sweep**：`decision_worker_allowed = decision_on and decision_worker and not public_only`；`public_only` app 永不启动后台 sweep。
- 当前版本明确采用**单进程部署**；不承诺多进程共享 SQLite 的并发安全（`events` active 唯一性、通知/证据跨进程竞态仍存在，见 V0.1 审查报告）。

## 6. 测试

```text
.venv/bin/python -m pytest -q
208 passed, 1 warning
```

（连续两次全量运行均通过；仅保留既有的 Starlette/httpx 弃用警告。）

新增 14 项：

| 测试 | 覆盖 |
| --- | --- |
| `test_rule_event_does_not_flap_on_short_recovery` | 3 bad/3 good 循环 10 次：1 次打开、0 次关闭 |
| `test_high_rate_samples_need_real_time_not_sample_count` | 高采样率下 3 个样本不打开，必须满足 2 秒 |
| `test_rule_event_requires_full_recovery_window` | 9 秒恢复不关闭，10 秒才关闭 |
| `test_short_recovery_does_not_create_second_event_or_investigation` | 短暂恢复不产生第二个 event/investigation |
| `test_data_gap_keeps_event_active_and_monitoring_stale` | 数据缺口：event 保持 active，监测 STALE |
| `test_recovery_after_gap_revalidates_and_updates_last_valid` | 恢复后重新计时，last_valid_at 更新 |
| `test_monitoring_states_and_last_valid_at` | NORMAL/UNKNOWN/STALE/NO_DATA 与 last_valid_at 保持 |
| `test_monitoring_endpoint` | `/v1/monitoring/{device_id}` |
| `test_long_event_cooldown_uses_closed_at` | 长时间事件关闭后 40 秒内重新出现被抑制 |
| `test_cooldown_expires_after_close_window` | 关闭超过 600 秒后允许新事件 |
| `test_sweep_records_error_and_recovers` | 单设备异常不终止 sweep，错误可清除 |
| `test_sweep_thread_starts_and_stops` | 线程启动/停止与 `thread_alive` |
| `test_only_local_app_starts_sweep` | local app 启动，public_only app 不启动 |
| `test_rule_notification_still_created_without_decision_engine` | 原有规则通知路径不回归 |

原有 194 项全部通过；`test_api`、`test_investigation`、`test_agent2_manager` 中三个“3 个好样本即解除”的夹具已延长到 10 秒恢复窗口，断言语义未改变。调查等待上限放宽只影响测试等待，不改变 Agent 调查状态机或安全逻辑。

## 7. 与硬件分支的冲突检查

`git merge-tree`（基于当前提交）：

| 组合 | 结果 | 冲突文件 |
| --- | --- | --- |
| `agent3-v0.1.1` + `hardware-api-v1` | 冲突 | `STATE.md`、`TASKS.md`、`guardian/apk_api.py`、`guardian/detection.py`、`guardian/factory.py` |
| `agent3-v0.1.1` + `v09-ble-reconnect` | 冲突 | 同上 |
| `hardware-api-v1` + `v09-ble-reconnect` | 干净 | 无 |

冲突解决方向：

| 文件 | 解决 |
| --- | --- |
| `guardian/detection.py` | 保留硬件分支的 `data_mode=replay` 真实设备规则放行条件，同时叠加本轮 open/close dwell、gap reset、监测记录 |
| `guardian/factory.py` | 同时保留 `HardwareStorage`/`EvidenceEngine`/hardware router、Decision Engine、规则 dwell 配置、单本地 sweep、monitoring 端点 |
| `guardian/apk_api.py` | `create_apk_router` 同时保留 `hardware=None` 和 `decision_engine=None` |
| `guardian/investigation.py` | 自动合并；验证 `notify` 参数与 H1 工具共存 |
| `tests/test_investigation.py` | 自动合并；跑定向测试确认恢复窗口与 H1 调查用例兼容 |
| `STATE.md` / `TASKS.md` | 手工合并 |

`guardian/agent.py`、`guardian/console.py`、`guardian/models.py` 本轮未修改，硬件分支对它们的改动不会与本分支冲突。

## 8. 未验证事项

- 没有真实 nRF52840/BLE/Android/H1 硬件在环；本轮不改这些链路。
- 2 秒/10 秒/5 秒是演示用稳定性参数，未做真实数据校准。
- 没有多进程共享 SQLite 压力测试；当前单进程部署。
- 没有控制台 UI 展示监测状态；只有 API。
- 没有新增机器学习模型，没有临床有效性声明。
- H1 仍未接 Decision Engine；集成时需在硬件分支的 ingest 钩子中补 `decision_engine.evaluate(...)`。

## 9. 回滚

- 分支未合并 main。
- 关闭 `GUARDIAN_DECISION_ENGINE` 不影响规则稳定性与监测状态；规则 dwell 仍生效。
- 如需临时恢复旧规则时序，可设 `GUARDIAN_RULE_OPEN_DWELL_SECONDS=0`、
  `GUARDIAN_RULE_CLOSE_DWELL_SECONDS=1`、`GUARDIAN_RULE_MAX_SAMPLE_GAP_SECONDS=60`。
