import QtQuick

/*
    Minimal status dot with a restrained pulse.  The dot never encodes data
    that the backend has not supplied; its colour is chosen by the parent from
    the authoritative visual state.
*/
Item {
    id: root

    property color color: "#6FBF8E"
    property bool pulse: false
    property bool reducedMotion: false
    property int size: 8

    implicitWidth: size
    implicitHeight: size

    onPulseChanged: {
        if (!pulse) {
            halo.opacity = 0
            halo.scale = 1
        }
    }
    onReducedMotionChanged: {
        if (reducedMotion) {
            halo.opacity = 0
            halo.scale = 1
        }
    }

    Rectangle {
        id: halo
        anchors.centerIn: parent
        width: root.size
        height: root.size
        radius: width / 2
        color: root.color
        opacity: 0

        SequentialAnimation on opacity {
            running: root.pulse && !root.reducedMotion
            loops: Animation.Infinite
            NumberAnimation { from: 0.26; to: 0; duration: 1500; easing.type: Easing.OutCubic }
            NumberAnimation { from: 0; to: 0.26; duration: 0 }
        }

        SequentialAnimation on scale {
            running: root.pulse && !root.reducedMotion
            loops: Animation.Infinite
            NumberAnimation { from: 1.0; to: 2.3; duration: 1500; easing.type: Easing.OutCubic }
            NumberAnimation { from: 2.3; to: 1.0; duration: 0 }
        }
    }

    Rectangle {
        anchors.centerIn: parent
        width: root.size
        height: root.size
        radius: width / 2
        color: root.color

        Behavior on color {
            ColorAnimation { duration: root.reducedMotion ? 0 : 220 }
        }
    }
}
