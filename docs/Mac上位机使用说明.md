# 知微 SenseMind Mac 上位机使用说明

适用范围：MacBook Air M1（Apple Silicon）+ 本地 FastAPI 后端。上位机是运行在浏览器中的本地 Web 控制台，不复制后端、不引入新的前端构建链。

## 启动

```sh
cd /path/to/guardian-angel-agent-starter
bash 启动守护天使.command          # 首次会准备 .venv
# 或者直接启动并自动打开控制台
bash 启动Mac上位机.command
```

打开 `http://127.0.0.1:8000/console`。服务默认只绑定 `127.0.0.1`；不要改成 `0.0.0.0` 暴露到公网。手机接入请使用单独的局域网/HTTPS 启动说明。

## 总览

- **设备状态**：设备 ID、Live/Replay 模式、数据新鲜度、来源、采样状态、质量、心率、SpO₂、呼吸、运动。
- **实时趋势**：选择设备后读取 `/v1/history`，绘制心率、SpO₂、加速度幅值趋势；无样本时明确显示缺失。
- **系统健康**：`/health`、`/v1/runtime/status`、`/v1/ml/status`，展示三个模型是否 available、DeepSeek 是否配置、硬件验收状态。
- **事件列表与详情**：规则事件、Agent 2.0 调查状态、人工确认、时间线、原始报告。
- **Evidence Engine 证据详情**：读取 `/v1/events/{id}/evidence`，展示来源、值、单位、质量、Live/Replay、synthetic、时间、模型版本和时钟不确定度；未提供的来源会明确列出，并说明缺失不能被解释为正常。
- **调查状态转换**：读取 `/v1/investigations/{id}`，展示 queued/running/retrying/completed/failed、尝试次数、token 用量和后续证据数量。
- **人工确认**：保留原有 `POST /v1/events/{id}/ack`；可配置本地控制台令牌 `GUARDIAN_CONSOLE_TOKEN`。AI 没有确认写权限。

## 数据语义

- `Live Mode · 硬件来源` 只在遥测记录的 `data_mode=live` 时显示；模拟器/回放始终显示 `Replay Mode · 历史/模拟`。
- 呼吸显示为 `—`，因为 main 分支尚未接入雷达原始数据；不使用 0 或推断值填充。
- 运动优先显示模型活动标签和置信度；没有模型输出时只显示加速度幅值，并注明“不是活动分类”。
- nRF52840 与 ESP32 状态来自遥测来源推断；H1 硬件 API 尚未合入 main，因此界面明确显示“无硬件连接证据”，不能当作真机连接证明。
- 研究性结果不是医学诊断；事件是实验规则，不是发作识别。

## 兼容性

控制台只读取现有接口，没有修改 APK `/v1` 契约、H1 协议、存储 schema 或模型。`/console` 与 `/v1/console/*` 只注册在本地应用，公共 APK 入口不会暴露。

## 验收

```sh
.venv/bin/python -m pytest -q
# 截图/人工检查
bash 启动Mac上位机.command
```

已实测：现有 `guardian.db` 副本可加载 8 台设备、事件、调查和趋势；证据为空的事件会显示“该事件暂无证据记录”。WebSocket 断线会自动重连。
