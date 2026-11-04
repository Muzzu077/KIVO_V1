import QtQuick
import "../styles"

/*
    KIVO Dashboard — window content.

    A floating desktop panel with a quiet left navigation rail and a page host.
    All pages read from the shared KivoIpc scope so the dashboard never holds a
    second backend connection or invented state.
*/
Item {
    id: dash

    width: 880
    height: 620

    property string currentPage: "overview"
    property var ipc: null

    signal pageSelected(string page)

    function selectPage(page) {
        dash.currentPage = String(page)
    }

    readonly property bool online: dash.ipc !== null && dash.ipc.connected === true

    Rectangle {
        anchors.fill: parent
        radius: 28
        color: Theme.surfaceBottom
        border.width: 1
        border.color: Theme.border
    }

    // ── Header ─────────────────────────────────────────────────────────────
    Item {
        id: dashHeader
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 56
        Text {
            text: "KIVO"
            font.pixelSize: 20
            font.bold: true
            color: Theme.textPrimary
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    // ── Body: navigation rail + page host ───────────────────────────────────
    Rectangle {
        id: dashBody
        anchors.top: dashHeader.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: parent.height - 56
        radius: 20
        color: Theme.surfaceRaised

        Rectangle {
            id: dashNav
            x: 0
            y: 0
            width: 200
            height: dashBody.height
            radius: 20
            color: Theme.surfaceSunken

            Column {
                anchors.fill: parent
                spacing: 4

                Item {
                    id: navOverview
                    width: 200
                    height: 44
                    property string page: "overview"
                    Rectangle {
                        anchors.fill: parent
                        radius: 12
                        color: dash.currentPage === navOverview.page ? Theme.accentDim : (navOverviewMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken)
                        border.width: 1
                        border.color: dash.currentPage === navOverview.page ? Theme.accentDim : Theme.divider
                    }
                    MouseArea {
                        id: navOverviewMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: dash.selectPage(navOverview.page)
                    }
                    Text {
                        x: 14
                        y: 12
                        text: "Overview"
                        font.pixelSize: Theme.fontBody
                        font.bold: dash.currentPage === navOverview.page
                        color: dash.currentPage === navOverview.page ? Theme.textPrimary : Theme.textSecondary
                    }
                }
                Item { width: 200; height: 4 }
                Item {
                    id: navAudio
                    width: 200
                    height: 44
                    property string page: "audio"
                    Rectangle {
                        anchors.fill: parent
                        radius: 12
                        color: dash.currentPage === navAudio.page ? Theme.accentDim : (navAudioMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken)
                        border.width: 1
                        border.color: dash.currentPage === navAudio.page ? Theme.accentDim : Theme.divider
                    }
                    MouseArea {
                        id: navAudioMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: dash.selectPage(navAudio.page)
                    }
                    Text {
                        x: 14
                        y: 12
                        text: "Audio"
                        font.pixelSize: Theme.fontBody
                        font.bold: dash.currentPage === navAudio.page
                        color: dash.currentPage === navAudio.page ? Theme.textPrimary : Theme.textSecondary
                    }
                }
                Item { width: 200; height: 4 }
                Item {
                    id: navActivity
                    width: 200
                    height: 44
                    property string page: "activity"
                    Rectangle {
                        anchors.fill: parent
                        radius: 12
                        color: dash.currentPage === navActivity.page ? Theme.accentDim : (navActivityMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken)
                        border.width: 1
                        border.color: dash.currentPage === navActivity.page ? Theme.accentDim : Theme.divider
                    }
                    MouseArea {
                        id: navActivityMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: dash.selectPage(navActivity.page)
                    }
                    Text {
                        x: 14
                        y: 12
                        text: "Activity"
                        font.pixelSize: Theme.fontBody
                        font.bold: dash.currentPage === navActivity.page
                        color: dash.currentPage === navActivity.page ? Theme.textPrimary : Theme.textSecondary
                    }
                }
                Item { width: 200; height: 4 }
                Item {
                    id: navAgents
                    width: 200
                    height: 44
                    property string page: "agents"
                    Rectangle {
                        anchors.fill: parent
                        radius: 12
                        color: dash.currentPage === navAgents.page ? Theme.accentDim : (navAgentsMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken)
                        border.width: 1
                        border.color: dash.currentPage === navAgents.page ? Theme.accentDim : Theme.divider
                    }
                    MouseArea {
                        id: navAgentsMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: dash.selectPage(navAgents.page)
                    }
                    Text {
                        x: 14
                        y: 12
                        text: "Agent Workspace"
                        font.pixelSize: Theme.fontBody
                        font.bold: dash.currentPage === navAgents.page
                        color: dash.currentPage === navAgents.page ? Theme.textPrimary : Theme.textSecondary
                    }
                }
                Item { width: 200; height: 4 }
                Item {
                    id: navDesktop
                    width: 200
                    height: 44
                    property string page: "desktop"
                    Rectangle {
                        anchors.fill: parent
                        radius: 12
                        color: dash.currentPage === navDesktop.page ? Theme.accentDim : (navDesktopMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken)
                        border.width: 1
                        border.color: dash.currentPage === navDesktop.page ? Theme.accentDim : Theme.divider
                    }
                    MouseArea {
                        id: navDesktopMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: dash.selectPage(navDesktop.page)
                    }
                    Text {
                        x: 14
                        y: 12
                        text: "Desktop Context"
                        font.pixelSize: Theme.fontBody
                        font.bold: dash.currentPage === navDesktop.page
                        color: dash.currentPage === navDesktop.page ? Theme.textPrimary : Theme.textSecondary
                    }
                }
                Item { width: 200; height: 4 }
                Item {
                    id: navSettings
                    width: 200
                    height: 44
                    property string page: "settings"
                    Rectangle {
                        anchors.fill: parent
                        radius: 12
                        color: dash.currentPage === navSettings.page ? Theme.accentDim : (navSettingsMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken)
                        border.width: 1
                        border.color: dash.currentPage === navSettings.page ? Theme.accentDim : Theme.divider
                    }
                    MouseArea {
                        id: navSettingsMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: dash.selectPage(navSettings.page)
                    }
                    Text {
                        x: 14
                        y: 12
                        text: "Settings"
                        font.pixelSize: Theme.fontBody
                        font.bold: dash.currentPage === navSettings.page
                        color: dash.currentPage === navSettings.page ? Theme.textPrimary : Theme.textSecondary
                    }
                }
            }
        }

        // Page host — pages fill the host and are shown one at a time.
        Item {
            id: pageHost
            x: 200
            y: 0
            width: 680
            height: 540

            DashboardOverview {
                id: overviewPage
                anchors.fill: parent
                ipc: dash.ipc
                visible: dash.currentPage === "overview"
            }
            DashboardAudio {
                id: audioPage
                anchors.fill: parent
                ipc: dash.ipc
                visible: dash.currentPage === "audio"
            }
            DashboardActivity {
                id: activityPage
                anchors.fill: parent
                ipc: dash.ipc
                history: dash.ipc !== null ? dash.ipc.taskHistory : []
                visible: dash.currentPage === "activity"
            }
            DashboardAgents {
                id: agentsPage
                anchors.fill: parent
                ipc: dash.ipc
                visible: dash.currentPage === "agents"
            }
            DashboardDesktopContext {
                id: desktopPage
                anchors.fill: parent
                ipc: dash.ipc
                visible: dash.currentPage === "desktop"
            }
            DashboardSettings {
                id: settingsPage
                anchors.fill: parent
                ipc: dash.ipc
                visible: dash.currentPage === "settings"
            }
        }
    }
}
