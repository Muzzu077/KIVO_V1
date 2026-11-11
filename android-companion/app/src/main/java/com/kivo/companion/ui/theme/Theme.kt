package com.kivo.companion.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// KIVO Desktop Palette Constants
val SurfaceTop = Color(0xFF1A201B)
val SurfaceBottom = Color(0xFF101411)
val SurfaceRaised = Color(0xFF1E251F)
val SurfaceSunken = Color(0xFF0C100D)
val Border = Color(0xFF2C372F)
val TextPrimary = Color(0xFFE8EFE9)
val TextSecondary = Color(0xFF9BA89F)
val TextMuted = Color(0xFF6F7C73)
val Accent = Color(0xFF6FBF8E)
val Mint = Color(0xFF9AD9B4)
val Danger = Color(0xFFD98A7A)
val Warning = Color(0xFFD6B36A)

private val DarkColorScheme = darkColorScheme(
    primary = Accent,
    secondary = Mint,
    background = SurfaceBottom,
    surface = SurfaceTop,
    surfaceVariant = SurfaceRaised,
    onPrimary = Color.Black,
    onBackground = TextPrimary,
    onSurface = TextPrimary,
    error = Danger
)

private val LightColorScheme = lightColorScheme(
    primary = Accent,
    secondary = Mint,
    background = Color(0xFFF6F9F7),
    surface = Color.White,
    surfaceVariant = Color(0xFFE8F0EA),
    onPrimary = Color.White,
    onBackground = Color(0xFF101411),
    onSurface = Color(0xFF101411),
    error = Danger
)

@Composable
fun KivoCompanionTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme

    MaterialTheme(
        colorScheme = colorScheme,
        content = content
    )
}
