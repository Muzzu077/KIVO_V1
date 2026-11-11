# NotchPixel — Technical Architecture & Feature Reverse Engineering Report

## 1. Executive Summary & Product Profile

**NotchPixel** (developed by Kore Industries, releases v0.5.64 – v0.5.94, October–November 2026) is a desktop utility for Windows 10 (Build 19041+) and Windows 11 that introduces an active "Dynamic Island" and top-edge notch overlay. Rather than functioning purely as an aesthetic shell or camera cutout mask, NotchPixel consolidates disparate desktop tasks—media playback, clipboard management, temporary file shelving, local network file transfer, hardware telemetry overlays, global audio muting, notification aggregation, and AI coding agent monitoring—into a single persistent surface.

### Product Metadata
- **Developer / Publisher**: Kore Industries (`kore-d`).
- **Target OS**: Windows 10 Version 2004 (Build 19041+) and Windows 11 (64-bit x64).
- **Distribution Method**: Microsoft Store packaged application (`9MXCRL0XBGPH`, signed MSIX/AppX container).
- **Licensing & Pricing**: Commercial proprietary ($9.99 one-time purchase with a 7-day trial; no cloud account or recurring subscription required).
- **Resource Footprint**: Advertised idle footprint of ~13 MB RAM and 0.0% CPU (measured on Intel Core i5-11400H, Windows 10 22H2).
- **Network Footprint**: Local-first; zero telemetry or analytics servers. Outbound traffic is restricted to user-initiated external queries (LRCLIB lyrics, weather feed) and an ephemeral local HTTP/HTTPS server for device-to-phone pairing.

---

## 2. Core UI & Composition Architecture

### 2.1 Geometry & Display States
NotchPixel supports two distinct physical geometries at the top center of the display:
1. **Hanging Notch**: Curves seamlessly into the top physical bezel of the monitor (mimicking modern MacBook displays).
2. **Floating Island**: Pill-shaped capsule floating a configurable distance below the top bezel (mimicking iOS Dynamic Island).
3. **Slim Game Strip**: A horizontally compressed, single-line telemetry banner (FPS, CPU, GPU, RAM, temps) activated when full-screen applications or games are in focus.

### 2.2 Hover & Click Interaction State Machine
The surface transitions through four discrete states:
- **Resting (Closed Pill)**: Displays ambient status (clock, weather glyph, battery percentage, or idle pixel pet). If media is active, displays track title and micro visualizer bars (pixel bars or rounded EQ).
- **Hover / Peek**: Triggered when the pointer rests on the notch with an adjustable hover delay (configurable animation speed: Quick vs Smooth). Expands into a compact peek card.
- **Card / Expanded Window**: Clicking or dwelling opens the full tabbed drawer (Music, Clipboard, Shelf, Deck, Widgets, Reminders, Controls, Agents, Notifications, Stream).
- **Detached / Desktop Pin**: Specific widgets (Day clock, weather, system stats) can be dragged off the island and pinned directly to the desktop workspace.

### 2.3 Visual Styles & Composition
- **Liquid Glass / Acrylic**: Real-time background blur and refraction sampling the desktop behind the window using DWM (Desktop Window Manager) composition APIs. Disables blur and drops to solid black over full-screen games to avoid GPU compositing overhead.
- **Solid Themes**: Pure Black (optimized for OLED panels), Graphite, and pastel themes (Rosé, Lavender, Peach, Sakura, Candy Glass) featuring custom progress bar styling (sparkles, hearts, falling petals).
- **Capture Protection**: Features a toggle to hide the island surface from OS screen captures and recordings using `SetWindowDisplayAffinity` (`WDA_EXCLUDEFROMCAPTURE`).

---

## 3. Detailed Investigation of All 16 Features

### Feature 1: Dynamic Island / Desktop Overlay
- **Behavior**: Persistent top-center window residing above standard desktop windows. Can stand on primary or secondary monitors (configurable via `Settings > Island > Monitor`).
- **Data Sources**: Win32 window management, display topology APIs, and compositor surface styling.
- **Failure Cases**: Stutter during live desktop wallpaper changes (e.g. Lively Wallpaper); mitigated in v0.5.89 by decoupling DWM blur updates from external wallpaper render loops.

### Feature 2: Hover and Click Expansion Behavior
- **Behavior**: Two-stage hit-testing. Hovering initiates an expanding spring animation; mouse exit shrinks the pill back to resting state. Reopens on the last-viewed tab or defaults to Music.
- **Edge Cases**: Mitigated accidental clicks when clicking browser tabs or title bars immediately adjacent to the notch by enforcing hit-box boundaries and open delay thresholds (v0.5.87).

### Feature 3: Media Controls & LRCLIB Lyrics Integration
- **Behavior**: Universal media playback display showing cover art, artist, track, scrubbable seek bar, and playback toggles.
- **Data Source**: Windows `SystemMediaTransportControls` (SMTC). Any application reporting to the Windows volume flyout (Spotify, Apple Music, YouTube in Chrome/Firefox/Edge, VLC, foobar2000) is automatically captured.
- **Lyrics Engine**: Integrated with **LRCLIB** (`https://lrclib.net`), a free, open-source lyrics database. Synchronized `.lrc` timestamped lines scroll in time with audio playback.
- **App Filtering**: Users can configure whitelist/blacklist rules (e.g., ignore browser video audio while retaining Spotify).

### Feature 4: Clipboard History & Pinned Entries
- **Behavior**: Stores copied items across system restarts, categorized into tabs: *All*, *Text*, *Links*, *Images*, *Files*, and *Snippets*. Clicking any item copies it back to the active clipboard.
- **Security & Privacy**:
  - Local database encrypted using Windows DPAPI (tied to the local user account).
  - Explicitly ignores copies containing password manager metadata (e.g., 1Password, Bitwarden, KeePass clipboard exclusion formats).
  - OCR capability ("Copy text") extracts strings from copied bitmap images.

### Feature 5: File Shelf & Drag-and-Drop
- **Behavior**: Drop zone where files from File Explorer or applications can be dragged onto the notch, held temporarily in a staging shelf, and dragged back out into other applications.
- **Data Handling**: Retains file path references and thumbnails; does not create duplicate file copies on disk. Files can be dismissed individually or cleared in batches.

### Feature 6: Files-to-Phone Transfer & QR Pairing
- **Behavior**: Clicking "Send to phone" on the file shelf generates an on-screen QR code. Scanning the QR code on a mobile device opens a mobile web app hosted directly by the PC on the local Wi-Fi network.
- **Networking & Security**:
  - Ephemeral local web server listening on a random port.
  - Generates a local self-signed TLS certificate or tokenized session URL valid for 2 minutes.
  - Requires user to explicitly click "Allow" on the PC desktop when a new mobile device connects.
  - Zero cloud intermediary; transfer occurs peer-to-peer across the local LAN.

### Feature 7: Desktop Widgets
- **Behavior**: Modular mini-cards: 12/24-hour clock, world clocks, weather (minimal sun/moon glyphs), to-do list, photo frame, battery monitors (laptop + Bluetooth peripherals), countdown timers, currency rates, calculator, air quality, and PC resource bars.
- **Tear-Off**: Widgets can be dragged out of the notch drawer and positioned permanently on the desktop canvas.

### Feature 8: Reminders & Focus Timer
- **Behavior**: Quick reminder creation with natural language or time pickers. Supports repeating schedules (daily, weekdays, weekly). Alerts trigger non-intrusive chimes and present "Snooze" and "Done" buttons inside the notch.
- **Overlay Priority**: Configured to alert even over full-screen games without breaking full-screen exclusive/borderless focus.

### Feature 9: Hardware Monitoring & Game Overlay
- **Behavior**: When 3D games launch, the full notch collapses into a slim single-line strip displaying real-time FPS, CPU utilization, GPU utilization, RAM utilization, network throughput, and hardware temperatures.
- **Data Acquisition**: Reads frame timing non-intrusively from DXGI and Direct3D 9 present events (utilizing ETW / Event Tracing for Windows, identical to Microsoft PresentMon and Xbox Game Bar). Does **not** inject DLLs or hook game memory, avoiding anti-cheat flags.

### Feature 10: Global Microphone Mute & Call Timer
- **Behavior**: Global hotkey (`Ctrl+Alt+M`) mutes the default audio input device across all applications simultaneously.
- **Status Indicators**: Displays an active microphone privacy dot naming the app accessing the sensor, shows a call duration counter, and sounds a warning chime if an application attempts to capture audio while muted.

### Feature 11: Notification Aggregation
- **Behavior**: Captures Windows desktop notifications, mirrors them as compact pill popups in the notch, and stacks simultaneous alerts. Users can scroll through stacked alerts using the mouse wheel and open the parent application by clicking its icon.

### Feature 12: AI-Agent Monitoring
- **Behavior**: Specialized status drawer tracking autonomous coding agents (Claude Code, Codex, Antigravity, Hermes, or custom CLI scripts).
- **Interface**: Displays active round/step, current tool being executed, execution time, and alerts the user when an agent is waiting for approval or input. Provides a "Stop" button to terminate the run.
- **Integration**: Communicates via local socket/named pipe or local CLI telemetry hooks.

### Feature 13: Quick System Controls
- **Behavior**: One-click quick toggles for display brightness (via DDC/CI for desktop monitors, WMI for laptop panels), audio volume mixer per application, Wi-Fi network selector, Bluetooth device connection, Night Light color temperature, and Windows power profiles.

### Feature 14: Web / Application Drawer
- **Behavior**: Embedded browser view sliding down beneath the island. Supports pinning web applications (e.g. documentation, ChatGPT, quick search).
- **Technology**: Built on Microsoft Edge WebView2 (Chromium engine), with support for extensions and isolated cookies.

### Feature 15: PC Deck (Phone-as-Controller) *(Exploratory)*
- **Behavior**: Turns any mobile device on the same local Wi-Fi into an external touch controller for the PC without installing a native mobile app.
- **Capabilities**: Touchpad cursor control, on-screen keyboard input, media playback scrubber, app launcher icons, and macro mode buttons for gaming, presentation slides, and media viewing.

### Feature 16: Taskbar Replacement *(Exploratory)*
- **Behavior**: Optional setting (`Settings > Taskbar`) that hides the default Windows shell taskbar and renders a rounded bottom-dock notch housing Start, pinned apps, active window tabs, system tray icons, quick settings, and the clock.

---

## 4. Technical Architecture & Reverse Engineering Analysis

### 4.1 Confirmed Findings (Observed Facts)
1. **Packaging**: Sold exclusively through the Microsoft Store as a packaged Win32 application (`x64` target, Windows 10 build 19041+ minimum).
2. **Media & Lyrics Architecture**: Directly leverages Windows `SystemMediaTransportControls` for playback tracking and makes outbound HTTPS requests to the public LRCLIB API (`lrclib.net`) for LRC-formatted lyric data.
3. **Telemetry & Anti-Cheat Safety**: Frame timing is acquired via ETW (Event Tracing for Windows) present events, requiring no DLL injection or process hooking.
4. **Local Network Transport**: Mobile pairing (PC Deck and file shelf) operates strictly over local Wi-Fi via an embedded HTTP server with QR-tokenized authentication and desktop authorization prompts.
5. **DWM Compositor Integration**: Uses native Windows composition visual layers (Mica/Acrylic/DWM blur) with fallback to solid black over full-screen DirectX applications.

### 4.2 Evidence-Backed Inferences
1. **Core Language & Framework**: Built using modern C#/.NET 8+ or C++/WinUI 3 with native interop (`P/Invoke`) for low-level Windows APIs (CoreAudio, ETW, DDC/CI, Shell hooks).
2. **Agent Interop**: Agent monitoring hooks rely on monitoring terminal process trees or listening to local Unix domain sockets / named pipes exposed by CLI tools.

### 4.3 Unknowns & Proprietary Boundaries
1. **Proprietary Source Code**: NotchPixel is closed-source; specific internal class hierarchies and algorithms remain proprietary to Kore Industries.
2. **Mac Version Implementation**: The announced macOS version remains in active development and has not been publicly released.

---

## 5. Security & Privacy Model

1. **Zero Cloud Exfiltration**: Clipboard history, file shelf items, notification bodies, and agent step transcripts remain exclusively in local volatile memory and encrypted user-space storage.
2. **Sensitive Clipboard Protection**: Automatically detects and drops items flagged with password manager clipboard formats.
3. **Local Pairing Boundaries**: Mobile pairing tokens expire in 2 minutes and enforce explicit desktop human approval before accepting remote input commands.
4. **Non-Invasive Telemetry**: Avoids memory injection or kernel drivers, relying entirely on documented OS event tracing.
