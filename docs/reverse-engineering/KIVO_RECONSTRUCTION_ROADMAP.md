# KIVO Reconstruction Roadmap — Native Linux/Wayland Implementation

## 1. Architectural Strategy & Guiding Principles

The objective of this roadmap is to adapt the strongest observable capabilities of **NotchPixel** and **HeyClicky** into **KIVO**'s native Arch Linux, Wayland (niri compositor), Quickshell (Qt6/QML), and Python backend architecture without compromising KIVO's strict engineering boundaries.

### Core Non-Negotiable Invariants
1. **No Monolithic Bloat**: Features are designed as independent, loosely coupled modules with explicit IPC contracts over `$XDG_RUNTIME_DIR/kivo/kivo.sock`.
2. **Wayland & niri Native**: Zero X11 fallbacks; all UI surfaces use `wlr-layer-shell-unstable-v1` via Quickshell 0.3.2.
3. **Zero Raw Media Over IPC**: Audio PCM streams, raw camera buffers, and full-resolution framebuffer bytes **never** traverse the Unix domain socket.
4. **Explicit Action & Human In-The-Loop**:
   - Screen capture occurs **only** on explicit user hotkey/PTT trigger. Continuous background vision is forbidden.
   - Filesystem mutations continue to require `AuthorizationPolicy`, `ApprovalGate`, and `ExecutionGrant`.
   - Destructive actions (deletions, external sends, financial transactions) require explicit human confirmation.
5. **Fail-Closed & Offline First**: Failure of external services (LRCLIB, cloud vision models, phone pairing) must degrade gracefully with zero disruption to the desktop shell.

---

## 2. Proposed Modular Component Architecture

### Component 1: Dynamic Notch & Multi-Drawer Shell (`shell/`)
- **Responsibilities**: Hosts the primary top-center layer surface, manages resting pill state, handles hover/dwell expansion, and presents modular drawers.
- **Boundaries**: Quickshell QML (`DynamicNotch.qml`, `NotchDrawer.qml`). Purely visual presentation; opens no secondary backend connections.
- **IPC Requirements**: Subscribes to `media_status_changed`, `agent_state_changed`, `mic_state_changed`, and `clipboard_updated` events.
- **Data Flow**: Quickshell receives JSON payloads from `KivoIpc.qml` -> updates QML property bindings -> animates geometry with spring curves.
- **Dependencies**: Quickshell 0.3.2, QtQuick 6.x.

### Component 2: Media Controls & LRCLIB Synchronized Lyrics (`kivo/media/`)
- **Responsibilities**: Monitors active media players on the desktop (Spotify, Chromium, Firefox, VLC), exposes playback controls (play, pause, skip, scrub), and streams synchronized `.lrc` lyrics.
- **Backend/Frontend Boundary**:
  - Backend: `kivo/media/service.py` connects to D-Bus `org.mpris.MediaPlayer2.*` via `pydbus` or `dbus-fast`. Fetches lyrics asynchronously from `https://lrclib.net/api/get`.
  - Frontend: `shell/components/NotchMediaDrawer.qml` renders album art, seek slider, and smooth-scrolling synchronized lyric lines.
- **IPC Requirements**: New operations `media_play`, `media_pause`, `media_skip`, `media_seek`; events `media_state_changed`, `media_lyrics_loaded`.
- **Permissions**: Read-only access to user D-Bus session.
- **Failure Handling**: If no player is active or LRCLIB returns 404, the drawer collapses gracefully to a simple clock/weather pill.

### Component 3: Wayland Clipboard History & Pinned Snippets (`kivo/clipboard/`)
- **Responsibilities**: Maintains an encrypted local history of copied text, links, and image paths across system reboots, with pinned snippet support.
- **Backend/Frontend Boundary**:
  - Backend: `kivo/clipboard/service.py` listens to Wayland clipboard events via `wl-paste --watch`. Stores metadata in local SQLite database (`$XDG_DATA_HOME/kivo/clipboard.db`).
  - Frontend: `shell/components/NotchClipboardDrawer.qml` displays categorized tabs (*All*, *Text*, *Links*, *Images*, *Pinned*).
- **Security & Privacy Filters**:
  - Explicitly filters out clipboard data flagged with `x-kde-passwordManagerHint` or containing credentials.
  - Encryption at rest via Linux Kernel Keyring or AES-256 with key in user session.
- **IPC Requirements**: Operations `clipboard_copy`, `clipboard_pin`, `clipboard_clear`; event `clipboard_item_added`.
- **Dependencies**: `wl-clipboard` (`wl-paste`), SQLite.

### Component 4: Local File Staging Shelf & QR Phone Bridge (`kivo/shelf/` & `kivo/bridge/`)
- **Responsibilities**: Drop zone for dragging and parking files; ephemeral local Wi-Fi web server for transferring files to/from mobile devices via QR code.
- **Backend/Frontend Boundary**:
  - Backend: `kivo/bridge/server.py` runs an ephemeral `aiohttp` HTTP server on a randomized port (`localhost:random`), generates a one-time session PIN, and enforces desktop confirmation when a phone requests a download.
  - Frontend: `shell/components/NotchShelfDrawer.qml` contains a Quickshell `DropArea` and generates a QR code image using a native QML QR component.
- **Security**: Local Wi-Fi only (binds to LAN interface); expires pairing token after 2 minutes; requires human click on desktop before file transfer starts.

### Component 5: Screen Drawing & Visual Guidance Overlay (`shell/components/ScreenOverlay.qml`)
- **Responsibilities**: Draws hardware-accelerated bounding boxes, animated vector arrows, and highlighted callouts directly over desktop applications during AI walkthroughs.
- **Boundary**:
  - Implemented entirely in Quickshell as a fullscreen `PanelWindow` anchored to all four edges on `Layer.Overlay` with `exclusive_zone: 0`.
  - Input region set to empty (`input_region: null` / `click-through`), allowing the user to click underlying applications while drawings are visible.
  - Uses QML `Canvas` or `QtQuick.Shapes` for vector curves and glowing neon bezier arrows (`Theme.mint`).
- **Data Flow**: Assistant multimodal response returns normalized target coordinates (`[ymin, xmin, ymax, xmax]`) -> IPC event `screen_annotation_draw` -> Quickshell triggers pen animation.

### Component 6: On-Demand Screen Vision Capture (`kivo/vision/`)
- **Responsibilities**: Captures an immediate high-resolution framebuffer snapshot upon user hotkey trigger and forwards it to the multimodal vision model.
- **Mechanism**:
  - Backend invokes `grim -t jpeg -q 85 /tmp/opencode/kivo_capture.jpg` (or Wayland screencopy portal `org.freedesktop.portal.ScreenCast`).
  - The frame is loaded in Python, preprocessed, passed directly to the vision model API, and unlinked immediately.
- **Privacy Invariant**: Frame is never saved permanently; zero background streaming.

### Component 7: System Dictation & Desktop Typing Injection (`kivo/dictation/`)
- **Responsibilities**: Allows the user to dictate text anywhere on the desktop, injecting transcribed text directly into the active window.
- **Mechanism**:
  - Global hotkey (`Mod+D`) activates audio capture ingress.
  - Local `FasterWhisperEngine` transcribes the voice stream.
  - Backend uses `wtype` or `ydotool` on Wayland to inject the text into the focused window.
  - Watch-for-correction: If user backspaces within 3 seconds, logs phonetic mismatch to improve local user vocabulary.

### Component 8: Real-Time Hardware Telemetry Strip (`kivo/telemetry/`)
- **Responsibilities**: Slim status overlay showing FPS, CPU, GPU, and RAM metrics over fullscreen applications.
- **Mechanism**:
  - Backend reads `/proc/stat` (CPU), `/proc/meminfo` (RAM), and `/sys/class/drm/card*/device/gpu_busy_percent` or `nvidia-smi` (GPU).
  - Pushes metrics at 2 Hz to Quickshell. Notch renders a slim 18px top strip.

### Component 9: Global Microphone Mute & Privacy Sensor Indicator (`kivo/audio/`)
- **Responsibilities**: Universal mute shortcut (`Mod+Shift+M`) toggling the default PipeWire audio source.
- **Mechanism**:
  - Backend calls `wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle`.
  - Monitors PipeWire streams; if an application opens a capture stream while muted, notch flashes a warning chime and displays the app name.

### Component 10: Autonomous Agent Monitoring Drawer & Stop Button (`shell/`)
- **Responsibilities**: Provides real-time visibility into KIVO's autonomous coding and task execution loops.
- **Interface**:
  - Notch expands to show current execution round (e.g. `Round 2/5: checking git status`).
  - Embeds KIVO's existing `StopButton.qml`, calling `conversation_manager.cancel_active()` on click.
  - Flashes amber glow when an approval prompt (`ApprovalRequest`) is waiting for human resolution.

---

## 3. Phased Implementation Sequence

```
Stage 12: Desktop Utility Foundation (MPRIS Media + LRCLIB Lyrics, Global Mic Mute, Hardware Stats)
   │
   ▼
Stage 13: Local Productivity Utilities (Wayland Clipboard History, File Shelf Drop, Local QR Phone Bridge)
   │
   ▼
Stage 14: Visual Guidance & Screen Annotation (On-Demand Wayland Screencopy, Fullscreen Click-Through Canvas)
   │
   ▼
Stage 15: Universal System Dictation & Desktop Injection (Whisper STT -> wtype, Correction Watcher)
   │
   ▼
Stage 16: Autonomous Agent HUD & Proactive Assistance (Notch Agent Drawer, Stop Button, Proactive Badges)
```

### Stage 12: Desktop Utility Foundation
- **Goal**: Deliver immediate high-delight desktop utilities in the notch.
- **Work Items**:
  1. Build `kivo/media/` MPRIS D-Bus listener and LRCLIB HTTP lyrics client.
  2. Implement `shell/components/NotchMediaDrawer.qml` with scrubbable progress bar and synchronized lyrics.
  3. Integrate PipeWire `wpctl` global mic mute hotkey and sensor privacy indicator.
  4. Add `/proc` and `/sys/class/hwmon` hardware reader and slim telemetry strip.
- **Verification Gates**: Pytest D-Bus mocks, QML lint, desktop health 9/9.

### Stage 13: Local Productivity Utilities
- **Goal**: Clipboard history, file shelf, and peer-to-peer phone sharing.
- **Work Items**:
  1. Build `kivo/clipboard/` using `wl-paste --watch`, SQLite storage, and password manager exclusions.
  2. Implement `shell/components/NotchClipboardDrawer.qml` with search, type tabs, and pinned snippets.
  3. Implement `DropArea` file shelf in Quickshell notch.
  4. Build `kivo/bridge/` local ephemeral HTTP server with QR pairing for LAN file transfer.
- **Verification Gates**: Encryption verification, zero data leak tests, QML lint.

### Stage 14: Visual Guidance & Screen Annotation
- **Goal**: Reproduce HeyClicky's defining multimodal capability on Wayland.
- **Work Items**:
  1. Build on-demand Wayland screencopy service using `grim` / portal.
  2. Build `shell/components/ScreenOverlay.qml` as a fullscreen click-through layer-shell overlay.
  3. Implement QML vector drawing engine rendering animated bezier arrows and bounding boxes from model coordinates.
  4. Connect vision model prompt loop for step-by-step visual guidance.
- **Verification Gates**: Coordinate alignment tests, click-through passivity tests, latency benchmarks.

### Stage 15: Universal System Dictation
- **Goal**: Direct voice-to-text typing into any active Wayland application.
- **Work Items**:
  1. Implement dictation mode state in `TranscriptionManager`.
  2. Connect transcription output to `wtype` / virtual keyboard injector.
  3. Build correction detection watcher to learn personal user corrections.
- **Verification Gates**: Typing latency tests, active window focus tests.

### Stage 16: Autonomous Agent HUD & Proactive Assistance
- **Goal**: Real-time ambient monitoring of KIVO's agent loop.
- **Work Items**:
  1. Add `AgentDrawer` to `DynamicNotch.qml`.
  2. Wire `agent_tool_started`, `agent_tool_completed`, and `ApprovalRequest` events.
  3. Embed `StopButton.qml` for immediate, single-click task cancellation.
  4. Connect `core.proactive_hub` events to ambient notch pill badges.
- **Verification Gates**: Turn cancellation tests, agent event latency tests, QML lint.

---

## 4. Security, Privacy & Boundary Guarantees

1. **Authorization Invariant**: No new feature introduced in this roadmap bypasses KIVO's existing `AuthorizationPolicy`, `ApprovalGate`, or `ExecutionGrant`.
2. **Ephemeral Vision Frames**: Screencopy images are created in `/tmp/opencode/`, held in RAM for inference, and deleted immediately. Permanent screenshot storage is prohibited.
3. **Clipboard Sanitization**: Passwords, OTP tokens, and secrets copied from password managers are identified via window classification and MIME metadata and dropped prior to SQLite insertion.
4. **Local Network Containment**: The QR phone pairing bridge binds strictly to the local Wi-Fi interface, requires manual desktop approval for every connecting client, and provides zero internet-facing endpoints.
5. **Passive Drawing Safety**: The visual annotation layer is strictly click-through and non-modal; it never intercepts mouse clicks or steals input focus from active applications.
