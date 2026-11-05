# SenseMind Agent 3.0 — T12-1.1 修复设计（Rev 2）

> 状态：**设计阶段，未实施**
> 设计提交基线：`c6c7e336adcc064852eaaa9e0b3455249457174d`
> 生产代码基线：`0bd774383a53e185381fb7d74d3b3c6e6e77d3e3`
> 分支：`claude/agent3-decision-v0.1.2`
> 范围：设备时间可信度、时钟异常恢复、sustained baseline、received_at 行级测试
> 不在范围：BLE/Android/H1 固件、main 合并、T12-2、新机器学习模型
> 修订：Rev 2，吸收第一轮设计评审的四项时间协议、序号、baseline、tick、会话和 worktree 意见

## 0. 评审修订摘要

Rev 2 的关键变化：

1. 四时间戳改为 **服务器发起** 的 t1/t2/t3/t4 定义，明确 offset/delay 数学式。
2. `age_upper_bound = estimated_age + uncertainty`；实时门控使用 upper/lower bound，不允许用 `max_lag + uncertainty` 放宽。
3. 增加网络不对称、时间戳精度、时钟漂移和 TTL 对 uncertainty 的影响。
4. `sample_seq` 明确为 **stream 级**，定义重复/倒退/跳跃/boot 变化/计数器归零。
5. Baseline 统一使用归一化 UTC 字段，原始设备时间保留；旧事件不再回退 `opened_at`。
6. 增加 `normalized_sample_at`、`source_started_at_utc`、`source_ended_at_utc`、`clock_calibration_id`。
7. `ingest_ticks` 增加 `process_instance_id`，明确 monotonic 只在本进程实例内可比。
8. 明确 ingest tick 与 telemetry 的事务关系、batch 幂等策略。
9. 明确 boot_id/session_id 的生成、持久性、旧会话处理、认证边界。
10. 记录当前脏 worktree，设计后续必须从干净 worktree 实施。

## 1. 目标与原则

T12-1 核验发现：

1. `received_at - sample_time` 无法区分固定时钟偏移与真实积压。
2. 大前跳、回拨、boot 变化和设备时间重置没有可靠恢复路径。
3. 旧事件 `source_started_at=NULL` 时把服务器 `opened_at` 隐式当成 sample_time，污染 baseline。
4. 两个在树生产入口有代码级 `received_at`，但缺少数据库行级测试；H1 不在当前分支。

T12-1.1 原则：

- **时间可信度必须显式建模**，不能从单向时间戳推断。
- **未知时间可信度不得触发实时规则**，也不得报告 `rule_evaluated=True`。
- **服务器 UTC、设备原始 sample_time、归一化 sample_time、进程 monotonic 严格分离**。
- **旧事件时间不确定时宁可 `baseline_unavailable`，不得使用污染数据**。
- **会话变化不能永久被旧会话时间阻塞**。
- **没有可信同步时，不声称能区分固定偏移与真实积压**。

## 2. 时间体系架构

```text
设备/网关
  ├─ raw_sample_time       设备原始时间
  ├─ sample_seq            每 stream 会话内计数器
  ├─ boot_id               每次启动唯一
  └─ time sync response    t2/t3 设备侧时间戳
          │
          ▼
  /v1/telemetry / APK batches / H1(未来)
          │
          ▼
  TimeTrustResolver
  ├─ session: (device_id, boot_id)
  ├─ sync: t1/t2/t3/t4, offset, delay, uncertainty, TTL
  ├─ stream: stream_id, sample_seq continuity
  ├─ normalized_sample_at = raw_sample_time - offset
  └─ clock_trust = trusted|degraded|expired|unsynced|unknown
          │
          ├─ clock_untrusted ──> VERIFY / rule_evaluated=False
          ├─ stream_gap ───────> GAP audit + dwell reset + continue
          ├─ historical_backlog -> VERIFY / no live rule
          └─ trusted ──────────> DetectionEngine (session/stream dwell)
                                      │
                                      ▼
                               DecisionEngine
                               ├─ live baseline: normalized UTC only
                               ├─ event-window exclusion
                               └─ baseline_unavailable when unprovable
```

### 2.1 四类时间

| 时间 | 来源 | 持久化 | 用途 |
|---|---|---|---|
| `raw_sample_time` | 设备/网关 `Telemetry.timestamp` | `telemetry.timestamp`；事件 `source_started_at`/`source_ended_at` | 原始样本、事件 source time |
| `normalized_sample_at` | `raw_sample_time - offset`，使用 `clock_calibration_id` | `telemetry.normalized_sample_at`；事件 UTC 字段 | baseline、窗口、跨设备比较 |
| `received_at` | 服务器 UTC | `telemetry.received_at` | 接收审计、freshness |
| `monotonic` | 服务器进程单调时钟 | `ingest_ticks.monotonic` | dwell、批次 tick；同进程实例内有效 |

`monotonic` 不用于跨进程、跨进程实例或跨重启比较；不得把原始 monotonic 数值持久化后与另一个进程比较。

## 3. 设备会话与 boot_id

### 3.1 会话键

```text
device_id
  └─ boot_id             设备/网关每次启动唯一
       └─ session_id     服务器生成 UUID
            ├─ session_epoch
            ├─ session_seq/stream counters
            └─ clock sync state
```

- 有 `boot_id`：`session_id = server_uuid(device_id, boot_id)`，会话内排序使用 `(stream_id, sample_seq)` 或可信时间。
- 无 `boot_id`：服务器可创建 `legacy` 会话，但 `clock_trust=unknown`，不得运行实时生理规则；仍可存储、展示、调查历史。
- **生成与持久性**：`boot_id` 由设备固件或受信任 gateway 在每次启动时生成；建议随机 UUID。它必须在一个 boot 内稳定、在重启/掉电复位后变化；如果设备没有 NVRAM/RTC 能力，gateway 可以按连接/会话生成，但必须标记 `boot_id_source=gateway_session`，不能当作真实 boot 证明。服务器持久化 `boot_id` 作为会话键，不把它当作密码学真实性证明。
- `boot_id_source`: `device|gateway_session|unknown`；`boot_id` 不是密码学真实性证明。
- 设备重启 / 异常复位：新 `boot_id` → 新 session，旧 session `CLOSED`；旧 `last_sample_time` 不能阻塞新 session。
- `boot_id` 不变但计数器归零：`BOOT_ID_REUSE_SUSPECTED` / `SEQUENCE_RESET_SUSPECTED`，要求重新同步和新 epoch。
- 重复旧会话请求：若 `session_id` 不是当前会话，返回 `stale_session`（或记录为归档），不得更新当前会话、不得触发规则。
- 同一会话重复包：按 stream seq 判 duplicate，不重新评估。
- 设备身份认证仍由现有 APK credential/device binding 负责；`boot_id`/`session_id` 只用于时间会话，不作为访问控制或真实性证明。

### 3.2 唯一约束

- `device_sessions.session_id` 主键。
- 部分唯一索引 `UNIQUE(device_id, boot_id) WHERE boot_id IS NOT NULL`。
- `stream_gaps` 唯一 `(session_id, stream_id, lo)`。
- `ingest_ticks.tick_id` 主键；`(process_instance_id, batch_id)` 唯一用于 batch 幂等。

## 4. 四时间戳同步协议

### 4.1 定义

本轮采用 **服务器发起** 的交换；协议可经 BLE gateway、H1 或未来 APK 双向通道承载，不修改 nRF52840 固件。

```text
t1: 服务器发送请求
t2: 设备/网关接收请求
t3: 设备/网关发送响应
t4: 服务器接收响应
```

数学定义：

```text
offset = ((t2 - t1) + (t3 - t4)) / 2
delay  = (t4 - t1) - (t3 - t2)
```

- 所有时间先归一化为 UTC、同一精度单位（毫秒）。
- `delay < 0` 视为无效。
- `delay > max_sync_delay`（建议 5000ms）不具备可信度。
- `offset` 正负号：`offset = device_clock - server_clock`。

### 4.2 HTTP/网关承载

服务器发起请求可通过：

1. 双向 BLE/H1 gateway：服务器下发 `TIME_SYNC_REQUEST`，gateway/设备回 `TIME_SYNC_RESPONSE`；
2. 未来 HTTP gateway：服务器创建 pending request，gateway 拉取并回传响应。

无论承载方式，记录 `t1/t2/t3/t4`。HTTP 轮询造成的等待会计入 `delay`，因此可能使同步不可信；不得把轮询延迟当作可信时间锚点。

### 4.3 offset 与归一化样本时间

```text
corrected_sample_time = raw_sample_time - offset
estimated_age         = received_at - corrected_sample_time
```

事件使用的 normalized 时间：

```text
normalized_sample_at = raw_sample_time - offset
```

`normalized_sample_at` 必须同时记录 `clock_calibration_id`（通常等于用于计算的 `sync_id`）。没有可信 sync 时，`normalized_sample_at` 为 NULL，不得用 raw 时间冒充。

### 4.4 uncertainty 组成

```text
U_network    = delay/2 + asymmetry_margin
U_precision  = server_timestamp_precision + device_timestamp_precision
U_base       = max(reported_uncertainty, U_network + U_precision)
U(age)       = U_base + |drift_ppm| * age_since_sync / 1_000_000
```

- 网络不对称：若无法证明上下行对称，`asymmetry_margin` 必须保守（最保守可用 `delay/2`，即 `U_network≈delay`）；不得假设完全对称。
- 时间戳精度：服务器和设备的时间戳分辨率都进入 uncertainty。
- 时钟漂移：使用最近 N 次同步的 offset 变化估计 `drift_ppm`；`U(age)` 随同步后时间增长。
- 报告值不能低于本地可证明的下界；取 max，不取 min。
- `delay` 本身不是 uncertainty；它用于约束 offset 的最大可能误差。

### 4.5 同步状态、TTL 与过期

```text
TRUSTED    有效 t1-t4、delay/uncertainty 达标、sync 未过期、seq 连续
DEGRADED   uncertainty 偏高或临近 TTL；仅展示/VERIFY，不跑 live
EXPIRED    TTL 到期或 U(age) > max_uncertainty；暂停 live
UNSYNCED   从未同步；VERIFY / clock_untrusted
UNKNOWN    无 boot_id/seq/校准的 legacy 数据；VERIFY / clock_untrusted
```

默认（演示参数）：

- `sync_ttl = 300s`；
- `max_uncertainty = 1000ms`；
- `max_sync_delay = 5000ms`；
- `max_offset = 300s`；
- `drift_ppm` 上限、`max_future_skew`、`max_sample_lag` 独立配置。

同步过期或 uncertainty 超限：

- 保留活动事件，不自动 resolved；
- 新样本 `VERIFY clock_sync_expired` / `clock_untrusted`；
- `rule_evaluated=False`；
- 重新同步且 seq 连续后才可恢复 live。

### 4.6 实时准入（严格）

对每个样本：

```text
estimated_age   = received_at - (raw_sample_time - offset)
uncertainty_s   = U(age_since_sync) / 1000
age_upper_bound = estimated_age + uncertainty_s
age_lower_bound = estimated_age - uncertainty_s
```

必须同时满足：

```text
clock_trust == TRUSTED
AND uncertainty_s <= max_uncertainty / 1000
AND now <= sync_expires_at
AND age_upper_bound <= max_sample_lag
AND age_lower_bound >= -max_future_skew
AND data_mode == live
AND stream/sample_seq 连续或已处理 GAP
```

**禁止：**

- 不得使用 `max_sample_lag + uncertainty` 放宽允许延迟；
- 不得把 `DEGRADED/EXPIRED/UNSYNCED/UNKNOWN` 当成 TRUSTED；
- 不得在 uncertainty 过大时靠扩大 `max_sample_lag` 通过；
- 不得在未同步时把 raw age 分类为 normal 或 backlog。

### 4.7 场景结果定义

| 场景 | 预期 |
|---|---|
| 设备慢 60s + 有效同步 | offset≈-60s；corrected age≈0；TRUSTED 后可 live |
| 设备快 60s + 有效同步 | offset≈+60s；corrected age≈0；TRUSTED 后可 live |
| 时钟准确 + 积压 60s | offset≈0；corrected age≈60s；`historical_backlog` |
| 积压数据分批上传 | 同一 batch 共享 tick；服务器 dwell 跨度不足；历史数据不 live |
| 无同步/未知 | `clock_untrusted`；不得判定 normal/backlog；`rule_evaluated=False` |
| 同步过期/uncertainty 超限 | `clock_sync_expired/degraded`；不 live |
| 网络不对称严重 | uncertainty 上升；无法满足 upper bound 时 `clock_untrusted` |

## 5. sample_seq 连续性

### 5.1 作用域

`sample_seq` 属于 **packet stream**，不是设备全局，也不是不同传感器共享序列：

```text
seq_key = (session_id, stream_id)
```

- H1：`stream_id` 使用现有硬件流 ID；`sequence_number` 为该流序列。
- APK：`vendor_sequence` 若为传感器级，则 `stream_id = metric`（hr/spo2/...）；不得把 HR 序列与 IMU 序列混用。
- Legacy `DeviceStatus.sample_counter`：`stream_id = "device"`，仅在该设备流内使用。
- 无 stream/seq：`sequence_trust=unknown`；不得用其他 sensor 的 seq 补齐或推断。

不同采样频率的流独立：IMU 的 gap 不得重置 HR 的 dwell，反之亦然。合并 telemetry 时保留 stream 标签。

### 5.2 事件定义

| 条件 | 处理 |
|---|---|
| 相同 `seq_key` 且 `sample_seq == last_seq`，内容相同 | duplicate：不写 monitoring、不推进 dwell、不 evaluate |
| 相同 seq，内容不同 | `sequence_conflict`：拒绝/隔离，不评估 |
| `sample_seq < last_seq` | `SEQUENCE_ROLLBACK`：重置该 stream dwell；不永久拒绝；要求 session 重锚/同步 |
| `sample_seq > last_seq + max_seq_gap` | `STREAM_GAP`：写 `stream_gaps`；重置该 stream dwell；接受 gap 后样本（若时间可信），继续 |
| `boot_id` 变化 | 新 session；旧 session CLOSED；不比较旧 seq |
| reboot 后 seq 归零且 boot_id 未变 | `SEQUENCE_RESET_SUSPECTED`：新 epoch，要求重同步 |
| `sample_seq` 相同但旧 session | `stale_session`；不得复活旧 session |

- `max_seq_gap` 按 stream 配置（例如 1Hz 流允许 1 个缺失；高采样流按预期步长）。
- 序列跳跃必须记录 GAP，而不是静默丢弃；但后续有效样本可继续，不得永久拒绝。
- 重复/倒退样本不得写 monitoring 或成为时间锚点。
- `stream_gaps` 记录 `lo/hi/detected_at/reason`，用于决策审计和 baseline 质量。

### 5.3 测试

- 重复序号、同号不同内容；
- 序号倒退、跳跃、boot 变化、seq 归零；
- 高采样 IMU 与 1Hz HR 双流，IMU gap 不影响 HR dwell；
- gap 后恢复正常；
- 旧 session 重复包不更新当前 session。

## 6. Ingest Tick、事务与幂等

### 6.1 process_instance_id

服务器每次进程启动生成 `process_instance_id`（UUID），写入每个 `ingest_tick`。

- `monotonic` 只在 **同一 `process_instance_id`** 内比较。
- 跨进程、跨应用重启、跨部署不得比较原始 monotonic 数值。
- 跨进程比较使用 `received_at` UTC、session、stream seq 和持久化会话状态。
- 应用重启：新 process_instance_id；dwell 候选清空；不得恢复旧 monotonic 窗口。

### 6.2 ingest_ticks

```sql
tick_id TEXT PRIMARY KEY,
process_instance_id TEXT NOT NULL,
device_id TEXT NOT NULL,
session_id TEXT,
batch_id TEXT,
source TEXT NOT NULL,
received_at TEXT NOT NULL,
monotonic REAL NOT NULL,
created_at TEXT NOT NULL
```

- `/v1/telemetry`：每个被接受请求一个 tick。
- APK batch：整个 batch 一个 tick；所有 telemetry 行共享 `ingest_tick_id`、`received_at`、`monotonic`。
- replay/dataset：同样 tick，但 `data_mode=replay`，不进入 live。

### 6.3 事务关系

- `/v1/telemetry`：tick + telemetry 行必须在同一 SQLite 事务内提交；规则评估在数据持久化后进行，`rule_evaluated` 仅在成功完成后置 true。
- APK：`(device_id, batch_id)` 是 batch 幂等键；tick 唯一 `(process_instance_id, batch_id)`；telemetry 行带 `batch_id`/`ingest_tick_id`。
- 重复 batch 请求：返回已有 receipt，不创建第二个 tick，不插入重复 telemetry。
- 若 APK 批次与原 telemetry 跨存储表需要同一事务，优先使用同一 SQLite 连接/事务；不能原子提交时增加 pending/reconciliation 状态，禁止“半提交后当成成功”。
- 同一 batch 的所有行共享 tick；不同 batch 不得共享 tick。

### 6.4 received_at 测试

- `/v1/telemetry`：`received_at IS NOT NULL`、`received_at_source='server'`、`ingest_tick_id` 存在。
- APK batch：所有 telemetry 行同一 `ingest_tick_id`、同一 `received_at`、`received_at_source='server'`。
- `process_instance_id` 相同；monotonic 只在该进程内比较。
- 应用重启：新 process_instance_id，旧 tick monotonic 不参与新 dwell。
- replay 不进入 live 规则；direct `put_telemetry` 未传接收时间仍为 `NULL/legacy_estimated`。
- H1 不在本分支，不宣称验证。

## 7. 设备重启与时间锚点恢复状态机

### 7.1 状态

```text
NEW_SESSION
  ├─ boot_id 新 / 首次见设备
  └─ clock_trust=UNSYNCED，dwell 清空

UNSYNCED
  ├─ 有效同步 → TRUSTED
  └─ 数据 → VERIFY clock_untrusted

TRUSTED
  ├─ 正常样本 → live
  ├─ TTL/uncertainty 超限 → EXPIRED/DEGRADED
  ├─ offset 大幅跳变 → RESYNC_REQUIRED（清空 dwell）
  ├─ sample_seq 回退/归零 → SESSION_RESET_SUSPECTED
  └─ boot_id 变化 → 旧会话 CLOSED，新会话 NEW_SESSION

RESYNC_REQUIRED
  ├─ 有效同步 + seq 连续 → TRUSTED
  └─ 无同步 → VERIFY / 不跑 live

EXPIRED
  ├─ 有效同步 → TRUSTED
  └─ 无同步 → 保持暂停

CLOSED
  └─ 旧会话时间不得阻塞新会话
```

### 7.2 恢复规则

1. 旧会话不永久阻塞新会话：排序键 `(session_id, stream_id, sample_seq)`，不是全局 latest timestamp。
2. 新会话初始 `UNSYNCED`，必须建立新同步锚点；未同步不 live。
3. 大前跳/大回拨/同步 offset 跳变：`RESYNC_REQUIRED`，重置 dwell；活动事件保留。
4. 异常期间 `rule_evaluated=False`；不产生新生理事件，不自动 resolved。
5. 恢复：有效同步 + stream seq 连续 + 实时样本；dwell 从 0 开始；close dwell 仍需 10s。
6. 应用重启：恢复 session 与 sync 记录；dwell 不持久化；process_instance_id 新；同步过期则暂停。
7. 设备重启/seq 归零：新 session/epoch；旧 last sample 不参与新会话；新会话可接受更早 sample_time。
8. `boot_id` 只是提示，不是真实性证明；认证仍靠 credential/device binding。

### 7.3 测试

- 正常 → 前跳 60s → RESYNC_REQUIRED → 同步 → 恢复；
- 正常 → 回拨 60s → SESSION_RESET_SUSPECTED → 重同步；
- boot_id 变化 / seq 归零 → 新会话不被旧时间阻塞；
- 应用重启：同步有效/过期；process_instance_id 变化；
- 异常期间无规则事件；3–5 个有效样本后恢复；
- 活动事件保持，恢复后满足 close dwell 才解除。

## 8. Baseline 时间统一与历史异常排除

### 8.1 归一化字段

保留原始设备时间，不覆盖：

```text
raw_sample_time          telemetry.timestamp
normalized_sample_at     telemetry.normalized_sample_at
clock_calibration_id     telemetry.clock_calibration_id / events.clock_calibration_id
source_started_at        events.source_started_at（原始设备时间）
source_started_at_utc    events.source_started_at_utc（归一化 UTC）
source_ended_at          events.source_ended_at（原始设备时间）
source_ended_at_utc      events.source_ended_at_utc（归一化 UTC）
source_time_basis        device_raw | normalized_utc | server_received | unknown
source_time_trust        trusted | estimated | unknown
```

- `opened_at/closed_at` 只用于服务器生命周期和展示，不进入 baseline。
- `normalized_sample_at` 只在 `clock_trust=TRUSTED` 时生成；否则 NULL。
- `clock_calibration_id` 标识计算 normalized 时间所用的 sync/calibration；同一窗口内 calibration 变化不得混用。
- baseline 只使用 normalized UTC；raw 仅作溯源。
- replay/synthetic 的 normalized 语义是 replay 虚拟时间，不得进入 live baseline。

### 8.2 旧事件

- `source_started_at=NULL` 或 `source_started_at_utc=NULL`：**不回退** `opened_at`。
- active sustained 事件：`VERIFY baseline_unavailable`。
- 旧事件 lifecycle 仍可展示；不参与 live baseline。
- 若旧事件 lifecycle 窗口与 baseline 相交且 source time 不可证明：`baseline_unavailable`，不使用污染样本。
- 若旧事件 source_started 存在但 `source_time_trust=unknown`，同样不可用。

### 8.3 事件窗口与 baseline 过滤

对 live sustained baseline：

1. 候选 run 使用 normalized sample time；
2. baseline 窗口 `now - 300s` 且 `< run_start_normalized`；
3. 只接受 `data_mode=live`；
4. 只接受 `clock_trust=TRUSTED`；`DEGRADED/EXPIRED/UNSYNCED/UNKNOWN` 排除；
5. 排除所有活动事件窗口；
6. 排除所有已关闭规则事件窗口（`high_heart_rate`、`low_spo2`、`unusual_motion`、`concurrent_signals`、`sustained_hr_change`）；
7. 排除 `[source_started_at_utc - guard, source_ended_at_utc + guard]`；`source_ended_at_utc=NULL` 时不能证明终点，若窗口相交则 `baseline_unavailable`；
8. 排除 replay/synthetic；
9. 排除 `clock_calibration_id` 不一致的样本；
10. 若 clean 样本数量/跨度不足，或任何不可证明窗口与 baseline 相交 → `VERIFY baseline_unavailable`。

活动/关闭事件处理：

- 活动规则事件窗口：`[source_started_at_utc - guard, now]`（若 source 不可证明 → unavailable）。
- 已关闭事件：`[source_started_at_utc - guard, source_ended_at_utc + guard]`。
- 旧事件 source 缺失：保守 unavailable，不静默排除/使用。
- 事件关闭后重新计算：排除该事件窗口；使用窗口外的 clean live 样本；若不足则 unavailable。
- 当前候选期：从 run_start 到当前，不进入 baseline。

### 8.4 数据模式规则

| 数据 | Live baseline | 说明 |
|---|---|---|
| live + trusted | 可用 | 正常实时 |
| live + untrusted | 不可用 | VERIFY |
| replay | 不用于 live | 仅 replay 证据/描述 |
| synthetic | 不用于 live | 仅合成测试 |
| mixed | 不可用 | 不混合 |

### 8.5 输出语义

- `baseline_unavailable`：`VERIFY`，不得输出 `normal_observe`，不得创建 sustained event。
- basis 中记录：
  - `baseline_status`
  - `excluded_event_ids`
  - `excluded_intervals`
  - `clock_calibration_ids`
  - `source_time_trust`
- 旧事件不得使用 `opened_at` 作为 sample time；不得把服务器 UTC 与设备 raw 混用。

### 8.6 测试

- 旧事件 `source_started_at=NULL` → baseline_unavailable；
- 历史 `high_heart_rate` 活动/关闭窗口均排除；
- 历史异常关闭后 baseline 恢复且 median 正常；
- 正常 baseline median 不被历史异常污染；
- 新事件候选期排除；
- replay/synthetic/unknown trust 排除；
- calibration 变化导致 unavailable；
- `source_ended_at_utc` 缺失/边界。

## 9. 数据库与迁移

### 9.1 新增表

`device_sessions`：

```sql
session_id TEXT PRIMARY KEY,
device_id TEXT NOT NULL,
boot_id TEXT,
boot_id_source TEXT NOT NULL DEFAULT 'unknown',
session_epoch INTEGER NOT NULL DEFAULT 0,
session_state TEXT NOT NULL,
clock_trust TEXT NOT NULL,
clock_offset_ms INTEGER,
clock_uncertainty_ms INTEGER,
clock_sync_source TEXT,
clock_synced_at TEXT,
clock_expires_at TEXT,
last_sample_time TEXT,
last_received_at TEXT,
last_sample_seq INTEGER,
created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
UNIQUE(device_id, boot_id)  -- partial unique where boot_id not null
```

`ingest_ticks`：

```sql
tick_id TEXT PRIMARY KEY,
process_instance_id TEXT NOT NULL,
device_id TEXT NOT NULL,
session_id TEXT,
batch_id TEXT,
source TEXT NOT NULL,
received_at TEXT NOT NULL,
monotonic REAL NOT NULL,
created_at TEXT NOT NULL
```

`clock_syncs`：sync_id、device_id、boot_id、t1/t2/t3/t4、offset、delay、uncertainty、drift、status、expires_at。

`stream_gaps`：

```sql
session_id TEXT NOT NULL,
stream_id TEXT NOT NULL,
lo INTEGER NOT NULL,
hi INTEGER NOT NULL,
detected_at TEXT NOT NULL,
reason TEXT,
PRIMARY KEY(session_id, stream_id, lo)
```

### 9.2 现有表增量

`telemetry`：

```text
session_id TEXT
boot_id TEXT
stream_id TEXT
sample_seq INTEGER
ingest_tick_id TEXT
normalized_sample_at TEXT
clock_calibration_id TEXT
clock_trust TEXT
clock_offset_ms INTEGER
clock_uncertainty_ms INTEGER
```

`events`：

```text
source_ended_at TEXT
source_started_at_utc TEXT
source_ended_at_utc TEXT
source_time_basis TEXT NOT NULL DEFAULT 'unknown'
source_time_trust TEXT NOT NULL DEFAULT 'unknown'
clock_calibration_id TEXT
```

### 9.3 迁移原则

- 全部 nullable 或安全默认；不删列、不删行、不重写历史时间。
- 旧 telemetry：新字段 NULL，`clock_trust='unknown'`；现有 `received_at`/`received_at_source` 保留。
- 旧 events：source UTC/ended 为 NULL，time basis/trust=`unknown`；禁止用 opened_at 回填 sample time。
- 旧 `received_at` 仍 NULL + `legacy_estimated`；不得伪造。
- 新索引：`(device_id, session_id, stream_id, sample_seq)`、`(device_id, received_at)`、`ingest_tick_id`、`(session_id, stream_id, lo)`。
- 幂等 `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE`；迁移测试用旧 schema 副本。
- 旧的 T12-1 `received_at`/`source_started_at` 字段保持兼容；新字段只增不改。

## 10. API 兼容

### 10.1 `/v1/telemetry`

新增可选字段：

```text
boot_id, stream_id, sample_seq,
clock_sync: {sync_id, ...}
```

- 旧客户端不传：`clock_trust=unknown`，live 规则暂停；默认 real rules 仍 off。
- `data_mode=replay` 保持测试/回放路径，不产生 live 告警。
- 响应新增 `clock`/`session`/`stream`/`rule_skipped`；`transitions` 结构不变。
- 同步失败/过期返回明确 `VERIFY`，不返回 normal。

### 10.2 APK

- `Measurement`/`Ingest` 增加可选 `boot_id/stream_id/sample_seq/clock_sync`；旧 APK 兼容。
- 无同步/无 seq 的 live APK 数据：存储、展示、调查可见，但不跑 live 生理规则。
- `phone_received_at` 只作 gateway estimated 参考，不能单独证明设备时钟。
- `/v1/time-sync` 由 gateway/phone 承载；本阶段不改 Android。
- 同一 batch tick 共享，不允许每行不同 tick。

### 10.3 H1

不在当前分支，不修改；未来接入同一 resolver；不得宣称 H1 已通过。

## 11. 测试用例清单

### 11.1 同步协议与不确定性

1. t1/t2/t3/t4 公式正确性（正/负 offset）；
2. `delay < 0`、过大 delay 拒绝；
3. 网络不对称时 uncertainty 上升，不能放宽 max_lag；
4. 时间戳精度、漂移、TTL 使 uncertainty 超限 → EXPIRED/DEGRADED；
5. 慢 60s + 同步 → live；
6. 快 60s + 同步 → live；
7. 准确 + 积压 60s → historical_backlog；
8. 积压 10s 分批上传 → 不 live；
9. 无同步/未知 → clock_untrusted，rule_evaluated=False；
10. 同步过期 → clock_sync_expired；
11. age_upper_bound/lower_bound 边界；
12. batch 共享 tick 单批不满足 dwell。

### 11.2 序号与会话

13. 重复序号、同号不同内容；
14. 序号倒退；
15. 序号跳跃 → GAP + dwell reset + 后续恢复；
16. boot_id 变化 → 新会话；
17. seq 归零/boot 重用；
18. 旧 session 重复包不更新当前；
19. 不同频率 stream 不互相污染；
20. 应用重启/process_instance_id 变化。

### 11.3 恢复

21. 前跳 60s → RESYNC_REQUIRED → 同步恢复；
22. 回拨 60s → SESSION_RESET；
23. boot/seq reset → 新 epoch；
24. 重启同步有效/过期；
25. 异常期间 dwell 清空、活动事件保持；
26. 恢复后 3–5 个有效样本；
27. 异常期间 rule_evaluated=False。

### 11.4 Baseline

28. 旧事件 source_started=NULL → baseline_unavailable；
29. 历史 high_heart_rate 活动/关闭窗口排除；
30. 历史异常关闭后 baseline 恢复；
31. 正常 median 不被污染；
32. 候选期排除；
33. replay/synthetic/unknown trust 排除；
34. calibration 变化 → unavailable；
35. source_ended_at_utc 边界。

### 11.5 Ingest/received_at

36. `/v1/telemetry` 行 received_at 非 NULL/server/tick；
37. APK batch 行 server/共享 tick；
38. process_instance_id 与 monotonic 同进程比较；
39. batch 幂等：重复 batch 不新增 tick/telemetry；
40. replay 不进入 live；
41. direct put_telemetry legacy_estimated；
42. 规则评估失败不标记成功、可重试。

## 12. 最小修改文件清单（实施阶段）

- `guardian/clock.py` 或新 `guardian/time_trust.py`：四时间戳、offset/uncertainty、session/stream
- `guardian/models.py`：可选 `boot_id/stream_id/sample_seq/clock_sync`；不破坏旧字段
- `guardian/storage.py`：sessions、ticks、gaps、normalized/UTC 字段、事务/迁移
- `guardian/detection.py`：clock_trust gate、stream seq、session recovery、dwell
- `guardian/decision.py`：normalized baseline、事件排除、baseline_unavailable
- `guardian/monitoring.py`：clock_untrusted/unknown 表达
- `guardian/factory.py`：time-sync endpoint、tick/事务、API
- `guardian/apk_api.py`：batch tick/session/seq/sync、幂等
- `guardian/investigation.py`：不改通知语义；只读 clock 状态
- 新测试：`test_v012_time_trust.py`、`test_v012_session_seq.py`、`test_v012_recovery.py`、`test_v012_baseline.py`、`test_v012_received_at.py`
- 文档：本设计 + 实施报告 + 迁移记录
- 不修改：BLE、Android、H1、main、Agent 2.0 默认通知路径

## 13. 风险与剩余限制

1. **无固件同步实现**：nRF52840 本阶段不改；同步需要 gateway/phone/后续固件。无同步时 live 规则暂停，是安全取舍。
2. **t1-t4 真实性**：t2/t3 由客户端/网关报告；没有密码学证明，boot_id 也可伪造。研究原型不宣称防篡改；认证仍靠 credential/device binding。
3. **legacy 设备**：无 boot_id/seq/sync → `clock_untrusted`，不跑 live。
4. **网络不对称**：无法证明路径对称时必须保守提高 uncertainty；可能导致无法 live，不能用扩大延迟规避。
5. **旧事件**：source 时间不可信，可能长期 `baseline_unavailable`；优先正确性。
6. **monotonic**：只在同 process_instance_id 内有效；跨重启不可比较；dwell 不恢复。
7. **事务**：APK batch 与 legacy telemetry 若不能完全原子，需要 pending/reconciliation；不得半提交成功。
8. **H1**：不在本分支，不宣称 PASS。
9. **阈值**：TTL/uncertainty/drift/lag 为演示参数，需真实设备校准。
10. **单进程 SQLite**：约束不变。
11. **兼容性**：旧客户端/旧 APK 不传同步/seq 时 live 规则暂停；需发布说明和网关升级。

## 14. Worktree 隔离与当前脏状态

### 14.1 当前状态（只读记录）

设计修订时工作区存在以下未提交变更：

```text
M STATE.md
M TASKS.md
M docs/decision-engine-v0.1.2-t12-1.md
M guardian/detection.py
M guardian/monitoring.py
M tests/test_v012_timebase.py
```

其中用户指定的三份文件：

- `guardian/detection.py`
- `guardian/monitoring.py`
- `tests/test_v012_timebase.py`

当前 diff 摘要（仅这三份文件）：`+203 / -48`（`detection.py +95/-39`、`monitoring.py +17/-6`、`test_v012_timebase.py +91/-3`；其他文件为外部文档/状态修改）。本轮设计提交不包含这些文件；不覆盖、不回退、不提交、不清理。

- 当前 HEAD：`c6c7e33`（设计文档提交前）
- 生产代码基线：`0bd7743`
- 设计修订再加一个 docs-only commit

### 14.2 后续实施隔离

不得在当前脏工作区直接实施。建议：

```bash
# 从设计修订提交（c6c7e33 + 本 Rev 2 文档）创建干净 worktree
git worktree add ../guardian-t12-1.1 <design-revision-sha>
cd ../guardian-t12-1.1
git switch -c claude/agent3-decision-v0.1.2-t12-1.1
```

如果严格要求从 `c6c7e33`：

```bash
git worktree add ../guardian-t12-1.1 c6c7e33
cd ../guardian-t12-1.1
git switch -c claude/agent3-decision-v0.1.2-t12-1.1
# 先纳入本 Rev 2 设计文档，再实施
```

无论哪种方式：

- 不复制当前脏文件；
- 不 `git stash`/`git clean` 当前工作区；
- 当前脏文件保持原样；
- 新 worktree 必须从干净 commit 开始；
- 不在当前分支直接实施。

## 15. T12-1.1 独立提交计划

本轮只提交 Rev 2 设计文档，不含生产代码。

后续实施（干净 worktree）：

1. `claude: T12-1.1: add session, stream sequence, ingest tick and sync schema`
2. `claude: T12-1.1: implement four-timestamp trust and strict age bounds`
3. `claude: T12-1.1: add session/stream recovery and clock-untrusted gating`
4. `claude: T12-1.1: normalize baseline time and exclude historical event windows`
5. `claude: T12-1.1: add received_at/tick idempotency tests and migration`
6. `claude: T12-1.1: full regression, docs and risk review`

每步独立可回滚；不合并 main，不 push，直到评审通过。
