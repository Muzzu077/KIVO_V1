import QtQuick
import QtQuick.Layouts
import "../styles"

/*
    KIVO Dashboard — Conversation page.

    Provides the multi-turn conversational dialogue thread between the user
    and KIVO, streaming response display, cloud consent control, provider selection
    (OmniRoute local gateway vs OpenRouter), model discovery, and text submission.
*/
Item {
    id: page

    property var ipc: null

    readonly property bool online: ipc !== null && ipc.connected === true
    property var conversationHistory: ipc && ipc.conversationHistory ? ipc.conversationHistory : []
    property var conversationStatus: ipc ? ipc.conversationStatus : null
    property bool cloudEnabled: ipc ? ipc.conversationCloudEnabled : false
    property string activeProvider: conversationStatus && conversationStatus.provider ? conversationStatus.provider.provider_name : "omniroute"
    property string currentModel: conversationStatus && conversationStatus.provider ? conversationStatus.provider.model : "agy/gemini-3.8-flash-high"
    property var discoveredModels: []
    property string testMessage: ""
    property bool testSuccess: false
    property bool testingConnection: false
    property bool discoveringModels: false
    property bool showModelPicker: false

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

            // Stop generation button
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

            // Clear chat button
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

        // ── Provider & Consent Configuration Card ───────────────────────────
        Rectangle {
            width: parent.width
            height: configCol.implicitHeight + 16
            radius: 12
            color: page.cloudEnabled
                ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.08)
                : Theme.surfaceSunken
            border.width: 1
            border.color: page.cloudEnabled ? Theme.accentSoft : Theme.divider

            Column {
                id: configCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 10
                spacing: 8

                // Row 1: Consent status, Provider Switcher & Toggle
                RowLayout {
                    width: parent.width
                    spacing: 8

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
                            text: page.cloudEnabled ? "AI Enabled" : "AI Disabled (Local Only)"
                            font.pixelSize: Theme.fontCaption
                            font.bold: true
                            color: page.cloudEnabled ? Theme.accent : Theme.warning
                        }
                    }

                    Item { Layout.fillWidth: true }

                    // Provider Tabs
                    Rectangle {
                        Layout.preferredWidth: 200
                        Layout.preferredHeight: 24
                        radius: 6
                        color: Theme.surfaceRaised
                        border.width: 1
                        border.color: Theme.border

                        Row {
                            anchors.fill: parent

                            // OmniRoute Tab
                            Rectangle {
                                width: 100
                                height: parent.height
                                radius: 5
                                color: page.activeProvider === "omniroute" ? Theme.accentDim : "transparent"
                                Text {
                                    anchors.centerIn: parent
                                    text: "OmniRoute"
                                    font.pixelSize: Theme.fontCaption - 1
                                    font.bold: page.activeProvider === "omniroute"
                                    color: page.activeProvider === "omniroute" ? Theme.textPrimary : Theme.textMuted
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: {
                                        if (page.ipc && page.ipc.conversationUpdateConfig) {
                                            page.ipc.conversationUpdateConfig({
                                                provider: "omniroute",
                                                model: "agy/gemini-3.8-flash-high"
                                            }, function(err, res) {
                                                page.refreshConversation()
                                            })
                                        }
                                    }
                                }
                            }

                            // OpenRouter Tab
                            Rectangle {
                                width: 100
                                height: parent.height
                                radius: 5
                                color: page.activeProvider === "openrouter" ? Theme.accentDim : "transparent"
                                Text {
                                    anchors.centerIn: parent
                                    text: "OpenRouter"
                                    font.pixelSize: Theme.fontCaption - 1
                                    font.bold: page.activeProvider === "openrouter"
                                    color: page.activeProvider === "openrouter" ? Theme.textPrimary : Theme.textMuted
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: {
                                        if (page.ipc && page.ipc.conversationUpdateConfig) {
                                            page.ipc.conversationUpdateConfig({
                                                provider: "openrouter",
                                                model: "meta-llama/llama-3.3-70b-instruct:free"
                                            }, function(err, res) {
                                                page.refreshConversation()
                                            })
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Enable / Disable Consent Button
                    Rectangle {
                        Layout.preferredWidth: toggleText.implicitWidth + 14
                        Layout.preferredHeight: 24
                        radius: 6
                        color: page.cloudEnabled ? Theme.surfaceRaised : Theme.accent
                        border.width: 1
                        border.color: page.cloudEnabled ? Theme.border : Theme.accentStrong

                        Text {
                            id: toggleText
                            anchors.centerIn: parent
                            text: page.cloudEnabled ? "Disable AI" : "Enable AI"
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

                // Row 2: Model & Endpoint Controls
                RowLayout {
                    width: parent.width
                    spacing: 8

                    Text {
                        text: "Model:"
                        font.pixelSize: Theme.fontCaption - 1
                        color: Theme.textMuted
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 24
                        radius: 6
                        color: Theme.surfaceRaised
                        border.width: 1
                        border.color: Theme.border

                        TextInput {
                            id: modelInput
                            anchors.fill: parent
                            anchors.leftMargin: 8
                            anchors.rightMargin: 8
                            verticalAlignment: TextInput.AlignVCenter
                            font.pixelSize: Theme.fontCaption - 1
                            font.family: Theme.monoFamily
                            color: Theme.textPrimary
                            text: page.currentModel
                            onAccepted: {
                                if (page.ipc && page.ipc.conversationUpdateConfig) {
                                    page.ipc.conversationUpdateConfig({ model: modelInput.text.trim() }, null)
                                }
                            }
                        }
                    }

                    // Discover Models Button
                    Rectangle {
                        Layout.preferredWidth: discBtnText.implicitWidth + 14
                        Layout.preferredHeight: 24
                        radius: 6
                        color: discMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceRaised
                        border.width: 1
                        border.color: Theme.border

                        Text {
                            id: discBtnText
                            anchors.centerIn: parent
                            text: page.discoveringModels ? "Querying…" : "Discover Models"
                            font.pixelSize: Theme.fontCaption - 1
                            color: Theme.accent
                        }

                        MouseArea {
                            id: discMouse
                            anchors.fill: parent
                            enabled: !page.discoveringModels && page.online
                            onClicked: {
                                page.discoveringModels = true
                                page.testMessage = ""
                                if (page.ipc && page.ipc.conversationListModels) {
                                    page.ipc.conversationListModels(function(err, res) {
                                        page.discoveringModels = false
                                        if (!err && res && Array.isArray(res.models)) {
                                            page.discoveredModels = res.models
                                            page.showModelPicker = true
                                            page.testMessage = "Found " + res.models.length + " models"
                                            page.testSuccess = true
                                        } else {
                                            page.testMessage = (err && err.error && err.error.message) || (res && res.error) || "Discovery failed"
                                            page.testSuccess = false
                                        }
                                    })
                                }
                            }
                        }
                    }

                    // Test Connection Button
                    Rectangle {
                        Layout.preferredWidth: testBtnText.implicitWidth + 14
                        Layout.preferredHeight: 24
                        radius: 6
                        color: testMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceRaised
                        border.width: 1
                        border.color: Theme.border

                        Text {
                            id: testBtnText
                            anchors.centerIn: parent
                            text: page.testingConnection ? "Testing…" : "Test Connection"
                            font.pixelSize: Theme.fontCaption - 1
                            color: Theme.textPrimary
                        }

                        MouseArea {
                            id: testMouse
                            anchors.fill: parent
                            enabled: !page.testingConnection && page.online
                            onClicked: {
                                page.testingConnection = true
                                page.testMessage = ""
                                if (page.ipc && page.ipc.conversationTestConnection) {
                                    page.ipc.conversationTestConnection(function(err, res) {
                                        page.testingConnection = false
                                        if (!err && res && res.ok) {
                                            page.testSuccess = true
                                            page.testMessage = res.message || "Connection successful"
                                        } else {
                                            page.testSuccess = false
                                            page.testMessage = (res && res.message) || (err && err.error && err.error.message) || "Connection failed"
                                        }
                                    })
                                }
                            }
                        }
                    }
                }

                // Row 3: Status / Disclaimer / Feedback text
                RowLayout {
                    width: parent.width

                    Text {
                        Layout.fillWidth: true
                        text: {
                            if (page.testMessage.length > 0)
                                return (page.testSuccess ? "✓ " : "✗ ") + page.testMessage
                            if (page.activeProvider === "omniroute")
                                return "OmniRoute Gateway (http://127.0.0.1:20128/v1) · Requests forwarded to upstream Gemini model · Audio remains local."
                            return "OpenRouter Cloud API · Requests sent securely over HTTPS · Audio remains local and is not uploaded."
                        }
                        font.pixelSize: Theme.fontCaption - 1
                        color: page.testMessage.length > 0
                            ? (page.testSuccess ? Theme.mint : Theme.danger)
                            : Theme.textSecondary
                        elide: Text.ElideRight
                    }

                    Text {
                        visible: page.discoveredModels.length > 0
                        text: page.showModelPicker ? "[Hide Models]" : "[Show Models (" + page.discoveredModels.length + ")]"
                        font.pixelSize: Theme.fontCaption - 1
                        color: Theme.accent
                        MouseArea {
                            anchors.fill: parent
                            onClicked: page.showModelPicker = !page.showModelPicker
                        }
                    }
                }

                // Model Picker Dropdown / List (when models discovered)
                Rectangle {
                    width: parent.width
                    height: 120
                    visible: page.showModelPicker && page.discoveredModels.length > 0
                    radius: 8
                    color: Theme.surfaceRaised
                    border.width: 1
                    border.color: Theme.border
                    clip: true

                    ListView {
                        anchors.fill: parent
                        anchors.margins: 4
                        model: page.discoveredModels
                        delegate: Rectangle {
                            width: parent.width
                            height: 24
                            radius: 4
                            color: modelPickMouse.containsMouse ? Theme.surfaceHover : (modelData === page.currentModel ? Theme.accentDim : "transparent")

                            Text {
                                anchors.left: parent.left
                                anchors.leftMargin: 8
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData
                                font.pixelSize: Theme.fontCaption - 1
                                font.family: Theme.monoFamily
                                color: modelData === page.currentModel ? Theme.accent : Theme.textPrimary
                            }

                            MouseArea {
                                id: modelPickMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                onClicked: {
                                    modelInput.text = modelData
                                    if (page.ipc && page.ipc.conversationUpdateConfig) {
                                        page.ipc.conversationUpdateConfig({ model: modelData }, null)
                                    }
                                    page.showModelPicker = false
                                }
                            }
                        }
                    }
                }
            }
        }

        // ── Pending Command Approval Card ────────────────────────────────────
        Rectangle {
            id: approvalCard
            width: parent.width
            height: approvalCol.implicitHeight + 16
            visible: page.ipc && page.ipc.pendingCommandApproval !== null
            radius: 10
            color: Qt.rgba(Theme.warning.r, Theme.warning.g, Theme.warning.b, 0.12)
            border.width: 1
            border.color: Theme.warning

            Column {
                id: approvalCol
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
                            color: Theme.warning
                        }
                        Text {
                            text: "TERMINAL COMMAND APPROVAL REQUIRED"
                            font.pixelSize: Theme.fontCaption
                            font.bold: true
                            color: Theme.warning
                        }
                    }

                    Item { Layout.fillWidth: true }

                    Text {
                        text: "Expires in ~60s"
                        font.pixelSize: Theme.fontCaption - 1
                        color: Theme.textMuted
                    }
                }

                Text {
                    width: parent.width
                    text: page.ipc && page.ipc.pendingCommandApproval ? page.ipc.pendingCommandApproval.reason : ""
                    font.pixelSize: Theme.fontBody
                    color: Theme.textPrimary
                    wrapMode: Text.WordWrap
                }

                // Command details box
                Rectangle {
                    width: parent.width
                    height: cmdDetailsCol.implicitHeight + 12
                    radius: 6
                    color: Theme.surfaceSunken
                    border.width: 1
                    border.color: Theme.border

                    Column {
                        id: cmdDetailsCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 8
                        spacing: 3

                        Text {
                            text: "Command: " + (page.ipc && page.ipc.pendingCommandApproval ? (page.ipc.pendingCommandApproval.command + " " + page.ipc.pendingCommandApproval.arguments.join(" ")) : "")
                            font.family: Theme.monoFamily
                            font.pixelSize: Theme.fontCaption
                            font.bold: true
                            color: Theme.mint
                            elide: Text.ElideRight
                            width: parent.width
                        }

                        Text {
                            text: "Directory: " + (page.ipc && page.ipc.pendingCommandApproval ? page.ipc.pendingCommandApproval.working_directory : "")
                            font.family: Theme.monoFamily
                            font.pixelSize: Theme.fontCaption - 1
                            color: Theme.textMuted
                            elide: Text.ElideRight
                            width: parent.width
                        }

                        Text {
                            text: "⚠️ Output will be shared with your configured AI provider."
                            font.pixelSize: Theme.fontCaption - 1
                            color: Theme.textSecondary
                        }
                    }
                }

                // Actions: Approve / Reject
                RowLayout {
                    width: parent.width
                    spacing: 10

                    Item { Layout.fillWidth: true }

                    // Reject Button
                    Rectangle {
                        Layout.preferredWidth: 80
                        Layout.preferredHeight: 28
                        radius: 6
                        color: Theme.surfaceRaised
                        border.width: 1
                        border.color: Theme.border

                        Text {
                            anchors.centerIn: parent
                            text: "Reject"
                            font.pixelSize: Theme.fontCaption
                            color: Theme.textSecondary
                        }

                        MouseArea {
                            anchors.fill: parent
                            onClicked: {
                                if (page.ipc && page.ipc.pendingCommandApproval) {
                                    page.ipc.commandResolveApproval(page.ipc.pendingCommandApproval.token, false, null)
                                }
                            }
                        }
                    }

                    // Approve Button
                    Rectangle {
                        Layout.preferredWidth: 120
                        Layout.preferredHeight: 28
                        radius: 6
                        color: Theme.accent
                        border.width: 1
                        border.color: Theme.accentStrong

                        Text {
                            anchors.centerIn: parent
                            text: "Approve & Run"
                            font.pixelSize: Theme.fontCaption
                            font.bold: true
                            color: "#000000"
                        }

                        MouseArea {
                            anchors.fill: parent
                            onClicked: {
                                if (page.ipc && page.ipc.pendingCommandApproval) {
                                    page.ipc.commandResolveApproval(page.ipc.pendingCommandApproval.token, true, null)
                                }
                            }
                        }
                    }
                }
            }
        }

        // ── Active Tool Execution Activity Banner ────────────────────────────
        Rectangle {
            width: parent.width
            height: 28
            visible: page.ipc && page.ipc.activeAgentTool && page.ipc.activeAgentTool.length > 0
            radius: 8
            color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.15)
            border.width: 1
            border.color: Theme.accent

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 10
                spacing: 6

                Rectangle {
                    width: 6
                    height: 6
                    radius: 3
                    color: Theme.mint
                    SequentialAnimation on opacity {
                        running: page.ipc && page.ipc.activeAgentTool && page.ipc.activeAgentTool.length > 0
                        loops: Animation.Infinite
                        NumberAnimation { from: 1.0; to: 0.2; duration: 400 }
                        NumberAnimation { from: 0.2; to: 1.0; duration: 400 }
                    }
                }

                Text {
                    text: page.ipc ? ("⚙ KIVO is executing " + page.ipc.activeAgentTool + "…") : ""
                    font.pixelSize: Theme.fontCaption - 1
                    font.bold: true
                    color: Theme.accent
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: "read-only tool"
                    font.pixelSize: Theme.fontCaption - 2
                    color: Theme.textMuted
                }
            }
        }

        // ── Message Thread (ListView) ───────────────────────────────────────
        Rectangle {
            width: parent.width
            height: page.showModelPicker ? 180 : ((page.ipc && page.ipc.pendingCommandApproval) ? 220 : 310)
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
                        ? "Hold Push-to-Talk or type below to speak with KIVO."
                        : "AI response processing is disabled. Enable above to begin chatting."
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
