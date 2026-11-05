package com.sensemind.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable
fun ScreenBackground(content: @Composable () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(DeepBg, Color(0xFF0A141D), DeepBg)))
    ) { content() }
}

@Composable
fun TopBar(title: String, subtitle: String? = null, trailing: @Composable (() -> Unit)? = null) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 14.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.headlineSmall)
            if (subtitle != null) {
                Text(subtitle, style = MaterialTheme.typography.bodySmall)
            }
        }
        trailing?.invoke()
    }
}

@Composable
fun SectionCard(
    title: String,
    subtitle: String? = null,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(CardBg, RoundedCornerShape(16.dp))
            .border(1.dp, Line, RoundedCornerShape(16.dp))
            .padding(14.dp)
    ) {
        Text(title.uppercase(), style = MaterialTheme.typography.labelSmall)
        if (subtitle != null) {
            Text(subtitle, style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 2.dp))
        }
        Spacer(Modifier.height(10.dp))
        content()
    }
}

@Composable
fun MetricTile(label: String, value: String, unit: String = "", note: String = "",
               accent: Color = Teal, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .background(CardBg2, RoundedCornerShape(12.dp))
            .border(1.dp, Line, RoundedCornerShape(12.dp))
            .padding(10.dp)
    ) {
        Text(label.uppercase(), style = MaterialTheme.typography.labelSmall, maxLines = 1)
        Row(verticalAlignment = Alignment.Bottom) {
            Text(value, fontSize = 24.sp, fontWeight = FontWeight.SemiBold, color = accent)
            if (unit.isNotBlank()) {
                Text(" $unit", style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(bottom = 3.dp))
            }
        }
        if (note.isNotBlank()) {
            Text(note, style = MaterialTheme.typography.bodySmall, color = Faint, maxLines = 2)
        }
    }
}

@Composable
fun Chip(text: String, color: Color = Muted) {
    Surface(
        color = color.copy(alpha = 0.10f),
        shape = RoundedCornerShape(999.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, color.copy(alpha = 0.45f)),
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 9.dp, vertical = 3.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(Modifier.size(6.dp).background(color, CircleShape))
            Text("  $text", style = MaterialTheme.typography.bodySmall, color = color)
        }
    }
}

@Composable
fun DataModeChip(mode: String?, synthetic: Boolean = false) {
    when {
        synthetic -> Chip("SYNTHETIC 合成", Amber)
        mode == "live" -> Chip("LIVE 硬件来源", Teal)
        mode == "replay" -> Chip("REPLAY 回放/模拟", Cyan)
        else -> Chip("模式未知", Faint)
    }
}

@Composable
fun DemoBanner(demoMode: com.sensemind.app.data.DemoMode?) {
    if (demoMode == null) return
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Amber.copy(alpha = 0.12f))
            .border(1.dp, Amber.copy(alpha = 0.5f), RoundedCornerShape(12.dp))
            .padding(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("演示数据", color = Amber, fontWeight = FontWeight.Bold)
        Text(
            " · ${demoMode.label} · 不是 Live，不代表真实用户或真实设备",
            style = MaterialTheme.typography.bodySmall,
            color = Ink,
        )
    }
}

@Composable
fun KeyValue(key: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(key, style = MaterialTheme.typography.bodySmall, color = Muted)
        Text(value, style = MaterialTheme.typography.bodySmall, color = Ink)
    }
}

@Composable
fun EmptyState(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = Faint,
        modifier = Modifier.padding(vertical = 12.dp))
}

@Composable
fun ErrorBanner(message: String, onRetry: (() -> Unit)? = null) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(Coral.copy(alpha = 0.10f))
            .border(1.dp, Coral.copy(alpha = 0.4f), RoundedCornerShape(12.dp))
            .padding(12.dp)
    ) {
        Text("请求失败：$message", style = MaterialTheme.typography.bodySmall, color = Coral)
        if (onRetry != null) {
            TextButton(onClick = onRetry) { Text("重试", color = Cyan) }
        }
    }
}

@Composable
fun PrimaryButton(
    text: String,
    enabled: Boolean = true,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.fillMaxWidth().height(48.dp),
        shape = RoundedCornerShape(12.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = Teal, contentColor = Color(0xFF04201D),
            disabledContainerColor = Line, disabledContentColor = Faint,
        ),
    ) { Text(text, fontWeight = FontWeight.SemiBold) }
}

@Composable
fun TrendChart(
    points: List<Double>,
    color: Color,
    modifier: Modifier = Modifier,
    heightDp: Int = 120,
) {
    if (points.size < 2) {
        EmptyState("样本不足，无法绘制趋势。")
        return
    }
    val min = points.min()
    val max = points.max()
    val span = (max - min).takeIf { it > 0.0001 } ?: 1.0
    Column(modifier = modifier) {
        Canvas(
            modifier = Modifier.fillMaxWidth().height(heightDp.dp)
                .background(CardBg2, RoundedCornerShape(10.dp))
                .padding(8.dp)
        ) {
            val w = size.width
            val h = size.height
            for (i in 0..3) {
                val y = h * i / 3f
                drawLine(Line, Offset(0f, y), Offset(w, y), strokeWidth = 1f)
            }
            val path = Path()
            points.forEachIndexed { index, value ->
                val x = w * index / (points.size - 1).coerceAtLeast(1)
                val y = h * (1f - ((value - min) / span).toFloat())
                if (index == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            drawPath(path, color, style = Stroke(width = 4f, cap = StrokeCap.Round))
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text("最低 %.1f".format(min), style = MaterialTheme.typography.bodySmall)
            Text("最新 %.1f".format(points.last()), style = MaterialTheme.typography.bodySmall,
                color = color)
            Text("最高 %.1f".format(max), style = MaterialTheme.typography.bodySmall)
        }
    }
}

fun isoToLocal(value: String?): String {
    if (value.isNullOrBlank()) return "—"
    return try {
        Instant.parse(value).atZone(ZoneId.systemDefault())
            .format(DateTimeFormatter.ofPattern("MM-dd HH:mm:ss"))
    } catch (_: Exception) {
        value.take(19)
    }
}
