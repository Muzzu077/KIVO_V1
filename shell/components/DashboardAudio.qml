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
    property var sttStatus: ipc ? ipc.sttStatus : null
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
        if (ipc.sttGetStatus) {
            ipc.sttGetStatus(function(err, res) {
                if (!err && res)
                    sttStatus = res
            })
        }
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

        function onSttTranscriptionStarted(capId) {
            page.statusMessage = "Transcribing speech…"
        }

        function onSttTranscriptionCompleted(result) {
            page.statusMessage = "Transcription ready"
            if (page.ipc && page.ipc.sttGetHistory)
                page.ipc.sttGetHistory(5, null)
        }

        function onSttTranscriptionFailed(err) {
            page.errorMessage = err || "Speech recognition failed"
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

            // Card 2: STT Engine & Language
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
                        text: "SPEECH RECOGNITION (STT)"
                        font.pixelSize: Theme.fontCaption - 1
                        font.bold: true
                        color: Theme.textMuted
                        font.letterSpacing: 0.5
                    }

                    RowLayout {
                        width: parent.width
                        spacing: 6

                        Text {
                            text: (page.sttStatus ? page.sttStatus.engine : "faster-whisper")
                                + " (" + (page.sttStatus ? page.sttStatus.model : "base") + ")"
                            font.pixelSize: Theme.fontBody
                            font.bold: true
                            color: Theme.accent
                        }

                        Item { Layout.fillWidth: true }

                        // Language badge / toggle
                        Rectangle {
                            Layout.preferredWidth: langText.implicitWidth + 12
                            Layout.preferredHeight: 20
                            radius: 6
                            color: Theme.surfaceRaised
                            border.width: 1
                            border.color: Theme.border

                            Text {
                                id: langText
                                anchors.centerIn: parent
                                text: "Lang: " + (page.sttStatus ? page.sttStatus.language : "auto")
                                font.pixelSize: Theme.fontCaption - 1
                                color: Theme.textSecondary
                            }

                            MouseArea {
                                anchors.fill: parent
                                onClicked: {
                                    var current = page.sttStatus ? page.sttStatus.language : "auto"
                                    var next = "auto"
                                    if (current === "auto") next = "en"
                                    else if (current === "en") next = "hi"
                                    else if (current === "hi") next = "te"
                                    else next = "auto"
                                    if (page.ipc && page.ipc.sttUpdateConfig) {
                                        page.ipc.sttUpdateConfig({ language: next }, function(err, res) {
                                            if (!err && res && res.status)
                                                page.sttStatus = res.status
                                        })
                                    }
                                }
                            }
                        }
                    }

                    Text {
                        text: "CTranslate2 int8 on CPU · local & private"
                        font.pixelSize: Theme.fontCaption - 1
                        color: Theme.textSecondary
                    }
                }
            }
        }

        // ── Latest Recognized Speech ─────────────────────────────────────────
        Rectangle {
            width: parent.width
            height: 64
            radius: 12
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            Column {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 2

                RowLayout {
                    width: parent.width
                    Text {
                        text: "LATEST RECOGNIZED SPEECH"
                        font.pixelSize: Theme.fontCaption - 1
                        font.bold: true
                        color: Theme.textMuted
                        font.letterSpacing: 0.5
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        visible: page.ipc && page.ipc.lastTranscript && page.ipc.lastTranscript.length > 0
                        text: "✓ recognized"
                        font.pixelSize: Theme.fontCaption - 1
                        color: Theme.mint
                    }
                }

                Text {
                    width: parent.width
                    text: (page.ipc && page.ipc.lastTranscript && page.ipc.lastTranscript.length > 0)
                        ? "\"" + page.ipc.lastTranscript + "\""
                        : "No recent speech. Hold Push-to-Talk to speak to KIVO."
                    font.pixelSize: Theme.fontBody
                    font.italic: !(page.ipc && page.ipc.lastTranscript && page.ipc.lastTranscript.length > 0)
                    color: (page.ipc && page.ipc.lastTranscript && page.ipc.lastTranscript.length > 0)
                        ? Theme.textPrimary : Theme.textMuted
                    elide: Text.ElideRight
                    maximumLineCount: 2
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── Default Microphone (WirePlumber) Control (Stage 12.2) ────────────
        Rectangle {
            width: parent.width
            height: 58
            radius: 12
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            RowLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 12

                // Icon / State Badge
                Rectangle {
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 34
                    radius: 8
                    color: {
                        if (!page.online || !page.ipc || !page.ipc.micAvailable) return Theme.surfaceRaised
                        if (page.ipc.micTransitioning) return Theme.surfaceHover
                        return (page.ipc.micMuted === true && page.ipc.micVerified) ? Qt.rgba(Theme.danger.r, Theme.danger.g, Theme.danger.b, 0.15) : Qt.rgba(Theme.mint.r, Theme.mint.g, Theme.mint.b, 0.15)
                    }
                    border.width: 1
                    border.color: {
                        if (!page.online || !page.ipc || !page.ipc.micAvailable) return Theme.border
                        return (page.ipc.micMuted === true && page.ipc.micVerified) ? Theme.danger : Theme.mint
                    }

                    Text {
                        anchors.centerIn: parent
                        text: (page.ipc && page.ipc.micMuted === true && page.ipc.micVerified) ? "🔇" : "🎙️"
                        font.pixelSize: 14
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1

                    RowLayout {
                        spacing: 6
                        Text {
                            text: "DEFAULT MICROPHONE"
                            font.pixelSize: Theme.fontCaption - 1
                            font.bold: true
                            color: Theme.textMuted
                            font.letterSpacing: 0.5
                        }
                        Text {
                            text: {
                                if (page.ipc && page.ipc.micTransitioning) return "· applying..."
                                if (!page.online || !page.ipc || !page.ipc.micAvailable) return "· offline"
                                if (page.ipc.micMuted === true && page.ipc.micVerified) return "· Muted (Verified)"
                                if (page.ipc.micMuted === false && page.ipc.micVerified) return "· Active (Unmuted)"
                                return "· " + (page.ipc.micStateString || "unknown")
                            }
                            font.pixelSize: Theme.fontCaption - 1
                            font.bold: true
                            color: {
                                if (page.ipc && page.ipc.micTransitioning) return Theme.warning
                                if (!page.online || !page.ipc || !page.ipc.micAvailable) return Theme.textMuted
                                return (page.ipc.micMuted === true && page.ipc.micVerified) ? Theme.danger : Theme.mint
                            }
                        }
                    }

                    Text {
                        text: {
                            if (!page.ipc || !page.ipc.micAvailable) return "WirePlumber source unavailable"
                            var name = page.ipc.micSourceDescription || page.ipc.micSourceName
                            return name + (page.ipc.micSourceId ? (" (Node " + page.ipc.micSourceId + ")") : "")
                        }
                        font.pixelSize: Theme.fontBody
                        color: Theme.textPrimary
                        font.weight: Font.Medium
                        elide: Text.ElideRight
                    }
                }

                // Action Button (Mute / Unmute / Retry)
                Rectangle {
                    Layout.preferredWidth: defaultMicBtnText.implicitWidth + 24
                    Layout.preferredHeight: 32
                    radius: 8
                    color: {
                        if (defaultMicBtnArea.pressed) return Theme.surfaceRaised
                        if (defaultMicBtnArea.containsMouse) return Theme.surfaceHover
                        if (page.ipc && page.ipc.micMuted === true && page.ipc.micVerified) return Theme.mint
                        return Theme.surfaceRaised
                    }
                    border.width: 1
                    border.color: (page.ipc && page.ipc.micMuted === true && page.ipc.micVerified) ? Theme.mint : Theme.border

                    Text {
                        id: defaultMicBtnText
                        anchors.centerIn: parent
                        text: {
                            if (page.ipc && page.ipc.micTransitioning) return "..."
                            if (!page.ipc || !page.ipc.micAvailable || page.ipc.micStateString === "error") return "Retry"
                            return (page.ipc.micMuted === true && page.ipc.micVerified) ? "Unmute" : "Mute"
                        }
                        color: (page.ipc && page.ipc.micMuted === true && page.ipc.micVerified) ? "#000000" : Theme.textPrimary
                        font.pixelSize: Theme.fontCaption
                        font.weight: Font.Medium
                    }

                    MouseArea {
                        id: defaultMicBtnArea
                        anchors.fill: parent
                        hoverEnabled: true
                        enabled: page.online && page.ipc && !page.ipc.micTransitioning
                        onClicked: {
                            if (!page.ipc) return
                            if (!page.ipc.micAvailable || page.ipc.micStateString === "error") {
                                page.ipc.micGetState(function(err, res) {})
                                return
                            }
                            var targetMute = !(page.ipc.micMuted === true && page.ipc.micVerified)
                            page.ipc.micSetMute(targetMute, function(err, res) {})
                        }
                    }
                }
            }
        }

        // PTT Disclaimer note if devices differ or for transparency
        Text {
            width: parent.width
            text: {
                var defName = (page.ipc && page.ipc.micSourceName) ? page.ipc.micSourceName : ""
                var selName = page.selectedDeviceName || ""
                if (defName.length > 0 && selName.length > 0 && !selName.toLowerCase().includes(defName.toLowerCase()) && !defName.toLowerCase().includes(selName.toLowerCase())) {
                    return "Note: KIVO PTT input (" + selName + ") differs from system default (" + (page.ipc.micSourceDescription || defName) + "). Muting default microphone does not mute PTT capture."
                }
                return "Note: Default microphone mute controls the WirePlumber default source. It does not guarantee KIVO PTT or other applications are muted."
            }
            font.pixelSize: Theme.fontCaption - 1
            color: Theme.textMuted
            wrapMode: Text.WordWrap
            maximumLineCount: 2
            elide: Text.ElideRight
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
            height: 180
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
