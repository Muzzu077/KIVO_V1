# KIVO — Stage 13 Architecture Specification: Local Productivity Utilities

```
Document:   Stage 13.0 Architecture Specification
Status:     Proposed (Research & Specification Stage)
Date:       2026-11-09
Baseline:   HEAD 1062c0d; Stage 12.2 completed
Features:   1. Clipboard Intelligence
            2. File Shelf
            3. Phone-to-PC Bridge
Scope:      Architecture, service boundaries, IPC contracts, security invariants, UI integration
```

---

## 1. System Invariants & Architectural Boundaries

1. **One backend, one IPC server, one QML bridge.** `KivoApp` manages lifecycle, `IPCServer` exposes Unix-domain socket framing, and `KivoIpc.qml` remains the sole UI bridge. No secondary background daemon or competing socket.
2. **Local-only execution.** Zero mandatory external cloud services or telemetry relays. Phone-to-PC bridge operates strictly peer-to-peer over the authenticated local Wi-Fi subnet.
3. **No automatic agent/LLM surface.** Staged files, clipboard text, and transferred documents are **never** injected into LLM prompt context or tool registries automatically. Staging a file or copying text is a local user desktop action, not an authorization grant.
4. **Strict privacy on clipboard history.** Sensitive credentials, passwords from password managers, API tokens, and private keys are detected, masked, and excluded from persistent history.
5. **Reference-based File Shelf.** File staging stores canonical references, not duplicate physical copies, and strictly enforces symlink safety and workspace boundaries. Staged files are never executed.
6. **Explicit desktop authorization for incoming files.** The PC must explicitly prompt and require user confirmation before any file transferred from a phone is committed to persistent storage (`~/Downloads`).
7. **No unauthenticated listening servers.** The Phone Bridge server is ephemeral: it binds strictly to the active LAN interface IP (never `0.0.0.0`), requires mutual token authentication, and shuts down immediately when pairing ends or KIVO stops.
8. **Bounded resource consumption.** All operations enforce bounded timeouts, capped buffer sizes, disk-quota prechecks, and rate limits.
9. **Zero new dependencies.** Reuses declared libraries (`fastapi`, `uvicorn`, `cryptography`, `qrcode[pil]`, `Pillow`) and installed system tools (`wl-clipboard`, `cliphist`).
10. **Preserve existing functionality.** Dynamic Notch media drawer, system telemetry, microphone mute, and task orchestration remain intact.

---

## 2. Component Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│ KivoApp (kivo/app.py)                                                │
│  owns: telemetry, microphone, clipboard, file_shelf, phone_bridge    │
│  lifecycle: orderly initialize(), run(), and shutdown()              │
└───────────────┬───────────────────┬──────────────────┬───────────────┘
                │                   │                  │
┌───────────────▼───────────┐ ┌─────▼──────────┐ ┌─────▼───────────────┐
│ ClipboardService          │ │ FileShelfService │ │ PhoneBridgeService    │
│  kivo/clipboard/          │ │  kivo/shelf/   │ │  kivo/bridge/       │
│  • inotify cliphist / wl  │ │  • DropArea ref│ │  • Ephemeral FastAPI│
│  • Sensitive detection    │ │  • Symlink safe│ │  • QR token pairing │
│  • Pin store (0600)       │ │  • Workspace   │ │  • E2EE WebCrypto   │
│  • run_bounded(wl-copy)   │ │    integration │ │  • Quarantine store │
└───────────────┬───────────┘ └─────┬──────────┘ └─────┬───────────────┘
                │                   │                  │
┌───────────────▼───────────────────▼──────────────────▼───────────────┐
│ IPCHandlers & IPCServer ($XDG_RUNTIME_DIR/kivo/kivo.sock)            │
│  operations: clip_*, shelf_*, bridge_*                               │
│  events: clip_history_changed, shelf_changed, bridge_pairing_req... │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │ newline-delimited JSON v1
┌───────────────────────────────────▼──────────────────────────────────┐
│ KivoIpc.qml (Shared QML Scope)                                       │
│  properties, signals, request dispatchers, reconnection resync       │
└───────────────┬───────────────────────────────────────┬──────────────┘
                │                                       │
┌───────────────▼──────────────────────┐ ┌──────────────▼──────────────┐
│ DynamicNotch.qml                     │ │ KivoDashboard.qml           │
│  • Compact: transfer / pairing chip  │ │  • DashboardClipboard.qml   │
│  • Expanded: quick shelf / clipboard │ │  • DashboardFileShelf.qml   │
│  • PTT & media drawer preserved      │ │  • DashboardPhoneBridge.qml │
└──────────────────────────────────────┘ └─────────────────────────────┘
```

---

## 3. Feature 1: Clipboard Intelligence (`kivo/clipboard/`)

### 3.1 Architectural Approach: Host cliphist Integration

The host runs `niri` with `wl-paste --watch cliphist store` actively running (PID 2466) writing to `~/.cache/cliphist/db` (BoltDB).

Instead of running a second competing daemon, KIVO implements `CliphistRunner` wrapping `kivo/core/bounded_process.py`:
- Reads history via `cliphist list` (~3 ms per call).
- Decodes full content on demand via `cliphist decode` (~3 ms).
- Removes items via `cliphist delete` or `cliphist delete-query`.
- Clears history via `cliphist wipe`.
- Re-copies items to clipboard via `wl-copy`.

To avoid polling storms, `ClipboardService` watches `~/.cache/cliphist/db` using `inotify` (file modification event) with a 250 ms debounce. When the database updates, KIVO triggers a refresh and emits `clip_history_changed`.

### 3.2 Data Models

```python
@dataclass(frozen=True)
class ClipItem:
    id: str                      # "clip_<hash>"
    raw_id: str                  # cliphist integer id or pin id
    type: str                    # "text" | "image"
    preview: str                 # Sanitized text preview (max 120 chars)
    char_count: int              # Length of text
    size_bytes: int              # Byte size of payload
    timestamp: float             # Epoch seconds
    is_pinned: bool              # True if saved in pinned store
    is_sensitive: bool           # True if detected as secret/password
    mime_type: str               # "text/plain", "image/png", etc.
```

### 3.3 Sensitive Content Detection & Privacy Filtering

Before presenting or caching clipboard text, the service passes content through a multi-layer privacy filter:
1. **MIME type exclusion**: If clipboard metadata contains `x-kde-passwordManagerHint: secret` or `application/x-secret`, it is flagged immediately.
2. **Entropy analysis**: Strings containing >24 characters with Shannon entropy > 4.5 (e.g. random passwords, high-entropy tokens) are flagged as sensitive.
3. **Regex heuristic scanning**:
   - Private keys: `-----BEGIN [A-Z ]*PRIVATE KEY-----`
   - Cloud tokens: `AKIA[0-9A-Z]{16}`, `ghp_[A-Za-z0-9_]{36}`, `glpat-[A-Za-z0-9_-]{20}`
   - JWTs: `eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`
   - Basic authentication: `[a-zA-Z0-9_-]+:[^\s]+@[a-zA-Z0-9.-]+`
4. **Privacy Action**:
   - `is_sensitive = True`.
   - Preview text is masked as `••••••••••••`.
   - Never written to pinned storage or persistent audit logs.
   - Decoded raw text is released from memory immediately after presentation.

### 3.4 Pinning & Retention Engine

- **Pinned Store**: Persistent JSON file stored at `$XDG_DATA_HOME/kivo/clipboard_pins.json` (file mode `0600`, directory mode `0700`).
- **Retention**:
  - Unpinned history: Managed by `cliphist` (host default: max 750 items).
  - Pinned items: Stored exclusively by KIVO; preserved indefinitely until explicitly unpinned.
  - Size caps: Text entries capped at 128 KiB; images capped at 5 MiB.

---

## 4. Feature 2: File Shelf (`kivo/shelf/`)

### 4.1 Reference-Based Staging Model

Files staged on the shelf represent **transient desktop references**, not physical copies. This prevents disk bloat, respects source permissions, and keeps staging instant (0 ms copy time).

```python
@dataclass(frozen=True)
class ShelfItem:
    id: str                      # "shelf_<uuid>"
    name: str                    # Sanitized filename (max 128 chars)
    extension: str               # Normalized extension (".pdf", ".png")
    canonical_path: str          # Real filesystem path (kept backend-side)
    size_bytes: int              # File size
    mime_type: str               # Detected MIME type
    mtime: float                 # Last modified epoch
    is_accessible: bool          # Verified readable
    is_workspace: bool           # True if inside an approved workspace
    workspace_name: str | None   # Workspace ID if applicable
```

### 4.2 Ingestion via Drag-and-Drop

1. User drags files onto Dynamic Notch or Dashboard.
2. Quickshell `DropArea` receives `drop.urls` (`file:///home/muzzu/Documents/invoice.pdf`).
3. QML invokes `shelf_add_uris({ "uris": [...] })`.
4. Backend validation:
   - Resolves `file://` URI to local path.
   - Resolves canonical path with `os.path.realpath`.
   - Opens file descriptor with `os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)` to prevent symlink traversal.
   - Verifies `fstat` (regular file or directory, readable, non-empty).
   - Generates unique shelf ID.
   - Stores item in `FileShelfService` memory.
   - Emits `shelf_changed` event.

### 4.3 Staged File Operations

- **Open File**: Invokes `run_bounded(["xdg-open", item.canonical_path], timeout=2.0)`.
- **Copy to Clipboard**: Invokes `run_bounded(["wl-copy", "--type", "text/uri-list", f"file://{path}"])`.
- **Remove from Shelf**: Unstages the reference from memory; the file on disk is untouched.
- **Clear Shelf**: Removes all staged references.
- **Transfer to Workspace**: Moves or copies file into an approved workspace root using `kivo.files.mutation_service.MutationService` with explicit user confirmation.

---

## 5. Feature 3: Phone-to-PC Bridge (`kivo/bridge/`)

### 5.1 Architecture: Ephemeral Zero-Install Local PWA

Rather than requiring a native mobile app install from app stores, KIVO serves a lightweight, authenticated Progressive Web App (PWA) directly over the local Wi-Fi network.

```
┌────────────────────────────────────────────────────────┐
│ Phone Camera                                           │
│  Scans Desktop QR code:                                │
│  http://192.168.1.7:8443/#key=<256-bit-token>&code=482 │
└───────────────────────────┬────────────────────────────┘
                            │ Mobile Safari / Chrome
┌───────────────────────────▼────────────────────────────┐
│ Phone Web Client (Zero Install PWA)                    │
│  • Reads #key from window.location.hash (client-only)  │
│  • Displays 6-digit confirmation code: 482 910         │
│  • End-to-end encrypted transfer using WebCrypto       │
└───────────────────────────┬────────────────────────────┘
                            │ Mutual Authenticated REST / WebSocket
┌───────────────────────────▼────────────────────────────┐
│ KIVO Bridge Server (FastAPI + Uvicorn)                 │
│  • Binds ONLY to LAN IP (192.168.1.7:8443)             │
│  • Validates session token & pairing state             │
│  • Streams uploads to quarantine directory             │
└───────────────────────────┬────────────────────────────┘
                            │ IPC Confirmation Event
┌───────────────────────────▼────────────────────────────┐
│ Quickshell Desktop UI (Dynamic Notch / Dashboard)      │
│  • Shows pairing prompt: "Allow Pixel 8 (482 910)?"   │
│  • Shows incoming file prompt: "Accept report.pdf?"    │
│  • Atomically moves accepted files to ~/Downloads      │
└────────────────────────────────────────────────────────┘
```

### 5.2 Mutual Pairing Handshake Protocol

1. **Activation**: User opens "Phone Bridge" in KIVO Dashboard.
2. **Server Spawn**: KIVO starts an ephemeral Uvicorn thread running FastAPI, binding strictly to the verified LAN IPv4 address (e.g. `192.168.1.7:8443`).
3. **Secret Generation**:
   - `pairing_secret`: 32 random bytes (base64url).
   - `sas_code`: 6-digit Short Authentication String (e.g. `482 910`).
   - `session_id`: UUIDv4.
4. **QR Code Rendering**:
   - URL: `http://<LAN_IP>:<PORT>/#k=<pairing_secret>&id=<session_id>&code=<sas_code>`
   - Rendered using `qrcode` + `PIL.Image` into an in-memory PNG.
5. **Phone Connect**:
   - Phone opens URL. The fragment (`#k=...`) is never transmitted in HTTP request headers.
   - Phone client sends WebSocket connect request with HMAC signature `HMAC-SHA256(pairing_secret, session_id)`.
6. **Desktop Confirmation Gate**:
   - KIVO desktop emits `bridge_pairing_requested` with client IP, User-Agent, and SAS code.
   - Notch/Dashboard shows prompt: `"Pair with phone at 192.168.1.45 (Code: 482 910)? [Approve] [Reject]"`.
   - Pairing is blocked until desktop user clicks "Approve".
   - Times out and drops if unapproved after 60 seconds.

### 5.3 File Transfer & Quarantine Protocol

#### Ingress (Phone -> PC)
1. Phone sends file metadata: filename, size, SHA-256 hash.
2. PC checks available disk space (`os.statvfs`). Size capped at 2 GB per file.
3. Chunks stream directly into quarantine folder:
   `$XDG_RUNTIME_DIR/kivo/bridge/quarantine/<transfer_id>.part` (mode `0600`).
4. Streaming SHA-256 checksum calculated and verified upon transfer completion.
5. Desktop UI displays incoming transfer confirmation:
   `"Received 'video.mp4' (45 MB) from Phone. Save to Downloads? [Accept] [Discard]"`
6. On Accept: Atomically renamed and moved to `~/Downloads/video.mp4`. If file exists, appends unique suffix (`video (1).mp4`); never overwrites.
7. On Discard / Timeout: Quarantine file is immediately deleted.

#### Egress (PC -> Phone)
1. Desktop user selects file from File Shelf or approved workspace.
2. Desktop generates a single-use download token valid for 5 minutes.
3. Phone downloads file chunk by chunk with authenticated bearer token.

---

## 6. Dynamic Notch & Dashboard UI Specification

### 6.1 Dynamic Notch Integration

- **Compact Idle View**:
  - `compactCpuChip`: `CPU 12%` (already implemented in Stage 12.2).
  - `compactMicChip`: `🔇` (already implemented in Stage 12.2).
  - `compactBridgeChip`: When transfer is active, shows `↑ 45%` or `↓ 78%` with subtle accent pulsing. When pairing request is pending, shows `📱 ?` badge.
  - Idle width expands dynamically (+28 px per active badge), strictly capped at 280 px.
- **Expanded Notch View**:
  - **Quick Productivity Section** (visible when media is inactive):
    - Top row: Last 3 clipboard entries with 1-click copy button.
    - Bottom row: Up to 3 staged files from File Shelf with "Open" and "Clear" actions.
  - If a file transfer is active, replaces quick productivity with a progress bar and cancel button.
  - Media drawer takes precedence when media playback is active.

### 6.2 Dashboard Pages

1. **`DashboardClipboard.qml`**:
   - Search bar with instant filter.
   - Segmented buttons: `All`, `Text`, `Images`, `Pinned`.
   - Entry list: preview text/thumbnail, character count, timestamp, pin button, delete button, copy button.
   - Bottom bar: "Clear Unpinned History" (with confirmation dialog).
2. **`DashboardFileShelf.qml`**:
   - Full drag-and-drop landing target rectangle.
   - Staged files list/grid: icon based on MIME type, filename, size, workspace origin badge.
   - Action buttons: "Open", "Copy File", "Send to Phone", "Remove", "Clear Shelf".
3. **`DashboardPhoneBridge.qml`**:
   - Top section: Bridge toggle switch (Start / Stop Bridge).
   - Pairing area: Large QR code display, server status (`192.168.1.7:8443`), pairing instructions.
   - Active Paired Devices list: device name, IP, connected time, "Revoke" button.
   - Pending Transfer Cards: Accept / Discard prompts for incoming files.

---

## 7. IPC Protocol Contracts

### 7.1 New Operations Allowlist

```
REQUEST_OPERATIONS += {
    # Clipboard
    "clip_get_history", "clip.get_history",
    "clip_get_item", "clip.get_item",
    "clip_copy_item", "clip.copy_item",
    "clip_pin_item", "clip.pin_item",
    "clip_delete_item", "clip.delete_item",
    "clip_clear_history", "clip.clear_history",

    # File Shelf
    "shelf_list_items", "shelf.list_items",
    "shelf_add_uris", "shelf.add_uris",
    "shelf_remove_item", "shelf.remove_item",
    "shelf_clear", "shelf.clear",
    "shelf_open_item", "shelf.open_item",

    # Phone Bridge
    "bridge_get_status", "bridge.get_status",
    "bridge_start", "bridge.start",
    "bridge_stop", "bridge.stop",
    "bridge_approve_pairing", "bridge.approve_pairing",
    "bridge_reject_pairing", "bridge.reject_pairing",
    "bridge_resolve_transfer", "bridge.resolve_transfer",
    "bridge_revoke_device", "bridge.revoke_device",
}
```

### 7.2 New Events Allowlist

```
EVENT_NAMES += (
    "clip_history_changed", "clip.history_changed",
    "shelf_items_changed", "shelf.items_changed",
    "bridge_status_changed", "bridge.status_changed",
    "bridge_pairing_requested", "bridge.pairing_requested",
    "bridge_transfer_requested", "bridge.transfer_requested",
    "bridge_transfer_progress", "bridge.transfer_progress",
    "bridge_transfer_completed", "bridge.transfer_completed",
)
```

### 7.3 Operation Schemas

#### `clip_get_history`
- Request params:
  ```json
  {
    "limit": 50,
    "search": "optional query string",
    "filter": "all" | "text" | "image" | "pinned"
  }
  ```
- Result:
  ```json
  {
    "items": [
      {
        "id": "clip_abc123",
        "type": "text",
        "preview": "function test() { ... }",
        "char_count": 142,
        "size_bytes": 142,
        "timestamp": 1700000000.0,
        "is_pinned": false,
        "is_sensitive": false,
        "mime_type": "text/plain"
      }
    ],
    "total_count": 120
  }
  ```

#### `clip_copy_item`
- Request params: `{"id": "clip_abc123"}`
- Result: `{"ok": true, "id": "clip_abc123"}`

#### `shelf_add_uris`
- Request params:
  ```json
  {
    "uris": ["file:///home/muzzu/Documents/report.pdf"]
  }
  ```
- Result:
  ```json
  {
    "added": [
      {
        "id": "shelf_987xyz",
        "name": "report.pdf",
        "extension": ".pdf",
        "size_bytes": 2048576,
        "mime_type": "application/pdf",
        "mtime": 1700000000.0,
        "is_accessible": true,
        "is_workspace": false,
        "workspace_name": null
      }
    ],
    "rejected": []
  }
  ```

#### `bridge_start`
- Request params: `{}`
- Result:
  ```json
  {
    "ok": true,
    "status": "running",
    "lan_ip": "192.168.1.7",
    "port": 8443,
    "qr_payload": "http://192.168.1.7:8443/#k=...&code=482910",
    "sas_code": "482 910",
    "expires_at": 1700003600.0
  }
  ```

#### `bridge_resolve_transfer`
- Request params:
  ```json
  {
    "transfer_id": "tx_456",
    "action": "accept" | "discard",
    "destination": "downloads"
  }
  ```
- Result: `{"ok": true, "transfer_id": "tx_456", "saved_path": "/home/muzzu/Downloads/report.pdf"}`
