# 知微 SenseMind Android App

Kotlin + Jetpack Compose 原型客户端。两种后端角色：

- **佩戴者（wearer）**：注册设备、BLE 扫描/连接、接收 nRF52840 原始包、尝试上传、设备状态。
- **照护者（caregiver）**：被授权监测对象列表、实时状态、事件通知、AI 调查报告与证据、人工确认、历史记录。

角色由后端 Bearer 凭证决定；App 不能自选或提升角色。

## 构建

本机已验证的环境：

```text
JDK 17 (Homebrew openjdk@17)
Android SDK platforms;android-35 / build-tools;35.0.0
Gradle 8.9 wrapper
```

如果本机没有 SDK，可使用腾讯镜像（Google `dl.google.com` 在此网络被重置）：

```sh
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME="$HOME/Library/Android/sdk"
SDK_TEST_BASE_URL=https://mirrors.cloud.tencent.com/AndroidSDK/ \
  "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" \
  --sdk_root="$ANDROID_HOME" "platforms;android-35" "build-tools;35.0.0" "platform-tools"
yes | SDK_TEST_BASE_URL=https://mirrors.cloud.tencent.com/AndroidSDK/ \
  "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$ANDROID_HOME" --licenses
```

构建：

```sh
cd android
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew :app:assembleDebug :app:testDebugUnitTest
# APK: app/build/outputs/apk/debug/app-debug.apk
```

安装到已连接真机：

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## 后端与配对

```sh
# Mac 本机
.venv/bin/python -m uvicorn guardian.main:app --host 0.0.0.0 --port 8000
# 佩戴者配对码
.venv/bin/python -m guardian.pairing --role wearer --name 佩戴者
# 照护者配对码（另一次）
.venv/bin/python -m guardian.pairing --role caregiver --name 照护者
```

- 开发版（debug）允许 `http://` 局域网地址；正式版（release）禁止 cleartext，只接受 `https://`。
- App 只保存后端 Bearer 凭证（Android Keystore 加密偏好），不包含 DeepSeek API Key。
- 不要把开发服务器直接映射到公网。

## 数据来源与演示模式

- 每条测量/证据都带 `source` 或 `data_mode`；UI 分别显示 `LIVE`、`REPLAY`、`SYNTHETIC`。
- Debug 构建可在配对页进入演示模式；演示数据强制标记 `REPLAY`/`SYNTHETIC`，**绝不标记 Live**，也不上传或 ACK 固件。
- Release 构建 `BuildConfig.ALLOW_DEMO=false`，不显示演示入口。

## nRF52840 BLE

源码核实自 `codex/v07-nrf52840`（只读，未修改硬件分支）：

```text
Service  5f9b3c10-1d2a-4f6e-9a7b-1c2d3e4f5a60
Data     5f9b3c11-1d2a-4f6e-9a7b-1c2d3e4f5a60  (READ/NOTIFY)
ACK      5f9b3c12-1d2a-4f6e-9a7b-1c2d3e4f5a60  (WRITE)
```

- 16 字节小端分片头：`SM`、version=1、reserved=0、message_id、index、count、total_length、CRC16-CCITT-FALSE（整段 JSON）。
- ACK 8 字节：`SA`、version=1、status（0 accepted / 1 retry）、message_id LE。
- App 只有在后端确认持久化（`accepted=true`）后才 ACK；后端未部署 `/v1/hardware/packets` 时不 ACK，固件会重发。
- 同一手环只能有一个 BLE 中心。连接前应停止 Mac BLE 网关；App 对连接失败会提示检查网关占用。
- **未在真机验证**：连接、断线重连、MTU、吞吐、传感器数据都需有实物后单独验收。源码核实不等于真机验证。

## 接口

- 复用 `/v1/pairings/exchange`、`/v1/me`、`/v1/measurements`、`/v1/alerts` 等既有契约。
- 新增照护者只读/确认接口：`/v1/subjects`、`/v1/caregiver/events`、`/v1/caregiver/events/{id}`、`/v1/caregiver/events/{id}/ack`。
- AI 调查报告来自服务端 Agent 2.0；App 只读，不能伪造已完成调查。
- 人工确认记录 `acknowledged_by`，不改变 AI 状态。

## 已知限制

- 当前无真机 nRF52840、ESP32 或 Android 真机连接；APK 可构建，但真机安装/联调需设备。
- main 后端尚未合入 H1 硬件 API，因此 BLE 原始包上传会在 404 时明确失败，不伪造成功。
- 未实现远程推送通知（应用内轮询事件/告警）。
- 未实现后台常驻 BLE 服务；屏幕关闭/进程回收后连接会断开。
- 研究性监测结果不是医学诊断、用药或急救建议。
