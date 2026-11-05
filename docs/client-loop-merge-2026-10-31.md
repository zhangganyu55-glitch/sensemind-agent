# 知微 SenseMind 客户端业务闭环合并报告

日期：2026-10-31
操作：将 `integration/client-loop` 合并到 `main`
范围：**软件层验收**。Android 页面视觉验收、BLE 实物测试、H1 硬件上传、毫米波雷达联调仍未完成，未标记为实物通过。

## 1. 合并前状态

| 项目 | 值 |
| --- | --- |
| 源分支 | `integration/client-loop` |
| 源提交 | `72294ae` |
| 目标分支 | `main` |
| 本地 main / `origin/main` | `ef3d18f` / `ef3d18f`（拉取后一致，无新提交） |
| 工作区 | 干净 |
| 集成分支包含 | `claude/mac-console` `b9ffbb0`、`claude/android-app` `d9ac302`、Agent 2.0（main 既有） |
| 删除文件 | 无 |

集成分支已先推送远程：

```text
origin/integration/client-loop = 72294ae
```

## 2. 合并结果

```text
main merge commit = dfd8d61
```

- 使用非快进合并，保留 Mac 控制台与 Android 两条分支历史。
- 无冲突；没有强制推送。
- 未修改任何硬件 WIP 分支：
  - `codex/v07-esp32` `6f59495`
  - `codex/v07-hardware-api` `54da47f`
  - `codex/v07-hardware-integration` `f3d9ed3`
  - `codex/v07-nrf52840` `21c3fb1`

## 3. 合并后 main 验证

| 检查 | 结果 |
| --- | --- |
| 全量 Python 测试 | **169 passed / 1 warning**（13.84 s） |
| 角色/APK/E2E 定向 | **30 passed / 1 warning**（5.90 s） |
| Live/Replay/证据定向 | **22 passed / 1 warning**（2.01 s） |
| 旧库迁移脚本 | 通过；`apk_credentials.role`、`apk_pairing_codes.role` 默认 `legacy`，新码角色 `caregiver` |
| 权限接口 | `CONSENT_ROLE_POLICY` 存在：`upload→wearer`、`ai_analysis→caregiver`、`remote_alerts→caregiver` |
| Live/Replay | 当前演示库遥测为 `replay`；Android 未把 replay 标为 Live；相关测试通过 |
| 密钥扫描 | 未发现真实 `sk-*`、私钥、GitHub token；仅 README 占位符 |
| 误提交 | 未跟踪 `.env`、数据库、APK、`.joblib`、`local.properties`、构建目录 |
| Android 版本 | 源码 `versionCode=2`、`versionName=0.2.0`；debug 允许局域网 HTTP，release 禁止 cleartext |

说明：`tests/test_v07.py`、`test_agent2_evidence.py`、`test_agent2_scenarios.py` 覆盖 `data_mode` / replay / live 相关行为；全量测试已包含这些用例。

## 4. 边界与未完成项

以下内容**没有**因本次合并被标记为通过：

1. Android 页面逐项视觉验收：无 ADB、无手机截图；只有手机请求日志、数据库和 Mac 控制台 API 证据。
2. nRF52840 BLE 实物连接、断线重连、MTU、吞吐。
3. H1 `/v1/hardware/packets` 上传闭环；当前仍为 404。
4. 毫米波雷达联调。
5. Release APK 发布签名（当前交付为 debug 签名）。
6. 调查重试的证据时态与 `synthetic` 字段语义，仍按验收报告中的已知问题保留。

## 5. 远程状态

- `origin/integration/client-loop` 已更新到 `72294ae`。
- 本轮按指令完成了本地 main 合并与验证；**未推送 main 到远程**（源分支推送是明确要求的步骤，main 推送未被明确要求）。
- 如用户需要，可执行普通的 `git push origin main`；禁止 force push。

最终本地 main HEAD 以 `git rev-parse main` 为准；合并提交为 `dfd8d61`。

## 6. 结论

软件层合并完成并已通过 main 全量回归。硬件与真机视觉项仍为未完成验收，不随本次合并改变。
