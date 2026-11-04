import QtQuick
import "../styles"

/*
    KIVO Dashboard — Agent Workspace page.

    Shows the real agent state exposed by the backend.  The persistent agent
    registry and agent-specific controls are not part of the current IPC, so
    those controls render their unavailable state instead of being faked.
*/
Item {
    id: page

    property var ipc: null

    readonly property string agentStateText: ipc !== null ? ipc.agentState : "idle"
    readonly property string agentDetailText: ipc !== null ? ipc.agentDetail : ""
    readonly property bool online: ipc !== null && ipc.connected === true

    Column {
        anchors.fill: parent
        width: parent.width
        height: parent.height
        spacing: 10

        Item {
            width: parent.width
            height: 56
            Rectangle {
                anchors.fill: parent
                radius: 12
                color: Theme.surfaceSunken
            }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Agent state"; font.pixelSize: Theme.fontCaption; color: Theme.textMuted; font.bold: true }
                Text { text: agentStateText; font.pixelSize: Theme.fontBody; color: Theme.textPrimary }
                Text { text: agentDetailText; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary; visible: agentDetailText.length > 0 }
            }
        }

        Item {
            width: parent.width
            height: 120
            Rectangle {
                anchors.fill: parent
                radius: 12
                color: Theme.surfaceSunken
                border.width: 1
                border.color: Theme.divider
            }
            Column {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 8
                Text { text: "Agent workspace registry"; font.pixelSize: Theme.fontBody; font.bold: true; color: Theme.textPrimary }
                Text {
                    text: "The agent registry is not exposed through the current KIVO IPC yet. Agent state shown above comes from the live backend."
                    font.pixelSize: Theme.fontCaption
                    color: Theme.textSecondary
                    wrapMode: Text.WordWrap
                }
            }
        }

        Item {
            width: parent.width
            height: 90
            Rectangle {
                anchors.fill: parent
                radius: 12
                color: Theme.surfaceSunken
            }
            Column {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 8
                Text { text: "Agent actions"; font.pixelSize: Theme.fontBody; font.bold: true; color: Theme.textPrimary }
                Text { text: "Agent-scoped controls are unavailable until the backend exposes an agent registry."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary; wrapMode: Text.WordWrap }
            }
        }
    }
}
