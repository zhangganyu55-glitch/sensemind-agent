package com.sensemind.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

val Teal = Color(0xFF5EEAD4)
val Cyan = Color(0xFF38BDF8)
val Violet = Color(0xFFA78BFA)
val Amber = Color(0xFFFBBF24)
val Coral = Color(0xFFF87171)
val Ink = Color(0xFFE6F1F5)
val Muted = Color(0xFF8FA3B0)
val Faint = Color(0xFF5D7280)
val DeepBg = Color(0xFF070C12)
val CardBg = Color(0xFF0D1620)
val CardBg2 = Color(0xFF111C27)
val Line = Color(0xFF1E303D)

private val scheme = darkColorScheme(
    primary = Teal,
    onPrimary = Color(0xFF04201D),
    secondary = Cyan,
    onSecondary = Color(0xFF03141C),
    tertiary = Violet,
    background = DeepBg,
    onBackground = Ink,
    surface = CardBg,
    onSurface = Ink,
    surfaceVariant = CardBg2,
    onSurfaceVariant = Muted,
    error = Coral,
    onError = Color(0xFF26080A),
    outline = Line,
)

private val typography = Typography(
    headlineLarge = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.SemiBold, fontSize = 28.sp, color = Ink),
    headlineSmall = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.SemiBold, fontSize = 20.sp, color = Ink),
    titleMedium = TextStyle(fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.Medium, fontSize = 16.sp, color = Ink),
    bodyMedium = TextStyle(fontFamily = FontFamily.SansSerif, fontSize = 14.sp, color = Ink),
    bodySmall = TextStyle(fontFamily = FontFamily.SansSerif, fontSize = 12.sp, color = Muted),
    labelSmall = TextStyle(fontFamily = FontFamily.Monospace, fontSize = 10.sp, color = Muted, letterSpacing = 1.sp),
)

@Composable
fun SenseMindTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = scheme, typography = typography, content = content)
}
