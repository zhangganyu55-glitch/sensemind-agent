package com.sensemind.app.ui

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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.sensemind.app.data.EvidenceDto
import com.sensemind.app.data.EventDetailDto
import com.sensemind.app.data.MeasurementDto
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive

@Composable
fun SubjectDetailScreen(state: AppUiState, viewModel: AppViewModel) {
    val subject = state.selectedSubject ?: return
    val latest = subject.latest
    Column(Modifier.fillMaxSize()) {
        TopBar(
            title = subject.device_id,
            subtitle = "${subject.model} · ${subject.firmware_version ?: "固件未知"}",
            trailing = { TextButton(onClick = viewModel::clearSubject) { Text("← 返回", color = Cyan) } })
        if (state.isDemo) {
            Column(Modifier.padding(horizontal = 20.dp)) { DemoBanner(state.session?.demoMode) }
            Spacer(Modifier.height(8.dp))
        }
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (state.error != null) item { ErrorBanner(state.error, viewModel::refresh) }
            item {
                SectionCard("实时状态", "数据来源与新鲜度必须一起看，不能只看数值") {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        MetricTile("心率", latest?.heart_rate?.let { "%.0f".format(it) } ?: "—",
                            "bpm", if (subject.stale) "数据过期" else "较新读数",
                            if (subject.stale) Coral else Teal, Modifier.weight(1f))
                        MetricTile("SpO₂", latest?.spo2?.let { "%.0f".format(it) } ?: "—",
                            "%", "原型估算", Cyan, Modifier.weight(1f))
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        MetricTile("呼吸", "—", "", "未接入雷达原始数据", Faint,
                            Modifier.weight(1f))
                        MetricTile("运动", activityLabel(subject.model_output), "",
                            "模型辅助证据；无输出时显示缺失", Violet, Modifier.weight(1f))
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        DataModeChip(subject.data_mode)
                        Chip(if (subject.stale) "数据过期" else "数据新鲜",
                            if (subject.stale) Coral else Teal)
                        Chip("最近 ${subject.age_seconds?.let { "%.0f".format(it) + "s" } ?: "未知"}",
                            Muted)
                    }
                }
            }
            item {
                SectionCard("趋势", "来自 /v1/measurements；仅绘制实际存在的数值") {
                    val hr = series(state.measurements, "hr")
                    val spo2 = series(state.measurements, "spo2")
                    Text("心率 bpm", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    TrendChart(hr, Coral)
                    Spacer(Modifier.height(10.dp))
                    Text("SpO₂ %", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    TrendChart(spo2, Cyan)
                }
            }
            item {
                SectionCard("设备与来源", "H1 硬件后端尚未合入 main") {
                    KeyValue("来源", latest?.source ?: "无数据")
                    KeyValue("数据模式", sourceModeLabel(subject.data_mode))
                    KeyValue("质量", latest?.signal_quality ?: "unknown")
                    KeyValue("读数有效", if (latest?.reading_valid == true) "是" else "否")
                    KeyValue("nRF52840", "未确认真机连接；本页不作硬件连接证明")
                    KeyValue("ESP32 / 雷达", "无 H1 数据；呼吸显示为缺失")
                }
            }
            item {
                SectionCard("事件", "点击查看 Agent 2.0 调查、证据与人工确认") {
                    if (state.subjectEvents.isEmpty()) {
                        EmptyState("该设备暂无规则事件。")
                    } else {
                        state.subjectEvents.forEach { event ->
                            EventRow(event) { viewModel.openEvent(event.id) }
                            Spacer(Modifier.height(8.dp))
                        }
                    }
                }
            }
            item {
                SectionCard("历史监测记录", "最近 ${state.measurements.size} 条样本") {
                    if (state.measurements.isEmpty()) EmptyState("暂无历史样本。")
                    else {
                        state.measurements.sortedByDescending { it.observed_at }
                            .take(10).forEach { sample ->
                                Row(Modifier.fillMaxWidth().padding(vertical = 4.dp),
                                    horizontalArrangement = Arrangement.SpaceBetween) {
                                    Text("${isoToLocal(sample.observed_at)} · ${sample.metric}",
                                        style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                                    Text("%.1f %s · %s".format(sample.value, sample.unit, sample.quality),
                                        style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                                }
                            }
                    }
                }
            }
            item { Spacer(Modifier.height(20.dp)) }
        }
    }
}

@Composable
fun EventDetailScreen(state: AppUiState, viewModel: AppViewModel) {
    val detail = state.eventDetail ?: return
    val event = detail.event
    val investigation = detail.investigation
    Column(Modifier.fillMaxSize()) {
        TopBar(
            title = "事件详情",
            subtitle = "${eventKindLabel(event.kind)} · ${event.device_id}",
            trailing = { TextButton(onClick = viewModel::closeEvent) { Text("← 返回", color = Cyan) } })
        if (state.isDemo) {
            Column(Modifier.padding(horizontal = 20.dp)) { DemoBanner(state.session?.demoMode) }
        }
        if (state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (state.error != null) item { ErrorBanner(state.error, viewModel::refresh) }
            if (state.notice != null) item { Text(state.notice, color = Teal) }
            item {
                SectionCard("事件概览", "规则事件不是医学诊断") {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Chip(if (event.status == "active") "活动（规则判断）" else "已解除（规则判断）",
                            if (event.status == "active") Amber else Teal)
                        Chip(if (event.acknowledged) "已人工确认" else "未人工确认",
                            if (event.acknowledged) Teal else Violet)
                    }
                    Spacer(Modifier.height(8.dp))
                    KeyValue("事件时间", isoToLocal(event.opened_at))
                    KeyValue("人工确认", event.acknowledged_by?.let {
                        "$it · ${isoToLocal(event.acknowledged_at)}"
                    } ?: "尚未确认")
                    KeyValue("AI 调查", investigationText(investigation, "status") +
                        " / " + investigationText(investigation, "ai_status"))
                    KeyValue("模型", investigationText(investigation, "model"))
                    KeyValue("证据数量", investigationText(investigation, "evidence_count"))
                    KeyValue("尝试 / 上限", investigationText(investigation, "attempts") + " / " +
                        investigationText(investigation, "max_attempts"))
                    if (!event.acknowledged && state.role != com.sensemind.app.data.Role.WEARER) {
                        Spacer(Modifier.height(10.dp))
                        PrimaryButton("人工确认事件", enabled = !state.busy) {
                            viewModel.acknowledge(event.id)
                        }
                        Text("确认只记录人工操作，不改变 Agent 调查状态，也不代表身体恢复。",
                            style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(top = 6.dp))
                    }
                }
            }
            item {
                SectionCard("Agent 2.0 调查", "仅显示服务端调查记录；客户端不能伪造完成状态") {
                    if (investigation == null) {
                        EmptyState("该事件没有调查记录。")
                    } else {
                        val structured = investigation["structured"] as? JsonObject
                        val conclusion = structuredText(structured, "conclusion")
                        if (conclusion.isNotBlank()) {
                            Text(conclusion, style = androidx.compose.material3.MaterialTheme.typography.bodyMedium)
                            Spacer(Modifier.height(6.dp))
                        }
                        KeyValue("调查结论", structuredText(structured, "investigation_status"))
                        KeyValue("Token 用量", investigationText(investigation, "usage"))
                        structuredStringList(structured, "uncertainties").take(6).forEach {
                            Text("• 不确定性：$it", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                        }
                        structuredStringList(structured, "missing_data").take(6).forEach {
                            Text("• 缺失：$it", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                        }
                        structuredStringList(structured, "recommended_action").take(6).forEach {
                            Text("• 建议：$it", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                        }
                    }
                }
            }
            item {
                SectionCard("Evidence Engine 证据详情", "每条证据带来源、模式、质量与时间；缺失不解释为正常") {
                    if (detail.evidence.isEmpty()) EmptyState("该事件暂无证据记录。")
                    else detail.evidence.forEach { evidence ->
                        EvidenceRow(evidence)
                        Spacer(Modifier.height(8.dp))
                    }
                }
            }
            item {
                SectionCard("调查状态转换", "服务端记录 queued/running/retrying/completed/failed") {
                    if (detail.transitions.isEmpty()) EmptyState("没有状态转换记录。")
                    else detail.transitions.forEach { transition ->
                        Text("${isoToLocal(transition.created_at)}  ${transition.from_status ?: "—"} → " +
                            "${transition.to_status}  ${transition.reason ?: ""}",
                            style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(vertical = 3.dp))
                    }
                }
            }
            item {
                SectionCard("边界", null) {
                    Text("AI 调查由服务端生成，App 只读；人工确认不改变 AI 状态。" +
                        "研究性监测结果不是诊断、用药或急救建议。",
                        style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                }
            }
            item { Spacer(Modifier.height(20.dp)) }
        }
    }
}

@Composable
private fun EvidenceRow(evidence: EvidenceDto) {
    Column(
        Modifier.fillMaxWidth()
            .background(CardBg2, RoundedCornerShape(10.dp))
            .border(1.dp, Line, RoundedCornerShape(10.dp))
            .padding(10.dp)
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically) {
            Text(evidenceLabel(evidence.source_type), color = Ink, fontWeight = FontWeight.SemiBold)
            DataModeChip(evidence.data_mode, evidence.synthetic)
        }
        Text("${jsonText(evidence.value)} ${evidence.unit.orEmpty()}",
            style = androidx.compose.material3.MaterialTheme.typography.bodyMedium)
        Text("质量 ${evidence.quality_status} · ${isoToLocal(evidence.timestamp)}" +
            " · 时钟不确定度 ${evidence.clock_uncertainty_ms?.let { "$it ms" } ?: "未知"}",
            style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
        if (!evidence.model_version.isNullOrBlank()) {
            Text("模型 ${evidence.model_version}", style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
        }
    }
}

private fun series(measurements: List<MeasurementDto>, metric: String): List<Double> =
    measurements.filter { it.metric == metric }.sortedBy { it.observed_at }.map { it.value }

private fun activityLabel(modelOutput: JsonElement?): String {
    val activity = (modelOutput as? JsonObject)
        ?.get("result")?.let { it as? JsonObject }
        ?.get("activity")?.let { it as? JsonObject } ?: return "—"
    val available = activity["available"]?.jsonPrimitive?.content == "true"
    if (!available) return "—"
    val label = activity["label"]?.jsonPrimitive?.content ?: "—"
    val confidence = activity["confidence"]?.jsonPrimitive?.content
    return confidence?.let { "$label ${(it.toDoubleOrNull()?.times(100))?.let { p -> "%.0f%%".format(p) } ?: ""}" }
        ?: label
}

private fun investigationText(investigation: JsonObject?, key: String): String {
    val value = investigation?.get(key) ?: return "—"
    return jsonText(value)
}

private fun structuredText(structured: JsonObject?, key: String): String {
    val value = structured?.get(key) ?: return "—"
    return jsonText(value)
}

private fun structuredStringList(structured: JsonObject?, key: String): List<String> {
    val array = structured?.get(key) as? JsonArray ?: return emptyList()
    return array.mapNotNull { item -> (item as? JsonPrimitive)?.content }
}

private fun jsonText(element: JsonElement?): String = when (element) {
    null -> "—"
    is JsonPrimitive -> element.content
    is JsonArray -> element.joinToString(", ") { jsonText(it) }
    is JsonObject -> element.entries.joinToString(", ") { "${it.key}=${jsonText(it.value)}" }
}.let { if (it.length > 240) it.take(240) + "…" else it }

private fun evidenceLabel(source: String): String = when (source) {
    "ppg" -> "原始 PPG"
    "heart_rate_estimate" -> "心率估计"
    "signal_quality" -> "信号质量"
    "activity" -> "活动"
    "imu_raw" -> "IMU 原始"
    "radar_status" -> "雷达状态"
    "breathing_rate" -> "呼吸率"
    "device_online" -> "设备在线"
    "historical_event" -> "历史事件"
    "device_status" -> "设备状态"
    else -> source
}
