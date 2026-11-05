package com.sensemind.app.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.sensemind.app.data.MeasurementDto
import com.sensemind.app.ble.BleDeviceInfo
import com.sensemind.app.ble.HardwarePacketParser
import com.sensemind.app.ble.NrfBleClient
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
fun WearerShell(state: AppUiState, viewModel: AppViewModel) {
    var tab by rememberSaveable { mutableIntStateOf(0) }
    Column(Modifier.fillMaxSize()) {
        TopBar(
            title = "佩戴者 · 设备与上传",
            subtitle = "${state.session?.name.orEmpty()} · ${state.role.label}" +
                if (state.isDemo) " · ${state.session?.demoMode?.label}" else "",
            trailing = { TextButton(onClick = viewModel::refresh) { Text("刷新", color = Cyan) } })
        Column(Modifier.weight(1f)) {
            if (state.isDemo) {
                Column(Modifier.padding(horizontal = 20.dp)) {
                    DemoBanner(state.session?.demoMode)
                }
            }
            if (state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            when (tab) {
                0 -> WearerOverview(state, viewModel)
                1 -> BleScreen(state, viewModel)
                else -> SettingsScreen(state, viewModel)
            }
        }
        TabBar(tab, listOf("总览", "BLE 设备", "设置")) { tab = it }
    }
}

@Composable
private fun WearerOverview(state: AppUiState, viewModel: AppViewModel) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { Spacer(Modifier.height(4.dp)) }
        if (state.error != null) item { ErrorBanner(state.error, viewModel::refresh) }
        if (state.notice != null) item { Text(state.notice, color = Teal) }
        val devices = state.me?.devices.orEmpty()
        if (devices.isEmpty()) {
            item {
                SectionCard("尚未注册设备", "佩戴者负责注册与上传；照护者只能只读查看") {
                    EmptyState("没有设备。注册后会得到后端设备 ID。")
                    PrimaryButton("注册本机设备") { viewModel.registerWearerDevice() }
                }
            }
        } else {
            items(devices, key = { it.id }) { device ->
                val readings = state.wearerReadings[device.id].orEmpty()
                val latestHr = readings.lastOrNull { it.metric == "hr" }?.value
                val latestSpo2 = readings.lastOrNull { it.metric == "spo2" }?.value
                val mode = readings.lastOrNull()?.let { measurementMode(it) }
                SectionCard(device.id, "${device.model} · ${device.firmware ?: "固件未知"}") {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        MetricTile("心率", latestHr?.let { "%.0f".format(it) } ?: "—", "bpm",
                            "来自已上传记录", Teal, Modifier.weight(1f))
                        MetricTile("SpO₂", latestSpo2?.let { "%.0f".format(it) } ?: "—", "%",
                            "原型估算", Cyan, Modifier.weight(1f))
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        DataModeChip(if (mode == "synthetic") "replay" else mode,
                            synthetic = mode == "synthetic")
                        Chip("上传样本 ${readings.size}", Muted)
                        Chip("最近 ${readings.lastOrNull()?.observed_at?.let { isoToLocal(it) } ?: "无"}",
                            Muted)
                    }
                    Text("设备已注册；数据来源由每条 measurement.source 决定，App 不把它改写成 Live。",
                        style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 6.dp))
                }
            }
        }
        item {
            SectionCard("数据上传路径", "两条路径严格区分，不把原始计数伪装成心率/血氧") {
                Text("1) APK 测量批次：注册设备后上传已校准的 hr/spo2 等指标；" +
                    "只有 live_device 会镜像到 Agent 存储。",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(6.dp))
                Text("2) nRF52840 H1 原始包：通过 BLE 接收 MAX30102/MPU6050 原始计数；" +
                    "需要后端 /v1/hardware/packets（main 尚未合入硬件 API）。" +
                    "后端未确认持久化时 App 不发送 BLE ACK，固件会重发。",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(6.dp))
                Text("当前没有真实 nRF 设备时，不伪造 BLE 连接或 Live 数据。",
                    color = Amber, style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
            }
        }
        item {
            SectionCard("事件通知", null) {
                if (state.alerts.isEmpty()) EmptyState(
                    if (state.remoteAlertsGranted) "已授权，但当前没有告警。"
                    else "佩戴者不轮询告警；告警由照护者凭证接收。")
                else state.alerts.take(4).forEach { alert ->
                    Text("${alert.kind} · ${isoToLocal(alert.created_at)}", color = Amber)
                    Text(alert.text, style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(4.dp))
                }
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

@Composable
private fun BleScreen(state: AppUiState, viewModel: AppViewModel) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val client = remember { NrfBleClient(context) }
    var devices by remember { mutableStateOf<List<BleDeviceInfo>>(emptyList()) }
    var scanning by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("未连接") }
    var log by remember { mutableStateOf<List<String>>(emptyList()) }
    var pendingAction by remember { mutableStateOf<(() -> Unit)?>(null) }

    val requiredPermissions = remember {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }
    }

    val startScan: () -> Unit = {
        devices = emptyList()
        scanning = true
        status = "扫描 SenseMind 服务…"
        client.startScan(
            onDevice = { device ->
                if (devices.none { it.address == device.address }) devices = devices + device
            },
            onError = { message -> scanning = false; status = message },
        )
    }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        val action = pendingAction
        pendingAction = null
        if (result.values.all { it }) {
            action?.invoke()
        } else {
            status = "蓝牙权限被拒绝；无法扫描或接收 nRF 数据"
        }
    }

    fun ensurePermissions(action: () -> Unit) {
        val missing = requiredPermissions.filter {
            ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) action() else {
            pendingAction = action
            permissionLauncher.launch(missing.toTypedArray())
        }
    }

    DisposableEffect(Unit) {
        onDispose { client.disconnect() }
    }
    LaunchedEffect(scanning) {
        if (scanning) {
            delay(15_000)
            if (scanning) {
                client.stopScan()
                scanning = false
                status = "扫描结束"
            }
        }
    }

    fun append(line: String) {
        log = (log + line).takeLast(16)
    }

    fun connect(device: BleDeviceInfo) {
        client.stopScan()
        scanning = false
        status = "连接 ${device.name}…"
        client.connect(
            address = device.address,
            onState = { message -> status = message },
            onPacket = { messageId, json ->
                val summary = runCatching { HardwarePacketParser.summarize(json) }.getOrNull()
                if (summary == null) {
                    append("收到无法解析的 JSON（message_id=$messageId），未 ACK")
                } else {
                    append("H1 ${summary.sourceType} · ${summary.sensorType}/${summary.streamId} " +
                        "seq=${summary.sequence} 样本=${summary.sampleCount} " +
                        "传感器=${if (summary.sensorPresent) "存在" else "缺失"}")
                    scope.launch {
                        val accepted = viewModel.uploadHardwarePacket(json)
                        if (accepted) {
                            client.sendAck(messageId)
                            append("后端已确认持久化，已发送 ACK message_id=$messageId")
                        } else {
                            append("未上传/未 ACK，固件将重发 message_id=$messageId")
                        }
                    }
                }
            },
            onError = { message -> status = message },
        )
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { Spacer(Modifier.height(4.dp)) }
        if (state.error != null) item { ErrorBanner(state.error, viewModel::refresh) }
        item {
            SectionCard("BLE 连接", "服务/特征 UUID 来自固件源码，尚未在真机验证") {
                Text("同一手环同时只能被一个 BLE 中心连接。连接前请在 Mac 上停止 BLE 网关，" +
                    "否则可能连接失败或抢断。App 不会修改固件。",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall, color = Amber)
                Spacer(Modifier.height(8.dp))
                KeyValue("状态", status)
                KeyValue("服务", "5f9b3c10-…-1c2d3e4f5a60")
                KeyValue("数据", "5f9b3c11-…（notify/read；READ 仅返回队列状态）")
                KeyValue("ACK", "5f9b3c12-…（write 8 字节 SA v1 status id）")
                Spacer(Modifier.height(8.dp))
                PrimaryButton(if (scanning) "扫描中…" else "扫描设备",
                    enabled = !scanning) { ensurePermissions { startScan() } }
                TextButton(onClick = {
                    client.disconnect()
                    status = "已断开"
                }) { Text("断开连接", color = Coral) }
                if (scanning) LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        }
        if (devices.isNotEmpty()) {
            item { Text("发现的设备", color = Ink, fontWeight = FontWeight.SemiBold) }
            items(devices, key = { it.address }) { device ->
                Row(
                    Modifier.fillMaxWidth()
                        .background(CardBg, RoundedCornerShape(12.dp))
                        .border(1.dp, Line, RoundedCornerShape(12.dp))
                        .padding(12.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(device.name, color = Ink, fontWeight = FontWeight.SemiBold)
                        Text("${device.address} · RSSI ${device.rssi}",
                            style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    }
                    TextButton(onClick = { ensurePermissions { connect(device) } }) {
                        Text("连接", color = Teal)
                    }
                }
            }
        }
        item {
            SectionCard("H1 原始数据包", "只显示固件实际发送的原始通道；不推算心率/血氧") {
                if (log.isEmpty()) {
                    EmptyState("尚无 BLE 数据。没有真实 nRF52840 时不会显示伪造连接。")
                } else {
                    log.forEach { line ->
                        Text("• $line", style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(vertical = 2.dp))
                    }
                }
            }
        }
        item {
            SectionCard("传输协议提醒", null) {
                Text("16 字节分片头：magic SM、version 1、message_id、fragment_index/count、" +
                    "total_length、CRC16-CCITT-FALSE；最大 4096 字节。ACK 8 字节：" +
                    "SA + version + status + message_id。只有服务端确认持久化后才 ACK。",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(6.dp))
                Text("真机连接、断线重连、MTU 与吞吐仍待有板时单独验证。",
                    color = Amber, style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

private fun measurementMode(sample: MeasurementDto): String = when (sample.source) {
    "live_device" -> "live"
    "synthetic" -> "synthetic"
    "replay", "device_history" -> "replay"
    else -> "unknown"
}
