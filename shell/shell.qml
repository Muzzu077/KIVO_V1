import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import "components"
import "services"
import "styles"

/*
    KIVO Stage 3 + Stage 4 — Dynamic Notch and Dashboard frontend.

    Run from the repository root with:
        qs -p shell

    Environment options:
        KIVO_SOCKET=/path/to/kivo.sock       override the IPC socket
        KIVO_NOTCH_LAYER=overlay             use the wlr overlay layer
        KIVO_NOTCH_REDUCED_MOTION=1          disable decorative motion
        KIVO_NOTCH_AUTO_COLLAPSE=0           keep the notch open after success
        KIVO_NOTCH_FOCUS_ON_EXPAND=0        never take keyboard focus on expand

    The notch panel is a fixed transparent layer surface with an input mask
    shaped like the visible capsule.  The dashboard is an independent floating
    panel; both read the same KivoIpc scope and never talk to the backend
    directly.
*/
ShellRoot {
    id: shell

    // ── Dashboard lifecycle ─────────────────────────────────────────────────
    // Independent of the notch: opening the dashboard never forces the notch
    // expanded or collapses it.
    property bool dashboardOpen: false

    function openDashboard() {
        shell.dashboardOpen = true
    }

    function closeDashboard() {
        shell.dashboardOpen = false
    }

    function toggleDashboard() {
        shell.dashboardOpen = !shell.dashboardOpen
    }

    function envString(name, fallback) {
        var value = Quickshell.env(name)
        if (value === undefined || value === null)
            return fallback
        var text = String(value)
        return text.length > 0 ? text : fallback
    }

    readonly property string layerSetting: envString("KIVO_NOTCH_LAYER", "top")
    readonly property bool overlayLayer: layerSetting === "overlay"
    readonly property bool reducedMotion: envString("KIVO_NOTCH_REDUCED_MOTION", "0") === "1"
    readonly property bool autoCollapseOnSuccess: envString("KIVO_NOTCH_AUTO_COLLAPSE", "1") !== "0"
    readonly property bool focusOnExpand: envString("KIVO_NOTCH_FOCUS_ON_EXPAND", "1") !== "0"
    readonly property string socketOverride: envString("KIVO_SOCKET", "")
    readonly property string layerNamespace: envString("KIVO_NOTCH_NAMESPACE", "kivo-notch")

    KivoIpc {
        id: kivoIpc
        socketPathOverride: shell.socketOverride
    }

    PanelWindow {
        id: panel

        // Only the top edge is anchored.  wlr-layer-shell centres the
        // unanchored horizontal axis, which keeps the notch at top-centre.
        anchors.top: true
        margins.top: Theme.marginTop

        implicitWidth: 600
        implicitHeight: 380
        color: "transparent"

        // The notch must never reserve desktop space or shift niri's layout.
        exclusiveZone: 0
        exclusionMode: ExclusionMode.Ignore

        // Layer-shell OnDemand only grants keyboard focus after a click, so
        // the collapsed notch never steals focus from niri.  When collapsed
        // and not hovered we explicitly release focus, which keeps Escape
        // available to the focused application.
        focusable: (shell.focusOnExpand
            && (notch.expanded || notch.hovered))

        WlrLayershell.namespace: shell.layerNamespace
        WlrLayershell.layer: shell.overlayLayer ? WlrLayer.Overlay : WlrLayer.Top
        WlrLayershell.keyboardFocus: (shell.focusOnExpand
            && (notch.expanded || notch.hovered))
            ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

        // Transparent pixels are not part of the input region.
        mask: Region {
            item: notch.inputRegion
        }

        DynamicNotch {
            id: notch
            anchors.top: parent.top
            anchors.horizontalCenter: parent.horizontalCenter
            ipc: kivoIpc
            reducedMotion: shell.reducedMotion
            autoCollapseOnSuccess: shell.autoCollapseOnSuccess
        }
    }

    PanelWindow {
        id: dashboardWindow

        // Independent desktop panel for the KIVO dashboard.  It never moves
        // the notch and never changes the notch's own input mask.
        anchors.top: true
        margins.top: 0

        implicitWidth: 880
        implicitHeight: 620
        color: "transparent"

        exclusiveZone: 0
        exclusionMode: ExclusionMode.Ignore

        focusable: true

        WlrLayershell.namespace: "kivo-dashboard"
        WlrLayershell.layer: WlrLayer.Top
        WlrLayershell.keyboardFocus: shell.dashboardOpen
            ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

        visible: shell.dashboardOpen

        mask: Region {
            item: dash
        }

        KivoDashboard {
            id: dash
            anchors.top: parent.top
            anchors.horizontalCenter: parent.horizontalCenter
            ipc: kivoIpc
        }
    }

    /*
        A deliberately small control surface for verification and future
        keybindings.  It exposes only notch presentation, dashboard control and
        the existing cancel_active_task intent; it cannot invoke arbitrary
        backend methods.
    */
    IpcHandler {
        id: control
        target: "kivo-notch"

        function expand(): void { notch.expand() }
        function collapse(): void { notch.collapse() }
        function toggle(): void { notch.toggleExpanded() }
        function pin(value: bool): void {
            notch.pinned = value
            if (value)
                notch.expanded = true
        }
        function requestStop(): bool { return notch.requestStop() }
        function state(): string { return notch.visualState }
        function expanded(): bool { return notch.expanded }
        function connected(): bool { return notch.connected }

        function openDashboard(): void { shell.dashboardOpen = true }
        function closeDashboard(): void { shell.dashboardOpen = false }
        function toggleDashboard(): void { shell.dashboardOpen = !shell.dashboardOpen }
        function dashboardOpen(): bool { return shell.dashboardOpen }

        function startCapture(): void { notch.startPushToTalk() }
        function stopCapture(): void { notch.stopPushToTalk() }
        function cancelCapture(): void { notch.cancelPushToTalk() }
        function audioState(): string { return notch.audioState }

        function snapshot(): string {
            return JSON.stringify({
                state: notch.visualState,
                expanded: notch.expanded,
                pinned: notch.pinned,
                hovered: notch.hovered,
                connected: notch.connected,
                connectionState: notch.connectionState,
                stale: notch.stale,
                activeTask: notch.activeTask,
                driveActive: notch.driveActive,
                cancellable: notch.cancellable,
                stopping: notch.stopping,
                taskGoal: notch.taskGoal,
                driveGoal: notch.driveGoal,
                driveAction: notch.driveAction,
                taskStatus: notch.taskStatus,
                taskCurrentStep: notch.taskCurrentStep,
                taskTotalSteps: notch.taskTotalSteps,
                elapsedText: notch.elapsedText,
                transientState: notch.transientState,
                transientTitle: notch.transientTitle,
                transientDetail: notch.transientDetail,
                reducedMotion: notch.reducedMotion,
                audioState: notch.audioState,
                audioCapturing: notch.audioState === "capturing",
                panelWidth: panel.width,
                panelHeight: panel.height,
                panelImplicitWidth: panel.implicitWidth,
                panelImplicitHeight: panel.implicitHeight,
                notchX: notch.x,
                notchY: notch.y,
                notchWidth: notch.width,
                notchHeight: notch.height,
                dashboardOpen: shell.dashboardOpen,
                dashboardWindowWidth: dashboardWindow.width,
                dashboardWindowHeight: dashboardWindow.height,
                lastCancellation: notch.ipc ? notch.ipc.lastCancellation : null,
                invalidMessages: notch.ipc ? notch.ipc.invalidMessages : 0,
                staleEvents: notch.ipc ? notch.ipc.staleEvents : 0,
                sequenceGaps: notch.ipc ? notch.ipc.sequenceGaps : 0,
                retryInSeconds: notch.ipc ? notch.ipc.retryInSeconds : 0,
                lastProtocolWarning: notch.ipc ? notch.ipc.lastProtocolWarning : "",
                lastError: notch.ipc ? notch.ipc.lastError : ""
            })
        }

        signal stateChanged(state: string)
    }

    Connections {
        target: notch
        function onVisualStateChanged() {
            control.stateChanged(notch.visualState)
        }
    }
}
