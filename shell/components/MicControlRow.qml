import QtQuick
import QtQuick.Layouts
import "../styles"

/*
    MicControlRow — Default Microphone mute control row with verification badge
    and explicit PTT caveat transparency note.
*/
Rectangle {
    id: root
    property var ipc: null
    property bool reducedMotion: false

    width: parent ? parent.width : 488
    implicitHeight: 46
    radius: 10
    color: Theme.surfaceSunken
    border.width: 1
    border.color: Theme.divider

    readonly property bool isMuted: root.ipc && root.ipc.micMuted === true && root.ipc.micVerified
    readonly property bool isTransitioning: root.ipc && root.ipc.micTransitioning
    readonly property bool isAvailable: root.ipc && root.ipc.micAvailable && root.ipc.micStateString !== "unavailable"

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        anchors.topMargin: 5
        anchors.bottomMargin: 5
        spacing: 2

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            // Microphone Icon & Name
            RowLayout {
                spacing: 6
                Text {
                    text: root.isMuted ? "🔇" : "🎙️"
                    font.pixelSize: 12
                }
                ColumnLayout {
                    spacing: 0
                    Text {
                        text: {
                            if (!root.ipc || !root.ipc.micAvailable) return "Default Microphone"
                            var desc = root.ipc.micSourceDescription
                            return desc && desc.length > 0 ? desc : "Default Microphone"
                        }
                        color: Theme.textPrimary
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontCaption
                        font.weight: Font.Medium
                        elide: Text.ElideRight
                    }
                    Text {
                        text: {
                            if (root.isTransitioning) return "Applying..."
                            if (!root.ipc || !root.ipc.connected) return "Disconnected"
                            if (!root.isAvailable) return "Unavailable"
                            return root.isMuted ? "Muted (Verified)" : "Active (Unmuted)"
                        }
                        color: {
                            if (root.isTransitioning) return Theme.warning
                            if (!root.isAvailable) return Theme.textMuted
                            return root.isMuted ? Theme.danger : Theme.mint
                        }
                        font.family: Theme.fontFamily
                        font.pixelSize: 8
                        font.weight: Font.Medium
                    }
                }
            }

            Item { Layout.fillWidth: true }

            // Mute / Unmute Button
            Rectangle {
                id: actionButton
                Layout.preferredWidth: btnText.implicitWidth + 20
                Layout.preferredHeight: 24
                radius: 6
                visible: root.isAvailable
                color: {
                    if (root.isTransitioning) return Theme.surfaceSunken
                    if (btnArea.pressed) return Theme.surfaceRaised
                    if (btnArea.containsMouse) return Theme.surfaceHover
                    return root.isMuted ? Theme.mint : Theme.surfaceRaised
                }
                border.width: 1
                border.color: root.isMuted ? Theme.mint : Theme.border

                Text {
                    id: btnText
                    anchors.centerIn: parent
                    text: {
                        if (root.isTransitioning) return "..."
                        return root.isMuted ? "Unmute" : "Mute"
                    }
                    color: root.isMuted ? "#000000" : Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontMicro
                    font.weight: Font.Medium
                }

                MouseArea {
                    id: btnArea
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: !root.isTransitioning && root.ipc && root.ipc.connected
                    onClicked: {
                        if (!root.ipc) return
                        var targetMute = !root.isMuted
                        root.ipc.micSetMute(targetMute, function(error, result) {})
                    }
                }
            }
        }

        // PTT Transparency Note
        Text {
            Layout.fillWidth: true
            text: "Default mic mute does not guarantee KIVO PTT is muted"
            color: Theme.textFaint
            font.family: Theme.fontFamily
            font.pixelSize: 8
            elide: Text.ElideRight
        }
    }
}
