# KIVO — Stage 12.1 MPRIS Media & Synchronized Lyrics Specification

## 1. Overview & Objectives

**Stage 12.1** introduces universal media playback control and synchronized `.lrc` lyric display to the KIVO Dynamic Notch, inspired by NotchPixel's implementation, tailored for Arch Linux and Wayland.

### Core Objectives
1. Automatically discover active desktop media players on the user D-Bus session (`Spotify`, `Chromium`, `Firefox`, `VLC`, `MPV`) via `playerctl`.
2. Extract playback metadata (track title, artist, album, artwork URL, duration, position, status).
3. Provide universal playback controls (Play, Pause, Play/Pause, Next, Previous, Seek).
4. Asynchronously fetch and parse synchronized `.lrc` lyrics from the public **LRCLIB** database (`https://lrclib.net`).
5. Render a compact now-playing pill in the resting notch, and a rich media drawer (`NotchMediaDrawer.qml`) with album art, scrubber, controls, and smooth-scrolling synced lyrics in the expanded notch.

---

## 2. IPC Protocol Specification

### 2.1 Allowed Request Operations (`kivo/ipc/protocol.py`)
Add the following operations to `REQUEST_OPERATIONS`:
- `media_get_status` / `media.get_status`
- `media_play` / `media.play`
- `media_pause` / `media.pause`
- `media_play_pause` / `media.play_pause`
- `media_next` / `media.next`
- `media_previous` / `media.previous`
- `media_seek` / `media.seek`
- `media_get_lyrics` / `media.get_lyrics`

### 2.2 Server Events (`kivo/ipc/protocol.py`)
Add the following event names to `EVENT_NAMES`:
- `media_status_changed` / `media.status_changed`
- `media_lyrics_loaded` / `media.lyrics_loaded`

### 2.3 Payload Schemas

#### Operation: `media_get_status`
- **Request**:
  ```json
  {"version": 1, "type": "request", "id": "req-1", "operation": "media_get_status", "params": {}}
  ```
- **Response**:
  ```json
  {
    "version": 1,
    "type": "response",
    "id": "req-1",
    "ok": true,
    "result": {
      "available": true,
      "player_name": "chromium.instance4841",
      "players": ["chromium.instance4841"],
      "status": "Playing",
      "title": "Song Title",
      "artist": "Artist Name",
      "album": "Album Name",
      "art_url": "file:///tmp/art.jpg",
      "position_seconds": 34.5,
      "duration_seconds": 210.0,
      "updated_at": 17283921.12,
      "can_seek": true,
      "can_play_pause": true,
      "can_go_next": true,
      "can_go_previous": true,
      "lyrics_available": true
    }
  }
  ```

#### Operation: `media_seek`
- **Request**:
  ```json
  {"version": 1, "type": "request", "id": "req-2", "operation": "media_seek", "params": {"position_seconds": 65.0}}
  ```
- **Response**:
  ```json
  {"version": 1, "type": "response", "id": "req-2", "ok": true, "result": {"position_seconds": 65.0}}
  ```

#### Operation: `media_get_lyrics`
- **Request**:
  ```json
  {"version": 1, "type": "request", "id": "req-3", "operation": "media_get_lyrics", "params": {}}
  ```
- **Response**:
  ```json
  {
    "version": 1,
    "type": "response",
    "id": "req-3",
    "ok": true,
    "result": {
      "track": "Song Title",
      "artist": "Artist Name",
      "synced": true,
      "lines": [
        {"time_seconds": 12.0, "text": "First lyric line"},
        {"time_seconds": 16.5, "text": "Second lyric line"}
      ]
    }
  }
  ```

#### Event: `media_status_changed`
- Emitted whenever track metadata, playback status (play/pause), or active player changes. Carries the full `media_get_status` snapshot.

#### Event: `media_lyrics_loaded`
- Emitted when LRCLIB lyrics are fetched and parsed for the current track. Carries `{track, artist, synced, lines}`.

---

## 3. Backend Implementation (`kivo/media/`)

### 3.1 `kivo/media/lyrics.py`
- **Class**: `LyricsClient`
- **HTTP Client**: Uses `httpx.AsyncClient` with a 3.0s timeout.
- **Endpoint**: `https://lrclib.net/api/get`
- **Params**: `artist_name`, `track_name`, `album_name`, `duration`
- **LRC Parser**:
  - Parses timestamp tags `\[(\d+):(\d+(?:\.\d+)?)\]` into total seconds.
  - Sorts lines chronologically.
- **Cache**: In-memory LRU dictionary bounded to 32 entries.

### 3.2 `kivo/media/service.py`
- **Class**: `MediaService`
- **Subprocess Runner**:
  - `playerctl` CLI runner with non-blocking execution (`asyncio.create_subprocess_exec`).
  - Active player selection with MRU fallback.
  - `get_status()`, `play()`, `pause()`, `play_pause()`, `next()`, `previous()`, `seek(position_seconds)`.
  - Position caching: tracks `(position_seconds, time.monotonic())` so position is accurately calculated without flooding D-Bus.
- **Background Event Poller**:
  - Async task running `playerctl --follow metadata` or periodic lightweight check (500ms ticker when active).
  - Fires `on_status_changed` and `on_lyrics_loaded` callbacks.
- **Lifecycle**:
  - `start()` / `close()` cleanly manages background tasks.

---

## 4. Frontend Implementation (`shell/`)

### 4.1 `shell/services/KivoIpc.qml`
- Exposes reactive properties:
  - `property var mediaData: null`
  - `property var mediaLyrics: null`
  - `readonly property bool mediaActive: mediaData && mediaData.available === true && mediaData.status !== "Unavailable"`
  - `readonly property bool mediaPlaying: mediaData && mediaData.status === "Playing"`
  - `readonly property string mediaTitle: _text(mediaData, "title", "")`
  - `readonly property string mediaArtist: _text(mediaData, "artist", "")`
  - `readonly property real mediaPosition: _number(mediaData, "position_seconds", 0)`
  - `readonly property real mediaDuration: _number(mediaData, "duration_seconds", 0)`
- Exposes request helper methods:
  - `mediaPlay(callback)`
  - `mediaPause(callback)`
  - `mediaPlayPause(callback)`
  - `mediaNext(callback)`
  - `mediaPrevious(callback)`
  - `mediaSeek(seconds, callback)`
  - `mediaGetLyrics(callback)`
- Handles events:
  - `case "media_status_changed": ...`
  - `case "media_lyrics_loaded": ...`

### 4.2 `shell/components/NotchMediaDrawer.qml`
- Slide-down drawer inside `DynamicNotch.qml`.
- Elements:
  - Album Art: Thumbnail with fallback placeholder.
  - Typography: Song title in `Theme.fontHeader`, artist in `Theme.textPrimary`.
  - Seek Bar: Draggable progress slider showing current position and total duration.
  - Controls: Previous button, Play/Pause toggle, Next button.
  - Synced Lyrics: ListView of timed lines; automatically highlights and centers the line matching the current interpolated position.

### 4.3 `shell/components/DynamicNotch.qml`
- **Resting Pill Mode**:
  - When `ipc.mediaPlaying`: Collapsed row shows a compact music pill next to the KIVO icon with scrolling artist/title and micro animated waveform bars (`Waveform.qml`).
- **Expanded Drawer Mode**:
  - Shows `NotchMediaDrawer.qml` when media tab is active or media is playing.

---

## 5. Verification & Testing Specifications

1. **Unit Tests (`tests/test_media_service.py`)**:
   - `test_lyrics_parser_valid_lrc`: Valid timestamped `.lrc` lines correctly parsed and sorted.
   - `test_lyrics_parser_plain_fallback`: Plain lyrics formatted when synced lyrics are absent.
   - `test_lyrics_cache_lru`: Verifies cache retention and eviction after 32 entries.
   - `test_media_service_parse_metadata`: Verifies parsing of `playerctl` metadata string formats.
   - `test_media_service_position_interpolation`: Position calculation reflects elapsed monotonic time.
   - `test_media_service_seek_bounds`: Verifies negative and NaN seek targets fail closed.
2. **IPC Integration Tests (`tests/test_media_ipc.py`)**:
   - Tests all 8 new operations through the Unix socket.
   - Verifies `media_status_changed` and `media_lyrics_loaded` event emission.
3. **Quality Gates**:
   - Backend pytest suite: `.venv/bin/python -m pytest tests/ -q`
   - Shell pytest suite: `.venv/bin/python -m pytest shell/tests/ -q`
   - QML lint: `/usr/lib/qt6/bin/qmllint shell/components/NotchMediaDrawer.qml`
   - Desktop health diagnostic: `./scripts/desktop-health.sh` (9/9 PASS)
   - `git diff --check`
   - Python compile check: `.venv/bin/python -m compileall kivo tests shell -q`
