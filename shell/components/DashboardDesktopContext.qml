import QtQuick
import "../styles"

/*
    KIVO Dashboard — Desktop Context page.

    Renders the UI-safe desktop context published by the backend.  Raw
    screenshots, OCR text and unapproved context sources are never displayed.
*/
Item {
    id: page

    property var ipc: null

    readonly property bool online: ipc !== null && ipc.connected === true
    readonly property var context: ipc !== null ? ipc.screenContext : null
    readonly property bool contextCleared: context !== null ? context.cleared === true : true
    readonly property string contextApp: context !== null ? context.active_app : ""
    readonly property string contextTitle: context !== null ? context.active_title : ""
    readonly property bool contextTerminal: context !== null ? context.is_terminal === true : false
    readonly property bool contextBrowser: context !== null ? context.is_browser === true : false
    readonly property string contextSource: context !== null ? context.source : ""
    readonly property real contextAgeSeconds: context !== null ? context.age_seconds : 0

    Column {
        anchors.fill: parent
        width: parent.width
        height: parent.height
        spacing: 10

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Active context"; font.pixelSize: Theme.fontCaption; color: Theme.textMuted; font.bold: true }
                Text { text: contextCleared ? "No active context" : contextApp; font.pixelSize: Theme.fontBody; color: Theme.textPrimary; elide: Text.ElideRight; maximumLineCount: 2 }
                Text { text: contextTitle; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary; visible: !contextCleared && contextTitle.length > 0; elide: Text.ElideRight; maximumLineCount: 2 }
            }
        }

        Item {
            width: parent.width
            height: 96
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 8
                Text { text: "Context metadata"; font.pixelSize: Theme.fontBody; font.bold: true; color: Theme.textPrimary }
                Text { text: "source: " + contextSource; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
                Text { text: "age: " + contextAgeSeconds + "s"; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
                Text { text: "terminal: " + contextTerminal + " · browser: " + contextBrowser; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }

        Item {
            width: parent.width
            height: 96
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 8
                Text { text: "Session details"; font.pixelSize: Theme.fontBody; font.bold: true; color: Theme.textPrimary }
                Text { text: "Workspace, compositor and session metadata are not exposed by the current IPC payload."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary; wrapMode: Text.WordWrap }
            }
        }
    }
}
