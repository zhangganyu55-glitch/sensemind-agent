# Agent 3.0 Decision Engine V0.1.2 — T12-1

> 状态：**仅完成 T12-1；未合并 main；未修改 BLE/Android/H1 固件**
> 分支：`claude/agent3-decision-v0.1.2`（从 `claude/agent3-decision-v0.1.1` `7561956` 创建）
> 结果：**218 passed / 1 warning**（T12-1 补充修复后 221，R1 修复后 225，R1-01 修复后 230 passed / 1 warning）
> 范围：时间体系、事件生命周期时间基、迁移标记、测试时钟

## 1. 三种时间

| 名称 | 来源 | 持久化 | 用途 |
| --- | --- | --- | --- |
| `sample_time` | `Telemetry.timestamp`（设备/样本时间） | `telemetry.timestamp` | 信号窗口、基线、`source_started_at` |
| `received_at` | 服务器 UTC 接收时间 | `telemetry.received_at` | 新鲜度、审计、事件生命周期 |
| `monotonic_time` | 进程单调时钟 | 不持久化 | dwell 的实际运行时间 |

- `guardian/clock.py` 提供 `SystemClock` / `ManualClock`，可注入 DetectionEngine 和 `create_app`。
- 同一 APK batch 的所有 packet 共用一个 `received_at` 和 monotonic tick；单批次不能单独满足 dwell。
- 事件 `opened_at`、`closed_at` 使用服务器 UTC；原始首个坏样本时间保存在 `events.source_started_at`。
- 进程重启后 dwell 候选在内存中清空；`reset_state()` 可显式重置。

## 2. dwell 双条件

打开和关闭都要求：

```text
连续样本数 >= required_samples
AND monotonic 跨度 >= dwell
AND sample_time 跨度 >= dwell
```

- 批量/积压数据即使样本时间跨度足够，也不能瞬间满足服务器运行时间条件。
- 实时数据需要真实时间推进；测试通过 `ManualClock` 模拟。
- 设备时钟前跳：样本时间突增但服务器接收间隔很小时标记 `clock_jump`，重置候选，不立即开事件。
- 设备时钟回拨/乱序：`sample_time <= 上一次有效 sample_time` 返回 `out_of_order_ignored`，不写 monitoring、不推进候选。
- 历史延迟：live 数据 `received_at - sample_time > max_sample_lag_seconds`（默认 5s）返回 `delayed_sample_ignored`；replay/test 数据不受该门控。
- 数据缺口仍重置候选，但不自动关闭活动事件。

## 3. 数据库迁移

幂等、增量，不删除数据：

```text
telemetry:
  + received_at TEXT NULL
  + received_at_source TEXT NOT NULL DEFAULT 'legacy_estimated'

events:
  + source_started_at TEXT NULL
```

迁移规则：

- 旧 `telemetry` 行 **不伪造** `received_at`：保持 NULL，并标记
  `received_at_source='legacy_estimated'`。
- 新服务器入口写入 `received_at`，标记 `received_at_source='server'`。
- 直接内部写入未提供接收时间时，也明确标记 `legacy_estimated`。
- 旧事件保留原数据，`source_started_at` 为 NULL；读取时回退到 `opened_at`。

## 4. 测试

新增 `tests/test_v012_timebase.py` 共 9 项：

- 单批次数据不能瞬间满足 dwell
- 服务器时间跨度足够但样本时间跨度不足时不开事件
- 正常实时数据满足 dwell
- 延迟历史数据分批上传不进入实时规则
- 设备时钟前跳不立即开事件
- 设备时钟回拨/乱序不产生错误事件
- 重启后候选安全重置
- `opened_at`/`closed_at` 服务器 UTC 与 `source_started_at` 样本时间
- 旧数据库迁移保留数据并标记 `legacy_estimated`

`tests/test_apk.py` 增加单批次不满足 dwell；既有 live 事件测试改为三个实时批次。

全量：`218 passed / 1 warning`（基线 208 + 10 新增）。

## 5. 未完成/风险

- T12-2 至 T12-6 未开始：GAP/STALE 统一语义、monitoring best-effort、replay 兼容、APK 重复批次 evaluate、评估节流、sweep lease、H1 契约均未实施。
- **T12-4 明确阻塞项**：replay 样本仍可能更新 live Monitoring；本轮只隔离了未来 replay 样本，未按 `data_mode` 隔离全部 replay 写入。
- HTTP 旧接口对乱序/回拨仍安全拒绝（409）；设备时钟自动重同步不在 T12-1 范围。
- `max_sample_lag_seconds` 是演示参数，未用真实设备延迟校准。
- 没有真实 nRF52840/BLE/Android/H1 在环验证。
- 单进程 SQLite 约束不变。
- 本分支未合并 main。

## 6. T12-1 补充修复（2026-11-03）

针对三个 P1：

1. **设备时钟前跳后正常数据持续被拒绝**
   - `DetectionEngine` 识别“样本时间大幅回退、但服务器接收/单调时钟正常前进、且样本本身新鲜”的时钟校正样本。
   - 该样本会重置 dwell 候选并重新锚定，后续正常数据恢复处理。
   - 迟到/重复样本仍返回 `out_of_order_ignored`，不会误重锚。

2. **未来时间戳污染 Monitoring/last_valid_at**
   - live 样本未来偏差 >5s 返回 `clock_skew_ignored`，不更新 monitoring、不写 `last_valid_at`、不成为排序锚点。
   - `monitoring.record` 对未来样本保持原监测状态（新设备保持 `NO_DATA`），不再写入未来 `last_seen_at/last_valid_at`。
   - replay 保留 30s 模型层容忍，但未来 replay 样本同样不会更新 live Monitoring；**历史 replay 样本仍可能更新 live Monitoring，列为 T12-4 阻塞项**。

3. **关闭真实设备规则时 Monitoring 停止更新**
   - Monitoring 更新移到 `real_data_rules_disabled` 门控之前；乱序和未来样本仍提前返回。
   - 被动模式下的 `ring/stm32/esp32` 实时样本现在会更新 `/v1/monitoring`。

4. **R1：时钟回退误判**
   - 新增每设备 `_clock_jump_state`；只有该设备实际接受过前跳样本后，才允许后续回退重新锚定。
   - 回退样本必须晚于前跳前锚点；重复的前跳前样本、无前跳的迟到数据继续 `out_of_order_ignored`。
   - 回退成功清除该设备状态；多设备状态隔离；恢复后再次前跳可重新记录。

5. **R1-01：零推进/迟到数据误重锚**
   - 回退重新锚定现在必须有设备自己的 `DeviceStatus.sample_counter` 证明候选样本产生于前跳之后；32 位回绕按 `0 < delta < 2^31` 判断。
   - `Telemetry` 没有通用 sequence/boot/session；APK 的 `vendor_sequence` 只存在于 APK 原始行，未进入 `Telemetry`，因此 APK/ring 走保守拒绝路径。
   - 服务器 `received_at` 和 monotonic 必须严格前进（`delta > 0`），同一 tick 的候选样本一律拒绝。
   - 无 counter 时保持 `clock_uncertain`，不重锚；等设备时间线越过临时前跳锚点后清除状态并恢复。代价是真实回拨在无 counter 设备上只能延迟恢复，这是数据契约限制。
   - 新增 5 项 R1-01 测试；补充修复累计新增 12 项测试。全量 **230 passed / 1 warning**。未修改 Agent 2.0、BLE、Android、H1。

### 6.1 5 秒未来容忍的设计说明

- 5s 与 `max_sample_lag_seconds` 使用同一量级，表示“允许的实时传输/设备时钟偏差”，不是模型层 30s 上限。
- 未来 ≤5s 的样本仍可能被判为 live：这是为了避免网络抖动或设备时钟轻微超前导致正常数据全部丢失；其 dwell 仍受 monotonic + sample_time 双条件约束。
- 未来 >5s 的 live 样本被视为不可用于实时判断：不进入候选、不更新 Monitoring、不成为排序锚点，只保留在 telemetry 审计中。
- replay 的 30s 是模型层 `Telemetry` 校验上限，不是 live 有效性阈值；T12-4 需要把 replay 从 live Monitoring 写入中彻底隔离。
