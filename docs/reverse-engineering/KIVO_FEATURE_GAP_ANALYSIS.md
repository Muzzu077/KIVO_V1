# KIVO Feature Gap Analysis — NotchPixel & HeyClicky vs Native KIVO

## 1. Executive Summary & Stack Alignment

This analysis benchmarks the capabilities of **NotchPixel** (desktop utility overlay) and **HeyClicky** (multimodal vision/voice AI companion) against **KIVO**'s existing architectural foundation. 

KIVO's platform stack:
- **Operating System**: Arch Linux (kernel 6.x).
- **Display Server & Window Manager**: Wayland only, running on the **niri** scrollable-tiling compositor.
- **Frontend Layer**: **Quickshell** 0.3.2 (Qt6 / QML / Wayland `wlr-layer-shell-unstable-v1` integration).
- **Backend Runtime**: Python 3.14 running in dedicated virtual environment (`.venv`).
- **IPC Protocol**: Unix-domain stream socket (`kivo.sock`) with newline-delimited JSON v1 framing (64 KB max frame).
- **Security & Agent Core**: Strict default-deny `AuthorizationPolicy`, tokenized `ApprovalGate`, cryptographic `ExecutionGrant`, and authoritative `MutationLifecycle` ledger.

KIVO already possesses several foundational capabilities that NotchPixel and HeyClicky implement from scratch: a native Wayland layer-shell notch (`DynamicNotch.qml`), an in-memory conversation runtime, local speech-to-text (`FasterWhisperEngine`), safe desktop action boundaries (`DesktopActionService`), and formal file-system isolation. The primary gaps are **media/telemetry desktop utilities** (from NotchPixel) and **live visual screen annotation / multimodal vision guidance** (from HeyClicky).

---

## 2. Comprehensive Comparison Matrix

| Feature | Target Origin | Existing KIVO Support | Gap Description | Proposed Native Linux/Wayland Implementation | Complexity | Feasibility / Classification |
|---|---|---|---|---|:---:|:---:|
| **Dynamic Island / Top Notch** | NotchPixel & HeyClicky | **Implemented** (`DynamicNotch.qml`) | Missing hover dwell timers, spring tab switching, and modular drawer popups. | Expand `DynamicNotch.qml` with modular tabs (`Music`, `Shelf`, `Agent`, `Widgets`) and smooth height transitions. | Medium | **Already Implemented** (Enhancement) |
| **Media Player & Playback Control** | NotchPixel | **Missing** | No system media player integration or playback scrubber. | `kivo/media/`: Python D-Bus listener connecting to `org.mpris.MediaPlayer2.*`. Publishes track, artist, art, and position over IPC. | Low | **Missing** (High Value) |
| **Synchronized Live Lyrics** | NotchPixel | **Missing** | No lyric synchronization. | Query public LRCLIB API (`https://lrclib.net/api/get`) on track change; stream timestamped `.lrc` lines synced to MPRIS position. | Low | **Missing** (High Value) |
| **Clipboard History & Pinned Snippets** | NotchPixel | **Missing** | No clipboard history tracking or pinned snippets. | `kivo/clipboard/`: Monitor `wl-paste -w` with SQLite storage. Filter password managers (`x-kde-passwordManagerHint`). Quickshell UI tab. | Medium | **Missing** (High Value) |
| **File Shelf & Drag-Drop** | NotchPixel | **Missing** | No staging area for files. | Quickshell `DropArea` accepting `text/uri-list`. Holds references in an in-memory shelf with thumbnail generation. | Medium | **Missing** (Medium Value) |
| **Files to Phone (QR Pairing)** | NotchPixel | **Missing** | No local network file sharing. | `kivo/bridge/`: Ephemeral Python `aiohttp` web server on random port. Renders pairing QR code in Quickshell notch. User confirms on desktop. | Low | **Missing** (Medium Value) |
| **Desktop Widgets & Teardown** | NotchPixel | **Partial** (`KivoDashboard.qml`) | Widgets exist only in floating dashboard; cannot pin widgets directly to desktop wallpaper. | Create detached Quickshell layer surfaces on `Layer.Bottom` for pinned desktop widgets (Day clock, weather, system stats). | Medium | **Partially Implemented** |
| **Reminders & Focus Timer** | NotchPixel | **Missing** | No cron/timer scheduling engine or notch alert chime. | Python `asyncio` priority queue scheduler. Emits reminder alert events over IPC; Quickshell renders snooze/done pill in notch. | Low | **Missing** (Medium Value) |
| **Hardware Stats / Game Overlay** | NotchPixel | **Partial** (`SystemInfo` tool) | Stats are query-only by model; no continuous real-time HUD strip over games. | Read `/proc/stat`, `/proc/meminfo`, and `/sys/class/hwmon` on 500ms ticker. Quickshell displays slim strip during fullscreen windows. | Low | **Partially Implemented** |
| **Global Mic Mute & Sensor Dots** | NotchPixel | **Missing** | No global audio input mute hotkey or active mic privacy indicator. | WirePlumber / PipeWire D-Bus integration via `wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle`. Track active recording streams. | Low | **Missing** (High Value) |
| **Notification Aggregation** | NotchPixel | **Missing** | No desktop notification daemon. | Implement `org.freedesktop.Notifications` D-Bus service in Quickshell; render stacked notification pills in notch with wheel scroll. | Medium | **Missing** (Medium Value) |
| **Autonomous Agent Monitoring** | NotchPixel & HeyClicky | **Partial** (Backend events exist) | Agent events (`agent_tool_started`, `agent_tool_completed`) exist in IPC but lack dedicated notch HUD drawer. | Add `AgentDrawer` to `DynamicNotch.qml` showing active round, executing tool, elapsed time, and prominent **Stop** button. | Low | **Partially Implemented** (High Value) |
| **Quick System Controls** | NotchPixel | **Partial** (Actions exist) | Toggles exist in Dashboard but are not accessible via quick-click notch drawer. | Add compact controls panel in notch: PipeWire audio sink selector, `brightnessctl`, NetworkManager Wi-Fi, BlueZ Bluetooth, night light. | Low | **Partially Implemented** |
| **Embedded Web Drawer** | NotchPixel | **Missing** | No browser view sliding out of notch. | Optional QtWebEngine view in Quickshell drawer (or lightweight external browser spawn via `niri` window rule). | High | **Missing** (Low Priority) |
| **PC Deck (Phone-as-Controller)** | NotchPixel | **Missing** | No phone trackpad or remote control interface. | Expand local `kivo/bridge/` server with touch event WebSocket feeding into `wtype` / `uinput` virtual mouse. | High | **Missing** (Exploratory) |
| **Taskbar Replacement** | NotchPixel | **Not Practical** | niri is a scrollable-tiling compositor without a traditional taskbar; Quickshell already handles status overlays. | niri workspaces and columns supersede Windows taskbar paradigm. Not aligned with KIVO's niri architecture. | High | **Not Practical on Platform** |
| **Global Hotkey Activation** | HeyClicky | **Implemented** (`kivo.niri` binds) | niri keybind invokes KIVO; PTT hotkey triggers voice capture. | Enhance existing niri hotkey (`Mod+Space` or dedicated mouse/keyboard key) to summon notch in contextual inquiry mode. | Low | **Already Implemented** |
| **On-Demand Screen Vision Capture** | HeyClicky | **Partial** (Screen context exists in memory) | Screen context is captured via OCR loop; lacks high-res visual frame capture on hotkey trigger. | Explicit hotkey triggers `xdg-desktop-portal` or `grim -t jpeg` screencopy -> encodes in-memory frame -> forwards to vision LLM. | Medium | **Partially Implemented** (High Value) |
| **Duplex Voice Conversation** | HeyClicky | **Partial** (Turn-based STT + LLM + TTS) | KIVO is turn-based (PTT -> Whisper STT -> LLM response); not streaming duplex. | Support OpenAI GPT Realtime API or Gemini Multimodal Live API over WebSockets alongside local Whisper fallback. | High | **Partially Implemented** (Requires External API) |
| **Speech-to-Text Dictation Mode** | HeyClicky | **Partial** (STT exists) | Transcribes for assistant brain only; cannot type text directly into arbitrary active windows. | Add dictation mode: Faster-Whisper transcribes speech -> pipes text to active window via `wtype` or `ydotool` on Wayland. | Medium | **Partially Implemented** (High Value) |
| **Screen Drawing & Visual Annotation** | HeyClicky | **Missing** | Cannot draw arrows or bounding boxes over the desktop. | Quickshell fullscreen layer-shell surface on `Layer.Overlay` with transparent background and click-through input region. QML Canvas renders vector shapes. | Medium | **Missing** (High Value / Wow Factor) |
| **Step-by-Step Visual Guidance** | HeyClicky | **Missing** | Assistant answers in text only; does not guide spatially. | Combine multimodal vision coordinates with screen drawing overlay: assistant highlights UI elements sequentially. | Medium | **Missing** (High Value) |
| **Dual Mode: Talk vs Agent** | HeyClicky | **Implemented** (`ConversationManager` vs `AgentRuntime`) | KIVO already strictly separates conversational dialogue from tool/file execution. | Surface mode distinction explicitly in notch UI: "Talk Mode" (no tools) vs "Agent Mode" (approval-gated tool execution). | Low | **Already Implemented** |
| **Agent Progress & Stop Button** | HeyClicky | **Implemented** (`StopButton.qml`, `conv_mgr.cancel_active()`) | `StopButton.qml` exists in codebase; needs direct prominence in the Notch view. | Place `StopButton` directly inside `DynamicNotch.qml` during active agent runs for one-click immediate cancellation. | Low | **Already Implemented** (UI Wiring) |
| **Chromium Browser Automation** | HeyClicky | **Missing** | KIVO has no browser DOM automation tools. | Develop native Chrome/Chromium Native Messaging host or Playwright/CDP tool for DOM extraction and form automation. | High | **Missing** (Medium Value) |
| **Strict Human Approval Policy** | HeyClicky | **Implemented** (`AuthorizationPolicy`, `ApprovalGate`, `ExecutionGrant`) | KIVO's security architecture is already more mathematically rigorous than HeyClicky's prompt-only rules. | Maintain KIVO's tamper-proof `ApprovalContext` and cryptographic grant validation. | None | **Already Implemented** (Superior in KIVO) |
| **Kill-On-Close Execution Boundary** | HeyClicky | **Implemented** (`BoundedFileWorker.close()`, `cancel_active()`) | KIVO already terminates background tasks and joins threads during shutdown and turn cancellation. | Maintain existing process management and cancellation semantics. | None | **Already Implemented** |

---

## 3. High-Value Opportunities for KIVO

### 1. The Dynamic Notch Media & Lyrics Suite (From NotchPixel)
- **Why**: Extreme daily utility. Linux media controls are fragmented; bringing MPRIS metadata (Spotify, Chrome, Firefox, VLC) and synchronized LRCLIB lyrics into KIVO's top-center notch provides instant polish and desktop delight without touching agent security boundaries.
- **Feasibility**: High. `org.mpris.MediaPlayer2` is standard across Linux desktops, and LRCLIB has a clean, free JSON API.

### 2. Live Screen Annotation & Visual Guidance (From HeyClicky)
- **Why**: The defining "magical" capability of modern multimodal assistants. Instead of merely telling the user "click the export button in the top right", KIVO can draw an animated, glowing vector arrow directly over the button using Quickshell.
- **Feasibility**: High. Quickshell can open a fullscreen `wlr-layer-shell` surface on `Layer.Overlay` with `exclusive_zone: 0` and an empty/click-through input region. QML Canvas or QtQuick Shapes draw the bounding boxes and arrows with zero desktop disruption.

### 3. Integrated Agent Execution HUD & Stop Button (From Both)
- **Why**: As KIVO's autonomous capabilities grow (Stage 11 filesystem tools, terminal execution), the user needs real-time ambient awareness of what the agent is doing without opening the large dashboard.
- **Feasibility**: Immediate. KIVO already publishes `agent_tool_started` and `agent_tool_completed` events over IPC, and possesses a working `StopButton.qml`. Wiring these into `DynamicNotch.qml` gives KIVO parity with HeyClicky's HUD.

### 4. System Dictation Mode with Desktop Injection (From HeyClicky)
- **Why**: KIVO already has local Faster-Whisper running in the background. Adding a shortcut to stream transcribed text directly into the active Wayland window via `wtype` turns KIVO into a universal dictation tool.
- **Feasibility**: High. Uses existing transcription manager and virtual keyboard injection.

### 5. Wayland Clipboard & File Staging Shelf (From NotchPixel)
- **Why**: Staging files and managing clipboard history in the notch matches KIVO's local productivity focus while preserving strict privacy.
- **Feasibility**: Medium. Standard `wl-clipboard` primitives and Quickshell drag-and-drop.
