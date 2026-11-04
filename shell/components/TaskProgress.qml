import QtQuick

/*
    Real task progress from the backend snapshot.

    The bar is driven by step statuses when they are present, otherwise by
    current_step/total_steps.  When the backend has no step count the bar is
    indeterminate; no percentage is invented.
*/
Item {
    id: root

    property int currentStep: 0
    property int totalSteps: 0
    property var steps: []
    property string status: ""
    property string elapsedText: ""
    property bool reducedMotion: false
    property color accent: "#6FBF8E"
    property color danger: "#D98A7A"

    implicitHeight: 46

    readonly property int knownTotal: totalSteps > 0
        ? totalSteps : (Array.isArray(steps) ? steps.length : 0)

    readonly property int completedSteps: {
        if (!Array.isArray(steps) || steps.length === 0)
            return Math.max(0, Math.min(currentStep, knownTotal))
        var count = 0
        for (var i = 0; i < steps.length; i++) {
            var step = steps[i]
            if (step && step.status === "completed")
                count += 1
        }
        return count
    }

    readonly property real progress: knownTotal > 0
        ? Math.max(0, Math.min(1, completedSteps / knownTotal)) : -1

    readonly property string stepText: knownTotal > 0
        ? "Step " + Math.min(Math.max(currentStep, 1), knownTotal) + " of " + knownTotal
        : "Working"

    readonly property string percentText: progress >= 0
        ? Math.round(progress * 100) + "%" : ""

    readonly property color barColor: status === "failed" ? danger : accent

    Column {
        anchors.fill: parent
        spacing: 8

        Item {
            width: parent.width
            height: 12

            Text {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: root.stepText
                color: "#9BA89F"
                font.family: "Noto Sans"
                font.pixelSize: 10
                font.letterSpacing: 0.3
            }

            Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: root.elapsedText.length > 0
                    ? root.elapsedText + (root.percentText.length > 0 ? "   " + root.percentText : "")
                    : root.percentText
                color: "#6F7C73"
                font.family: "JetBrainsMono Nerd Font"
                font.pixelSize: 10
            }
        }

        Rectangle {
            id: track
            width: parent.width
            height: 5
            radius: 2.5
            color: "#243028"
            clip: true

            Rectangle {
                id: staticFill
                x: 0
                height: parent.height
                radius: parent.radius
                color: root.barColor
                visible: root.progress >= 0
                width: root.progress >= 0
                    ? Math.max(root.progress > 0 ? 5 : 0, track.width * root.progress)
                    : 0

                Behavior on width {
                    NumberAnimation {
                        duration: root.reducedMotion ? 0 : 320
                        easing.type: Easing.OutCubic
                    }
                }
            }

            Rectangle {
                id: indeterminateFill
                y: 0
                height: parent.height
                radius: parent.radius
                color: root.barColor
                visible: root.progress < 0
                width: Math.max(18, track.width * 0.28)

                SequentialAnimation on x {
                    running: root.progress < 0 && !root.reducedMotion
                    loops: Animation.Infinite
                    NumberAnimation {
                        from: 0
                        to: Math.max(0, track.width - indeterminateFill.width)
                        duration: 1250
                        easing.type: Easing.InOutSine
                    }
                    NumberAnimation {
                        from: Math.max(0, track.width - indeterminateFill.width)
                        to: 0
                        duration: 1250
                        easing.type: Easing.InOutSine
                    }
                }
            }
        }
    }
}
