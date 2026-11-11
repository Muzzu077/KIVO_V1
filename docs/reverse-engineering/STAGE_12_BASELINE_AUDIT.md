# KIVO — Stage 12 Baseline Architecture & Roadmap Audit

## 1. Executive Summary & Readiness Verdict

```
CLASSIFICATION: READY FOR STAGE 12.1
```

### Readiness Analysis
An independent inspection of KIVO's actual source code, running runtime state, and underlying Arch Linux host environment was conducted to validate the feasibility of Stages 12 through 16.

1. **Host Capabilities Confirmed**: Probing the system environment confirmed that `/usr/bin/playerctl`, `/usr/bin/busctl`, `/usr/bin/wpctl`, `/usr/bin/pactl`, `/usr/bin/wl-paste`, `/usr/bin/wl-copy`, `/usr/bin/grim`, and `/usr/bin/wtype` are all pre-installed on the host system.
2. **Active MPRIS Verification**: Direct runtime interrogation revealed an active Chromium media instance (`chromium.instance4841`) exposing standard `org.mpris.MediaPlayer2` properties on the user D-Bus session.
3. **Clean Architecture Fit**: KIVO's existing modular design (`KivoApp._components` in `kivo/app.py`, `IPCHandlers` in `kivo/ipc/handlers.py`, and `KivoIpc.qml` in `shell/services/KivoIpc.qml`) allows Stage 12.1 (MPRIS media controls + LRCLIB lyrics) to be integrated without altering core agent security or IPC framing.
4. **Preservation of Core Security Invariants**: Media playback and lyrics synchronization are strictly desktop utility functions. They do not invoke tools, modify the filesystem, or interact with `AuthorizationPolicy`, `ApprovalGate`, `ExecutionGrant`, or `MutationLifecycle`.

---

## 2. Source Code Verification of Gap-Analysis Findings

Every classification in `docs/reverse-engineering/KIVO_FEATURE_GAP_ANALYSIS.md` was independently audited against the actual repository source code:

### 2.1 Confirmed "Already Implemented" Features
- **Dynamic Notch Layer Surface**: Verified in `shell/shell.qml:68-108` and `shell/components/DynamicNotch.qml`. Runs as a Wayland layer-shell window (`WlrLayer.Top`), `exclusiveZone: 0`, transparent background, with input region bounded by `capsule`.
- **Global Compositor Hotkey**: Verified in `~/.config/niri/kivo.kdl:6-10`. Binds `Mod+Alt+K` and `Mod+Shift+Alt+K` to Quickshell IPC calls (`toggleDashboard`, `toggle`).
- **Talk vs Agent Operational Separation**: Verified in `kivo/conversation/conversation_manager.py:508-522` and `kivo/agent/agent_runtime.py:31-108`. Conversational streaming runs without tools; tool calls are gated in multi-round agent turns.
- **Agent Stop Button & Cancellation**: Verified in `shell/components/StopButton.qml:1-118` and `kivo/conversation/conversation_manager.py:621-653`. Cancels active tasks, closes pending approvals, and drains committed mutations.
- **Kill-On-Close Execution Boundary**: Verified in `kivo/files/tools.py:258-310` and `kivo/conversation/conversation_manager.py:690-702`. Bounded worker close sets cancel events and terminates threads safely.
- **Strict Authorization & Approval Core**: Verified in `kivo/agent/authorization.py`, `approval_context.py`, `approval_gate.py`, and `tool_executor.py`. Tamper-proof cryptographic context and grant verification.

### 2.2 Clarifications on "Partially Implemented" Features
- **On-Demand Screen Vision Capture**: `core/screen_context.py` holds in-memory `ContextFrame`, but `kivo/app.py:497-512` strips image bytes (`_screen_snapshot`), returning metadata only. Continuous background capture via `core/screen_observer.py` is disabled to comply with KIVO Rule 7. High-resolution vision must be strictly on-demand via `grim`.
- **System Telemetry**: `kivo/agent/tools/system_info.py` provides on-demand queries for LLM prompts, but lacks an ambient, real-time ticker strip in the notch.
- **System Dictation**: Local STT is operational via `FasterWhisperEngine` (`kivo/stt/manager.py`), but output feeds only the assistant conversation brain, not external desktop window inputs (`wtype`).
- **Agent Activity HUD**: IPC events `agent_tool_started` and `agent_tool_completed` are published, but `DynamicNotch.qml` lacks an active-tool drawer and step counter.

### 2.3 Confirmed "Missing" Features
- **MPRIS Media Player & Playback Control**: Zero occurrences of `mpris`, `playerctl`, or `MediaPlayer2` across `kivo/` and `shell/`.
- **Synchronized LRCLIB Lyrics**: Zero lyrics clients or `.lrc` parsers in `kivo/`.
- **Wayland Clipboard History**: Only a read-only tool exists (`read_clipboard`); no history daemon or SQLite ring buffer exists.
- **Drag-and-Drop File Shelf**: No `DropArea` or file staging drawer exists in `shell/`.
- **Files to Phone (QR Pairing)**: `qrcode` is installed in `.venv`, but no local web server exists in `kivo/`.
- **Global Mic Mute Key**: No global audio input mute hotkey exists in `kivo/` or `kivo.kdl`.
- **Notification Aggregator**: No D-Bus notification daemon exists in Quickshell.
- **Screen Drawing Overlay**: No transparent click-through canvas overlay exists in Quickshell.

---

## 3. Code Reuse Inventory

The following existing assets will be reused directly in Stages 12–16:
1. `shell/components/DynamicNotch.qml`: Capsule geometry, spring animations, resting pill, and PTT audio integration.
2. `shell/components/StopButton.qml`: Animated pulse styling and click signals for the agent drawer.
3. `shell/services/KivoIpc.qml`: Newline-delimited JSON transport, request ID tracking, sequence validation, and defensive scalar converters.
4. `kivo/ipc/protocol.py` & `kivo/ipc/handlers.py`: 64 KB message framing, allowlisted operation dispatch, and event broadcasting.
5. `kivo/app.py`: Ordered component lifecycle (`initialize()` -> `run()` -> `shutdown()`).
6. Python `.venv` packages: `httpx` (0.28.1), `psutil` (7.2.2), `qrcode` (8.2), `faster-whisper` (1.2.1).

---

## 4. Architectural Risks & Mitigations

1. **D-Bus Blocking Calls**: Synchronous D-Bus queries can hang if a media player freezes.
   - *Mitigation*: Run all `playerctl` and D-Bus calls via non-blocking asynchronous subprocesses (`asyncio.create_subprocess_exec`) with strict 500ms timeouts.
2. **Redraw Flooding & High IPC Overhead**: Streaming position updates at 60 Hz floods the Unix socket.
   - *Mitigation*: Backend emits reference position + monotonic timestamp only on track change, seek, or pause. Quickshell interpolates playback position locally at 10 Hz using a lightweight QML timer.
3. **Competing Media Players**: Multiple players (e.g. YouTube in Chromium + Spotify paused) can produce ambiguous states.
   - *Mitigation*: Track active players using `playerctl -l`; prefer the currently `Playing` player, then most recently active (MRU). Provide an explicit player switch toggle in the expanded drawer.
4. **Wayland Input Mask Alignment**: Drawers opening outside the capsule can result in clicks passing through or blocking underlying apps.
   - *Mitigation*: Quickshell `Region { item: notch.inputRegion }` dynamically encapsulates the expanded capsule and active drawers. Collapsing strictly restores the 28px resting capsule mask.

---

## 5. Security & Privacy Guarantees

1. **Zero Raw Media Over IPC**: Socket framing strictly carries JSON metadata; audio PCM streams, raw camera buffers, and full-resolution framebuffer bytes never cross the IPC bridge.
2. **Strict User Consent for Vision**: Screen capture occurs **only** on explicit user hotkey/PTT trigger. Continuous background vision streaming is prohibited.
3. **Preservation of KIVO Core Authorization**: No desktop utility or media feature bypasses `AuthorizationPolicy`, `ApprovalGate`, `ExecutionGrant`, or `MutationLifecycle`.
4. **Clipboard Sanitization**: Passwords, OTP tokens, and secrets flagged with `x-kde-passwordManagerHint` are dropped prior to SQLite insertion.
5. **Local Network Containment**: Mobile pairing bridges bind strictly to the local Wi-Fi interface, require manual desktop approval, and provide zero internet-facing endpoints.
