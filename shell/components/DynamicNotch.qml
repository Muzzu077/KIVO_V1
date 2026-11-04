import QtQuick
import QtQuick.Layouts
import "../styles"

/*
    The visible KIVO Dynamic Notch.

    State is derived exclusively from KivoIpc snapshots/events.  The component
    owns presentation, expansion, pinning and the cancellation *intent*; it
    never fabricates progress, audio levels or task outcomes.
*/
Item {
    id: notch

    property var ipc: null
    property bool reducedMotion: false
    property bool autoCollapseOnSuccess: true

    property bool expanded: false
    property bool pinned: false
    property bool hovered: false

    // A short-lived result banner.  Only real backend outcomes create one.
    property bool transientActive: false
    property string transientState: ""
    property string transientTitle: ""
    property string transientDetail: ""
    property bool transientAutoCollapse: false

    // Cancellation is not "successful" until a terminal backend event arrives.
    property bool awaitingCancelConfirmation: false
    property string stoppingMessage: ""
    property int stopWaitSeconds: 0

    property bool _wasDriveActive: false
    property string _trackedDriveGoal: ""
    property real _driveStartMs: 0
    property real nowTick: 0

    // Drive and task terminal events can describe the same cancellation.  Keep
    // the most recent terminal key so one backend outcome produces one banner.
    property string _lastTerminalKey: ""
    property real _lastTerminalMs: 0

    property alias inputRegion: capsule

    // ── Push-to-Talk audio state ───────────────────────────────────────────
    property bool pttPressed: false
    property real pttElapsed: 0.0
    property string lastCaptureDuration: ""
    property string lastCaptureTime: ""

    Timer {
        id: pttElapsedTimer
        interval: 100
        repeat: true
        running: notch.audioState === "capturing"
        onTriggered: {
            notch.pttElapsed += 0.1
        }
    }

    function startPushToTalk() {
        if (!connected)
            return
        pttPressed = true
        pttElapsed = 0.0
        if (ipc)
            ipc.audioStartCapture()
    }

    function stopPushToTalk() {
        if (!pttPressed && audioState !== "capturing")
            return
        pttPressed = false
        if (ipc)
            ipc.audioStopCapture()
    }

    function cancelPushToTalk() {
        pttPressed = false
        pttElapsed = 0.0
        if (ipc)
            ipc.audioCancelCapture()
    }

    // ── Authoritative shortcuts ────────────────────────────────────────────
    readonly property bool connected: !!(ipc && ipc.connected)
    readonly property bool stale: ipc ? ipc.stale : true
    readonly property string connectionState: ipc ? ipc.connectionState : "disconnected"
    readonly property string assistantState: ipc ? ipc.assistantState : "idle"
    readonly property string assistantDetail: ipc ? ipc.assistantDetail : ""
    readonly property string audioState: ipc ? ipc.audioState : "unavailable"
    readonly property string agentDetail: ipc ? ipc.agentDetail : ""

    readonly property bool driveActive: !!(ipc && ipc.driveActive)
    readonly property bool activeTask: !!(ipc && ipc.activeTask)
    readonly property string taskGoal: ipc ? ipc.taskGoal : ""
    readonly property string driveGoal: ipc ? ipc.driveGoal : ""
    readonly property string driveAction: ipc ? ipc.driveAction : ""
    readonly property string taskStatus: ipc ? ipc.taskStatus : ""
    readonly property int taskCurrentStep: ipc ? ipc.taskCurrentStep : 0
    readonly property int taskTotalSteps: ipc ? ipc.taskTotalSteps : 0
    readonly property var taskSteps: ipc ? ipc.taskSteps : []

    readonly property bool stopping: !!(ipc && ipc.cancelling)
        || awaitingCancelConfirmation

    readonly property bool cancellable: connected && activeTask
        && (taskStatus === "pending" || taskStatus === "running")
        && !(ipc && ipc.cancelling)
        && !awaitingCancelConfirmation

    // ── Visual state machine ───────────────────────────────────────────────
    readonly property string visualState: {
        if (!connected)
            return "offline"
        if (stale)
            return "syncing"
        if (stopping)
            return "stopping"
        if (transientActive)
            return transientState
        if (driveActive || activeTask)
            return "driving"

        var state = assistantState
        if (state === "listening" || state === "wake_word" || state === "voice_command")
            return "listening"
        if (state === "thinking")
            return "thinking"
        if (state === "speaking")
            return "speaking"
        if (state === "driving")
            return "driving"
        if (state === "error")
            return "error"
        if (state === "cancellation")
            return "cancelled"
        return "idle"
    }

    readonly property bool transientIconVisible: visualState === "success"
        || visualState === "error" || visualState === "cancelled"

    readonly property color stateAccent: {
        switch (visualState) {
        case "listening": return notch.audioState === "capturing" ? Theme.danger : Theme.mint
        case "thinking": return Theme.accentSoft
        case "speaking": return Theme.mint
        case "driving": return Theme.accent
        case "success": return Theme.accentStrong
        case "error": return Theme.danger
        case "cancelled": return Theme.neutral
        case "stopping": return Theme.warning
        case "offline": return Theme.offline
        case "syncing": return Theme.accentSoft
        default: return Theme.accent
        }
    }

    readonly property bool statePulse: visualState === "listening"
        || visualState === "thinking" || visualState === "speaking"
        || visualState === "driving" || visualState === "syncing"

    readonly property string stateLabel: {
        switch (visualState) {
        case "listening": return notch.audioState === "capturing" ? "Recording" : "Listening"
        case "thinking": {
            if (notch.ipc && notch.ipc.sttTranscribing)
                return "Understanding…"
            if (notch.ipc && notch.ipc.conversationState === "streaming")
                return "Responding…"
            return "Thinking"
        }
        case "speaking": return "Speaking"
        case "driving": return "Driving"
        case "success": return "Done"
        case "error": return "Attention"
        case "cancelled": return "Stopped"
        case "stopping": return "Stopping"
        case "offline": return "Offline"
        case "syncing": return "Syncing"
        default: return "Ready"
        }
    }

    readonly property string stateGlyph: {
        switch (visualState) {
        case "success": return "✓"
        case "error": return "!"
        case "cancelled": return "■"
        case "stopping": return "…"
        case "offline": return "…"
        case "syncing": return "…"
        default: return ""
        }
    }

    readonly property string collapsedLabel: {
        switch (visualState) {
        case "listening": return notch.audioState === "capturing" ? "Recording…" : "Listening"
        case "thinking": {
            if (notch.ipc && notch.ipc.sttTranscribing)
                return "Understanding…"
            if (notch.ipc && notch.ipc.conversationState === "streaming")
                return "Responding…"
            return "Thinking"
        }
        case "speaking": return "Speaking"
        case "driving": return taskTitle.length > 0 ? taskTitle : "Working"
        case "success": return "Done"
        case "error": return "Attention"
        case "cancelled": return "Stopped"
        case "stopping": return "Stopping"
        case "offline": return "Reconnecting"
        case "syncing": return "Syncing"
        default: return "Ready"
        }
    }

    readonly property int collapsedWidth: {
        switch (visualState) {
        case "driving": return 334
        case "error": return 264
        case "listening": return 218
        case "speaking": return 218
        case "thinking": return 202
        case "success": return 194
        case "cancelled": return 204
        case "stopping": return 226
        case "offline": return 218
        case "syncing": return 196
        default: return 176
        }
    }

    readonly property int expandedHeight: {
        switch (visualState) {
        case "driving": return 302
        case "stopping": return 302
        case "error": return 252
        case "listening": return 252
        case "speaking": return 252
        case "thinking": return 224
        case "success": return 222
        case "cancelled": return 222
        case "offline": return 224
        case "syncing": return 224
        default: return 212
        }
    }

    readonly property int motionDuration: reducedMotion ? 0 : Theme.durationNormal
    readonly property int fastDuration: reducedMotion ? 0 : Theme.durationFast

    // ── Derived copy ───────────────────────────────────────────────────────
    readonly property string taskTitle: {
        if (taskGoal.length > 0)
            return taskGoal
        if (driveGoal.length > 0)
            return driveGoal
        return ""
    }

    readonly property string currentStepDescription: {
        if (!ipc)
            return ""
        var steps = ipc.taskSteps
        if (Array.isArray(steps)) {
            for (var i = 0; i < steps.length; i++) {
                if (steps[i] && steps[i].status === "running")
                    return String(steps[i].description || "")
            }
            for (var j = 0; j < steps.length; j++) {
                if (steps[j] && steps[j].status === "pending")
                    return String(steps[j].description || "")
            }
        }
        if (driveAction.length > 0)
            return driveAction
        return ""
    }

    readonly property string expandedTitle: {
        switch (visualState) {
        case "driving": return taskTitle.length > 0 ? taskTitle : "Working"
        case "success": return transientTitle.length > 0 ? transientTitle : "Task complete"
        case "error": return transientTitle.length > 0 ? transientTitle : "Something needs attention"
        case "cancelled": return transientTitle.length > 0 ? transientTitle : "Task stopped"
        case "stopping": return "Stopping"
        case "offline": return "Backend unavailable"
        case "syncing": return "Synchronizing"
        case "listening": return "Listening"
        case "thinking": return "Thinking"
        case "speaking": return "Speaking"
        default: return "Ready"
        }
    }

    readonly property string expandedDetail: {
        switch (visualState) {
        case "driving":
            if (currentStepDescription.length > 0)
                return currentStepDescription
            if (agentDetail.length > 0)
                return agentDetail
            return "Working through the active task."
        case "success":
        case "error":
        case "cancelled":
            if (transientDetail.length > 0)
                return transientDetail
            if (assistantDetail.length > 0)
                return assistantDetail
            return ""
        case "stopping":
            if (stopWaitSeconds >= 5)
                return "The stop request was accepted. Waiting for the backend to reach a safe step boundary."
            return stoppingMessage.length > 0
                ? stoppingMessage : "Waiting for backend confirmation…"
        case "offline":
            return ipc && ipc.lastError.length > 0
                ? ipc.lastError : "KIVO will reconnect automatically."
        case "syncing":
            return "Refreshing authoritative state from the KIVO backend."
        case "listening":
        case "thinking":
        case "speaking":
            return assistantDetail.length > 0
                ? assistantDetail : "The backend reports an active assistant state."
        default:
            return assistantDetail.length > 0
                ? assistantDetail : "The KIVO backend is connected and idle."
        }
    }

    readonly property string transcriptSummary: {
        if (!ipc)
            return ""
        if (ipc.lastAssistantResponse.length > 0)
            return ipc.lastAssistantResponse
        if (ipc.lastTranscript.length > 0)
            return "You: " + ipc.lastTranscript
        return ""
    }

    readonly property string contextLine: {
        if (!ipc || !ipc.screenContext || ipc.screenContext.cleared)
            return ""
        var app = ipc.screenContext.active_app || ""
        var title = ipc.screenContext.active_title || ""
        if (app.length === 0)
            return ""
        return title.length > 0 ? app + " · " + title : app
    }

    readonly property string audioNote: {
        if (!ipc)
            return ""
        if (visualState !== "listening" && visualState !== "speaking")
            return ""
        if (ipc.audioState === "unavailable")
            return "Audio feed unavailable — showing backend state only."
        if (ipc.audioDetail.length > 0)
            return ipc.audioDetail
        return ""
    }

    readonly property string backendMeta: {
        var version = ipc && ipc.serviceVersion.length > 0
            ? " · KIVO " + ipc.serviceVersion : ""
        return "IPC v1" + version + (connected ? "" : " · offline")
    }

    readonly property string footerHint: {
        if (!connected)
            return ipc && ipc.retryInSeconds > 0
                ? "Retrying in " + ipc.retryInSeconds + "s"
                : "Reconnecting automatically"
        if (pinned)
            return "Pinned open"
        if (stale)
            return "Synchronizing"
        return expanded ? "Click the capsule to collapse" : "Click to expand"
    }

    readonly property real elapsedSeconds: {
        if (!connected)
            return 0
        if (ipc && ipc.taskCreatedAt > 0)
            return Math.max(0, nowTick / 1000 - ipc.taskCreatedAt)
        if (ipc && ipc.driveActive && _driveStartMs > 0)
            return Math.max(0, (nowTick - _driveStartMs) / 1000)
        return 0
    }

    readonly property string elapsedText: formatDuration(elapsedSeconds)

    // ── Helpers ────────────────────────────────────────────────────────────
    function pad2(value) {
        return value < 10 ? "0" + value : String(value)
    }

    function formatDuration(seconds) {
        if (!isFinite(seconds) || seconds < 0)
            seconds = 0
        var total = Math.floor(seconds)
        var hours = Math.floor(total / 3600)
        var minutes = Math.floor((total % 3600) / 60)
        var secs = total % 60
        if (hours > 0)
            return hours + ":" + pad2(minutes) + ":" + pad2(secs)
        return minutes + ":" + pad2(secs)
    }

    function expand() {
        if (expanded)
            return
        if (transientActive && transientState === "success")
            clearTransient()
        expanded = true
        nowTick = Date.now()
    }

    function collapse() {
        if (!expanded)
            return
        expanded = false
        pinned = false
    }

    function toggleExpanded() {
        if (expanded)
            collapse()
        else
            expand()
    }

    function togglePinned() {
        pinned = !pinned
        if (pinned)
            expanded = true
    }

    function clearTransient() {
        transientTimer.stop()
        transientActive = false
        transientState = ""
        transientTitle = ""
        transientDetail = ""
        transientAutoCollapse = false
    }

    function showTransient(state, title, detail, duration, autoCollapse) {
        transientState = state
        transientTitle = title
        transientDetail = detail
        transientAutoCollapse = autoCollapse === true
        transientActive = true
        transientTimer.interval = duration > 0 ? duration : 2200
        transientTimer.restart()
        nowTick = Date.now()
    }

    function finishTransient() {
        var shouldCollapse = transientAutoCollapse && autoCollapseOnSuccess && !pinned
        clearTransient()
        if (shouldCollapse)
            collapse()
    }

    function requestStop() {
        if (!ipc)
            return false
        if (!connected || !cancellable || stopping)
            return false
        return ipc.cancelActiveTask() === true
    }

    function handleCancellation(result) {
        if (!result)
            return
        if (result.ok !== true) {
            awaitingCancelConfirmation = false
            stopWaitTimer.stop()
            showTransient("error", "Stop failed",
                          result.message || "The cancellation request failed.",
                          4200, false)
            return
        }
        if (result.cancelled === true) {
            awaitingCancelConfirmation = true
            stoppingMessage = result.message || "Stopping…"
            stopWaitSeconds = 0
            stopWaitTimer.restart()
            nowTick = Date.now()
            return
        }
        awaitingCancelConfirmation = false
        stopWaitTimer.stop()
        showTransient("error", "Nothing to stop",
                      result.message || "No active task to cancel.",
                      3000, false)
    }

    function _acceptTerminal(outcome, label) {
        var key = String(outcome) + "|" + String(label)
        var now = Date.now()
        if (_lastTerminalKey === key && (now - _lastTerminalMs) < 1500)
            return false
        _lastTerminalKey = key
        _lastTerminalMs = now
        return true
    }

    function handleTaskFinished(result) {
        if (!result)
            return
        if (awaitingCancelConfirmation) {
            awaitingCancelConfirmation = false
            stopWaitTimer.stop()
        }
        if (result.outcome === "completed") {
            if (!_acceptTerminal("completed", result.goal))
                return
            showTransient("success", "Task complete",
                          result.goal || "The task completed.", 2200, true)
        } else if (result.outcome === "cancelled") {
            if (!_acceptTerminal("cancelled", result.goal || result.summary))
                return
            showTransient("cancelled", "Task stopped",
                          result.summary || result.goal || "The task was stopped.",
                          2600, false)
        } else if (result.outcome === "failed") {
            if (!_acceptTerminal("failed", result.goal || result.summary))
                return
            showTransient("error", "Task failed",
                          result.summary || "The task did not complete.",
                          4200, false)
        }
    }

    function handleDriveFinished(result) {
        if (!result)
            return
        if (awaitingCancelConfirmation) {
            awaitingCancelConfirmation = false
            stopWaitTimer.stop()
        }
        if (result.outcome === "completed") {
            if (!_acceptTerminal("completed", result.goal))
                return
            showTransient("success", "Desktop work complete",
                          result.goal || "The active Drive session finished.",
                          2200, true)
        } else if (result.outcome === "cancelled") {
            if (!_acceptTerminal("cancelled", result.goal || result.reason))
                return
            showTransient("cancelled", "Drive stopped",
                          result.reason || result.goal || "The Drive session was stopped.",
                          2600, false)
        } else if (result.outcome === "failed") {
            if (!_acceptTerminal("failed", result.goal || result.reason))
                return
            showTransient("error", "Drive failed",
                          result.reason || "The Drive session did not complete.",
                          4200, false)
        }
    }

    function clearTransientForActivity() {
        if (!transientActive)
            return
        clearTransient()
    }

    // ── Backend wiring ─────────────────────────────────────────────────────
    Connections {
        target: notch.ipc

        function onStatusUpdated() {
            notch.nowTick = Date.now()
            if (notch.ipc && notch.ipc.driveActive && !notch._wasDriveActive) {
                // The first authoritative status after connect carries the
                // real elapsed time.  Anchor the local display clock to it.
                notch._wasDriveActive = true
                notch._trackedDriveGoal = notch.ipc.driveGoal
                notch._driveStartMs = Date.now()
                    - Math.max(0, notch.ipc.driveElapsed) * 1000
            }
            if (notch.connected && (notch.activeTask || notch.driveActive))
                notch.clearTransientForActivity()
            if (notch.connected && notch.awaitingCancelConfirmation
                    && !notch.activeTask && !notch.driveActive) {
                notch.awaitingCancelConfirmation = false
                stopWaitTimer.stop()
                notch.clearTransient()
            }
        }

        function onTaskUpdated() {
            notch.nowTick = Date.now()
            if (notch.connected && notch.activeTask)
                notch.clearTransientForActivity()
            if (notch.connected && notch.awaitingCancelConfirmation
                    && !notch.activeTask && !notch.driveActive) {
                // The authoritative snapshot no longer contains an active
                // task.  The terminal event normally arrives first; if it was
                // missed, clear the stopping state without claiming success.
                notch.awaitingCancelConfirmation = false
                stopWaitTimer.stop()
                notch.clearTransient()
            }
        }

        function onDriveUpdated() {
            notch.nowTick = Date.now()
            if (notch.ipc && notch.ipc.driveActive) {
                if (!notch._wasDriveActive
                        || notch._trackedDriveGoal !== notch.ipc.driveGoal) {
                    notch._trackedDriveGoal = notch.ipc.driveGoal
                    notch._driveStartMs = Date.now()
                        - Math.max(0, notch.ipc.driveElapsed) * 1000
                }
                notch._wasDriveActive = true
                notch.clearTransientForActivity()
            } else {
                notch._wasDriveActive = false
                notch._trackedDriveGoal = ""
            }
        }

        function onConnectionChanged() {
            notch.nowTick = Date.now()
            if (!notch.connected) {
                notch.awaitingCancelConfirmation = false
                stopWaitTimer.stop()
                notch._wasDriveActive = false
                notch._trackedDriveGoal = ""
                notch._driveStartMs = 0
            } else if (notch.awaitingCancelConfirmation
                    && !notch.activeTask && !notch.driveActive) {
                notch.awaitingCancelConfirmation = false
                stopWaitTimer.stop()
            }
        }

        function onCancellationFinished(result) {
            notch.handleCancellation(result)
        }

        function onTaskFinished(result) {
            notch.handleTaskFinished(result)
        }

        function onDriveFinished(result) {
            notch.handleDriveFinished(result)
        }

        function onBackendError(message) {
            if (notch.connected)
                notch.showTransient("error", "Backend error", message, 4200, false)
        }

        function onAudioCaptureCompleted(metadata) {
            var dur = metadata && metadata.duration ? metadata.duration : 0
            notch.lastCaptureDuration = Number(dur).toFixed(1)
            notch.lastCaptureTime = "just now"
            notch.showTransient("success", "Audio Captured",
                                notch.lastCaptureDuration + "s microphone audio received",
                                2500, false)
        }

        function onAudioCaptureError(error) {
            notch.showTransient("error", "Capture Error",
                                error || "Microphone capture failed",
                                3500, false)
        }

        function onSttTranscriptionStarted(captureId) {
            notch.showTransient("thinking", "Transcribing…",
                                "Understanding speech…", 4000, false)
        }

        function onSttTranscriptionCompleted(result) {
            if (result && result.text) {
                notch.showTransient("success", "Heard",
                                    result.text, 3500, false)
            } else {
                notch.showTransient("neutral", "No speech detected",
                                    "Recording was silent or unrecognized", 2500, false)
            }
        }

        function onSttTranscriptionFailed(error) {
            notch.showTransient("error", "Transcription failed",
                                error || "Speech recognition error", 3500, false)
        }

        function onConversationResponseStarted(msgId, convId) {
            notch.showTransient("thinking", "Thinking…",
                                "Generating response…", 4000, false)
        }

        function onConversationResponseCompleted(message) {
            var preview = message && message.content ? message.content.substring(0, 70) : "Answer ready"
            notch.showTransient("success", "KIVO", preview, 3500, false)
        }

        function onConversationResponseFailed(error) {
            notch.showTransient("error", "AI Response Failed",
                                error || "Provider error", 3500, false)
        }

        function onConversationStatusUpdated(status) {
            if (status && status.state === "cloud_disabled") {
                notch.showTransient("neutral", "Cloud AI Disabled",
                                    "Enable in Dashboard to chat with OpenRouter", 3500, false)
            }
        }

        function onAgentToolStarted(name, callId) {
            var label = name === "get_current_time" ? "Checking current time…"
                : (name === "get_system_overview" ? "Checking system overview…" : "Running " + name + "…")
            notch.showTransient("thinking", "Tool Active", label, 4000, false)
        }

        function onAgentToolCompleted(result) {
            if (result && !result.success) {
                notch.showTransient("error", "Tool Failed",
                                    result.error_message || "Tool execution failed", 3000, false)
            }
        }

        function onCommandApprovalRequested(request) {
            var cmd = request && request.command ? request.command : "command"
            notch.showTransient("warning", "Approval Needed",
                                "Command proposed: " + cmd + " (Check Dashboard)", 5000, false)
        }

        function onCommandApprovalResolved(result) {
            var st = result && result.status ? result.status : ""
            if (st === "approved") {
                notch.showTransient("success", "Approved", "Executing command…", 2000, false)
            } else if (st === "rejected") {
                notch.showTransient("cancelled", "Rejected", "Command was cancelled", 2500, false)
            }
        }
    }

    Timer {
        id: transientTimer
        repeat: false
        onTriggered: notch.finishTransient()
    }

    Timer {
        id: stopWaitTimer
        interval: 1000
        repeat: true
        running: notch.awaitingCancelConfirmation
        onTriggered: notch.stopWaitSeconds += 1
    }

    Timer {
        interval: 1000
        repeat: true
        running: notch.connected && (notch.driveActive || notch.activeTask || notch.stopping)
        onTriggered: notch.nowTick = Date.now()
    }

    Component.onCompleted: {
        nowTick = Date.now()
        if (ipc && ipc.driveActive) {
            _wasDriveActive = true
            _trackedDriveGoal = ipc.driveGoal
            _driveStartMs = Date.now() - Math.max(0, ipc.driveElapsed) * 1000
        }
    }

    // Escape collapses only while the notch is expanded.  The panel uses
    // keyboardFocus OnDemand, so this does not take focus merely by existing.
    Shortcut {
        sequence: "Escape"
        enabled: notch.expanded
        context: Qt.WindowShortcut
        onActivated: notch.collapse()
    }

    // ── Surface ────────────────────────────────────────────────────────────
    width: capsule.width
    height: capsule.height

    // Two low-opacity offset layers suggest a soft shadow without a glow.
    Rectangle {
        x: capsule.x + 4
        y: capsule.y + 7
        width: Math.max(0, capsule.width - 8)
        height: capsule.height
        radius: capsule.radius
        color: "#000000"
        opacity: expanded ? 0.14 : 0.10
        Behavior on opacity { NumberAnimation { duration: notch.motionDuration } }
    }

    Rectangle {
        x: capsule.x + 2
        y: capsule.y + 3
        width: Math.max(0, capsule.width - 4)
        height: capsule.height
        radius: capsule.radius
        color: "#000000"
        opacity: expanded ? 0.26 : 0.20
        Behavior on opacity { NumberAnimation { duration: notch.motionDuration } }
    }

    Rectangle {
        id: capsule

        width: expanded ? Theme.expandedWidth : collapsedWidth
        height: expanded ? expandedHeight : Theme.collapsedHeight
        radius: expanded ? Theme.radiusExpanded : Theme.radiusCollapsed

        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.surfaceTop }
            GradientStop { position: 0.55; color: Theme.surfaceBottom }
            GradientStop { position: 1.0; color: "#0D110E" }
        }

        border.width: 1
        border.color: hovered ? Theme.borderStrong : Theme.border

        Behavior on width {
            NumberAnimation {
                duration: notch.motionDuration
                easing.type: Theme.easeOut
            }
        }
        Behavior on height {
            NumberAnimation {
                duration: notch.motionDuration
                easing.type: Theme.easeOut
            }
        }
        Behavior on radius {
            NumberAnimation {
                duration: notch.motionDuration
                easing.type: Theme.easeOut
            }
        }
        Behavior on border.color {
            ColorAnimation { duration: notch.fastDuration }
        }

        // A quiet inner highlight along the top edge.
        Rectangle {
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.topMargin: 1
            anchors.leftMargin: expanded ? 22 : 14
            anchors.rightMargin: expanded ? 22 : 14
            height: 1
            color: Theme.accent
            opacity: hovered ? 0.22 : 0.12
            Behavior on opacity { NumberAnimation { duration: notch.fastDuration } }
        }

        // Background click target.  Buttons and other MouseAreas are placed
        // above it and consume their own clicks.
        MouseArea {
            id: toggleArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: notch.toggleExpanded()
            onContainsMouseChanged: notch.hovered = containsMouse
        }

        // ── Collapsed view ─────────────────────────────────────────────────
        Item {
            id: collapsedContent
            anchors.fill: parent
            visible: !notch.expanded
            opacity: notch.expanded ? 0 : 1
            Behavior on opacity {
                NumberAnimation { duration: notch.fastDuration }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 13
                anchors.rightMargin: 10
                spacing: 7

                NotchCharacterIcon {
                    Layout.preferredWidth: 22
                    Layout.preferredHeight: 22
                    Layout.alignment: Qt.AlignVCenter
                    size: 22
                    accent: notch.stateAccent
                    muted: !notch.connected
                    reducedMotion: notch.reducedMotion
                }

                Text {
                    Layout.alignment: Qt.AlignVCenter
                    text: "KIVO"
                    color: Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                    font.letterSpacing: 2.0
                }

                Rectangle {
                    Layout.preferredWidth: visible ? 1 : 0
                    Layout.preferredHeight: 13
                    Layout.alignment: Qt.AlignVCenter
                    color: Theme.divider
                    visible: notch.visualState !== "idle"
                }

                Waveform {
                    Layout.preferredWidth: visible ? 32 : 0
                    Layout.preferredHeight: 18
                    Layout.alignment: Qt.AlignVCenter
                    visible: notch.visualState === "listening" || notch.visualState === "speaking"
                    active: visible
                    live: notch.ipc && (notch.audioState === "listening"
                        || notch.audioState === "speaking" || notch.audioState === "capturing")
                    intensity: 0.5
                    barCount: 5
                    color: notch.stateAccent
                    reducedMotion: notch.reducedMotion
                }

                Row {
                    Layout.preferredWidth: visible ? 16 : 0
                    Layout.preferredHeight: 12
                    Layout.alignment: Qt.AlignVCenter
                    visible: notch.visualState === "thinking"
                    spacing: 3

                    Repeater {
                        model: 3
                        Rectangle {
                            width: 3
                            height: 3
                            radius: 1.5
                            color: notch.stateAccent
                            opacity: 0.35
                            SequentialAnimation on opacity {
                                running: notch.visualState === "thinking" && !notch.reducedMotion
                                loops: Animation.Infinite
                                PauseAnimation { duration: index * 130 }
                                NumberAnimation { from: 0.3; to: 1.0; duration: 300 }
                                NumberAnimation { from: 1.0; to: 0.3; duration: 300 }
                            }
                        }
                    }
                }

                Text {
                    Layout.fillWidth: visible
                    Layout.alignment: Qt.AlignVCenter
                    text: notch.collapsedLabel
                    color: notch.visualState === "idle" ? Theme.textSecondary : Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 10
                    font.weight: Font.Medium
                    font.letterSpacing: 0.2
                    elide: Text.ElideRight
                    maximumLineCount: 1
                }

                Text {
                    Layout.preferredWidth: visible ? implicitWidth : 0
                    Layout.alignment: Qt.AlignVCenter
                    visible: notch.visualState === "driving" || notch.visualState === "stopping"
                    text: notch.elapsedText
                    color: Theme.textMuted
                    font.family: Theme.monoFamily
                    font.pixelSize: 9
                }

                StopButton {
                    Layout.preferredWidth: visible ? implicitWidth : 0
                    Layout.preferredHeight: visible ? implicitHeight : 0
                    Layout.alignment: Qt.AlignVCenter
                    visible: notch.visualState === "driving" && notch.cancellable
                    compact: true
                    available: notch.cancellable
                    busy: notch.stopping
                    reducedMotion: notch.reducedMotion
                    onClicked: notch.requestStop()
                }

                StatusIndicator {
                    Layout.preferredWidth: 8
                    Layout.preferredHeight: 8
                    Layout.alignment: Qt.AlignVCenter
                    color: notch.stateAccent
                    pulse: notch.statePulse
                    reducedMotion: notch.reducedMotion
                }
            }
        }

        // ── Expanded view ──────────────────────────────────────────────────
        Item {
            id: expandedContent
            anchors.fill: parent
            anchors.margins: 16
            visible: notch.expanded
            opacity: notch.expanded ? 1 : 0

            Behavior on opacity {
                NumberAnimation { duration: notch.fastDuration }
            }

            RowLayout {
                id: header
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: 34
                spacing: 9

                NotchCharacterIcon {
                    Layout.preferredWidth: 28
                    Layout.preferredHeight: 28
                    Layout.alignment: Qt.AlignVCenter
                    size: 28
                    accent: notch.stateAccent
                    muted: !notch.connected
                    reducedMotion: notch.reducedMotion
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 0

                    Text {
                        text: "KIVO"
                        color: Theme.textPrimary
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        font.letterSpacing: 2.4
                    }
                    Text {
                        text: "Dynamic Notch"
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 8
                        font.letterSpacing: 0.9
                    }
                }

                Rectangle {
                    Layout.preferredWidth: statusPillText.implicitWidth + 16
                    Layout.preferredHeight: 20
                    Layout.alignment: Qt.AlignVCenter
                    radius: 10
                    color: Qt.rgba(notch.stateAccent.r, notch.stateAccent.g,
                                   notch.stateAccent.b, 0.10)
                    border.width: 1
                    border.color: Qt.rgba(notch.stateAccent.r, notch.stateAccent.g,
                                          notch.stateAccent.b, 0.28)

                    Text {
                        id: statusPillText
                        anchors.centerIn: parent
                        text: notch.stateLabel
                        color: notch.stateAccent
                        font.family: Theme.fontFamily
                        font.pixelSize: 9
                        font.weight: Font.Medium
                        font.letterSpacing: 0.4
                    }
                }

                Rectangle {
                    id: pinButton
                    Layout.preferredWidth: 36
                    Layout.preferredHeight: 22
                    Layout.alignment: Qt.AlignVCenter
                    radius: 11
                    color: pinMouse.containsMouse
                        ? Theme.surfaceHover
                        : (notch.pinned
                            ? Qt.rgba(notch.stateAccent.r, notch.stateAccent.g,
                                      notch.stateAccent.b, 0.12)
                            : "transparent")
                    border.width: 1
                    border.color: notch.pinned ? Theme.accentSoft : Theme.border

                    Text {
                        anchors.centerIn: parent
                        text: notch.pinned ? "PINNED" : "PIN"
                        color: notch.pinned ? Theme.accentStrong : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 7
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.7
                    }

                    MouseArea {
                        id: pinMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: notch.togglePinned()
                    }
                }
            }

            Rectangle {
                id: headerDivider
                anchors.top: header.bottom
                anchors.topMargin: 10
                anchors.left: parent.left
                anchors.right: parent.right
                height: 1
                color: Theme.divider
            }

            Column {
                id: body
                anchors.top: headerDivider.bottom
                anchors.topMargin: 10
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: 8

                RowLayout {
                    width: parent.width
                    spacing: 10

                    Rectangle {
                        Layout.preferredWidth: visible ? 28 : 0
                        Layout.preferredHeight: visible ? 28 : 0
                        Layout.alignment: Qt.AlignTop
                        visible: notch.transientIconVisible
                        radius: 14
                        color: Qt.rgba(notch.stateAccent.r, notch.stateAccent.g,
                                       notch.stateAccent.b, 0.12)
                        border.width: 1
                        border.color: Qt.rgba(notch.stateAccent.r, notch.stateAccent.g,
                                              notch.stateAccent.b, 0.30)

                        Text {
                            anchors.centerIn: parent
                            text: notch.stateGlyph
                            color: notch.stateAccent
                            font.family: Theme.fontFamily
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Text {
                            Layout.fillWidth: true
                            text: notch.expandedTitle
                            color: Theme.textPrimary
                            font.family: Theme.fontFamily
                            font.pixelSize: 18
                            font.weight: Font.DemiBold
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }

                        Text {
                            Layout.fillWidth: true
                            visible: text.length > 0
                            text: notch.expandedDetail
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            lineHeight: 1.15
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                    }
                }

                Waveform {
                    width: parent.width
                    height: 34
                    visible: notch.visualState === "listening" || notch.visualState === "speaking"
                    active: visible
                    live: notch.ipc && (notch.audioState === "listening"
                        || notch.audioState === "speaking" || notch.audioState === "capturing")
                    intensity: 0.62
                    barCount: 13
                    color: notch.stateAccent
                    reducedMotion: notch.reducedMotion
                }

                Text {
                    width: parent.width
                    visible: (notch.visualState === "listening" || notch.visualState === "speaking")
                        && text.length > 0
                    text: notch.audioNote
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 9
                    wrapMode: Text.WordWrap
                }

                TaskProgress {
                    width: parent.width
                    visible: notch.visualState === "driving" || notch.visualState === "stopping"
                    currentStep: notch.taskCurrentStep
                    totalSteps: notch.taskTotalSteps
                    steps: notch.taskSteps
                    status: notch.taskStatus
                    elapsedText: notch.elapsedText
                    reducedMotion: notch.reducedMotion
                    accent: Theme.accent
                    danger: Theme.danger
                }

                Row {
                    width: parent.width
                    spacing: 10
                    visible: notch.visualState === "driving" || notch.visualState === "stopping"

                    StopButton {
                        compact: false
                        available: notch.cancellable
                        busy: notch.stopping
                        reducedMotion: notch.reducedMotion
                        onClicked: notch.requestStop()
                    }

                    Text {
                        width: parent.width - 106
                        anchors.verticalCenter: parent.verticalCenter
                        text: notch.stopping
                            ? (notch.stopWaitSeconds >= 5
                                ? "Stop requested. Waiting for a safe step boundary…"
                                : "Stop requested. Waiting for backend confirmation…")
                            : (notch.cancellable
                                ? "Stops the active task at the next safe step."
                                : "No cancellable task is active.")
                        color: notch.stopping ? Theme.warning : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 9
                        wrapMode: Text.WordWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                }

                // ── Push-to-Talk audio capture control ────────────────────────────────
                Rectangle {
                    width: parent.width
                    height: 44
                    visible: notch.visualState !== "driving" && notch.visualState !== "stopping" && notch.visualState !== "offline"
                    radius: 12
                    color: notch.audioState === "capturing"
                        ? Qt.rgba(Theme.danger.r, Theme.danger.g, Theme.danger.b, 0.12)
                        : Theme.surfaceSunken
                    border.width: 1
                    border.color: notch.audioState === "capturing" ? Theme.danger : Theme.divider

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10
                        spacing: 8

                        Rectangle {
                            id: pttButton
                            Layout.preferredWidth: pttBtnLabel.implicitWidth + 24
                            Layout.preferredHeight: 28
                            radius: 8
                            color: {
                                if (!notch.connected)
                                    return Theme.surfaceSunken
                                if (notch.audioState === "capturing")
                                    return Theme.danger
                                return pttMouseArea.pressed
                                    ? Theme.accent
                                    : (pttMouseArea.containsMouse ? Theme.surfaceHover : Theme.surfaceRaised)
                            }
                            border.width: 1
                            border.color: notch.audioState === "capturing" ? Theme.danger : Theme.border

                            Row {
                                anchors.centerIn: parent
                                spacing: 5

                                Rectangle {
                                    width: 6
                                    height: 6
                                    radius: 3
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: notch.audioState === "capturing" ? "#FFFFFF" : Theme.mint

                                    SequentialAnimation on opacity {
                                        running: notch.audioState === "capturing"
                                        loops: Animation.Infinite
                                        NumberAnimation { from: 1.0; to: 0.2; duration: 350 }
                                        NumberAnimation { from: 0.2; to: 1.0; duration: 350 }
                                    }
                                }

                                Text {
                                    id: pttBtnLabel
                                    text: notch.audioState === "capturing" ? "Release to Send" : "Hold to Speak"
                                    color: notch.audioState === "capturing" ? "#FFFFFF" : Theme.textPrimary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                    font.weight: Font.Medium
                                }
                            }

                            MouseArea {
                                id: pttMouseArea
                                anchors.fill: parent
                                enabled: notch.connected
                                hoverEnabled: true
                                onPressed: notch.startPushToTalk()
                                onReleased: notch.stopPushToTalk()
                                onCanceled: notch.cancelPushToTalk()
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1

                            Text {
                                text: {
                                    if (notch.audioState === "capturing")
                                        return "Recording (" + notch.pttElapsed.toFixed(1) + "s)"
                                    if (notch.lastCaptureDuration.length > 0)
                                        return "Captured " + notch.lastCaptureDuration + "s audio"
                                    if (notch.audioState === "unavailable")
                                        return "No microphone available"
                                    return "Push-to-Talk"
                                }
                                color: notch.audioState === "capturing" ? Theme.danger : Theme.textPrimary
                                font.family: Theme.fontFamily
                                font.pixelSize: 9
                                font.weight: Font.Medium
                            }

                            Text {
                                text: {
                                    if (notch.audioState === "capturing")
                                        return "Audio stream active"
                                    if (notch.ipc && notch.ipc.audioDeviceName.length > 0)
                                        return notch.ipc.audioDeviceName
                                    return "Press and hold button to record"
                                }
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 8
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }

                        Rectangle {
                            Layout.preferredWidth: 46
                            Layout.preferredHeight: 24
                            visible: notch.audioState === "capturing"
                            radius: 6
                            color: pttCancelMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceSunken
                            border.width: 1
                            border.color: Theme.border

                            Text {
                                anchors.centerIn: parent
                                text: "Cancel"
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 8
                            }

                            MouseArea {
                                id: pttCancelMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                onClicked: notch.cancelPushToTalk()
                            }
                        }
                    }
                }

                Rectangle {
                    width: parent.width
                    height: visible ? transcriptText.implicitHeight + 14 : 0
                    visible: notch.transcriptSummary.length > 0
                        && notch.visualState !== "driving"
                        && notch.visualState !== "stopping"
                        && notch.visualState !== "offline"
                    radius: 9
                    color: Theme.surfaceSunken
                    border.width: 1
                    border.color: Theme.divider

                    Text {
                        id: transcriptText
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10
                        text: notch.transcriptSummary
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        wrapMode: Text.WordWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                }

                Text {
                    width: parent.width
                    visible: notch.contextLine.length > 0
                        && notch.visualState !== "offline"
                    text: "Context · " + notch.contextLine
                    color: Theme.textFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 9
                    elide: Text.ElideRight
                }

                Text {
                    width: parent.width
                    visible: notch.visualState === "offline"
                    text: notch.ipc && notch.ipc.retryInSeconds > 0
                        ? "Retrying in " + notch.ipc.retryInSeconds + "s"
                        : "Reconnecting automatically"
                    color: Theme.textMuted
                    font.family: Theme.monoFamily
                    font.pixelSize: 9
                }
            }

            RowLayout {
                id: footer
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                height: 14

                Text {
                    Layout.fillWidth: true
                    text: notch.backendMeta
                    color: Theme.textFaint
                    font.family: Theme.monoFamily
                    font.pixelSize: 8
                    elide: Text.ElideRight
                }

                Text {
                    text: notch.footerHint
                    color: Theme.textFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 8
                }
            }
        }
    }
}
