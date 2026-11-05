# 任务队列

状态：`todo` / `doing` / `blocked` / `done`

| ID | 任务 | 状态 | 负责人 | 分支 | 备注 |
| --- | --- | --- | --- | --- | --- |
| T1 | 下载 PPG-DaLiA 完整包并核对 zip 结构 | done | claude | — | 已下载并校验：`data/raw/ppg-dalia-sciebo.zip`（2,864,337,783 bytes，`unzip -t` 通过） |
| T2 | 核对 PPG 8 s 窗口 / 2 s 步长标签对齐 | done | codex | codex/ppg-training | PDF + S1/S10/S15 共 15 窗核对无偏移；参数保护及 8 个新增测试；见 `docs/ppg-alignment.md` |
| T3 | 训练 PPG 质量 / HR 模型 | done | codex | codex/ppg-training | 本地 6000 窗；质量 acc 75.33% / macro F1 65.95%；HR MAE 7.76 bpm；run `20261031-ppg-dalia-rf-local` |
| T4 | 验证两个模型 available | done | codex | codex/ppg-training | `/v1/ml/status`、`/v1/ml/analyze` |
| T5 | PPG 真实数据回放 + 控制台检查 | done | codex | codex/ppg-training | `scripts/replay_dataset.py` |
| T6 | 改善活动模型 walking 召回率 | todo | — | — | 可后置 |
| T7 | README / 版本记录 | done | codex | codex/ppg-training | V7 已完成 v0.7 README / API 版本与验收说明，覆盖原 v0.6 收尾项 |
| T8 | 全量测试 | done | codex | codex/ppg-training | `.venv/bin/python -m pytest -q`：118 passed / 2 warnings（既有 NumPy 除零 + Starlette/httpx 弃用） |

| V7 | v0.7 第一阶段：模式、控制台、真实 API、质量审计、10 分钟稳定性 | done | codex | codex/ppg-training | 127 passed / 1 warning；真实 API、600 秒回放通过；见 docs/v07-report.md；不合并 main |
| A1 | Agent 2.0 统一证据模型与 Evidence Engine | done | claude | claude/agent2 | 10 类来源、可用性、时间对齐、幂等持久化；`guardian/evidence.py` |
| A2 | Investigation Manager（状态机/去重/重试/恢复/并发/成本/后续证据） | done | claude | claude/agent2 | `guardian/investigation.py`、`investigation_transitions`；默认 2 次重试、1 worker、8k token 上限 |
| C1 | DeepSeek Structured Reasoner 与证据引用校验 | done | claude | claude/agent2 | `guardian/reasoner.py`；拒绝不存在的引用、编造数值、危险建议；不足降级 inconclusive |
| D1 | 10 个合成多模态场景 | done | claude | claude/agent2 | `guardian/synthetic.py`、`tests/test_agent2_scenarios.py`；全部 synthetic/replay |
| E1 | Agent 2.0 测试、真实 DeepSeek 调用与架构文档 | done | claude | claude/agent2 | 161 passed / 1 warning；`docs/agent2-architecture.md`、`docs/agent2-deepseek.json`；不合并 main |
| I1 | v0.7 + Agent 2.0 软件集成验收 | done | claude | integration/sensemind-v07-agent2 | 161 passed；6 模型、DB 迁移、Live/Replay、真实回放、合成 DeepSeek 通过；见 `docs/integration-acceptance-2026-10-31.md` |
| I2 | 合并软件集成分支到 main 并打版本标记 | done | claude | main | Merge Commit `1a6a1ce`；main 复跑 161 passed；未合并硬件 WIP；tag `v0.7.0-agent2` |

## 客户端阶段 · Mac 上位机（2026-10-31）

状态图例：⬜ Pending / 🔵 In Progress / ✅ Completed / ❌ Failed / ⏸ Blocked

| ID | 子任务 | 状态 | 负责人 | 分支 | 证据 |
| --- | --- | --- | --- | --- | --- |
| M1 | 审查后端 API、现有控制台、H1 边界 | ✅ Completed | claude | claude/mac-console | API/console/H1 审计记录 |
| M2 | 创建独立 worktree 与分支 | ✅ Completed | claude | claude/mac-console | commit `20343a7` |
| M3 | 深色总览、趋势、设备/模型健康、Live/Replay | ✅ Completed | claude | claude/mac-console | `guardian/console.py` |
| M4 | Evidence Engine 证据详情与调查状态转换 | ✅ Completed | claude | claude/mac-console | 截图与真实数据库副本验收 |
| M5 | 页面验收与完整回归 | ✅ Completed | claude | claude/mac-console | 161 passed；`Mac控制台-总览/证据详情.png` |

整体进度：**5/5 完成（100%）**。

## 客户端阶段 · Android / 角色后端（2026-10-31）

状态图例：⬜ Pending / 🔵 In Progress / ✅ Completed / ❌ Failed / ⏸ Blocked

| ID | 子任务 | 状态 | 负责人 | 分支 | 证据 / 阻塞原因与下一步 |
| --- | --- | --- | --- | --- | --- |
| A1 | 审查 APK 契约、授权模型、固件 BLE 源码 | ✅ Completed | claude | claude/android-app | 无角色模型、UUID/分片源码核实 |
| A2 | 创建独立 worktree 与分支 | ✅ Completed | claude | claude/android-app | commit `4e07f78` |
| A3 | 后端 role 列、配对 CLI `--role`、旧库迁移 | ✅ Completed | claude | claude/android-app | `tests/test_client_roles.py` |
| A4 | 照护者对象/事件/调查/证据/确认接口与 family 隔离 | ✅ Completed | claude | claude/android-app | 166 passed |
| A5 | Kotlin + Compose 双角色 UI、设置、错误/离线状态 | ✅ Completed | claude | claude/android-app | `android/app/src/main/java` |
| A6 | 可替换仓库、REPLAY/SYNTHETIC 强制标识 | ✅ Completed | claude | claude/android-app | 单元测试禁止 live |
| A7 | BLE H1 分片/CRC/ACK 客户端（源码核实） | ✅ Completed | claude | claude/android-app | 未真机验证，见 A11 |
| A8 | APK debug/release 构建与单元测试 | ✅ Completed | claude | claude/android-app | debug 11 MB；5 unit tests；release lintVital |
| A9 | HTTP E2E：角色/上传/调查/证据/确认 | ✅ Completed | claude | claude/android-app | e2e PASS，H1 404 不 ACK |
| A10 | Android 真机安装与联调 | ⏸ Blocked | claude | claude/android-app | `adb devices` 为空；连接真机后 `adb install -r` 并连后端 |
| A11 | nRF52840 BLE 真机连接/重连/数据 | ⏸ Blocked | claude | claude/android-app | 无实物；UUID/协议仅源码核实；有板后单独验收 |
| A12 | H1 `/v1/hardware/packets` 上传闭环 | ⏸ Blocked | claude | claude/android-app | main 未合入硬件 API，当前 404；合入后验证 ACK |
| A13 | 合并 `claude/android-app` 到 main | ⬜ Pending | claude | — | 等待用户确认；不自动合并 |

整体进度：**9/13 完成（69%）**；3 项阻塞；1 项待确认；0 失败。

## 完整业务闭环（2026-10-31，已合并 main）

| ID | 子任务 | 状态 | 证据 / 阻塞原因与下一步 |
| --- | --- | --- | --- |
| C1 | 核实服务、端口、数据库、分支、ADB、LLM 配置 | ✅ Completed | 8000/8001 无监听；18707 为无关旧服务；adb 无设备；LLM deepseek |
| C2 | 合并 Mac 与 Android 到独立集成分支 | ✅ Completed | `integration/client-loop` 合并 `b9ffbb0` + `d9ac302`，仅 TASKS/STATE 冲突已合并 |
| C3 | 统一服务与数据库 | ✅ Completed | 单一 `0.0.0.0:8001` 同时提供 `/console` 与 `/v1`，共用 `android-backend/guardian.db` |
| C4 | 后端 remote_alerts 授权角色校验与越权测试 | ✅ Completed | 角色/范围策略；169 passed；真实 HTTP 授权 200、撤销 403、越权 403 |
| C5 | Android 同意开关与未授权停止轮询 | ✅ Completed | 0.2.0 APK；`/v1/me` 驱动；授权前 403、授权后 200、撤销后停止请求 |
| C6 | 构建并交付新 debug APK | ✅ Completed | `0.2.0-debug`；SHA-256 `c88112a8…f72d` |
| C7 | 启动共享后端并保留手机旧凭证 | ✅ Completed | 手机重配对后 `照护者-测试设备` 凭证有效；subjects/events 200 |
| C8 | synthetic/replay 事件真实 DeepSeek 调查 | ✅ Completed | 事件 `e3e47606…`；重试后 `analysis_source=ai`、5768 tokens、16 条 replay 证据、inconclusive |
| C9 | Android 真机查看调查与证据 | ✅ Completed | 手机 `172.20.10.14` 请求事件详情 200；按服务端数据核对，未采集屏幕截图 |
| C10 | Android 人工确认与 Mac 同步 | ✅ Completed | 手机 POST ack 200；DB `照护者-测试设备` @10:38:07Z；Mac console API 同步 `human_acknowledged` |
| C11 | 真机 remote_alerts 授权/撤销 | ✅ Completed | 授权后 alerts 200×4；撤销后无新请求；旧 403 停止 |
| C12 | 全量测试、报告、SHA、合并结论 | ✅ Completed | 169 passed；5 Android tests；报告 `docs/client-loop-acceptance-2026-10-31.md`；代码 `1bf08b4` |
| C13 | 推送集成分支并合并 main | ✅ Completed | 远程 main 预检 `ef3d18f` 无新提交；推送 `integration/client-loop` `72294ae`；非快进合并提交 `dfd8d61` |
| C14 | main 合并后接口、角色、迁移、Live/Replay 验证 | ✅ Completed | main **169 passed**；角色/APK/E2E **30 passed**；Live/Replay/证据 **22 passed**；旧库迁移通过；未强制推送 |

## Agent 3.0 · Decision Engine V0.1（2026-11-03）

| ID | 子任务 | 状态 | 负责人 | 分支 | 证据 / 备注 |
| --- | --- | --- | --- | --- | --- |
| DE1 | 架构审查、H1 分支关系与最小改动方案 | ✅ done | claude | claude/agent3-decision-engine | 已交付审查报告；State Engine 确认不存在；H1 仅在 `claude/hardware-api-v1` |
| DE2 | `decisions` 表、决策模块、质量/基线判定 | ✅ done | claude | claude/agent3-decision-engine | 默认 `GUARDIAN_DECISION_ENGINE=off`；真实设备持续变化需 `GUARDIAN_DECISION_REAL_SUSTAINED=on` |
| DE3 | 接入 `/v1/telemetry`、APK ingest、后台 sweep、只读接口 | ✅ done | claude | claude/agent3-decision-engine | 保持原有规则通知；`sustained_hr_change` 走真实的 `notify=False` 分支 |
| DE4 | 场景测试与全量回归 | ✅ done | claude | claude/agent3-decision-engine | **194 passed / 1 warning**；25 新增，含原 8 类 + 真实设备/不连续/基线污染/多事件/无通知/原有告警/并发/重启 |
| DE5 | 文档、测试结果、开发总结、推送分支 | ✅ done | claude | claude/agent3-decision-engine | `docs/decision-engine-v0.1.md`；未合并 main |

## Agent 3.0 · Decision Engine V0.1.1（2026-11-03）

| ID | 子任务 | 状态 | 负责人 | 分支 | 证据 / 备注 |
| --- | --- | --- | --- | --- | --- |
| S1 | 规则事件真实时间稳定性：打开/解除 dwell、数据缺口重置 | ✅ done | claude | claude/agent3-decision-v0.1.1 | open 2s / close 10s / gap 5s；无新增全局冷却 |
| S2 | STALE/UNKNOWN/NO_DATA 监测状态与最后有效数据时间 | ✅ done | claude | claude/agent3-decision-v0.1.1 | `device_monitoring` + `last_valid_at`；中断不自动 resolved |
| S3 | sustained 冷却改为基于 closed_at/cooldown_until | ✅ done | claude | claude/agent3-decision-v0.1.1 | `COALESCE(closed_at, opened_at)`；长事件回归通过 |
| S4 | sweep 可靠性字段与只允许本地 app 启动 | ✅ done | claude | claude/agent3-decision-v0.1.1 | last_sweep_at / last_error / thread_alive；public app 不启动 |
| S5 | 新增测试、全量回归、文档、推分支 | ✅ done | claude | claude/agent3-decision-v0.1.1 | **208 passed / 1 warning**；`docs/decision-engine-v0.1.1.md`；未合并 main |

## Agent 3.0 · Decision Engine V0.1.2

| ID | 子任务 | 状态 | 负责人 | 分支 | 证据 / 备注 |
| --- | --- | --- | --- | --- | --- |
| T12-1 | 时间体系：sample/server/monotonic、dwell 双条件、事件服务器时间、迁移标记 | ✅ done | claude | claude/agent3-decision-v0.1.2 | **218 passed / 1 warning**；`guardian/clock.py`、`source_started_at`、`legacy_estimated`；报告 `docs/decision-engine-v0.1.2-t12-1.md`；未合并 main |
| T12-1b | T12-1 补充修复：时钟前跳恢复、未来时间戳隔离、被动 Monitoring | ✅ done | claude | claude/agent3-decision-v0.1.2 | 3 项针对性测试；**221 passed / 1 warning**；未改 Agent 2.0/BLE/Android/H1 |
| T12-1c | R1 时钟回退误判修复：每设备 clock_jump 状态、仅前跳后允许重锚 | ✅ done | claude | claude/agent3-decision-v0.1.2 | 4 项针对性测试；**225 passed / 1 warning**；未改 Agent 2.0/BLE/Android/H1 |
| T12-1d | R1-01：严格时间推进 + 设备计数器证明，无 counter 保守拒绝 | ✅ done | claude | claude/agent3-decision-v0.1.2 | 5 项新增测试；**230 passed / 1 warning**；APK/ring 无 counter 走延迟恢复；未改 Agent 2.0/BLE/Android/H1 |
| T12-2 | 统一 GAP/STALE 连续性语义 | ⬜ todo | — | — | 未开始 |
| T12-3 | monitoring best-effort 与失败重试语义 | ⬜ todo | — | — | 未开始 |
| T12-4 | replay 兼容与 APK 重复批次/节流 | ⬜ todo | — | — | **阻塞项：replay 仍可能更新 live Monitoring；本轮未扩大修改** |
| T12-5 | sweep lease 与 H1 Decision 契约 | ⬜ todo | — | — | 未开始 |
| T12-6 | 全量测试、迁移文档、发布报告 | ⬜ todo | — | — | 未开始 |

## 认领规则

- 开始前把状态改成 `doing`，填负责人和分支。
- 完成后改 `done`，并在 `STATE.md` 写结果。
