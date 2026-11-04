import QtQuick

/*
    A temporary KIVO emblem: a soft leaf/diamond with a carved K.

    This is intentionally not character artwork and contains no lip-sync or
    emotion state.  It is a compact, recognisable mark for the notch.
*/
Item {
    id: root

    property int size: 24
    property color accent: "#6FBF8E"
    property bool muted: false
    property bool reducedMotion: false

    implicitWidth: size
    implicitHeight: size

    onAccentChanged: canvas.requestPaint()
    onMutedChanged: canvas.requestPaint()
    onSizeChanged: canvas.requestPaint()

    Canvas {
        id: canvas
        anchors.fill: parent

        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()

        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)

            var w = width
            var h = height
            if (w <= 0 || h <= 0)
                return

            var base = root.muted ? Qt.rgba(0.42, 0.48, 0.44, 0.85) : root.accent
            var light = Qt.lighter(base, 1.18)
            var dark = Qt.darker(base, 1.55)

            var gradient = ctx.createLinearGradient(0, 0, w, h)
            gradient.addColorStop(0, light)
            gradient.addColorStop(0.55, base)
            gradient.addColorStop(1, dark)

            ctx.fillStyle = gradient
            ctx.beginPath()
            ctx.moveTo(w * 0.50, h * 0.04)
            ctx.bezierCurveTo(w * 0.91, h * 0.15, w * 0.96, h * 0.63, w * 0.50, h * 0.96)
            ctx.bezierCurveTo(w * 0.04, h * 0.63, w * 0.09, h * 0.15, w * 0.50, h * 0.04)
            ctx.closePath()
            ctx.fill()

            // Inner rim keeps the mark crisp against the charcoal surface.
            ctx.strokeStyle = Qt.rgba(1, 1, 1, root.muted ? 0.10 : 0.20)
            ctx.lineWidth = Math.max(0.8, w * 0.035)
            ctx.stroke()

            // Carved K.
            ctx.strokeStyle = "#0D120F"
            ctx.lineWidth = Math.max(1.5, w * 0.088)
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.beginPath()
            ctx.moveTo(w * 0.385, h * 0.27)
            ctx.lineTo(w * 0.385, h * 0.73)
            ctx.moveTo(w * 0.385, h * 0.51)
            ctx.lineTo(w * 0.625, h * 0.27)
            ctx.moveTo(w * 0.385, h * 0.51)
            ctx.lineTo(w * 0.645, h * 0.74)
            ctx.stroke()

            // Small specular highlight on the upper-left edge.
            ctx.strokeStyle = Qt.rgba(1, 1, 1, root.muted ? 0.10 : 0.28)
            ctx.lineWidth = Math.max(0.7, w * 0.024)
            ctx.beginPath()
            ctx.moveTo(w * 0.31, h * 0.27)
            ctx.quadraticCurveTo(w * 0.26, h * 0.42, w * 0.30, h * 0.54)
            ctx.stroke()
        }
    }
}
