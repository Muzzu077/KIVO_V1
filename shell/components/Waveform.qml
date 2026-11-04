import QtQuick

/*
    Activity waveform.

    The current backend exposes audio *state* but no amplitude stream, so this
    component animates a restrained activity pattern rather than pretending to
    be a real audio level.  When a live amplitude source is added later,
    `intensity` can be driven from it without changing the layout.
*/
Item {
    id: root

    property bool active: false
    property bool live: false
    property bool reducedMotion: false
    property color color: "#6FBF8E"
    property int barCount: 5
    property real intensity: live ? 0.85 : 0.48
    property real minBarHeight: 3
    property real maxBarHeight: 18

    implicitWidth: barCount * 4
    implicitHeight: maxBarHeight

    property real phase: 0
    property bool _animate: active && !reducedMotion

    Timer {
        interval: 90
        repeat: true
        running: root._animate
        onTriggered: root.phase += 0.34
    }

    Row {
        anchors.centerIn: parent
        spacing: 2

        Repeater {
            model: root.barCount

            Rectangle {
                id: bar
                width: 2
                radius: 1
                color: root.color
                opacity: root.active ? 1.0 : 0.42
                anchors.verticalCenter: parent.verticalCenter

                readonly property real wave: {
                    if (!root.active)
                        return 0.24 + 0.18 * Math.abs(Math.sin(index * 1.7))
                    if (root.reducedMotion)
                        return 0.32 + 0.22 * Math.abs(Math.sin(index * 1.1))
                    return Math.abs(Math.sin(root.phase + index * 0.82))
                }

                height: root.minBarHeight
                    + (root.maxBarHeight - root.minBarHeight) * wave * root.intensity

                Behavior on height {
                    NumberAnimation {
                        duration: root.reducedMotion ? 0 : 90
                        easing.type: Easing.InOutSine
                    }
                }
                Behavior on opacity {
                    NumberAnimation { duration: root.reducedMotion ? 0 : 180 }
                }
            }
        }
    }
}
