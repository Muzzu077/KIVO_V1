import QtQuick
import QtQuick.Layouts
import "../styles"

/*
    KIVO Dashboard — Audio page.

    Manages microphone input devices, displays live capture status,
    lists available devices, allows device selection, and exposes device refresh.
    All data is queried live from the KIVO backend via KivoIpc.
*/
Item {
    id: page

    property var ipc: null

    readonly property bool online: ipc !== null && ipc.connected === true
    property var devices: []
    property var audioStatus: null
    property string selectedDeviceName: ipc ? ipc.audioDeviceName : ""
    property string statusMessage: ""
    property string errorMessage: ""
    property bool refreshing: false

    function refreshAudio() {
        if (!online)
            return
        refreshing = true
        errorMessage = ""
        ipc.audioGetStatus(function(error, result) {
            refreshing = false
            if (error) {
                errorMessage = error.error.message || "Failed to query audio status"
                return
            }
            audioStatus = result
            if (result && result.device)
                selectedDeviceName = result.device.name || ""
        })
        ipc.audioListDevices(function(error, result) {
            if (!error && result && Array.isArray(result.devices)) {
                devices = result.devices
            }
        })
    }

    Component.onCompleted: {
        refreshAudio()
    }

    Connections {
        target: page.ipc

        function onStatusUpdated() {
            if (page.visible && !page.audioStatus)
                page.refreshAudio()
        }

        function onAudioCaptureStarted(deviceName) {
            page.statusMessage = "Recording active on " + (deviceName || "microphone")
        }

        function onAudioCaptureStopped() {
            page.statusMessage = "Recording stopped"
            page.refreshAudio()
        }

        function onAudioCaptureCompleted(meta) {
            page.statusMessage = "Captured " + (meta.duration ? meta.duration.toFixed(1) : "0") + "s audio"
            page.refreshAudio()
        }

        function onAudioCaptureError(err) {
            page.errorMessage = err || "Audio capture error"
        }

        function onAudioDevicesUpdated(devList) {
            page.devices = devList
        }
    }

    onVisibleChanged: {
        if (visible)
            refreshAudio()
    }

    Column {
        anchors.fill: parent
        anchors.margins: 20
        spacing: 12

        // ── Header / Refresh ────────────────────────────────────────────────
        RowLayout {
            width: parent.width
            height: 36

            Text {
                text: "Audio & Microphone"
                font.pixelSize: 16
                font.bold: true
                color: Theme.textPrimary
            }

            Item { Layout.fillWidth: true }

            Rectangle {
                Layout.preferredWidth: refreshText.implicitWidth + 20
                Layout.preferredHeight: 28
                radius: 8
                color: refreshMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken
                border.width: 1
                border.color: Theme.border

                Text {
                    id: refreshText
                    anchors.centerIn: parent
                    text: page.refreshing ? "Refreshing…" : "Refresh Devices"
                    font.pixelSize: Theme.fontCaption
                    color: Theme.textPrimary
                }

                MouseArea {
                    id: refreshMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: page.online && !page.refreshing
                    onClicked: page.refreshAudio()
                }
            }
        }

        // ── Status banner if error ──────────────────────────────────────────
        Rectangle {
            width: parent.width
            height: errorText.implicitHeight + 14
            visible: page.errorMessage.length > 0
            radius: 8
            color: Qt.rgba(Theme.danger.r, Theme.danger.g, Theme.danger.b, 0.15)
            border.width: 1
            border.color: Theme.danger

            Text {
                id: errorText
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                text: page.errorMessage
                color: Theme.danger
                font.pixelSize: Theme.fontCaption
                wrapMode: Text.WordWrap
            }
        }

        // ── Status Cards ────────────────────────────────────────────────────
        RowLayout {
            width: parent.width
            height: 72
            spacing: 12

            // Card 1: Capture Status
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: parent.height
                radius: 12
                color: Theme.surfaceSunken
                border.width: 1
                border.color: Theme.divider

                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4

                    Text {
                        text: "CAPTURE STATUS"
                        font.pixelSize: Theme.fontCaption - 1
                        font.bold: true
                        color: Theme.textMuted
                        font.letterSpacing: 0.5
                    }

                    Row {
                        spacing: 6
                        Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            anchors.verticalCenter: parent.verticalCenter
                            color: {
                                if (page.ipc && page.ipc.audioCapturing)
                                    return Theme.danger
                                if (page.ipc && page.ipc.audioState === "idle")
                                    return Theme.mint
                                return Theme.textMuted
                            }
                        }
                        Text {
                            text: {
                                if (!page.online)
                                    return "Backend Offline"
                                if (page.ipc && page.ipc.audioCapturing)
                                    return "Recording Active"
                                if (page.ipc && page.ipc.audioState === "idle")
                                    return "Ready"
                                return "Unavailable"
                            }
                            font.pixelSize: Theme.fontBody
                            font.bold: true
                            color: Theme.textPrimary
                        }
                    }

                    Text {
                        text: page.statusMessage.length > 0
                            ? page.statusMessage
                            : (page.online ? "Push-to-Talk available" : "Connect backend to enable audio")
                        font.pixelSize: Theme.fontCaption
                        color: Theme.textSecondary
                        elide: Text.ElideRight
                        width: parent.width
                    }
                }
            }

            // Card 2: Selected Microphone
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: parent.height
                radius: 12
                color: Theme.surfaceSunken
                border.width: 1
                border.color: Theme.divider

                Column {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4

                    Text {
                        text: "SELECTED MICROPHONE"
                        font.pixelSize: Theme.fontCaption - 1
                        font.bold: true
                        color: Theme.textMuted
                        font.letterSpacing: 0.5
                    }

                    Text {
                        text: page.selectedDeviceName.length > 0 ? page.selectedDeviceName : "System Default"
                        font.pixelSize: Theme.fontBody
                        font.bold: true
                        color: Theme.accent
                        elide: Text.ElideRight
                        width: parent.width
                    }

                    Text {
                        text: page.devices.length + " input devices discovered"
                        font.pixelSize: Theme.fontCaption
                        color: Theme.textSecondary
                    }
                }
            }
        }

        // ── Available Devices List ──────────────────────────────────────────
        Text {
            text: "AVAILABLE MICROPHONES"
            font.pixelSize: Theme.fontCaption - 1
            font.bold: true
            color: Theme.textMuted
            font.letterSpacing: 0.5
        }

        Rectangle {
            width: parent.width
            height: 280
            radius: 12
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider
            clip: true

            ListView {
                id: deviceList
                anchors.fill: parent
                anchors.margins: 6
                spacing: 4
                model: page.devices

                delegate: Rectangle {
                    id: deviceDelegate
                    width: deviceList.width
                    height: 44
                    radius: 8
                    property bool isSelected: modelData.name === page.selectedDeviceName
                        || (!page.selectedDeviceName && modelData.is_default)
                    color: isSelected
                        ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.12)
                        : (itemMouse.containsMouse ? Theme.surfaceHover : "transparent")
                    border.width: isSelected ? 1 : 0
                    border.color: Theme.accent

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 12
                        anchors.rightMargin: 12
                        spacing: 10

                        Text {
                            text: isSelected ? "●" : "○"
                            font.pixelSize: 12
                            color: isSelected ? Theme.accent : Theme.textMuted
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1

                            Text {
                                text: modelData.name || "Unknown device"
                                font.pixelSize: Theme.fontBody
                                font.bold: isSelected
                                color: isSelected ? Theme.textPrimary : Theme.textSecondary
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }

                            Text {
                                text: (modelData.hostapi || "ALSA")
                                    + " · " + (modelData.max_input_channels || 1) + " ch"
                                    + " · " + Math.round(modelData.default_samplerate || 16000) + " Hz"
                                    + (modelData.is_default ? " · default" : "")
                                font.pixelSize: Theme.fontCaption - 1
                                color: Theme.textMuted
                            }
                        }

                        Rectangle {
                            Layout.preferredWidth: selectBtnText.implicitWidth + 14
                            Layout.preferredHeight: 24
                            radius: 6
                            visible: !isSelected
                            color: selectMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceRaised
                            border.width: 1
                            border.color: Theme.border

                            Text {
                                id: selectBtnText
                                anchors.centerIn: parent
                                text: "Select"
                                font.pixelSize: Theme.fontCaption - 1
                                color: Theme.textPrimary
                            }

                            MouseArea {
                                id: selectMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                onClicked: {
                                    if (page.ipc) {
                                        page.ipc.audioSelectDevice(modelData.name, function(err, res) {
                                            if (err)
                                                page.errorMessage = err.error.message || "Select failed"
                                            else
                                                page.refreshAudio()
                                        })
                                    }
                                }
                            }
                        }
                    }

                    MouseArea {
                        id: itemMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: {
                            if (!isSelected && page.ipc) {
                                page.ipc.audioSelectDevice(modelData.name, function(err, res) {
                                    if (err)
                                        page.errorMessage = err.error.message || "Select failed"
                                    else
                                        page.refreshAudio()
                                })
                            }
                        }
                    }
                }

                Text {
                    anchors.centerIn: parent
                    visible: page.devices.length === 0
                    text: page.online ? "No microphones detected" : "Backend offline — cannot list devices"
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontBody
                }
            }
        }
    }
}
