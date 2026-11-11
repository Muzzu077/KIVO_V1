import QtQuick
import QtQuick.Layouts
import "../styles"

/*
    NotchMediaDrawer — media playback controls and synchronized lyrics.

    Position is interpolated locally at 10 Hz while playing to prevent
    D-Bus/IPC socket flooding, and resets on track change or seek.
*/
Item {
    id: drawer

    property var ipc: null
    property bool reducedMotion: false
    property bool showLyrics: false

    property real interpolatedPosition: 0.0
    property real lastUpdateMs: 0.0
    property bool isSeeking: false
    property real seekRatio: 0.0

    readonly property bool hasMedia: ipc && ipc.mediaActive
    readonly property bool isPlaying: ipc && ipc.mediaPlaying
    readonly property real duration: ipc ? Math.max(0, ipc.mediaDuration) : 0
    readonly property real currentDisplayPosition: isSeeking ? (seekRatio * duration) : interpolatedPosition
    readonly property string title: ipc && ipc.mediaTitle ? ipc.mediaTitle : "Nothing playing"
    readonly property string artist: ipc && ipc.mediaArtist ? ipc.mediaArtist : ""
    readonly property string album: ipc && ipc.mediaAlbum ? ipc.mediaAlbum : ""
    readonly property string artUrl: ipc && ipc.mediaArtUrl ? ipc.mediaArtUrl : ""
    // Defense in depth: the backend only emits approved local/internal
    // artwork references, and QML never fetches a remote URL.
    readonly property string safeArtUrl: {
        if (!artUrl)
            return ""
        if (artUrl.indexOf("file://") === 0
                || artUrl.indexOf("qrc:/kivo/artwork/") === 0
                || artUrl.indexOf("image://kivo-artwork/") === 0)
            return artUrl
        return ""
    }

    function formatTime(seconds) {
        if (!seconds || isNaN(seconds) || seconds < 0)
            return "0:00"
        var s = Math.floor(seconds)
        var mins = Math.floor(s / 60)
        var secs = s % 60
        return mins + ":" + (secs < 10 ? "0" : "") + secs
    }

    Timer {
        id: interpolatorTimer
        interval: 100
        repeat: true
        running: drawer.isPlaying && !drawer.isSeeking
        onTriggered: {
            if (drawer.duration > 0 && drawer.interpolatedPosition < drawer.duration) {
                var elapsed = (Date.now() - drawer.lastUpdateMs) / 1000.0
                drawer.interpolatedPosition = Math.min(drawer.duration, (drawer.ipc ? drawer.ipc.mediaPosition : 0) + Math.max(0, elapsed))
            }
        }
    }

    Connections {
        target: drawer.ipc
        function onMediaPlaybackUpdated(data) {
            if (!drawer.isSeeking) {
                drawer.lastUpdateMs = Date.now()
                drawer.interpolatedPosition = drawer.ipc ? drawer.ipc.mediaPosition : 0
            }
        }
    }

    implicitWidth: parent ? parent.width : 500
    implicitHeight: mainColumn.implicitHeight + 12

    ColumnLayout {
        id: mainColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 6
        spacing: 8

        // ── Top Row: Album Art + Track Info + Controls ─────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            // Album Artwork
            Rectangle {
                Layout.preferredWidth: 48
                Layout.preferredHeight: 48
                radius: 8
                color: Theme.surfaceSunken
                border.width: 1
                border.color: Theme.border
                clip: true

                Image {
                    id: artImage
                    anchors.fill: parent
                    source: drawer.safeArtUrl
                    sourceSize.width: 48
                    sourceSize.height: 48
                    asynchronous: true
                    fillMode: Image.PreserveAspectCrop
                    visible: status === Image.Ready
                }

                Text {
                    anchors.centerIn: parent
                    visible: !drawer.safeArtUrl || artImage.status !== Image.Ready
                    text: "♫"
                    color: Theme.accentSoft
                    font.pixelSize: 18
                }
            }

            // Track & Artist Typography
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Text {
                    Layout.fillWidth: true
                    text: drawer.title
                    color: Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontBody
                    font.bold: true
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: drawer.artist.length > 0 ? (drawer.album.length > 0 ? (drawer.artist + " — " + drawer.album) : drawer.artist) : "No active player"
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontCaption
                    elide: Text.ElideRight
                }
            }

            // Playback Action Buttons
            RowLayout {
                spacing: 4

                // Previous
                Rectangle {
                    Layout.preferredWidth: 30
                    Layout.preferredHeight: 30
                    radius: 15
                    color: prevMouse.containsMouse ? Theme.surfaceHover : "transparent"

                    Text {
                        anchors.centerIn: parent
                        text: "⏮"
                        color: Theme.textSecondary
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: prevMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: if (drawer.ipc) drawer.ipc.mediaPrevious()
                    }
                }

                // Play / Pause Toggle
                Rectangle {
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 34
                    radius: 17
                    color: ppMouse.containsMouse ? Theme.accent : Theme.mint

                    Text {
                        anchors.centerIn: parent
                        text: drawer.isPlaying ? "⏸" : "▶"
                        color: Theme.surfaceSunken
                        font.pixelSize: 13
                        font.bold: true
                    }

                    MouseArea {
                        id: ppMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: if (drawer.ipc) drawer.ipc.mediaPlayPause()
                    }
                }

                // Next
                Rectangle {
                    Layout.preferredWidth: 30
                    Layout.preferredHeight: 30
                    radius: 15
                    color: nextMouse.containsMouse ? Theme.surfaceHover : "transparent"

                    Text {
                        anchors.centerIn: parent
                        text: "⏭"
                        color: Theme.textSecondary
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: nextMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: if (drawer.ipc) drawer.ipc.mediaNext()
                    }
                }

                // Lyrics Toggle Button
                Rectangle {
                    Layout.preferredWidth: 30
                    Layout.preferredHeight: 30
                    radius: 6
                    color: drawer.showLyrics ? Theme.accentDim : (lyrMouse.containsMouse ? Theme.surfaceHover : "transparent")
                    border.width: drawer.showLyrics ? 1 : 0
                    border.color: Theme.accentSoft

                    Text {
                        anchors.centerIn: parent
                        text: "LRC"
                        color: drawer.showLyrics ? Theme.mint : Theme.textMuted
                        font.family: Theme.monoFamily
                        font.pixelSize: 9
                        font.bold: true
                    }

                    MouseArea {
                        id: lyrMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            drawer.showLyrics = !drawer.showLyrics
                            if (drawer.showLyrics && drawer.ipc && (!drawer.ipc.mediaLyrics || drawer.ipc.mediaLyrics.track !== drawer.title))
                                drawer.ipc.mediaGetLyrics()
                        }
                    }
                }
            }
        }

        // ── Progress Bar & Time Labels ────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            visible: drawer.duration > 0

            Text {
                text: drawer.formatTime(drawer.currentDisplayPosition)
                color: Theme.textMuted
                font.family: Theme.monoFamily
                font.pixelSize: Theme.fontCaption - 1
            }

            Rectangle {
                id: progressTrack
                Layout.fillWidth: true
                Layout.preferredHeight: 4
                radius: 2
                color: Theme.surfaceSunken
                border.width: 1
                border.color: Theme.border

                Rectangle {
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    radius: parent.radius
                    color: Theme.accent
                    width: drawer.duration > 0
                        ? Math.max(0, Math.min(parent.width, (drawer.currentDisplayPosition / drawer.duration) * parent.width))
                        : 0
                }

                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    preventStealing: true

                    function updateSeek(mouseX) {
                        var ratio = Math.max(0.0, Math.min(1.0, mouseX / width))
                        drawer.seekRatio = ratio
                    }

                    onPressed: function(mouse) {
                        drawer.isSeeking = true
                        updateSeek(mouse.x)
                    }

                    onPositionChanged: function(mouse) {
                        if (drawer.isSeeking)
                            updateSeek(mouse.x)
                    }

                    onReleased: function(mouse) {
                        if (drawer.isSeeking) {
                            updateSeek(mouse.x)
                            var targetSec = drawer.seekRatio * drawer.duration
                            drawer.interpolatedPosition = targetSec
                            drawer.lastUpdateMs = Date.now()
                            drawer.isSeeking = false
                            if (drawer.ipc)
                                drawer.ipc.mediaSeek(targetSec)
                        }
                    }

                    onCanceled: {
                        drawer.isSeeking = false
                    }
                }
            }

            Text {
                text: drawer.formatTime(drawer.duration)
                color: Theme.textMuted
                font.family: Theme.monoFamily
                font.pixelSize: Theme.fontCaption - 1
            }
        }

        // ── Synced Lyrics Drawer ──────────────────────────────────────────
        Rectangle {
            id: lyricsBox
            Layout.fillWidth: true
            Layout.preferredHeight: drawer.showLyrics ? 120 : 0
            visible: drawer.showLyrics
            color: Theme.surfaceSunken
            radius: 8
            border.width: 1
            border.color: Theme.border
            clip: true

            Behavior on Layout.preferredHeight {
                NumberAnimation { duration: drawer.reducedMotion ? 0 : 200; easing.type: Easing.OutCubic }
            }

            ListView {
                id: lyricsList
                anchors.fill: parent
                anchors.margins: 8
                spacing: 6
                model: (drawer.ipc && drawer.ipc.mediaLyrics && drawer.ipc.mediaLyrics.lines) ? drawer.ipc.mediaLyrics.lines : []
                clip: true

                delegate: Item {
                    width: lyricsList.width
                    height: lineText.implicitHeight

                    readonly property bool isCurrent: {
                        if (!modelData || modelData.time_seconds === undefined)
                            return false
                        var lineTime = modelData.time_seconds
                        var nextLine = lyricsList.model[index + 1]
                        var nextTime = nextLine ? nextLine.time_seconds : 99999
                        return drawer.currentDisplayPosition >= lineTime && drawer.currentDisplayPosition < nextTime
                    }

                    onIsCurrentChanged: {
                        if (isCurrent && !lyricsList.moving)
                            lyricsList.positionViewAtIndex(index, ListView.Center)
                    }

                    Text {
                        id: lineText
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: Math.min(parent.width, implicitWidth)
                        text: modelData.text || ""
                        color: isCurrent ? Theme.mint : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: isCurrent ? Theme.fontBody : Theme.fontCaption
                        font.bold: isCurrent
                        elide: Text.ElideRight
                    }
                }

                Text {
                    anchors.centerIn: parent
                    visible: lyricsList.count === 0
                    text: drawer.hasMedia ? "No synchronized lyrics found for this track" : "Play music to see lyrics"
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontCaption
                }
            }
        }
    }
}
