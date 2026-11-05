package com.sensemind.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.sensemind.app.BuildConfig
import com.sensemind.app.data.Role

@Composable
fun SettingsScreen(state: AppUiState, viewModel: AppViewModel) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { Spacer(Modifier.height(4.dp)) }
        item {
            SectionCard("会话", "Bearer 凭证由后端签发；App 不能自行提升角色") {
                KeyValue("名称", state.session?.name ?: "—")
                KeyValue("角色", state.role.label)
                KeyValue("家庭 ID", state.session?.familyId?.take(12) ?: "—")
                KeyValue("监护人 ID", state.session?.guardianId?.take(12) ?: "—")
                KeyValue("后端", state.session?.baseUrl ?: "—")
                KeyValue("数据模式", state.session?.demoMode?.label ?: "后端 API（按每条记录标注 Live/Replay）")
                KeyValue("构建", if (BuildConfig.DEBUG) "debug · 允许局域网 HTTP" else "release · 强制 HTTPS")
            }
        }
        if (state.error != null) item { ErrorBanner(state.error, viewModel::refresh) }
        if (state.notice != null) item { Text(state.notice, color = Teal) }
        item {
            SectionCard("系统健康", "后端自报状态，不代表硬件验收") {
                KeyValue("服务版本", state.health?.version ?: "—")
                KeyValue("APK 契约", state.health?.apk_contract ?: "—")
                KeyValue("刷新", "每 5 秒（演示 10 秒）")
                Spacer(Modifier.height(8.dp))
                TextButton(onClick = viewModel::refresh) { Text("立即刷新", color = Cyan) }
                TextButton(onClick = { viewModel.clearMessages() }) { Text("清除提示", color = Muted) }
            }
        }
        item {
            SectionCard("通知授权", "remote_alerts 只影响告警读取；由后端校验角色，撤销立即生效") {
                when {
                    state.isDemo -> Text(
                        "演示模式使用本地数据，不需要也不写入后端授权。",
                        style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    state.role == Role.WEARER -> {
                        KeyValue("remote_alerts", "由照护者凭证持有")
                        Text("佩戴者角色不能授予通知同意；告警由照护者端接收。",
                            color = Muted,
                            style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                    }
                    else -> {
                        KeyValue("remote_alerts",
                            if (state.remoteAlertsGranted) "已授权" else "未授权")
                        Text(
                            if (state.remoteAlertsGranted)
                                "已授权：App 会轮询被授权范围内的告警。撤销后立即停止。"
                            else
                                "未授权：App 不会请求 /v1/alerts。授予后只读取当前凭证范围内的告警。",
                            color = Muted,
                            style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
                        Spacer(Modifier.height(8.dp))
                        TextButton(
                            onClick = { viewModel.setRemoteAlerts(!state.remoteAlertsGranted) },
                            enabled = !state.busy,
                        ) {
                            Text(
                                if (state.remoteAlertsGranted) "撤销 remote_alerts 同意"
                                else "授予 remote_alerts 同意",
                                color = if (state.remoteAlertsGranted) Coral else Cyan,
                                fontWeight = FontWeight.SemiBold)
                        }
                    }
                }
            }
        }
        item {
            SectionCard("安全说明", null) {
                Text("• 不包含 DeepSeek API Key；后端负责模型调用\n" +
                    "• Bearer 凭证使用 Android Keystore 加密存储；撤销后立即失效\n" +
                    "• 开发版支持局域网 HTTP；正式版拒绝 HTTP\n" +
                    "• 不把开发服务器直接暴露到公网\n" +
                    "• App 只读 AI 调查；人工确认由照护者点击并记录\n" +
                    "• 研究性监测不是诊断、用药或急救指导",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall)
            }
        }
        item {
            SectionCard("退出", "退出时尝试撤销当前 Bearer 凭证") {
                Text(state.session?.demoMode?.let { "当前是演示会话，不会访问后端。" }
                    ?: "撤销后需要 Mac 端重新生成配对码。", color = Muted)
                Spacer(Modifier.height(8.dp))
                TextButton(onClick = viewModel::logout) {
                    Text(if (state.isDemo) "退出演示" else "退出并撤销凭证", color = Coral,
                        fontWeight = FontWeight.SemiBold)
                }
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}
