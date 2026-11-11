import QtQuick
import Quickshell
import Quickshell.Io

/*
    KivoIpc — the only bridge between the notch and the Python backend.

    Transport: the existing Stage 2 Unix socket at
    $XDG_RUNTIME_DIR/kivo/kivo.sock, protocol version 1, one JSON object per
    newline.  This component never invents state: every value shown by the UI
    comes from get_status, get_current_task or a real backend event.
*/
Scope {
    id: ipc

    // ── Configuration ──────────────────────────────────────────────────────
    property string socketPathOverride: ""

    readonly property string defaultSocketPath: {
        var dir = Quickshell.env("XDG_RUNTIME_DIR")
        if (!dir)
            return ""
        var value = String(dir)
        if (value.length === 0)
            return ""
        if (value.charAt(value.length - 1) === "/")
            value = value.substring(0, value.length - 1)
        return value + "/kivo/kivo.sock"
    }

    readonly property string socketPath: socketPathOverride.length > 0
        ? socketPathOverride : defaultSocketPath

    // ── Connection state ───────────────────────────────────────────────────
    property bool connected: false
    property bool subscribed: false
    property bool stale: true
    property string connectionState: "disconnected"
    property string lastError: ""
    property int reconnectAttempt: 0
    property int retryInSeconds: 0
    property bool everConnected: false

    // ── Authoritative snapshots ────────────────────────────────────────────
    property var status: null
    property var task: null
    property bool activeTask: false
    property var drive: ({
        active: false,
        goal: "",
        action: "",
        reason: "",
        elapsed: 0
    })
    property var screenContext: ({
        cleared: true,
        active_app: "",
        active_title: "",
        is_terminal: false,
        is_browser: false,
        source: "",
        age_seconds: 0
    })
    property string lastTranscript: ""
    property string lastAssistantResponse: ""

    // ── Protocol health ────────────────────────────────────────────────────
    property int lastSequence: 0
    property int sequenceGaps: 0
    property int staleEvents: 0
    property int invalidMessages: 0
    property string lastProtocolWarning: ""

    // ── Cancellation ───────────────────────────────────────────────────────
    property bool cancelling: false
    property var lastCancellation: null

    // ── Derived status fields (defensive; never trust types blindly) ───────
    readonly property string serviceVersion: _text(status, "version", "")
    readonly property int protocolVersion: _number(status, "protocol_version", 1)
    readonly property int backendPid: _number(status, "pid", 0)
    readonly property string assistantState: _text(status, "assistant_state", "idle")
    readonly property string assistantDetail: _text(status, "assistant_detail", "")
    readonly property string audioState: _text(status, "audio_state", "unavailable")
    readonly property string audioDetail: _text(status, "audio_detail", "")
    readonly property string agentState: _text(status, "agent_state", "idle")
    readonly property string agentDetail: _text(status, "agent_detail", "")
    readonly property real uptimeSeconds: _number(status, "uptime_seconds", 0)

    readonly property bool driveActive: drive && drive.active === true
    readonly property string driveGoal: _text(drive, "goal", "")
    readonly property string driveAction: _text(drive, "action", "")
    readonly property string driveReason: _text(drive, "reason", "")
    readonly property real driveElapsed: _number(drive, "elapsed", 0)

    readonly property string taskGoal: _text(task, "goal", "")
    readonly property string taskStatus: _text(task, "status", "")
    readonly property string taskSummary: _text(task, "summary", "")
    readonly property int taskCurrentStep: _number(task, "current_step", 0)
    readonly property int taskTotalSteps: _number(task, "total_steps", 0)
    readonly property bool taskSuccess: task && task.success === true
    readonly property var taskSteps: task && Array.isArray(task.steps) ? task.steps : []
    readonly property real taskCreatedAt: _number(task, "created_at", 0)

    // Task history returned by get_dashboard_data / task_history_updated.
    // Presentation only: no private memory or tool internals are surfaced.
    property var taskHistory: []

    readonly property bool taskCancellable: connected && activeTask
        && (taskStatus === "pending" || taskStatus === "running")
        && !cancelling

    // ── Audio properties ───────────────────────────────────────────────────
    property var audioData: null
    property bool audioCapturing: audioState === "capturing"
    property real audioCaptureDuration: 0.0
    property string audioDeviceName: ""
    property var lastAudioCapture: null
    property string lastAudioError: ""

    // ── STT properties ─────────────────────────────────────────────────────
    property var sttStatus: null
    property bool sttTranscribing: false
    property var lastTranscription: null
    property var transcriptionHistory: []

    // ── Conversation properties ────────────────────────────────────────────
    property var conversationStatus: null
    property string conversationState: "idle"
    property bool conversationCloudEnabled: false
    property string conversationModel: ""
    property string streamingResponseAccumulated: ""
    property var conversationHistory: []
    property var conversationAvailableModels: []

    // ── Agent tool properties ──────────────────────────────────────────────
    property string activeAgentTool: ""
    property string activeAgentToolStatus: ""
    property var toolExecutionLog: []
    property var pendingCommandApproval: null

    // ── Media properties (Stage 12.1 MPRIS) ─────────────────────────────────
    property var mediaData: null
    property var mediaLyrics: null
    readonly property bool mediaActive: mediaData && mediaData.available === true && mediaData.status !== "Unavailable"
    readonly property bool mediaPlaying: mediaData && mediaData.status === "Playing"
    readonly property string mediaTitle: _text(mediaData, "title", "")
    readonly property string mediaArtist: _text(mediaData, "artist", "")
    readonly property string mediaAlbum: _text(mediaData, "album", "")
    readonly property string mediaArtUrl: _text(mediaData, "art_url", "")
    readonly property real mediaPosition: _number(mediaData, "position_seconds", 0)
    readonly property real mediaDuration: _number(mediaData, "duration_seconds", 0)
    readonly property string mediaStatus: _text(mediaData, "status", "Unavailable")
    readonly property string mediaPlayerName: _text(mediaData, "player_name", "")
    readonly property bool mediaCanSeek: _bool(mediaData, "can_seek", true)
    readonly property bool mediaLyricsAvailable: _bool(mediaData, "lyrics_available", false)

    // ── Telemetry properties (Stage 12.2) ──────────────────────────────────
    property var telemetrySnapshot: null
    property int telemetrySequence: 0
    property bool telemetryResyncPending: true
    readonly property bool telemetryLinkStale: telemetryResyncPending || !connected
    readonly property bool telemetryAvailable: telemetrySnapshot !== null
    readonly property string telemetryServiceState: _text(telemetrySnapshot, "service_state", "starting")

    readonly property var telemetryCpu: telemetrySnapshot ? telemetrySnapshot.cpu : null
    readonly property string telemetryCpuStatus: _text(telemetryCpu && telemetryCpu.utilization, "status", "unavailable")
    readonly property real telemetryCpuPercent: _number(telemetryCpu && telemetryCpu.utilization, "percent", 0.0)
    readonly property bool telemetryCpuStale: _bool(telemetryCpu && telemetryCpu.utilization, "stale", false)
    readonly property real telemetryCpuFreqMhz: _number(telemetryCpu && telemetryCpu.frequency, "mhz", 0.0)
    readonly property real telemetryCpuFreqMaxMhz: _number(telemetryCpu && telemetryCpu.frequency, "max_mhz", 0.0)

    readonly property var telemetryMemory: telemetrySnapshot ? telemetrySnapshot.memory : null
    readonly property string telemetryMemoryStatus: _text(telemetryMemory, "status", "unavailable")
    readonly property real telemetryMemoryPercent: _number(telemetryMemory, "percent", 0.0)
    readonly property real telemetryMemoryUsedBytes: _number(telemetryMemory, "used_bytes", 0)
    readonly property real telemetryMemoryTotalBytes: _number(telemetryMemory, "total_bytes", 0)
    readonly property bool telemetryMemoryStale: _bool(telemetryMemory, "stale", false)

    readonly property var telemetryGpu: telemetrySnapshot ? telemetrySnapshot.gpu : null
    readonly property string telemetryGpuStatus: _text(telemetryGpu, "status", "unavailable")
    readonly property string telemetryGpuName: _text(telemetryGpu, "name", "")
    readonly property real telemetryGpuPercent: _number(telemetryGpu, "utilization_percent", 0.0)
    readonly property real telemetryGpuTempC: _number(telemetryGpu, "temperature_c", 0.0)
    readonly property real telemetryGpuMemUsedBytes: _number(telemetryGpu, "memory_used_bytes", 0)
    readonly property real telemetryGpuMemTotalBytes: _number(telemetryGpu, "memory_total_bytes", 0)
    readonly property real telemetryGpuMemPercent: _number(telemetryGpu, "memory_percent", 0.0)
    readonly property bool telemetryGpuStale: _bool(telemetryGpu, "stale", false)

    readonly property var telemetryTemperatures: telemetrySnapshot && Array.isArray(telemetrySnapshot.temperatures) ? telemetrySnapshot.temperatures : []

    readonly property var telemetryBattery: telemetrySnapshot ? telemetrySnapshot.battery : null
    readonly property string telemetryBatteryStatus: _text(telemetryBattery, "status", "unavailable")
    readonly property bool telemetryBatteryPresent: _bool(telemetryBattery, "present", false)
    readonly property int telemetryBatteryPercent: _number(telemetryBattery, "percent", 0)
    readonly property string telemetryBatteryState: _text(telemetryBattery, "state", "unknown")
    readonly property var telemetryBatteryAcOnline: telemetryBattery && typeof telemetryBattery.ac_online === "boolean" ? telemetryBattery.ac_online : null
    readonly property bool telemetryBatteryStale: _bool(telemetryBattery, "stale", false)

    readonly property var telemetryStorage: telemetrySnapshot ? telemetrySnapshot.storage : null
    readonly property string telemetryStorageStatus: _text(telemetryStorage, "status", "unavailable")
    readonly property real telemetryStoragePercent: _number(telemetryStorage, "percent", 0.0)
    readonly property real telemetryStorageUsedBytes: _number(telemetryStorage, "used_bytes", 0)
    readonly property real telemetryStorageTotalBytes: _number(telemetryStorage, "total_bytes", 0)
    readonly property bool telemetryStorageStale: _bool(telemetryStorage, "stale", false)

    // ── Microphone properties (Stage 12.2 Default Mic Mute) ────────────────
    property var micState: null
    property int micRevision: 0
    property bool micResyncPending: true
    property bool micTransitioning: false
    readonly property bool micLinkStale: micResyncPending || !connected
    readonly property bool micAvailable: micState !== null
    readonly property string micStateString: _text(micState, "state", "unavailable")
    readonly property var micMuted: micState && typeof micState.muted === "boolean" ? micState.muted : null
    readonly property bool micVerified: _bool(micState, "verified", false)
    readonly property var micSource: micState ? micState.source : null
    readonly property string micSourceName: _text(micSource, "name", "")
    readonly property string micSourceDescription: _text(micSource, "description", "Default microphone")
    readonly property int micSourceId: _number(micSource, "id", 0)
    readonly property string micErrorCode: _text(micState, "error_code", "")
    readonly property string micErrorMessage: _text(micState, "error_message", "")
    readonly property bool micStale: _bool(micState, "stale", false)

    // ── Signals ────────────────────────────────────────────────────────────
    signal statusUpdated()
    signal taskUpdated()
    signal driveUpdated()
    signal connectionChanged()
    signal cancellationFinished(var result)
    signal taskFinished(var result)
    signal driveFinished(var result)
    signal backendError(string message)
    signal protocolWarning(string message)
    signal transcriptChanged()
    signal audioCaptureStarted(string deviceName)
    signal audioCaptureStopped()
    signal audioCaptureCompleted(var metadata)
    signal audioCaptureError(string error)
    signal audioDevicesUpdated(var devices)
    signal sttStatusUpdated(var status)
    signal sttTranscriptionStarted(string captureId)
    signal sttTranscriptionCompleted(var result)
    signal sttTranscriptionFailed(string error)
    signal conversationStatusUpdated(var status)
    signal conversationUserMessage(var message)
    signal conversationResponseStarted(string messageId, string conversationId)
    signal conversationResponseDelta(string delta, string accumulated)
    signal conversationResponseCompleted(var message)
    signal conversationResponseFailed(string error)
    signal conversationCancelled()
    signal agentToolStarted(string name, string callId)
    signal agentToolCompleted(var result)
    signal commandApprovalRequested(var request)
    signal commandApprovalResolved(var result)
    signal mediaPlaybackUpdated(var data)
    signal mediaLyricsUpdated(var data)
    signal telemetryUpdated(var snapshot)
    signal micStateUpdated(var state)

    // ── Internal state ─────────────────────────────────────────────────────
    property bool _wantConnected: false
    property int _nextRequestId: 0
    property var _pending: ({})
    property int _protocolErrorBurst: 0
    property string _cancelRequestId: ""

    readonly property var _emptyDrive: ({
        active: false,
        goal: "",
        action: "",
        reason: "",
        elapsed: 0
    })

    // ── Helpers ────────────────────────────────────────────────────────────
    function _isObject(value) {
        return value !== null && typeof value === "object" && !Array.isArray(value)
    }

    function _text(object, key, fallback) {
        if (!_isObject(object))
            return fallback
        var value = object[key]
        return typeof value === "string" ? value : fallback
    }

    function _number(object, key, fallback) {
        if (!_isObject(object))
            return fallback
        var value = object[key]
        return typeof value === "number" && isFinite(value) ? value : fallback
    }

    function _bool(object, key, fallback) {
        if (!_isObject(object))
            return fallback
        return typeof object[key] === "boolean" ? object[key] : fallback
    }

    function _copy(object) {
        var result = {}
        if (!_isObject(object))
            return result
        for (var key in object)
            result[key] = object[key]
        return result
    }

    function _merge(base, patch) {
        var result = _copy(base)
        if (!_isObject(patch))
            return result
        for (var key in patch)
            result[key] = patch[key]
        return result
    }

    function _setConnectionState(value) {
        if (connectionState === value)
            return
        connectionState = value
        connectionChanged()
    }

    function _warn(message) {
        lastProtocolWarning = String(message)
        console.warn("[KivoIpc] " + lastProtocolWarning)
        protocolWarning(lastProtocolWarning)
    }

    function _invalid(message) {
        invalidMessages += 1
        _protocolErrorBurst += 1
        _warn(message)
        if (_protocolErrorBurst >= 20) {
            _protocolErrorBurst = 0
            _warn("too many invalid messages; restarting the IPC connection")
            _restartConnection()
        }
    }

    // ── Connection lifecycle ───────────────────────────────────────────────
    Component.onCompleted: {
        if (socketPath.length === 0) {
            lastError = "XDG_RUNTIME_DIR is not available; cannot locate the KIVO socket."
            _setConnectionState("disconnected")
            return
        }
        connect()
    }

    function connect() {
        _wantConnected = true
        if (socket.connected)
            return
        if (socketPath.length === 0) {
            lastError = "No KIVO socket path is available."
            _setConnectionState("disconnected")
            return
        }
        retryInSeconds = 0
        _setConnectionState(reconnectAttempt > 0 ? "reconnecting" : "connecting")
        socket.path = socketPath
        socket.connected = true
        connectTimeout.restart()
    }

    function disconnect() {
        _wantConnected = false
        connectTimeout.stop()
        retryTimer.stop()
        countdownTimer.stop()
        retryInSeconds = 0
        if (socket.connected)
            socket.connected = false
        connected = false
        subscribed = false
        stale = true
        cancelling = false
        _setConnectionState("disconnected")
    }

    function _restartConnection() {
        _wantConnected = true
        connectTimeout.stop()
        if (socket.connected)
            socket.connected = false
        _scheduleReconnect()
    }

    function _scheduleReconnect() {
        if (!_wantConnected)
            return
        if (retryTimer.running)
            return
        reconnectAttempt += 1
        var delay = Math.min(1000 * Math.pow(2, Math.min(reconnectAttempt - 1, 4)), 10000)
        retryInSeconds = Math.ceil(delay / 1000)
        retryTimer.interval = delay
        retryTimer.restart()
        countdownTimer.restart()
        _setConnectionState("reconnecting")
    }

    function _onConnected() {
        connectTimeout.stop()
        retryTimer.stop()
        countdownTimer.stop()
        retryInSeconds = 0
        connected = true
        everConnected = true
        subscribed = false
        reconnectAttempt = 0
        retryInSeconds = 0
        lastError = ""
        _protocolErrorBurst = 0
        lastSequence = 0
        sequenceGaps = 0
        staleEvents = 0
        telemetrySequence = 0
        micRevision = 0
        telemetryResyncPending = true
        micResyncPending = true
        micTransitioning = false
        _setConnectionState("connected")

        // The order is intentional: refresh authoritative state first, then
        // subscribe to future changes.
        request("get_status", null, function(error, result) {
            if (error) {
                lastError = error.error.message
                backendError(lastError)
                return
            }
            if (!_applyStatus(result))
                _invalid("get_status returned an invalid result")
        })
        request("get_current_task", null, function(error, result) {
            if (error) {
                lastError = error.error.message
                backendError(lastError)
                return
            }
            if (!_applyTaskResult(result))
                _invalid("get_current_task returned an invalid result")
        })
        telemetryGetSnapshot()
        micGetState()
        _subscribe()
    }

    function _onDisconnected() {
        var wasConnected = connected
        connected = false
        subscribed = false
        stale = true
        cancelling = false
        _cancelRequestId = ""
        telemetryResyncPending = true
        micResyncPending = true
        micTransitioning = false
        cancelTimeout.stop()
        if (wasConnected) {
            lastError = "Connection to the KIVO backend was lost."
            _failPending("The KIVO backend disconnected.")
            _setConnectionState("reconnecting")
            _scheduleReconnect()
        } else {
            _failPending("The KIVO backend is not reachable.")
            _setConnectionState("reconnecting")
            _scheduleReconnect()
        }
    }

    function _subscribe() {
        request("subscribe_events", {}, function(error, result) {
            if (error) {
                lastError = error.error.message
                backendError(lastError)
                subscribeRetry.restart()
                return
            }
            if (!_isObject(result) || result.subscribed !== true) {
                _invalid("subscribe_events returned an invalid result")
                subscribeRetry.restart()
                return
            }
            subscribed = true
        })
    }

    function _failPending(reason) {
        var ids = Object.keys(_pending)
        for (var i = 0; i < ids.length; i++) {
            var id = ids[i]
            var callback = _pending[id]
            delete _pending[id]
            if (typeof callback === "function") {
                callback({
                    ok: false,
                    error: { code: "connection_lost", message: String(reason) }
                }, null)
            }
        }
    }

    // ── Requests ───────────────────────────────────────────────────────────
    function request(operation, params, callback) {
        if (!socket.connected) {
            if (typeof callback === "function") {
                callback({
                    ok: false,
                    error: {
                        code: "not_connected",
                        message: "The KIVO backend is not connected."
                    }
                }, null)
            }
            return ""
        }

        _nextRequestId += 1
        var id = "kivo-" + _nextRequestId
        var payload = {
            version: 1,
            type: "request",
            id: id,
            operation: String(operation)
        }
        if (params !== null && params !== undefined)
            payload.params = params

        if (typeof callback === "function")
            _pending[id] = callback
        else
            _pending[id] = true

        socket.write(JSON.stringify(payload) + "\n")
        socket.flush()
        return id
    }

    function cancelActiveTask() {
        if (cancelling)
            return false
        if (!connected) {
            lastCancellation = {
                ok: false,
                cancelled: false,
                message: "The KIVO backend is not connected."
            }
            cancellationFinished(lastCancellation)
            return false
        }
        if (!activeTask) {
            lastCancellation = {
                ok: false,
                cancelled: false,
                message: "No active task to cancel."
            }
            cancellationFinished(lastCancellation)
            return false
        }

        cancelling = true
        var requestId = ""
        requestId = request("cancel_active_task", null, function(error, result) {
            if (requestId !== _cancelRequestId)
                return
            _cancelRequestId = ""
            cancelTimeout.stop()
            cancelling = false
            if (error) {
                lastCancellation = {
                    ok: false,
                    cancelled: false,
                    message: error.error.message || "The cancellation request failed."
                }
            } else if (!_isObject(result) || typeof result.cancelled !== "boolean") {
                lastCancellation = {
                    ok: false,
                    cancelled: false,
                    message: "The backend returned an invalid cancellation result."
                }
            } else {
                lastCancellation = {
                    ok: true,
                    cancelled: result.cancelled,
                    message: typeof result.message === "string"
                        ? result.message
                        : (result.cancelled ? "Cancelling…" : "No active task to cancel.")
                }
            }
            cancellationFinished(lastCancellation)
        })

        if (requestId.length === 0) {
            cancelling = false
            return false
        }
        _cancelRequestId = requestId
        cancelTimeout.restart()
        return true
    }

    // ── Dashboard data ─────────────────────────────────────────────────────
    function getDashboardData(callback) {
        var result = request("get_dashboard_data", null, function(error, payload) {
            if (error) {
                if (typeof callback === "function")
                    callback(error, null)
                return
            }
            if (_isObject(payload) && Array.isArray(payload.task_history)) {
                taskHistory = payload.task_history
            } else {
                taskHistory = []
            }
            if (_isObject(payload) && _isObject(payload.audio)) {
                audioData = payload.audio
                if (payload.audio.device)
                    audioDeviceName = _text(payload.audio.device, "name", "")
                if (payload.audio.last_capture)
                    lastAudioCapture = payload.audio.last_capture
            }
            if (_isObject(payload) && _isObject(payload.stt)) {
                sttStatus = payload.stt
            }
            if (_isObject(payload) && Array.isArray(payload.transcriptions)) {
                transcriptionHistory = payload.transcriptions
            }
            if (_isObject(payload) && _isObject(payload.conversation)) {
                conversationStatus = payload.conversation
                conversationState = _text(payload.conversation, "state", "idle")
                conversationCloudEnabled = payload.conversation.cloud_enabled === true
            }
            if (_isObject(payload) && Array.isArray(payload.conversation_history)) {
                conversationHistory = payload.conversation_history
            }
            if (_isObject(payload) && _isObject(payload.media)) {
                mediaData = payload.media
            }
            if (typeof callback === "function")
                callback(null, payload)
        })
        return result
    }

    // ── Audio control ──────────────────────────────────────────────────────
    function audioGetStatus(callback) {
        return request("audio_get_status", null, function(error, result) {
            if (!error && _isObject(result)) {
                audioData = result
                if (result.device)
                    audioDeviceName = _text(result.device, "name", "")
                if (result.last_capture)
                    lastAudioCapture = result.last_capture
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function audioListDevices(callback) {
        return request("audio_list_devices", null, function(error, result) {
            if (!error && _isObject(result)) {
                if (Array.isArray(result.devices))
                    audioDevicesUpdated(result.devices)
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function audioSelectDevice(deviceName, callback) {
        return request("audio_select_device", { "device": String(deviceName) }, function(error, result) {
            if (!error && _isObject(result) && result.ok) {
                if (result.device)
                    audioDeviceName = _text(result.device, "name", "")
                audioGetStatus()
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function audioStartCapture(callback) {
        if (!connected) {
            if (typeof callback === "function")
                callback({ error: { message: "Backend offline" } }, null)
            return ""
        }
        return request("audio_start_capture", null, function(error, result) {
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function audioStopCapture(callback) {
        if (!connected) {
            if (typeof callback === "function")
                callback({ error: { message: "Backend offline" } }, null)
            return ""
        }
        return request("audio_stop_capture", null, function(error, result) {
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function audioCancelCapture(callback) {
        if (!connected)
            return ""
        return request("audio_cancel_capture", null, callback)
    }

    // ── STT control ────────────────────────────────────────────────────────
    function sttGetStatus(callback) {
        return request("stt_get_status", null, function(error, result) {
            if (!error && _isObject(result)) {
                sttStatus = result
                sttTranscribing = (result.state === "transcribing")
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function sttGetHistory(limit, callback) {
        var params = { limit: typeof limit === "number" ? limit : 10 }
        return request("stt_get_history", params, function(error, result) {
            if (!error && _isObject(result) && Array.isArray(result.history)) {
                transcriptionHistory = result.history
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function sttUpdateConfig(cfg, callback) {
        return request("stt_update_config", cfg, function(error, result) {
            if (!error && _isObject(result) && result.status) {
                sttStatus = result.status
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    // ── Conversation control ───────────────────────────────────────────────
    function conversationGetStatus(callback) {
        return request("conversation_get_status", null, function(error, result) {
            if (!error && _isObject(result)) {
                conversationStatus = result
                conversationState = _text(result, "state", "idle")
                conversationCloudEnabled = result.cloud_enabled === true
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function conversationGetHistory(callback) {
        return request("conversation_get_history", null, function(error, result) {
            if (!error && _isObject(result) && Array.isArray(result.history)) {
                conversationHistory = result.history
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function conversationSubmit(text, callback) {
        return request("conversation_submit", { "text": String(text) }, function(error, result) {
            if (!error && _isObject(result) && result.status) {
                conversationStatus = result.status
                conversationState = _text(result.status, "state", "idle")
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function conversationCancel(callback) {
        return request("conversation_cancel", null, callback)
    }

    function conversationClear(callback) {
        return request("conversation_clear", null, function(error, result) {
            if (!error) {
                conversationHistory = []
                streamingResponseAccumulated = ""
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function conversationUpdateConfig(cfg, callback) {
        return request("conversation_update_config", cfg, function(error, result) {
            if (!error && _isObject(result) && result.status) {
                conversationStatus = result.status
                conversationCloudEnabled = result.status.cloud_enabled === true
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function conversationListModels(callback) {
        return request("conversation_list_models", null, function(error, result) {
            if (!error && _isObject(result) && Array.isArray(result.models)) {
                conversationAvailableModels = result.models
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function conversationTestConnection(callback) {
        return request("conversation_test_connection", null, function(error, result) {
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    // ── Command approval control ───────────────────────────────────────────
    function commandResolveApproval(token, approved, turnId, toolCallId, callback) {
        return request("command_resolve_approval", {
            "token": String(token),
            "approved": approved === true,
            "turn_id": String(turnId || ""),
            "tool_call_id": String(toolCallId || "")
        }, function(error, result) {
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function commandGetPendingApprovals(callback) {
        return request("command_get_pending_approvals", null, function(error, result) {
            if (!error && _isObject(result) && Array.isArray(result.pending)) {
                if (result.pending.length > 0)
                    pendingCommandApproval = result.pending[0]
                else
                    pendingCommandApproval = null
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    // ── Media controls (Stage 12.1 MPRIS) ──────────────────────────────────
    function mediaGetStatus(callback) {
        return request("media_get_status", null, function(error, result) {
            if (!error && _isObject(result))
                mediaData = _copy(result)
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function mediaPlay(callback) {
        return request("media_play", null, callback)
    }

    function mediaPause(callback) {
        return request("media_pause", null, callback)
    }

    function mediaPlayPause(callback) {
        return request("media_play_pause", null, callback)
    }

    function mediaNext(callback) {
        return request("media_next", null, callback)
    }

    function mediaPrevious(callback) {
        return request("media_previous", null, callback)
    }

    function mediaSeek(positionSeconds, callback) {
        return request("media_seek", { "position_seconds": Number(positionSeconds) }, callback)
    }

    function mediaGetLyrics(callback) {
        return request("media_get_lyrics", null, function(error, result) {
            if (!error && _isObject(result))
                mediaLyrics = _copy(result)
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    // ── Telemetry & Microphone controls (Stage 12.2) ───────────────────────
    function telemetryGetSnapshot(callback) {
        return request("telemetry_get_snapshot", null, function(error, result) {
            if (!error && _isObject(result)) {
                var seq = _number(result, "sequence", 0)
                if (seq >= telemetrySequence || telemetryResyncPending) {
                    telemetrySnapshot = _copy(result)
                    telemetrySequence = seq
                    telemetryResyncPending = false
                    telemetryUpdated(telemetrySnapshot)
                }
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function micGetState(callback) {
        return request("mic_get_state", null, function(error, result) {
            if (!error && _isObject(result)) {
                var rev = _number(result, "revision", 0)
                if (rev >= micRevision || micResyncPending) {
                    micState = _copy(result)
                    micRevision = rev
                    micResyncPending = false
                    micTransitioning = false
                    micStateUpdated(micState)
                }
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    function micSetMute(muted, callback) {
        micTransitioning = true
        return request("mic_set_mute", { "muted": muted === true }, function(error, result) {
            micTransitioning = false
            if (!error && _isObject(result)) {
                if (_isObject(result.state)) {
                    micState = _copy(result.state)
                    micRevision = _number(result.state, "revision", micRevision)
                    micResyncPending = false
                    micStateUpdated(micState)
                }
            }
            if (typeof callback === "function")
                callback(error, result)
        })
    }

    // ── Incoming messages ──────────────────────────────────────────────────
    function _handleLine(line) {
        if (typeof line !== "string")
            return
        var text = line.trim()
        if (text.length === 0)
            return
        if (text.length > 65536) {
            _invalid("backend message exceeds the 65536-byte frame limit")
            return
        }

        var message
        try {
            message = JSON.parse(text)
        } catch (error) {
            _invalid("backend sent malformed JSON")
            return
        }

        if (!_isObject(message)) {
            _invalid("backend message is not a JSON object")
            return
        }
        if (message.version !== 1) {
            _invalid("backend sent an unsupported protocol version")
            return
        }

        if (message.type === "response") {
            _handleResponse(message)
            return
        }
        if (message.type === "event") {
            _handleEvent(message)
            return
        }
        _invalid("backend sent an unknown message type")
    }

    function _handleResponse(message) {
        var id = message.id
        if (typeof id !== "string" || !(id in _pending)) {
            _warn("ignoring a response with an unknown request id")
            return
        }

        var callback = _pending[id]
        delete _pending[id]
        if (typeof callback !== "function")
            return

        if (message.ok === true) {
            if (!_isObject(message.result)) {
                callback({
                    ok: false,
                    error: {
                        code: "invalid_response",
                        message: "The backend returned a non-object result."
                    }
                }, null)
                return
            }
            callback(null, message.result)
            return
        }

        var detail = _isObject(message.error) ? message.error : {}
        callback({
            ok: false,
            error: {
                code: typeof detail.code === "string" ? detail.code : "backend_error",
                message: typeof detail.message === "string"
                    ? detail.message : "The backend rejected the request."
            }
        }, null)
    }

    function _handleEvent(message) {
        var name = message.event
        if (typeof name !== "string" || name.length === 0) {
            _invalid("backend event has no name")
            return
        }

        var sequence = message.sequence
        if (typeof sequence !== "number" || !isFinite(sequence)
                || Math.floor(sequence) !== sequence || sequence < 1) {
            _invalid("backend event has an invalid sequence")
            return
        }
        if (sequence <= lastSequence) {
            // A duplicate or out-of-order event must not overwrite newer state.
            staleEvents += 1
            return
        }
        if (lastSequence > 0 && sequence > lastSequence + 1) {
            // Sequence gaps are legal (per-client filters), but recording them
            // helps diagnose a lost subscription.
            sequenceGaps += 1
            _warn("event sequence gap: " + lastSequence + " -> " + sequence)
        }
        lastSequence = sequence

        var data = _isObject(message.data) ? message.data : {}

        switch (name) {
        case "assistant_state_changed":
            _applyStateEvent("assistant", data)
            break
        case "audio_state_changed":
            _applyStateEvent("audio", data)
            break
        case "agent_state_changed":
            _applyStateEvent("agent", data)
            break
        case "drive_started":
            _applyDriveStarted(data)
            break
        case "drive_progress":
            _applyDriveProgress(data)
            break
        case "drive_completed":
            _applyDriveFinished(data, "completed")
            break
        case "drive_failed":
            _applyDriveFinished(data, "failed")
            break
        case "drive_cancelled":
            _applyDriveFinished(data, "cancelled")
            break
        case "task_history_updated":
            _applyTaskHistory(data)
            break
        case "screen_context_updated":
            _applyScreenContext(data)
            break
        case "user_transcript":
            _applyTranscript(data, false)
            break
        case "assistant_response":
            _applyTranscript(data, true)
            break
        case "audio_capture_started":
            audioCapturing = true
            audioCaptureDuration = 0.0
            if (data.device_name)
                audioDeviceName = String(data.device_name)
            audioCaptureStarted(audioDeviceName)
            break
        case "audio_capture_stopped":
            audioCapturing = false
            audioCaptureStopped()
            break
        case "audio_capture_completed":
            audioCapturing = false
            lastAudioCapture = _copy(data)
            audioCaptureCompleted(lastAudioCapture)
            break
        case "audio_capture_error":
            audioCapturing = false
            lastAudioError = _text(data, "error", "Audio capture failed")
            audioCaptureError(lastAudioError)
            break
        case "audio_device_changed":
            if (data.name)
                audioDeviceName = String(data.name)
            break
        case "stt_status_changed":
            sttStatus = _copy(data)
            sttTranscribing = (data.state === "transcribing")
            sttStatusUpdated(sttStatus)
            break
        case "stt_transcription_started":
            sttTranscribing = true
            sttTranscriptionStarted(_text(data, "capture_id", ""))
            break
        case "stt_transcription_completed":
            sttTranscribing = false
            lastTranscription = _copy(data)
            sttTranscriptionCompleted(lastTranscription)
            break
        case "stt_transcription_empty":
            sttTranscribing = false
            break
        case "stt_transcription_failed":
            sttTranscribing = false
            sttTranscriptionFailed(_text(data, "error", "Transcription failed"))
            break
        case "conversation_state_changed":
            conversationStatus = _copy(data)
            conversationState = _text(data, "state", "idle")
            conversationCloudEnabled = data.cloud_enabled === true
            conversationStatusUpdated(conversationStatus)
            break
        case "conversation_user_message":
            if (_isObject(data)) {
                var hist = Array.isArray(conversationHistory) ? conversationHistory.slice() : []
                hist.push(_copy(data))
                conversationHistory = hist
                conversationUserMessage(data)
            }
            break
        case "conversation_response_started":
            streamingResponseAccumulated = ""
            conversationState = "thinking"
            conversationResponseStarted(_text(data, "message_id", ""), _text(data, "conversation_id", ""))
            break
        case "conversation_response_delta":
            conversationState = "streaming"
            streamingResponseAccumulated = _text(data, "accumulated", "")
            conversationResponseDelta(_text(data, "delta", ""), streamingResponseAccumulated)
            break
        case "conversation_response_completed":
            conversationState = "idle"
            streamingResponseAccumulated = ""
            if (_isObject(data)) {
                var hist2 = Array.isArray(conversationHistory) ? conversationHistory.slice() : []
                hist2.push(_copy(data))
                conversationHistory = hist2
                conversationResponseCompleted(data)
            }
            break
        case "conversation_response_failed":
            conversationState = "failed"
            streamingResponseAccumulated = ""
            conversationResponseFailed(_text(data, "error", "Response generation failed"))
            break
        case "conversation_cancelled":
            conversationState = "cancelled"
            streamingResponseAccumulated = ""
            conversationCancelled()
            break
        case "agent_tool_started":
            activeAgentTool = _text(data, "tool_name", "")
            activeAgentToolStatus = "running"
            agentToolStarted(activeAgentTool, _text(data, "tool_call_id", ""))
            break
        case "agent_tool_completed":
            activeAgentTool = ""
            activeAgentToolStatus = data.success === true ? "completed" : "failed"
            var tlog = Array.isArray(toolExecutionLog) ? toolExecutionLog.slice() : []
            tlog.push(_copy(data))
            toolExecutionLog = tlog
            agentToolCompleted(data)
            break
        case "command_approval_requested":
            pendingCommandApproval = _copy(data)
            commandApprovalRequested(data)
            break
        case "command_approval_resolved":
            if (pendingCommandApproval && pendingCommandApproval.token === data.token)
                pendingCommandApproval = null
            commandApprovalResolved(data)
            break
        case "media_status_changed":
            if (_isObject(data)) {
                mediaData = _copy(data)
                mediaPlaybackUpdated(mediaData)
            }
            break
        case "media_lyrics_loaded":
            if (_isObject(data)) {
                mediaLyrics = _copy(data)
                mediaLyricsUpdated(mediaLyrics)
            }
            break
        case "telemetry_snapshot_changed":
        case "telemetry.snapshot_changed":
            if (_isObject(data)) {
                var tseq = _number(data, "sequence", 0)
                if (tseq >= telemetrySequence || telemetryResyncPending) {
                    telemetrySnapshot = _copy(data)
                    telemetrySequence = tseq
                    telemetryResyncPending = false
                    telemetryUpdated(telemetrySnapshot)
                }
            }
            break
        case "mic_state_changed":
        case "mic.state_changed":
            if (_isObject(data)) {
                var mrev = _number(data, "revision", 0)
                if (mrev >= micRevision || micResyncPending) {
                    micState = _copy(data)
                    micRevision = mrev
                    micResyncPending = false
                    micTransitioning = false
                    micStateUpdated(micState)
                }
            }
            break
        case "confirmation_required":
            // The backend confirmation gate is intentionally unbound.  Do not
            // render an approval control in this milestone.
            break
        case "error":
            _applyBackendError(data)
            break
        default:
            _warn("ignoring an unknown backend event: " + name)
            break
        }
    }

    // ── Snapshot application ───────────────────────────────────────────────
    function _applyStatus(result) {
        if (!_isObject(result))
            return false
        if (result.service !== "kivo" || result.protocol_version !== 1)
            return false
        if (typeof result.assistant_state !== "string"
                || typeof result.audio_state !== "string"
                || typeof result.agent_state !== "string")
            return false
        if (result.drive !== null && result.drive !== undefined
                && !_isObject(result.drive))
            return false

        status = _copy(result)
        activeTask = result.active_task === true
        drive = _isObject(result.drive) ? _copy(result.drive) : _copy(_emptyDrive)
        if (_isObject(result.media))
            mediaData = _copy(result.media)
        if (_isObject(result.telemetry)) {
            var tseq2 = _number(result.telemetry, "sequence", 0)
            if (tseq2 >= telemetrySequence || telemetryResyncPending) {
                telemetrySnapshot = _copy(result.telemetry)
                telemetrySequence = tseq2
                telemetryResyncPending = false
                telemetryUpdated(telemetrySnapshot)
            }
        }
        if (_isObject(result.microphone)) {
            var mrev2 = _number(result.microphone, "revision", 0)
            if (mrev2 >= micRevision || micResyncPending) {
                micState = _copy(result.microphone)
                micRevision = mrev2
                micResyncPending = false
                micTransitioning = false
                micStateUpdated(micState)
            }
        }
        stale = false
        statusUpdated()
        driveUpdated()
        taskUpdated()
        return true
    }

    function _applyTaskResult(result) {
        if (!_isObject(result))
            return false
        if (result.task === null) {
            task = null
            activeTask = false
            taskUpdated()
            return true
        }
        if (!_isObject(result.task) || typeof result.task.task_id !== "string")
            return false
        task = _copy(result.task)
        activeTask = true
        taskUpdated()
        return true
    }

    function _applyStateEvent(kind, data) {
        var state = data.state
        if (typeof state !== "string" || state.length === 0) {
            _invalid(kind + " event has no valid state")
            return
        }
        var detail = typeof data.detail === "string" ? data.detail : ""
        var patch = {}
        if (kind === "assistant") {
            patch.assistant_state = state
            patch.assistant_detail = detail
        } else if (kind === "audio") {
            patch.audio_state = state
            patch.audio_detail = detail
        } else {
            patch.agent_state = state
            patch.agent_detail = detail
        }
        status = _merge(status, patch)
        statusUpdated()
    }

    function _applyDriveStarted(data) {
        if (typeof data.goal !== "string" && typeof data.action !== "string") {
            _invalid("drive_started event has no goal or action")
            return
        }
        drive = {
            active: true,
            goal: _text(data, "goal", driveGoal),
            action: _text(data, "action", "Starting…"),
            reason: "",
            elapsed: 0
        }
        driveUpdated()
    }

    function _applyDriveProgress(data) {
        if (typeof data.action !== "string" && typeof data.goal !== "string") {
            _invalid("drive_progress event has no action or goal")
            return
        }
        drive = {
            active: true,
            goal: _text(data, "goal", driveGoal),
            action: _text(data, "action", driveAction),
            reason: "",
            elapsed: driveElapsed
        }
        driveUpdated()
    }

    function _applyDriveFinished(data, outcome) {
        var reason = _text(data, "reason", "")
        var goal = _text(data, "goal", driveGoal)
        drive = {
            active: false,
            goal: goal,
            action: "",
            reason: reason,
            elapsed: 0
        }
        driveUpdated()
        driveFinished({
            outcome: outcome,
            goal: goal,
            reason: reason
        })
    }

    function _applyTaskHistory(data) {
        if (typeof data.active_task !== "boolean") {
            _invalid("task_history_updated event has no active_task flag")
            return
        }

        var history = Array.isArray(data.task_history) ? data.task_history : []
        taskHistory = history

        var nextTask = task
        if (data.current_task === null) {
            nextTask = null
        } else if (_isObject(data.current_task)
                && typeof data.current_task.task_id === "string") {
            nextTask = _copy(data.current_task)
        } else if (data.current_task !== undefined) {
            _invalid("task_history_updated event has an invalid current_task")
            return
        }

        activeTask = data.active_task
        task = nextTask
        taskUpdated()

        if (data.reason === "finished" && !activeTask && nextTask === null) {
            var history = Array.isArray(data.task_history) ? data.task_history : []
            if (history.length > 0 && _isObject(history[0]))
                _emitTaskFinished(history[0])
        }
    }

    function _emitTaskFinished(snapshot) {
        var statusValue = _text(snapshot, "status", "")
        var success = snapshot.success === true
        var summary = _text(snapshot, "summary", "")
        var goal = _text(snapshot, "goal", "")
        var outcome = "unknown"
        if (statusValue === "cancelled")
            outcome = "cancelled"
        else if (statusValue === "completed" && success)
            outcome = "completed"
        else if (statusValue === "failed")
            outcome = "failed"
        else
            return
        taskFinished({
            outcome: outcome,
            goal: goal,
            summary: summary
        })
    }

    function _applyScreenContext(data) {
        if (typeof data.cleared !== "boolean") {
            _invalid("screen_context_updated event has no cleared flag")
            return
        }
        screenContext = _copy(data)
    }

    function _applyTranscript(data, assistant) {
        var text = typeof data.text === "string" ? data.text
            : (typeof data.content === "string" ? data.content : "")
        if (text.length === 0)
            return
        if (assistant)
            lastAssistantResponse = text
        else
            lastTranscript = text
        transcriptChanged()
    }

    function _applyBackendError(data) {
        var message = _text(data, "message", "The KIVO backend reported an error.")
        lastError = message
        backendError(message)
    }

    // ── Timers and socket ──────────────────────────────────────────────────
    Socket {
        id: socket
        path: ipc.socketPath

        parser: SplitParser {
            onRead: function(line) {
                ipc._handleLine(line)
            }
        }

        onConnectionStateChanged: {
            if (socket.connected)
                ipc._onConnected()
            else
                ipc._onDisconnected()
        }

        onError: function(error) {
            ipc.lastError = "Could not connect to " + ipc.socketPath + "."
            if (!socket.connected)
                ipc._scheduleReconnect()
        }
    }

    Timer {
        id: connectTimeout
        interval: 4000
        repeat: false
        onTriggered: {
            if (!socket.connected) {
                ipc.lastError = "Timed out connecting to the KIVO backend."
                ipc._restartConnection()
            }
        }
    }

    Timer {
        id: retryTimer
        repeat: false
        onTriggered: ipc.connect()
    }

    Timer {
        id: countdownTimer
        interval: 1000
        repeat: true
        running: ipc.retryInSeconds > 0
        onTriggered: {
            if (ipc.retryInSeconds > 0)
                ipc.retryInSeconds -= 1
            if (ipc.retryInSeconds <= 0)
                running = false
        }
    }

    Timer {
        id: subscribeRetry
        interval: 2000
        repeat: false
        onTriggered: {
            if (ipc.connected && !ipc.subscribed)
                ipc._subscribe()
        }
    }

    Timer {
        id: cancelTimeout
        interval: 12000
        repeat: false
        onTriggered: {
            if (!ipc.cancelling)
                return
            // A late response must not overwrite the failure result.
            ipc._cancelRequestId = ""
            ipc.cancelling = false
            ipc.lastCancellation = {
                ok: false,
                cancelled: false,
                message: "The backend did not confirm the stop request."
            }
            ipc.cancellationFinished(ipc.lastCancellation)
        }
    }
}
