import QtQuick
import QtQuick.Layouts
import "../styles"

/*
    TelemetrySummary — 3x2 compact hardware telemetry grid for the expanded notch.
*/
Rectangle {
    id: root
    property var ipc: null
    property bool reducedMotion: false

    width: parent ? parent.width : 488
    implicitHeight: 74
    color: "transparent"

    readonly property bool linkStale: !ipc || ipc.telemetryLinkStale

    GridLayout {
        anchors.fill: parent
        columns: 3
        rows: 2
        rowSpacing: 6
        columnSpacing: 6

        // ── 1. CPU ─────────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 8
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 1

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: "CPU"
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontMicro
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.5
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: root.ipc && root.ipc.telemetryCpuStale ? "stale" : ""
                        color: Theme.warning
                        font.family: Theme.fontFamily
                        font.pixelSize: 8
                        visible: text.length > 0
                    }
                }

                Text {
                    text: {
                        if (!root.ipc || !root.ipc.telemetryAvailable) return "—"
                        if (root.ipc.telemetryCpuStatus === "warming_up") return "warming up"
                        if (root.ipc.telemetryCpuStatus !== "ok") return "—"
                        return Math.round(root.ipc.telemetryCpuPercent) + "%"
                    }
                    color: root.linkStale || (root.ipc && root.ipc.telemetryCpuStale) ? Theme.textMuted : Theme.textPrimary
                    font.family: Theme.monoFamily
                    font.pixelSize: Theme.fontSmall
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                }
            }
        }

        // ── 2. Memory ──────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 8
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 1

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: "RAM"
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontMicro
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.5
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: root.ipc && root.ipc.telemetryMemoryStale ? "stale" : ""
                        color: Theme.warning
                        font.family: Theme.fontFamily
                        font.pixelSize: 8
                        visible: text.length > 0
                    }
                }

                Text {
                    text: {
                        if (!root.ipc || !root.ipc.telemetryAvailable) return "—"
                        if (root.ipc.telemetryMemoryStatus !== "ok") return "—"
                        var usedGb = (root.ipc.telemetryMemoryUsedBytes / 1073741824).toFixed(1)
                        var totGb = Math.round(root.ipc.telemetryMemoryTotalBytes / 1073741824)
                        return Math.round(root.ipc.telemetryMemoryPercent) + "% (" + usedGb + "/" + totGb + "G)"
                    }
                    color: root.linkStale || (root.ipc && root.ipc.telemetryMemoryStale) ? Theme.textMuted : Theme.textPrimary
                    font.family: Theme.monoFamily
                    font.pixelSize: Theme.fontSmall
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                }
            }
        }

        // ── 3. GPU ─────────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 8
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 1

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: "GPU"
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontMicro
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.5
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: root.ipc && root.ipc.telemetryGpuStale ? "stale" : ""
                        color: Theme.warning
                        font.family: Theme.fontFamily
                        font.pixelSize: 8
                        visible: text.length > 0
                    }
                }

                Text {
                    text: {
                        if (!root.ipc || !root.ipc.telemetryAvailable) return "—"
                        if (root.ipc.telemetryGpuStatus === "unsupported") return "None"
                        if (root.ipc.telemetryGpuStatus !== "ok") return "—"
                        return Math.round(root.ipc.telemetryGpuPercent) + "% (" + Math.round(root.ipc.telemetryGpuTempC) + "°C)"
                    }
                    color: root.linkStale || (root.ipc && root.ipc.telemetryGpuStale) ? Theme.textMuted : Theme.textPrimary
                    font.family: Theme.monoFamily
                    font.pixelSize: Theme.fontSmall
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                }
            }
        }

        // ── 4. Temperature (CPU Package) ───────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 8
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 1

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: "TEMP"
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontMicro
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.5
                    }
                    Item { Layout.fillWidth: true }
                }

                Text {
                    text: {
                        if (!root.ipc || !root.ipc.telemetryAvailable) return "—"
                        var temps = root.ipc.telemetryTemperatures
                        if (!temps || temps.length === 0) return "—"
                        var cpuTemp = null
                        for (var i = 0; i < temps.length; i++) {
                            if (temps[i].id === "cpu_package" && temps[i].status === "ok") {
                                cpuTemp = temps[i].celsius
                                break
                            }
                        }
                        if (cpuTemp === null && temps[0].status === "ok") cpuTemp = temps[0].celsius
                        if (cpuTemp === null) return "—"
                        return Math.round(cpuTemp) + "°C Package"
                    }
                    color: root.linkStale ? Theme.textMuted : Theme.textPrimary
                    font.family: Theme.monoFamily
                    font.pixelSize: Theme.fontSmall
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                }
            }
        }

        // ── 5. Battery ─────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 8
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 1

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: "BATTERY"
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontMicro
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.5
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: root.ipc && root.ipc.telemetryBatteryStale ? "stale" : ""
                        color: Theme.warning
                        font.family: Theme.fontFamily
                        font.pixelSize: 8
                        visible: text.length > 0
                    }
                }

                Text {
                    text: {
                        if (!root.ipc || !root.ipc.telemetryAvailable) return "—"
                        if (root.ipc.telemetryBatteryStatus === "unsupported" || !root.ipc.telemetryBatteryPresent) return "No Battery"
                        if (root.ipc.telemetryBatteryStatus !== "ok") return "—"
                        var stateStr = root.ipc.telemetryBatteryState
                        var shortState = stateStr === "charging" ? "⚡" : (stateStr === "discharging" ? "↓" : "")
                        return root.ipc.telemetryBatteryPercent + "% " + shortState
                    }
                    color: root.linkStale || (root.ipc && root.ipc.telemetryBatteryStale) ? Theme.textMuted : Theme.textPrimary
                    font.family: Theme.monoFamily
                    font.pixelSize: Theme.fontSmall
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                }
            }
        }

        // ── 6. Storage ─────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 8
            color: Theme.surfaceSunken
            border.width: 1
            border.color: Theme.divider

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 1

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: "DISK"
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontMicro
                        font.weight: Font.DemiBold
                        font.letterSpacing: 0.5
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: root.ipc && root.ipc.telemetryStorageStale ? "stale" : ""
                        color: Theme.warning
                        font.family: Theme.fontFamily
                        font.pixelSize: 8
                        visible: text.length > 0
                    }
                }

                Text {
                    text: {
                        if (!root.ipc || !root.ipc.telemetryAvailable) return "—"
                        if (root.ipc.telemetryStorageStatus !== "ok") return "—"
                        var usedGb2 = Math.round(root.ipc.telemetryStorageUsedBytes / 1073741824)
                        var totGb2 = Math.round(root.ipc.telemetryStorageTotalBytes / 1073741824)
                        return Math.round(root.ipc.telemetryStoragePercent) + "% (" + usedGb2 + "/" + totGb2 + "G)"
                    }
                    color: root.linkStale || (root.ipc && root.ipc.telemetryStorageStale) ? Theme.textMuted : Theme.textPrimary
                    font.family: Theme.monoFamily
                    font.pixelSize: Theme.fontSmall
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                }
            }
        }
    }
}
