import QtQuick
import QtQuick.Layouts
import "../styles"

/*
    KIVO Dashboard — Conversation page.

    Provides the multi-turn conversational dialogue thread between the user
    and KIVO, streaming response display, cloud consent control, model configuration,
    and text submission.
*/
Item {
    id: page

    property var ipc: null

    readonly property bool online: ipc !== null && ipc.connected === true
    property var conversationHistory: ipc && ipc.conversationHistory ? ipc.conversationHistory : []
    property var conversationStatus: ipc ? ipc.conversationStatus : null
    property bool cloudEnabled: ipc ? ipc.conversationCloudEnabled : false
    property string currentModel: conversationStatus && conversationStatus.provider ? conversationStatus.provider.model : "meta-llama/llama-3.3-70b-instruct:free"

    function refreshConversation() {
        if (!online)
            return
        if (ipc.conversationGetStatus)
            ipc.conversationGetStatus(null)
        if (ipc.conversationGetHistory)
            ipc.conversationGetHistory(null)
    }

    Component.onCompleted: {
        refreshConversation()
    }

    onVisibleChanged: {
        if (visible)
            refreshConversation()
    }

    Connections {
        target: page.ipc

        function onConversationStatusUpdated(status) {
            page.conversationStatus = status
            page.cloudEnabled = status && status.cloud_enabled === true
        }

        function onConversationUserMessage(msg) {
            msgList.positionViewAtEnd()
        }

        function onConversationResponseCompleted(msg) {
            msgList.positionViewAtEnd()
        }
    }

    Column {
        anchors.fill: parent
        anchors.margins: 20
        spacing: 10

        // ── Header & Action Bar ─────────────────────────────────────────────
        RowLayout {
            width: parent.width
            height: 32

            Text {
                text: "Conversation"
                font.pixelSize: 16
                font.bold: true
                color: Theme.textPrimary
            }

            Item { Layout.fillWidth: true }

            // Cancel button (visible when generating)
            Rectangle {
                Layout.preferredWidth: cancelBtnText.implicitWidth + 18
                Layout.preferredHeight: 26
                visible: page.ipc && (page.ipc.conversationState === "thinking" || page.ipc.conversationState === "streaming")
                radius: 6
                color: Theme.dangerDeep
                border.width: 1
                border.color: Theme.danger

                Text {
                    id: cancelBtnText
                    anchors.centerIn: parent
                    text: "Stop Generating"
                    font.pixelSize: Theme.fontCaption - 1
                    color: "#FFFFFF"
                }

                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        if (page.ipc && page.ipc.conversationCancel)
                            page.ipc.conversationCancel(null)
                    }
                }
            }

            // Clear history button
            Rectangle {
                Layout.preferredWidth: clearBtnText.implicitWidth + 18
                Layout.preferredHeight: 26
                radius: 6
                color: clearMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken
                border.width: 1
                border.color: Theme.border

                Text {
                    id: clearBtnText
                    anchors.centerIn: parent
                    text: "Clear Chat"
                    font.pixelSize: Theme.fontCaption - 1
                    color: Theme.textMuted
                }

                MouseArea {
                    id: clearMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        if (page.ipc && page.ipc.conversationClear)
                            page.ipc.conversationClear(null)
                    }
                }
            }
        }

        // ── Cloud AI Privacy & Consent Card ─────────────────────────────────
        Rectangle {
            width: parent.width
            height: consentCol.implicitHeight + 16
            radius: 12
            color: page.cloudEnabled
                ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.08)
                : Theme.surfaceSunken
            border.width: 1
            border.color: page.cloudEnabled ? Theme.accentSoft : Theme.divider

            Column {
                id: consentCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 10
                spacing: 6

                RowLayout {
                    width: parent.width

                    Row {
                        spacing: 6
                        Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            anchors.verticalCenter: parent.verticalCenter
                            color: page.cloudEnabled ? Theme.mint : Theme.warning
                        }
                        Text {
                            text: page.cloudEnabled ? "Cloud AI Enabled (OpenRouter)" : "Cloud AI Disabled (Local Only)"
                            font.pixelSize: Theme.fontCaption
                            font.bold: true
                            color: page.cloudEnabled ? Theme.accent : Theme.warning
                        }
                    }

                    Item { Layout.fillWidth: true }

                    // Model Badge
                    Text {
                        text: "Model: " + page.currentModel
                        font.pixelSize: Theme.fontCaption - 1
                        color: Theme.textMuted
                        font.family: Theme.monoFamily
                        elide: Text.ElideRight
                    }

                    // Toggle Button
                    Rectangle {
                        Layout.preferredWidth: toggleText.implicitWidth + 16
                        Layout.preferredHeight: 24
                        radius: 6
                        color: page.cloudEnabled ? Theme.surfaceRaised : Theme.accent
                        border.width: 1
                        border.color: page.cloudEnabled ? Theme.border : Theme.accentStrong

                        Text {
                            id: toggleText
                            anchors.centerIn: parent
                            text: page.cloudEnabled ? "Disable Cloud AI" : "Enable Cloud AI"
                            font.pixelSize: Theme.fontCaption - 1
                            font.bold: !page.cloudEnabled
                            color: page.cloudEnabled ? Theme.textSecondary : "#000000"
                        }

                        MouseArea {
                            anchors.fill: parent
                            onClicked: {
                                var next = !page.cloudEnabled
                                if (page.ipc && page.ipc.conversationUpdateConfig) {
                                    page.ipc.conversationUpdateConfig({ cloud_enabled: next }, function(err, res) {
                                        if (!err && res && res.status)
                                            page.cloudEnabled = res.status.cloud_enabled === true
                                    })
                                }
                            }
                        }
                    }
                }

                Text {
                    width: parent.width
                    text: page.cloudEnabled
                        ? "Recognized speech and conversation context are sent to OpenRouter. Audio remains local and is not uploaded."
                        : "Cloud processing is off. Spoken speech is transcribed locally, but no data leaves your machine. Enable above to receive AI answers."
                    font.pixelSize: Theme.fontCaption - 1
                    color: Theme.textSecondary
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── Message Thread (ListView) ───────────────────────────────────────
        Rectangle {
            width: parent.width
            height: 330
            radius: 12
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider
            clip: true

            ListView {
                id: msgList
                anchors.fill: parent
                anchors.margins: 10
                spacing: 8
                model: page.conversationHistory

                delegate: Column {
                    id: msgDelegate
                    width: msgList.width
                    spacing: 3

                    property bool isUser: modelData.role === "user"

                    RowLayout {
                        width: parent.width

                        Item { visible: msgDelegate.isUser; Layout.fillWidth: true }

                        Text {
                            text: msgDelegate.isUser ? "You" : "KIVO"
                            font.pixelSize: Theme.fontCaption - 1
                            font.bold: true
                            color: msgDelegate.isUser ? Theme.accent : Theme.textMuted
                        }

                        Item { visible: !msgDelegate.isUser; Layout.fillWidth: true }
                    }

                    RowLayout {
                        width: parent.width

                        Item { visible: msgDelegate.isUser; Layout.fillWidth: true }

                        Rectangle {
                            Layout.maximumWidth: parent.width * 0.85
                            Layout.preferredWidth: Math.min(parent.width * 0.85, msgContent.implicitWidth + 20)
                            Layout.preferredHeight: msgContent.implicitHeight + 16
                            radius: 10
                            color: msgDelegate.isUser ? Theme.accentDim : Theme.surfaceRaised
                            border.width: 1
                            border.color: msgDelegate.isUser ? Theme.accentSoft : Theme.border

                            Text {
                                id: msgContent
                                anchors.fill: parent
                                anchors.margins: 10
                                text: modelData.content || ""
                                font.pixelSize: Theme.fontBody
                                color: Theme.textPrimary
                                wrapMode: Text.WordWrap
                            }
                        }

                        Item { visible: !msgDelegate.isUser; Layout.fillWidth: true }
                    }
                }

                // Streaming response card
                footer: Item {
                    width: msgList.width
                    height: (page.ipc && page.ipc.streamingResponseAccumulated && page.ipc.streamingResponseAccumulated.length > 0)
                        ? streamCard.implicitHeight + 10 : 0
                    visible: page.ipc && page.ipc.streamingResponseAccumulated && page.ipc.streamingResponseAccumulated.length > 0

                    Rectangle {
                        id: streamCard
                        anchors.left: parent.left
                        width: Math.min(parent.width * 0.85, streamContent.implicitWidth + 24)
                        height: streamContent.implicitHeight + 16
                        radius: 10
                        color: Theme.surfaceRaised
                        border.width: 1
                        border.color: Theme.accent

                        Text {
                            id: streamContent
                            anchors.fill: parent
                            anchors.margins: 10
                            text: page.ipc ? page.ipc.streamingResponseAccumulated : ""
                            font.pixelSize: Theme.fontBody
                            color: Theme.textPrimary
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                Text {
                    anchors.centerIn: parent
                    visible: page.conversationHistory.length === 0 && !(page.ipc && page.ipc.streamingResponseAccumulated)
                    text: page.cloudEnabled
                        ? "Hold Push-to-Talk or type below to start a conversation with KIVO."
                        : "Cloud AI is disabled. Enable cloud processing above to chat."
                    font.pixelSize: Theme.fontBody
                    color: Theme.textMuted
                }
            }
        }

        // ── Input Bar ───────────────────────────────────────────────────────
        Rectangle {
            width: parent.width
            height: 40
            radius: 10
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.border

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 6
                spacing: 6

                TextInput {
                    id: chatInput
                    Layout.fillWidth: true
                    font.pixelSize: Theme.fontBody
                    color: Theme.textPrimary
                    clip: true
                    activeFocusOnPress: true
                    onAccepted: sendBtn.send()

                    Text {
                        anchors.fill: parent
                        text: "Ask KIVO a question…"
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontBody
                        visible: chatInput.text.length === 0
                    }
                }

                Rectangle {
                    id: sendBtn
                    Layout.preferredWidth: 54
                    Layout.preferredHeight: 28
                    radius: 6
                    color: (chatInput.text.trim().length > 0) ? Theme.accent : Theme.surfaceRaised
                    border.width: 1
                    border.color: Theme.border

                    function send() {
                        var txt = chatInput.text.trim()
                        if (txt.length > 0 && page.ipc && page.ipc.conversationSubmit) {
                            page.ipc.conversationSubmit(txt, null)
                            chatInput.text = ""
                        }
                    }

                    Text {
                        anchors.centerIn: parent
                        text: "Send"
                        font.pixelSize: Theme.fontCaption
                        font.bold: true
                        color: (chatInput.text.trim().length > 0) ? "#000000" : Theme.textMuted
                    }

                    MouseArea {
                        anchors.fill: parent
                        onClicked: sendBtn.send()
                    }
                }
            }
        }
    }
}
