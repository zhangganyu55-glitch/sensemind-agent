# SenseMind 软件集成验收报告

- 验收时间：2026-10-31 16:30（+08:00）
- 验收范围：v0.7（`codex/ppg-training@ee5b93b`）+ Agent 2.0（`claude/agent2@f022b2d`）
- 集成分支：`integration/sensemind-v07-agent2`
- 代码基线：`f022b2d`
- 结论：**PASS（软件层）**
- 未合并 `main`；未修改硬件固件、H1 协议或已训练模型；未强制推送

## 1. 范围与约束

本次只集成两个软件分支的有效功能：

1. v0.7：Replay/Live 模式、监测控制台、真实 DeepSeek 工具调用、质量审计、600 秒稳定性验证。
2. Agent 2.0：Evidence Engine、Investigation Manager、Structured Reasoner、10 个合成多模态场景、新增证据/调查接口。

H1/H2 硬件协议、ESP32/nRF52840/STM32 固件和 Codex 的四条硬件分支保持原样，不纳入本次软件集成分支，也不做任何提交或推送。

## 2. Git 提交图与继承关系

| 项目 | 结果 |
| --- | --- |
| 远程抓取 | `git fetch --all --prune` 无新提交；远程分支 SHA 未变化 |
| v0.7 | `ee5b93b`（`origin/codex/ppg-training`） |
| Agent 2.0 | `f022b2d`（`origin/claude/agent2`） |
| `f022b2d` 的父提交 | `ee5b93b` |
| `git merge-base ee5b93b f022b2d` | `ee5b93b` |
| 分叉计数 | v0.7 独有 0 个提交；Agent 2.0 独有 1 个提交 |
| v0.7 是否已包含在集成中 | 是（直接祖先，无需额外合并提交） |
| v0.7..集成 删除文件 | 0 |
| `main` | `283c710`；其树与 `d172dc0` **完全相同**，而 `d172dc0` 是集成分支祖先 → `main` 无遗漏内容 |
| stash | 无 |

`main` 的合并提交 `283c710` 虽然不在集成分支的祖先链上，但 `git diff d172dc0 283c710` 为空，说明该合并只改变分支拓扑，没有引入任何文件内容；`main` 的全部内容已包含在 v0.7/Agent 2.0 中。

### 未合并的硬件分支（有意保留，未改动）

| 分支 | 提交 | 内容 |
| --- | --- | --- |
| `codex/v07-hardware-api` | `54da47f` | H1 协议冻结 + H2 硬件接收器 |
| `codex/v07-hardware-integration` | `f3d9ed3` | H2 硬件集成 WIP |
| `codex/v07-nrf52840` | `21c3fb1` | nRF52840 固件 / BLE 网关 WIP |
| `codex/v07-esp32` | `6f59495` | ESP32 固件 / 核心测试 WIP |

四个 Codex 工作树均干净，无未提交改动。H1 协议提交为 `325bc92`，`protocol/schema.json` 的 SHA-256 为：

```text
51a18c1784cb016da223c4c3837f332a8a915ee217f66cec7bdb3b33fe59d8d7
```

集成分支不包含 `protocol/` 或 `docs/hardware/`，因此从结构上不可能修改 H1 协议。

## 3. 集成内容

### 3.1 来自 v0.7

- 监测模式与来源约束（Live/Replay）
- 控制台与 WebSocket 推送
- 真实 DeepSeek 工具调用链路
- 质量审计、600 秒稳定性验证
- PPG/PAMAP2 回放脚本与推理 API
- 原有 APK `/v1` 契约和本地查询接口

### 3.2 来自 Agent 2.0（`A–E`）

| 任务 | 实现 |
| --- | --- |
| A Investigation Manager | 状态机、重复事件合并、瞬时错误重试、重启恢复、并发 worker、token 上限、后续证据 |
| B Evidence Engine | 10 类证据、统一字段、可用性、时间对齐、replay/live 标记、`(event_id, source_ref)` 幂等持久化 |
| C Structured Reasoner | evidence_id 引用校验、数值来源校验、危险建议拦截、证据不足降级 `inconclusive` |
| D 合成场景 | 10 个 synthetic/replay 场景 |
| E 测试与文档 | 新增 34 项测试、真实 DeepSeek 合成调用、架构文档 |

新增接口：`GET /v1/investigations/{id}`、`GET /v1/events/{event_id}/evidence`、`GET /v1/evidence/{evidence_id}`、`POST /v1/investigations/{id}/retry`。公开 APK 应用（`public_only=True`）中这些接口仍返回 404。

## 4. 接口与 H1 边界

- `guardian/apk_api.py`、`apk_models.py`、`apk_storage.py`、`pairing.py` 相对 v0.7 **零差异**，APK 契约未变。
- `guardian/factory.py` 的改动为**纯新增**：新增可选运行时参数、新增证据/调查路由、`/health` 增加字段、摄入后追加证据（异常不阻断摄入）。
- `DeepSeekClient.complete()` 新增可选 `max_tokens`，默认仍为 1200，向后兼容。
- `protocol/`、`docs/hardware/` 不在集成分支中；四条硬件分支 SHA 未变化。

## 5. 自动化测试

```text
.venv/bin/python -m pytest -q
161 passed, 1 warning in 13.97s
```

唯一警告是既有的 Starlette/httpx `TestClient` 弃用提示。测试覆盖原有 127 项 v0.7 行为，并新增 34 项 Agent 2.0 测试（证据 7、推理 7、管理器 9、场景 9、API 2）。

## 6. 六个模型产物

产物均为本地文件，`.gitignore` 排除了 `artifacts/`，不随仓库分发。

| 产物 | 版本 | 大小 | 校验/加载 |
| --- | --- | --- | --- |
| `artifacts/activity/activity_rf.joblib` | `pamap2-rf-v0.6.0-20261030` | 23,982,663 B | SHA-256 通过 |
| `artifacts/activity/activity_metadata.json` | 同上 | 14,131 B | SHA-256 通过 |
| `artifacts/activity/activity_metadata.joblib` | 同上 | 4,941 B | SHA-256 通过、可加载 |
| `artifacts/ppg/ppg_hr_rf.joblib` | `ppg-dalia-rf-v0.6.0-20261031` | 39,728,785 B | SHA-256 通过 |
| `artifacts/ppg/ppg_metadata.json` | 同上 | 4,493 B | SHA-256 通过 |
| `artifacts/ppg/ppg_quality_rf.joblib` | 同上 | 16,279,337 B | SHA-256 通过 |

`scripts/verify_artifacts.py` 六个产物全部输出 `verified`。`SensorModels.status()`：

- `activity.available=true`，feature version `pamap2-wrist-acc-hr-v1`
- `ppg.available=true`，feature version `ppg-dalia-bvp-acc-v1`

合成输入推理：

- PAMAP2 域：`activity.available=true`，label `sitting`，confidence `0.7102`
- PPG-DaLiA 域：`signal.available=true`，reliability `0.834`，estimated HR `72.53`

以上为软件链路验证值，不是准确率或临床结论；本次未重新训练模型。

## 7. Live/Replay 来源隔离

| 检查 | 结果 |
| --- | --- |
| `source=simulator` | `data_mode=replay` |
| `source=stm32` / `esp32` | `data_mode=live` |
| `source=stm32` + `dataset` | `data_mode=replay` |
| 显式冲突的 `data_mode` | Pydantic 拒绝 |
| 合成场景证据包 | `data_modes=["replay"]`、`has_synthetic=true` |
| 未接入原始 PPG | `available=false`，原因 `raw_ppg_not_ingested` |
| 无硬件来源的 live 物理证据 | 被拒绝（`live physical evidence requires a hardware source`） |
| 真实 stm32 遥测 | 证据标记为 `live`，`has_synthetic=false` |
| APK 上传 | 仅 `live_device` 镜像到 Agent 存储；`replay`、`device_history` 不进入，不能回退 `latest` |

真实回放验收（`scripts/verify_ml_replay.py`，本机 PPG-DaLiA 数据，不联网）：

- 6 组 PPG 回放（S13/S14/S15 × 2 种活动），每组 20 tick、13 次推理，共 78 次
- 每组 13 条推理全部持久化，Agent 工具均返回 `get_model_outputs`
- PAMAP2 回放 12 tick、7 次推理；结果 `standing 6 / ascending_stairs 1`
- 与本次运行前的记录对比，tick、推理数、降级数、activity 聚合完全一致

## 8. 数据库兼容与迁移

使用 Agent 2.0 之前生成的 `.runtime/v07/console-final.db` 副本测试，原库不被修改：

| 检查 | 结果 |
| --- | --- |
| 迁移前数据 | telemetry 20 / events 1 / investigations 1 / ml_inferences 13 |
| 迁移后数据 | 完全一致，无丢失 |
| 新增 investigations 列 | `attempts, max_attempts, duplicate_of_event_id, structured_json, usage_json, evidence_count, last_error_code, deadline_at` |
| 新增表 | `evidence`、`investigation_transitions` |
| 重复初始化 | 幂等（连续打开两次无错误） |
| 证据幂等 | 同一 `(event_id, source_ref)` 第二次写入返回原 ID、`created=false` |
| 迁移后读写 | evidence、transition、structured、usage 均可读回 |
| 旧 events 列路径 | 通过删列副本实测可补回，行数不变 |
| `apk_credentials.expires_at` | 通过删列副本实测可补回 |
| 当前 `guardian.db` 副本 | 2,254 telemetry / 14 events / 8 investigations / 77 ml_inferences 可正常读取 |

## 9. DeepSeek 合成数据调查链路

`scripts/verify_agent2_deepseek.py` 使用脚本内生成的合成波形和合成证据，未发送真实用户或 PPG-DaLiA 信号：

| 项目 | 结果 |
| --- | --- |
| `status` / `ai_status` | `completed` / `completed` |
| 模型 | `deepseek-flash` |
| 耗时 | 6.54 秒 |
| 证据数 | 13 |
| structured | `inconclusive` |
| usage | prompt 8,348 / completion 910 / total 9,258 tokens |
| 状态转换 | `queued → running → completed` |
| fallback | `false` |
| 记录文件 | `docs/agent2-deepseek.json`（已刷新） |

模型正确识别了数据为合成/回放、IMU 恒定、缺少原始 PPG/雷达/呼吸数据，并给出 `inconclusive` 与人工复核建议，而非编造结论。记录中不含 API Key、Authorization、Bearer 或提示词正文。

## 10. 密钥与隐私扫描

- 当前分支共 85 个受版本控制文件。
- `.env`、`guardian.db`、`data/raw/`、`artifacts/`、`.runtime/`、`logs/` 均被 `.gitignore` 忽略；`.env`、`guardian.db` 从未进入 Git 历史。
- 当前树与全部提交历史扫描 `sk-*`、GitHub token、私钥、Bearer 密钥模式：**无真实密钥命中**。
- `DEEPSEEK_API_KEY` / Bearer 的命中仅为 `.env.example` 占位符、README 示例、代码读取逻辑和测试字面量（如 `Bearer invalid`、测试生成的临时 token）。
- Git 历史中 `data/raw/`、`artifacts/`、`.pkl`、`.dat` 对象数为 0；数据集和模型未误提交。
- `docs/agent2-deepseek.json` 为脱敏记录；`docs/t4-t5-replay-results.json` 为聚合结果，不含原始波形。

## 11. 遗漏检查

- 软件层无遗漏：v0.7 全部提交在集成祖先链中；`main` 合并提交无内容差异；v0.7..集成无文件删除。
- 未合并的四条硬件分支属于 H1/H2、nRF52840、ESP32 固件范围，按用户要求不集成、不修改。
- 无 stash、无未提交的 Codex 工作树改动。
- 未重训模型、未改动已有模型产物；六个产物仅做加载与校验。

## 12. 已知警告与限制

- pytest 唯一警告为既有 Starlette/httpx 弃用提示，非本次回归。
- 回放脚本在 sklearn 并行预测时出现若干 `ResourceWarning: unclosed database`，不影响结果；属于已有连接生命周期问题，可后续清理。
- 本次没有真实硬件联调；H1/H2、nRF52840、ESP32 均未验收。
- 数据与模型不入库：新机器需复制 `data/`、`artifacts/` 和 `.env` 后才能复现模型与回放检查。
- 合成 DeepSeek 调用证明链路与安全降级有效，不代表对真实用户数据的诊断能力。
- token 上限是 token 数，不是美元计费上限。

## 13. 交付与下一步

- 分支：`integration/sensemind-v07-agent2`
- 代码基线：`f022b2d`
- 本报告随验收提交推送；最终提交 SHA 以 `origin/integration/sensemind-v07-agent2` 的 HEAD 为准
- 刷新后的证据：`docs/agent2-deepseek.json`、`docs/t4-t5-replay-results.json`
- 状态更新：`STATE.md`、`TASKS.md`
- **未合并 `main`；等待用户确认后再执行合并**
- 后续阶段：H1/H2 硬件分支与软件集成结果的联合验收，再进行 ESP32/nRF52840 实机联调
