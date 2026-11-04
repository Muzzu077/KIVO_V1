pragma Singleton

import QtQuick

/*
    KIVO Dynamic Notch — visual constants.

    The palette is deliberately quiet: smoked charcoal, forest green and a
    restrained emerald highlight.  There is no neon blue, no large glow and no
    borrowed Dynamic Island geometry.
*/
QtObject {
    // Surfaces
    readonly property color surfaceTop: "#1A201B"
    readonly property color surfaceBottom: "#101411"
    readonly property color surfaceRaised: "#1E251F"
    readonly property color surfaceSunken: "#0C100D"
    readonly property color surfaceHover: "#202922"

    // Lines
    readonly property color border: "#2C372F"
    readonly property color borderStrong: "#3D4C41"
    readonly property color divider: "#243028"

    // Type
    readonly property color textPrimary: "#E8EFE9"
    readonly property color textSecondary: "#9BA89F"
    readonly property color textMuted: "#6F7C73"
    readonly property color textFaint: "#526057"

    // Accents
    readonly property color accent: "#6FBF8E"
    readonly property color accentStrong: "#8FE0B0"
    readonly property color accentSoft: "#4E8B68"
    readonly property color accentDim: "#31543F"
    readonly property color mint: "#9AD9B4"
    readonly property color warning: "#D6B36A"
    readonly property color danger: "#D98A7A"
    readonly property color dangerDeep: "#8C4C42"
    readonly property color neutral: "#A9B3AC"
    readonly property color offline: "#6F7A73"

    // Typography
    readonly property string fontFamily: "Noto Sans"
    readonly property string monoFamily: "JetBrainsMono Nerd Font"

    readonly property int fontMicro: 9
    readonly property int fontCaption: 10
    readonly property int fontSmall: 11
    readonly property int fontBody: 13
    readonly property int fontTitle: 17
    readonly property int fontDisplay: 21

    // Geometry
    readonly property int collapsedHeight: 40
    readonly property int expandedWidth: 520
    readonly property int radiusCollapsed: 20
    readonly property int radiusExpanded: 26
    readonly property int marginTop: 8
    readonly property int shadowOffset: 4

    // Motion
    readonly property int durationInstant: 80
    readonly property int durationFast: 140
    readonly property int durationNormal: 260
    readonly property int durationSlow: 420
    readonly property int easeOut: Easing.OutCubic
    readonly property int easeInOut: Easing.InOutCubic
}
