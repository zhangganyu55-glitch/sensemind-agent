# 客户端阶段：接口、角色与安全记录

日期：2026-10-31。范围：Mac 上位机 + Android App；不修改 H1 冻结协议和硬件 WIP 分支。

## 后端角色模型

`apk_credentials` 与 `apk_pairing_codes` 新增 `role` 列，旧库自动补列并保留 `legacy`：

| 角色 | 设备注册/测量上传 | 配置发布 | 照护者对象/事件/调查 | 事件人工确认 |
| --- | --- | --- | --- | --- |
| `wearer` | 允许 | 拒绝 | 拒绝 | 拒绝 |
| `caregiver` | 拒绝 | 允许 | 允许 | 允许 |
| `legacy` | 允许（兼容旧 APK） | 允许 | 允许 | 允许 |

- 新配对码由本机 CLI `python -m guardian.pairing --role wearer|caregiver` 生成；App 配对响应与 `/v1/me` 返回角色。
- 后端凭据是唯一授权来源；客户端没有角色切换或提权接口。
- `legacy` 仅用于已有 APK 兼容，新客户端不得使用。

## 新增照护者接口

```text
GET  /v1/subjects
GET  /v1/caregiver/events?device_id=&limit=
GET  /v1/caregiver/events/{event_id}
POST /v1/caregiver/events/{event_id}/ack
```

- 全部要求 `caregiver`（或兼容 `legacy`）Bearer。
- 设备、事件、证据、调查均按凭据的 `family_id` 校验；跨家庭返回 404。
- `ack` 只写 `acknowledged_by/at`，不修改 `investigations.status/ai_status`，客户端不能伪造调查完成。
- 返回的调查记录来自服务端 `Storage`（Agent 2.0），包括 `report`、`structured`、`evidence_count`、`usage`、状态转换和证据列表。

## APK 安全

- 不打包 DeepSeek API Key；DeepSeek 只在后端使用。
- Bearer token 使用 Android Keystore 支持的 `EncryptedSharedPreferences`；Keystore 初始化失败时有私有偏好回退，并在文档中标注。
- Debug 允许局域网 HTTP；Release `usesCleartextTraffic=false`，UI 拒绝非 HTTPS。
- 默认不把 uvicorn 绑定到 `0.0.0.0`；手机接入需用户显式启动局域网服务，并自行承担网络隔离。
- 无远程推送；事件通知通过认证 API 轮询，避免在 APK 内嵌入推送服务密钥。

## Live/Replay

- 后端 `Telemetry` 仍强制 `data_mode` 与 `source/dataset` 一致；APK ingest 只镜像 `live_device`。
- Android UI 对每条证据显示 `LIVE/REPLAY/SYNTHETIC`；演示仓库全部为 replay/synthetic，测试断言禁止 live。
- Mac 控制台显示 `Live Mode · 硬件来源` 或 `Replay Mode · 历史/模拟`；H1 未接入时显示“无硬件连接证据”。

## BLE 源码核实

从 `origin/codex/v07-nrf52840` 只读核实：

- UUID 与 16 字节分片/8 字节 ACK 与 `protocol/README.md` 一致。
- 固件 `main.c` 发送 H1 `HardwarePacket` JSON，`source_type` 在测试构建强制为 `test`，真实构建为 `hardware`。
- 固件 READ 特征只返回队列状态（used/overruns），不是截断 JSON。
- 未发现真机验证日志；不得声称已连接真实 nRF52840。

## 待办 / 限制

- H1 `/v1/hardware/packets` 后端只在硬件分支；main 未合并。Android 上传路径返回 404 时明确失败且不 ACK。
- 无真实 BLE 板、无 Android 真机连接；BLE/安装验收需硬件。
- 未做推送、后台 BLE、TLS 证书固定、远程公网部署。
