import QtQuick
import "../styles"

/*
    KIVO Dashboard — Overview page.

    Every value rendered here comes from the shared KivoIpc scope: the latest
    get_status snapshot, the current task, the drive snapshot and task history.
    Nothing is hard-coded or synthesised for display.
*/
Item {
    id: page

    property var ipc: null

    readonly property bool online: ipc !== null && ipc.connected === true
    readonly property string assistantStateText: ipc !== null ? ipc.assistantState : "idle"
    readonly property string assistantDetailText: ipc !== null ? ipc.assistantDetail : ""
    readonly property string audioStateText: ipc !== null ? ipc.audioState : "unavailable"
    readonly property string agentStateText: ipc !== null ? ipc.agentState : "idle"
    readonly property bool taskActive: ipc !== null && ipc.activeTask === true
    readonly property string taskGoalText: ipc !== null ? ipc.taskGoal : ""
    readonly property string taskStatusText: ipc !== null ? ipc.taskStatus : ""
    readonly property string taskSummaryText: ipc !== null ? ipc.taskSummary : ""
    readonly property bool driveActive: ipc !== null && ipc.driveActive === true
    readonly property string driveGoalText: ipc !== null ? ipc.driveGoal : ""
    readonly property string driveActionText: ipc !== null ? ipc.driveAction : ""
    readonly property real driveElapsedSeconds: ipc !== null ? ipc.driveElapsed : 0
    readonly property int uptimeSeconds: ipc !== null ? Math.round(ipc.uptimeSeconds) : 0
    readonly property var history: ipc !== null ? ipc.taskHistory : []

    component DashboardRow: Item {
        id: row
        width: parent.width
        height: 48
        property string label: ""
        property string value: ""
        property string detail: ""

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
            y: 9
            width: row.width - 28
            spacing: 2
            Text {
                text: row.label
                font.pixelSize: Theme.fontCaption
                color: Theme.textMuted
                font.bold: true
            }
            Text {
                text: row.value
                font.pixelSize: Theme.fontBody
                color: Theme.textPrimary
                elide: Text.ElideRight
                maximumLineCount: 2
            }
            Text {
                text: row.detail
                font.pixelSize: Theme.fontCaption
                color: Theme.textSecondary
                visible: row.detail.length > 0
            }
        }
    }

    Column {
        anchors.fill: parent
        width: parent.width
        height: parent.height
        spacing: 10
        Item { height: 6 }
        DashboardRow { label: "Assistant"; value: assistantStateText; detail: assistantDetailText }
        DashboardRow { label: "Audio"; value: audioStateText; detail: audioStateText === "unavailable" ? "audio feed unavailable — backend state only" : audioStateText }
        DashboardRow { label: "Agent"; value: agentStateText; detail: agentStateText === "idle" ? "no active agent task" : agentStateText }
        DashboardRow { label: "Task"; value: taskGoalText.length > 0 ? taskGoalText : (taskActive ? "Task running" : "No task"); detail: taskStatusText + (taskSummaryText.length > 0 ? " — " + taskSummaryText : "") }
        DashboardRow { label: "Drive"; value: driveActive ? driveGoalText : "Idle"; detail: driveActive ? driveActionText + " · elapsed " + formatElapsed(driveElapsedSeconds) : "" }
        Item { height: 6 }
        Item {
            width: parent.width
            height: 44
            Rectangle {
                anchors.fill: parent
                radius: 12
                color: Theme.surfaceSunken
            }
            Row {
                anchors.fill: parent
                spacing: 10
                Text { text: "KIVO backend"; font.pixelSize: Theme.fontBody; color: Theme.textSecondary }
                Text { text: online ? "Online" : "Offline"; font.pixelSize: Theme.fontBody; font.bold: true; color: online ? Theme.accent : Theme.danger }
            }
            Text { anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom; x: parent.width - 14 - implicitWidth; y: 10; text: "uptime " + formatDuration(uptimeSeconds); font.pixelSize: Theme.fontCaption; color: Theme.textMuted }
        }
    }

    property bool dashboardLoadRequested: false

    function requestDashboardData() {
        if (dashboardLoadRequested)
            return
        dashboardLoadRequested = true
        if (ipc !== null)
            ipc.getDashboardData(function(error, result) { })
    }

    Component.onCompleted: requestDashboardData()

    onVisibleChanged: {
        if (visible)
            requestDashboardData()
        else
            dashboardLoadRequested = false
    }

    function formatElapsed(totalSeconds) {
        var value = Math.max(0, Math.round(totalSeconds))
        var minutes = Math.floor(value / 60)
        var seconds = value % 60
        return minutes + ":" + (seconds < 10 ? "0" : "") + seconds
    }

    function formatDuration(totalSeconds) {
        var value = Math.max(0, Math.round(totalSeconds))
        var hours = Math.floor(value / 3600)
        var minutes = Math.floor((value % 3600) / 60)
        if (hours > 0)
            return hours + "h " + minutes + "m"
        return minutes + "m " + (value % 60) + "s"
    }
}
