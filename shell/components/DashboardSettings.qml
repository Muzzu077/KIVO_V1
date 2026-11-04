import QtQuick
import "../styles"

/*
    KIVO Dashboard — Settings page.

    Renders preference sections.  Only controls backed by an existing backend
    or persisted configuration mechanism are interactive; everything else is
    presented as unavailable state.
*/
Item {
    id: page

    property var ipc: null

    property bool reducedMotion: false
    property bool autonomousDrive: false

    // The notch reads KIVO_NOTCH_REDUCED_MOTION from the environment at launch.
    // There is no persisted preference backend yet, so these are display-only
    // until a real settings mechanism exists.
    function setReducedMotion(value) {
        reducedMotion = value
    }
    function setAutonomousDrive(value) {
        autonomousDrive = value
    }

    Column {
        anchors.fill: parent
        width: parent.width
        height: parent.height
        spacing: 10

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Appearance"; font.pixelSize: Theme.fontCaption; color: Theme.textMuted; font.bold: true }
                Text { text: "Dashboard and notch theme follow the shared KIVO design system."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Dynamic Notch animations"; font.pixelSize: Theme.fontBody; color: Theme.textPrimary }
                Text { text: "Reduced motion is applied from the KIVO_NOTCH_REDUCED_MOTION environment setting."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Voice and audio"; font.pixelSize: Theme.fontBody; color: Theme.textPrimary }
                Text { text: "Audio input is not available in the current backend build."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Autonomous drive"; font.pixelSize: Theme.fontBody; color: Theme.textPrimary }
                Text { text: "Autonomous actions require the existing backend confirmation and action policy."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Model and provider configuration"; font.pixelSize: Theme.fontBody; color: Theme.textPrimary }
                Text { text: "Provider and model settings are managed outside the frontend."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Privacy"; font.pixelSize: Theme.fontBody; color: Theme.textPrimary }
                Text { text: "Voiceprints, memory and credentials are never surfaced in the dashboard."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }

        Item {
            width: parent.width
            height: 56
            Rectangle { anchors.fill: parent; radius: 12; color: Theme.surfaceSunken }
            Column {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                x: 14
                y: 10
                width: parent.width - 28
                spacing: 2
                Text { text: "Permissions"; font.pixelSize: Theme.fontBody; color: Theme.textPrimary }
                Text { text: "Permission grants are evaluated by the backend action policy."; font.pixelSize: Theme.fontCaption; color: Theme.textSecondary }
            }
        }
    }
}
