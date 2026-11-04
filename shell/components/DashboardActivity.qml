import QtQuick
import "../styles"

/*
    KIVO Dashboard — Activity page.

    Shows the authoritative current task and recent task history from the
    shared KivoIpc scope.  Only UI-safe task snapshots are rendered; private
    tool internals and raw output text are not.
*/
Item {
    id: page

    property var ipc: null
    property var history: []

    ListModel {
        id: historyModel
    }

    function refreshHistory() {
        historyModel.clear()
        var items = page.history
        if (!Array.isArray(items))
            return
        for (var i = 0; i < items.length && i < 10; i++) {
            var entry = items[i]
            if (!entry || typeof entry !== "object")
                continue
            historyModel.append({
                task_id: String(entry.task_id || ""),
                goal: String(entry.goal || ""),
                status: String(entry.status || ""),
                success: entry.success === true,
                summary: String(entry.summary || ""),
                current_step: Number(entry.current_step) || 0,
                total_steps: Number(entry.total_steps) || 0,
                created_at: Number(entry.created_at) || 0
            })
        }
    }

    Component.onCompleted: refreshHistory()
    onHistoryChanged: refreshHistory()
    onVisibleChanged: {
        // Load the authoritative task history through the dashboard operation.
        if (visible && ipc !== null)
            ipc.getDashboardData(function(error, result) { })
    }

    readonly property bool taskActive: ipc !== null && ipc.activeTask === true
    readonly property string taskGoalText: ipc !== null ? ipc.taskGoal : ""
    readonly property string taskStatusText: ipc !== null ? ipc.taskStatus : ""
    readonly property string taskSummaryText: ipc !== null ? ipc.taskSummary : ""
    readonly property int taskCurrentStep: ipc !== null ? ipc.taskCurrentStep : 0
    readonly property int taskTotalSteps: ipc !== null ? ipc.taskTotalSteps : 0
    readonly property bool taskSuccess: ipc !== null && ipc.taskSuccess === true

    component TaskRow: Item {
        id: row
        property string goalText: ""
        property string statusText: ""
        property bool successValue: true
        property string summaryText: ""
        property int stepValue: 0
        property int totalValue: 0

        width: page.width
        height: 52
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
            Text {
                text: row.goalText
                font.pixelSize: Theme.fontBody
                color: Theme.textPrimary
                elide: Text.ElideRight
                maximumLineCount: 2
            }
            Text {
                text: row.statusText + (row.totalValue > 0 ? " · step " + row.stepValue + " of " + row.totalValue : "")
                font.pixelSize: Theme.fontCaption
                color: row.successValue ? Theme.textSecondary : Theme.danger
            }
            Text {
                text: row.summaryText
                font.pixelSize: Theme.fontCaption
                color: Theme.textSecondary
                visible: row.summaryText.length > 0
            }
        }
    }

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
                Text { text: "Current task"; font.pixelSize: Theme.fontCaption; color: Theme.textMuted; font.bold: true }
                Text { text: taskGoalText; font.pixelSize: Theme.fontBody; color: Theme.textPrimary; elide: Text.ElideRight; maximumLineCount: 2 }
                Text { text: taskStatusText; font.pixelSize: Theme.fontCaption; color: taskSuccess ? Theme.textSecondary : Theme.danger }
            }
        }

        Item { height: 6 }

        Item {
            width: parent.width
            height: 44
            Rectangle {
                anchors.fill: parent
                radius: 12
                color: Theme.surfaceSunken
            }
            Text { anchors.fill: parent; text: "Task history"; font.pixelSize: Theme.fontBody; font.bold: true; color: Theme.textPrimary; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter }
        }

        Repeater {
            model: historyModel
            delegate: TaskRow {
                goalText: model.goal
                statusText: model.status
                successValue: model.success === true
                summaryText: model.summary
                stepValue: model.current_step
                totalValue: model.total_steps
            }
        }

        Item {
            width: parent.width
            height: 40
            Rectangle {
                anchors.fill: parent
                radius: 12
                color: Theme.surfaceSunken
            }
            Text { anchors.fill: parent; text: "No task history yet."; font.pixelSize: Theme.fontBody; color: Theme.textMuted; anchors.verticalCenter: parent.verticalCenter }
            visible: historyModel.count === 0
        }
    }
}
