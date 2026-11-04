import QtQuick

/*
    The only cancellation control in the notch.

    It is intentionally dumb: it emits clicked(), and the parent calls the
    existing cancel_active_task IPC operation.  The button disables itself
    while a request is in flight and never announces success on its own.
*/
Rectangle {
    id: root

    property bool available: true
    property bool busy: false
    property bool compact: false
    property bool reducedMotion: false
    property string label: "STOP"

    signal clicked()

    implicitWidth: compact ? 54 : 96
    implicitHeight: compact ? 24 : 32
    width: implicitWidth
    height: implicitHeight
    radius: height / 2

    color: {
        if (!available || busy)
            return "#20251F"
        return mouse.containsMouse ? "#4A2C27" : "#38231F"
    }
    border.width: 1
    border.color: {
        if (!available || busy)
            return "#3A4039"
        return mouse.containsMouse ? "#D98A7A" : "#6E4038"
    }

    Behavior on color {
        ColorAnimation { duration: root.reducedMotion ? 0 : 140 }
    }
    Behavior on border.color {
        ColorAnimation { duration: root.reducedMotion ? 0 : 140 }
    }

    Row {
        anchors.centerIn: parent
        spacing: root.compact ? 5 : 7

        Rectangle {
            width: root.compact ? 5 : 6
            height: width
            radius: 1
            color: root.available && !root.busy ? "#E8A093" : "#6F7C73"
            anchors.verticalCenter: parent.verticalCenter
        }

        Text {
            text: root.busy && !root.compact ? "STOPPING" : root.label
            color: root.available && !root.busy ? "#F0C4B9" : "#77827A"
            font.family: "Noto Sans"
            font.pixelSize: root.compact ? 8 : 9
            font.weight: Font.DemiBold
            font.letterSpacing: 1.1
            anchors.verticalCenter: parent.verticalCenter
        }

        // A small busy indicator, only in the compact form where the text is
        // replaced by the fixed STOP label.
        Row {
            visible: root.busy && root.compact
            spacing: 2
            anchors.verticalCenter: parent.verticalCenter

            Repeater {
                model: 3
                Rectangle {
                    width: 2
                    height: 2
                    radius: 1
                    color: "#D98A7A"
                    opacity: 0.35
                    SequentialAnimation on opacity {
                        running: root.busy && !root.reducedMotion
                        loops: Animation.Infinite
                        PauseAnimation { duration: index * 110 }
                        NumberAnimation { from: 0.25; to: 1.0; duration: 260 }
                        NumberAnimation { from: 1.0; to: 0.25; duration: 260 }
                    }
                }
            }
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        enabled: root.available && !root.busy
        cursorShape: root.available ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: root.clicked()

        onPressed: root.scale = root.reducedMotion ? 1 : 0.97
        onReleased: root.scale = 1
        onCanceled: root.scale = 1
    }

    Behavior on scale {
        NumberAnimation {
            duration: root.reducedMotion ? 0 : 100
            easing.type: Easing.OutCubic
        }
    }
}
